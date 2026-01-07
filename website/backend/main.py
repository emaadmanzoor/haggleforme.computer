import asyncio
import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import openai

BASE_DIR = Path(__file__).resolve().parents[2]


def _load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            cleaned = line.strip()
            if not cleaned or cleaned.startswith("#") or "=" not in cleaned:
                continue
            key, value = cleaned.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except Exception:
        return


try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(dotenv_path=BASE_DIR / ".env", override=False)
except Exception:
    _load_dotenv_file(BASE_DIR / ".env")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY environment variable is required for the web API")

NEGOTIATION_MODEL = os.environ.get("NEGOTIATION_MODEL", "gpt-4.1-mini")
MAX_ROUNDS = int(os.environ.get("NEGOTIATION_MAX_ROUNDS", "12"))
MODEL_THROTTLE_SECONDS = float(os.environ.get("NEGOTIATION_THROTTLE_SEC", "1.0"))
MODEL_CONCURRENCY = max(1, int(os.environ.get("NEGOTIATION_CONCURRENCY", "4")))
OPENAI_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT", "60"))

DEFAULT_START_MESSAGE = (
    "Let's begin the negotiation. Please make your initial offer and inquiries."
)

DEFAULT_BUYER_STRATEGY = (
    "Offer a deal a few thousand under the KBB range and go up by a few hundred dollars "
    "as needed with the aim of closing as low as possible."
)
DEFAULT_SELLER_STRATEGY = (
    "Start with an offer that’s a few thousand dollars above the midpoint of the blue book "
    "value range. Go down by $500–$1000 each round if necessary. Aim to close a deal as far "
    "above the trade-in price as possible."
)
EMAAD_BUYER_STRATEGY = (
    "Start with a firm low anchor well below the KBB range. Make small, slow concessions, "
    "and push hard on the walk-away threat. Aim to close as low as possible and do not "
    "match midpoints unless it is the final round."
)
EMAAD_SELLER_STRATEGY = (
    "Open with a very high anchor above the KBB range. Concede reluctantly in small steps, "
    "and emphasize scarcity and demand. Hold the line near the top of the range and only "
    "move meaningfully if the deal is close to closing."
)
ALEX_BUYER_STRATEGY = DEFAULT_BUYER_STRATEGY
ALEX_SELLER_STRATEGY = DEFAULT_SELLER_STRATEGY

ACCEPTANCE_RE = re.compile(
    r"\bI accept the offer of\s*\$([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*\.?",
    re.IGNORECASE,
)

BUYER_TEMPLATE_PATH = BASE_DIR / "buyerprompt.txt"
SELLER_TEMPLATE_PATH = BASE_DIR / "sellerprompt.txt"

LEADERBOARD_PATH = Path(__file__).resolve().parent / "leaderboard.json"
USERS_PATH = Path(__file__).resolve().parent / "users.json"
STRATEGIES_PATH = Path(__file__).resolve().parent / "strategies.json"
MATCHES_PATH = Path(__file__).resolve().parent / "matches.json"

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
_PASSWORD_ITERATIONS = 120_000

_USERS_LOCK = asyncio.Lock()
_LEADERBOARD_LOCK = asyncio.Lock()
_STRATEGIES_LOCK = asyncio.Lock()
_MATCHES_LOCK = asyncio.Lock()

active_tokens: Dict[str, str] = {}

openai_client = openai.AsyncOpenAI(
    api_key=OPENAI_API_KEY,
    timeout=OPENAI_TIMEOUT,
)

_MODEL_CALL_SEMAPHORE = asyncio.Semaphore(MODEL_CONCURRENCY)
_MODEL_CALL_LOCK = asyncio.Lock()
_LAST_MODEL_CALL = 0.0


LeaderboardRecord = Dict[str, Any]
LeaderboardEntry = Dict[str, Any]


def _load_template(path: Path, *, fallback: str) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


BUYER_TEMPLATE = _load_template(
    BUYER_TEMPLATE_PATH,
    fallback=(
        "You are a buyer negotiation agent.\n"
        "If you accept an offer, reply exactly: I accept the offer of $X.\n\n"
        "Negotiation strategy:\n"
        "buyerPrompt"
    ),
)
SELLER_TEMPLATE = _load_template(
    SELLER_TEMPLATE_PATH,
    fallback=(
        "You are a seller negotiation agent.\n"
        "If you accept an offer, reply exactly: I accept the offer of $X.\n\n"
        "Negotiation strategy:\n"
        "sellerPrompt"
    ),
)


def _build_prompt(template: str, placeholder: str, strategy: str) -> str:
    return template.replace(placeholder, strategy)


async def _load_users() -> Dict[str, Dict[str, str]]:
    if not USERS_PATH.exists():
        return {}
    data = await asyncio.to_thread(USERS_PATH.read_text)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    cleaned: Dict[str, Dict[str, str]] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        username = value.get("username")
        salt = value.get("salt")
        password = value.get("password")
        if not all(isinstance(item, str) for item in (username, salt, password)):
            continue
        cleaned[key] = {"username": username, "salt": salt, "password": password}
    return cleaned


async def _save_users(records: Dict[str, Dict[str, str]]) -> None:
    serialized = json.dumps(records, indent=2)
    await asyncio.to_thread(USERS_PATH.write_text, serialized)


async def _load_leaderboard() -> Dict[str, LeaderboardRecord]:
    if not LEADERBOARD_PATH.exists():
        return {}
    data = await asyncio.to_thread(LEADERBOARD_PATH.read_text)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    cleaned: Dict[str, LeaderboardRecord] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        cleaned[key] = {
            "username": str(value.get("username", "")),
            "matches": int(value.get("matches", 0)),
            "agreements": int(value.get("agreements", 0)),
            "total_surplus": float(value.get("total_surplus", 0.0)),
        }
    return cleaned


async def _save_leaderboard(records: Dict[str, LeaderboardRecord]) -> None:
    serialized = json.dumps(records, indent=2)
    await asyncio.to_thread(LEADERBOARD_PATH.write_text, serialized)


async def _update_leaderboard(username: str, *, surplus: float, agreement: bool) -> List[LeaderboardEntry]:
    async with _LEADERBOARD_LOCK:
        records = await _load_leaderboard()
        key = username.strip().lower()
        record = records.get(key)
        if not record:
            record = {
                "username": username,
                "matches": 0,
                "agreements": 0,
                "total_surplus": 0.0,
            }
        record["matches"] = int(record.get("matches", 0)) + 1
        record["agreements"] = int(record.get("agreements", 0)) + (1 if agreement else 0)
        record["total_surplus"] = float(record.get("total_surplus", 0.0)) + float(surplus)
        records[key] = record
        await _save_leaderboard(records)
    return _sorted_leaderboard(records)


async def _get_leaderboard() -> List[LeaderboardEntry]:
    records = await _load_leaderboard()
    users = await _load_users()
    for user in users.values():
        username = str(user.get("username", "")).strip()
        if not username:
            continue
        key = username.lower()
        if key not in records:
            records[key] = {
                "username": username,
                "matches": 0,
                "agreements": 0,
                "total_surplus": 0.0,
            }
    return _sorted_leaderboard(records)


async def _get_user_total_surplus(username: str) -> Optional[float]:
    if not username:
        return None
    records = await _load_leaderboard()
    record = records.get(username.strip().lower())
    if not record:
        return 0.0
    return float(record.get("total_surplus", 0.0))


def _sorted_leaderboard(records: Dict[str, LeaderboardRecord]) -> List[LeaderboardEntry]:
    entries: List[LeaderboardEntry] = []
    for raw in records.values():
        matches = int(raw.get("matches", 0))
        agreements = int(raw.get("agreements", 0))
        total_surplus = float(raw.get("total_surplus", 0.0))
        entries.append(
            {
                "username": str(raw.get("username", "")),
                "matches": matches,
                "agreements": agreements,
                "total_surplus": total_surplus,
            }
        )
    entries.sort(
        key=lambda e: (
            -e["total_surplus"],
            -e["agreements"],
            -e["matches"],
            e["username"].lower(),
        )
    )
    return entries[:10]


async def _load_strategies() -> List[Dict[str, Any]]:
    if not STRATEGIES_PATH.exists():
        return []
    data = await asyncio.to_thread(STRATEGIES_PATH.read_text)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        strategy = item.get("strategy")
        if role not in ("buyer", "seller") or not isinstance(strategy, str):
            continue
        cleaned.append(
            {
                "entry_id": str(item.get("entry_id", "")),
                "role": role,
                "strategy": strategy,
                "created_at": int(item.get("created_at", int(time.time()))),
                "username": item.get("username") if isinstance(item.get("username"), str) else None,
                "registered": bool(item.get("registered", False)),
            }
        )
    return cleaned


def _ensure_default_strategies(records: List[Dict[str, Any]]) -> tuple[bool, List[Dict[str, Any]]]:
    updated = False
    existing_ids = {str(item.get("entry_id", "")) for item in records if isinstance(item, dict)}
    for item in records:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("entry_id", ""))
        if entry_id == "seed-buyer-emaad":
            if item.get("username") != "Emaad" or item.get("strategy") != EMAAD_BUYER_STRATEGY:
                item["username"] = "Emaad"
                item["strategy"] = EMAAD_BUYER_STRATEGY
                item["role"] = "buyer"
                item["registered"] = True
                updated = True
        if entry_id == "seed-seller-emaad":
            if item.get("username") != "Emaad" or item.get("strategy") != EMAAD_SELLER_STRATEGY:
                item["username"] = "Emaad"
                item["strategy"] = EMAAD_SELLER_STRATEGY
                item["role"] = "seller"
                item["registered"] = True
                updated = True
        if entry_id == "seed-buyer-alex":
            if item.get("username") != "Alex" or item.get("strategy") != ALEX_BUYER_STRATEGY:
                item["username"] = "Alex"
                item["strategy"] = ALEX_BUYER_STRATEGY
                item["role"] = "buyer"
                item["registered"] = True
                updated = True
        if entry_id == "seed-seller-alex":
            if item.get("username") != "Alex" or item.get("strategy") != ALEX_SELLER_STRATEGY:
                item["username"] = "Alex"
                item["strategy"] = ALEX_SELLER_STRATEGY
                item["role"] = "seller"
                item["registered"] = True
                updated = True
    # Do not seed anonymous default strategies into the pool.
    if "seed-buyer-emaad" not in existing_ids:
        records.append(
            {
                "entry_id": "seed-buyer-emaad",
                "role": "buyer",
                "strategy": EMAAD_BUYER_STRATEGY,
                "created_at": int(time.time()),
                "username": "Emaad",
                "registered": True,
            }
        )
        updated = True
    if "seed-seller-emaad" not in existing_ids:
        records.append(
            {
                "entry_id": "seed-seller-emaad",
                "role": "seller",
                "strategy": EMAAD_SELLER_STRATEGY,
                "created_at": int(time.time()),
                "username": "Emaad",
                "registered": True,
            }
        )
        updated = True
    if "seed-buyer-alex" not in existing_ids:
        records.append(
            {
                "entry_id": "seed-buyer-alex",
                "role": "buyer",
                "strategy": ALEX_BUYER_STRATEGY,
                "created_at": int(time.time()),
                "username": "Alex",
                "registered": True,
            }
        )
        updated = True
    if "seed-seller-alex" not in existing_ids:
        records.append(
            {
                "entry_id": "seed-seller-alex",
                "role": "seller",
                "strategy": ALEX_SELLER_STRATEGY,
                "created_at": int(time.time()),
                "username": "Alex",
                "registered": True,
            }
        )
        updated = True
    return updated, records


async def _save_strategies(records: List[Dict[str, Any]]) -> None:
    serialized = json.dumps(records, indent=2)
    await asyncio.to_thread(STRATEGIES_PATH.write_text, serialized)


async def _load_matches() -> List[Dict[str, Any]]:
    if not MATCHES_PATH.exists():
        return []
    data = await asyncio.to_thread(MATCHES_PATH.read_text)
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for item in parsed:
        if isinstance(item, dict):
            cleaned.append(item)
    return cleaned


async def _save_matches(records: List[Dict[str, Any]]) -> None:
    serialized = json.dumps(records, indent=2)
    await asyncio.to_thread(MATCHES_PATH.write_text, serialized)


def _derive_password(secret: bytes, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", secret, salt, _PASSWORD_ITERATIONS)
    return base64.b64encode(digest).decode("ascii")


def _issue_token(username: str) -> str:
    for token, current in list(active_tokens.items()):
        if current.lower() == username.lower():
            active_tokens.pop(token, None)
    token = secrets.token_urlsafe(32)
    active_tokens[token] = username
    return token


def _extract_token(request: Request) -> Optional[str]:
    header = request.headers.get("Authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    token = token.strip()
    return token or None


def _decode_client_hash(value: str) -> bytes:
    cleaned = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", cleaned):
        try:
            return bytes.fromhex(cleaned)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid password hash.") from exc
    # Fallback: accept raw passwords when the client can't hash (e.g., insecure context).
    return hashlib.sha256(cleaned.encode("utf-8")).digest()


def _validate_username_or_raise(username: str) -> str:
    cleaned = username.strip()
    if not USERNAME_PATTERN.fullmatch(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3-32 characters using letters, numbers, underscores, or hyphens.",
        )
    return cleaned


async def _chat_complete(
    messages: List[Dict[str, str]],
    model: str,
    *,
    temperature: float = 0.7,
    response_format: Optional[Dict[str, str]] = None,
) -> str:
    async with _MODEL_CALL_SEMAPHORE:
        async with _MODEL_CALL_LOCK:
            loop = asyncio.get_running_loop()
            now = loop.time()
            global _LAST_MODEL_CALL
            wait_time = (_LAST_MODEL_CALL + MODEL_THROTTLE_SECONDS) - now
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            _LAST_MODEL_CALL = loop.time()
        params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format:
            params["response_format"] = response_format
        response = await openai_client.chat.completions.create(**params)
        content = response.choices[0].message.content
        return content.strip() if isinstance(content, str) else ""


def extract_acceptance_price(text: str) -> Optional[float]:
    if not text:
        return None
    match = ACCEPTANCE_RE.search(text)
    if not match:
        return None
    return coerce_price(match.group(1))


def coerce_price(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value)
        if cleaned:
            try:
                return float(cleaned)
            except ValueError:
                return None
    return None


def outcome_from_rounds(rounds: List[Dict[str, Any]]) -> tuple[bool, Optional[float]]:
    price: Optional[float] = None
    for item in rounds:
        candidate = extract_acceptance_price(str(item.get("text", "")))
        if candidate is not None:
            price = candidate
    return price is not None, price


async def run_negotiation(
    *,
    buyer_prompt: str,
    seller_prompt: str,
    model: str,
    max_rounds: int,
    start_message: str,
) -> List[Dict[str, Any]]:
    buyer_history = [
        {"role": "system", "content": buyer_prompt},
        {"role": "user", "content": start_message},
    ]
    seller_history = [
        {"role": "system", "content": seller_prompt},
    ]
    rounds: List[Dict[str, Any]] = []

    buyer_reply = await _chat_complete(buyer_history, model)
    buyer_history.append({"role": "assistant", "content": buyer_reply})
    rounds.append(
        {
            "round": 1,
            "speaker": "Buyer",
            "prompt": start_message,
            "text": buyer_reply,
        }
    )

    if extract_acceptance_price(buyer_reply) is not None:
        return rounds

    last_message = buyer_reply
    for round_idx in range(2, max_rounds + 1):
        if round_idx % 2 == 0:
            speaker = "Seller"
            seller_history.append({"role": "user", "content": last_message})
            reply = await _chat_complete(seller_history, model)
            seller_history.append({"role": "assistant", "content": reply})
        else:
            speaker = "Buyer"
            buyer_history.append({"role": "user", "content": last_message})
            reply = await _chat_complete(buyer_history, model)
            buyer_history.append({"role": "assistant", "content": reply})

        rounds.append(
            {
                "round": round_idx,
                "speaker": speaker,
                "prompt": last_message,
                "text": reply,
            }
        )

        if extract_acceptance_price(reply) is not None:
            break
        last_message = reply

    return rounds


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password_hash: str = Field(..., min_length=1, max_length=256)


class AuthResponse(BaseModel):
    token: str
    username: str


class StrategyRequest(BaseModel):
    role: str = Field(..., min_length=4, max_length=6)
    strategy: str = Field(..., min_length=1, max_length=4000)


class MatchRound(BaseModel):
    round: int
    speaker: str
    text: str


class MatchResult(BaseModel):
    opponent_role: Optional[str] = None
    opponent_label: Optional[str] = None
    agreement: Optional[bool] = None
    price: Optional[float] = None
    rounds: Optional[int] = None
    surplus: Optional[float] = None
    opponent_surplus: Optional[float] = None
    transcript: List[MatchRound] = Field(default_factory=list)


class SubmissionResponse(BaseModel):
    status: str
    entry_id: str
    role: str
    opponent_role: Optional[str] = None
    opponent_label: Optional[str] = None
    user_total_surplus: Optional[float] = None
    agreement: Optional[bool] = None
    price: Optional[float] = None
    rounds: Optional[int] = None
    surplus: Optional[float] = None
    opponent_surplus: Optional[float] = None
    total_surplus: Optional[float] = None
    matches: List[MatchResult] = Field(default_factory=list)
    transcript: List[MatchRound] = Field(default_factory=list)
    leaderboard: List[Dict[str, Any]] = Field(default_factory=list)
    message: Optional[str] = None


app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/auth/register", response_model=AuthResponse)
async def register_user(payload: AuthRequest) -> AuthResponse:
    username = _validate_username_or_raise(payload.username)
    client_secret = _decode_client_hash(payload.password_hash)
    async with _USERS_LOCK:
        users = await _load_users()
        key = username.strip().lower()
        if key in users:
            raise HTTPException(status_code=409, detail="Username already exists.")
        salt = secrets.token_bytes(16)
        derived = _derive_password(client_secret, salt)
        users[key] = {
            "username": username,
            "salt": base64.b64encode(salt).decode("ascii"),
            "password": derived,
        }
        await _save_users(users)
    token = _issue_token(username)
    return AuthResponse(token=token, username=username)


@app.post("/api/auth/login", response_model=AuthResponse)
async def login_user(payload: AuthRequest) -> AuthResponse:
    username = _validate_username_or_raise(payload.username)
    client_secret = _decode_client_hash(payload.password_hash)
    async with _USERS_LOCK:
        users = await _load_users()
    record = users.get(username.strip().lower())
    if not record:
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    try:
        salt = base64.b64decode(record["salt"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Credential store is corrupted.") from exc
    derived = _derive_password(client_secret, salt)
    stored = record.get("password", "")
    if not isinstance(stored, str) or not hmac.compare_digest(stored, derived):
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    canonical_username = record.get("username", username)
    token = _issue_token(canonical_username)
    return AuthResponse(token=token, username=canonical_username)


@app.post("/api/auth/logout")
async def logout_user(request: Request):
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=400, detail="Missing auth token.")
    active_tokens.pop(token, None)
    return JSONResponse({"status": "ok"})


@app.post("/api/submit", response_model=SubmissionResponse)
async def submit_strategy(payload: StrategyRequest, request: Request) -> SubmissionResponse:
    role = payload.role.strip().lower()
    if role not in ("buyer", "seller"):
        raise HTTPException(status_code=400, detail="Role must be buyer or seller.")
    strategy = payload.strategy.strip()
    if not strategy:
        raise HTTPException(status_code=400, detail="Strategy cannot be empty.")

    token = _extract_token(request)
    if token:
        username = active_tokens.get(token)
        if not username:
            raise HTTPException(status_code=401, detail="Invalid auth token.")
        registered = True
    else:
        username = None
        registered = False

    entry_id = uuid.uuid4().hex
    entry = {
        "entry_id": entry_id,
        "role": role,
        "strategy": strategy,
        "created_at": int(time.time()),
        "username": username,
        "registered": registered,
    }

    async with _STRATEGIES_LOCK:
        strategies = await _load_strategies()
        defaults_added, strategies = _ensure_default_strategies(strategies)
        strategies.append(entry)
        strategies = [
            item
            for item in strategies
            if str(item.get("entry_id", "")) not in ("default-buyer", "default-seller")
        ]
        if defaults_added or entry:
            await _save_strategies(strategies)

    leaderboard_snapshot = await _get_leaderboard()
    top_usernames = {
        entry.get("username")
        for entry in leaderboard_snapshot
        if isinstance(entry, dict) and isinstance(entry.get("username"), str)
    }
    if username:
        top_usernames.discard(username)
    opponent_role = "seller" if role == "buyer" else "buyer"
    opponents = [
        candidate
        for candidate in strategies
        if candidate.get("role") == opponent_role
        and candidate.get("username") in top_usernames
        and candidate.get("registered")
    ]
    user_total_surplus = await _get_user_total_surplus(username) if username else None

    if not opponents:
        return SubmissionResponse(
            status="queued",
            entry_id=entry_id,
            role=role,
            leaderboard=leaderboard_snapshot,
            user_total_surplus=user_total_surplus,
            message="Waiting for an opponent strategy to join the pool.",
        )

    matches_payload: List[MatchResult] = []
    total_surplus = 0.0

    for opponent in opponents:
        buyer_entry = entry if role == "buyer" else opponent
        seller_entry = entry if role == "seller" else opponent

        buyer_prompt = _build_prompt(BUYER_TEMPLATE, "buyerPrompt", buyer_entry["strategy"])
        seller_prompt = _build_prompt(SELLER_TEMPLATE, "sellerPrompt", seller_entry["strategy"])

        try:
            rounds = await run_negotiation(
                buyer_prompt=buyer_prompt,
                seller_prompt=seller_prompt,
                model=NEGOTIATION_MODEL,
                max_rounds=MAX_ROUNDS,
                start_message=DEFAULT_START_MESSAGE,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Negotiation model call failed: {exc}",
            ) from exc

        agreement, price = outcome_from_rounds(rounds)

        buyer_surplus = 0.0
        seller_surplus = 0.0
        if agreement and price is not None:
            buyer_surplus = 22000 - price
            seller_surplus = price - 18000

        match_record = {
            "match_id": uuid.uuid4().hex,
            "created_at": int(time.time()),
            "buyer_entry": buyer_entry["entry_id"],
            "seller_entry": seller_entry["entry_id"],
            "buyer_username": buyer_entry.get("username"),
            "seller_username": seller_entry.get("username"),
            "agreement": agreement,
            "price": price,
            "rounds": len(rounds),
            "buyer_surplus": buyer_surplus,
            "seller_surplus": seller_surplus,
            "transcript": rounds,
        }

        async with _MATCHES_LOCK:
            matches = await _load_matches()
            matches.append(match_record)
            await _save_matches(matches)

        if buyer_entry.get("registered") and buyer_entry.get("username"):
            leaderboard_snapshot = await _update_leaderboard(
                buyer_entry["username"], surplus=buyer_surplus, agreement=agreement
            )
        if seller_entry.get("registered") and seller_entry.get("username"):
            leaderboard_snapshot = await _update_leaderboard(
                seller_entry["username"], surplus=seller_surplus, agreement=agreement
            )

        opponent_label = opponent.get("username") or "Anonymous"
        user_surplus = buyer_surplus if role == "buyer" else seller_surplus
        opponent_surplus = seller_surplus if role == "buyer" else buyer_surplus
        if agreement:
            total_surplus += user_surplus

        matches_payload.append(
            MatchResult(
                opponent_role=opponent.get("role"),
                opponent_label=opponent_label,
                agreement=agreement,
                price=price,
                rounds=len(rounds),
                surplus=user_surplus,
                opponent_surplus=opponent_surplus,
                transcript=[
                    MatchRound(**{k: r[k] for k in ("round", "speaker", "text")})
                    for r in rounds
                ],
            )
        )

    user_total_surplus = await _get_user_total_surplus(username) if username else None

    return SubmissionResponse(
        status="matched",
        entry_id=entry_id,
        role=role,
        user_total_surplus=user_total_surplus,
        total_surplus=total_surplus,
        matches=matches_payload,
        leaderboard=leaderboard_snapshot,
    )


@app.get("/api/leaderboard")
async def get_leaderboard():
    leaderboard = await _get_leaderboard()
    return {"leaderboard": leaderboard}


@app.get("/api/prompts")
async def get_prompts():
    return {"buyer_prompt": BUYER_TEMPLATE, "seller_prompt": SELLER_TEMPLATE}


__all__ = ["app"]
