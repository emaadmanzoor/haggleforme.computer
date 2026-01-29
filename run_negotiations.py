import argparse
import json
import os
import re
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime
from threading import Event, Thread

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI
from typing import Optional

try:
    from tqdm import tqdm
except Exception:  # pragma: no cover - optional dependency
    tqdm = None

DEFAULT_START_MESSAGE = (
    "Let's begin the negotiation. Please make your initial offer and inquiries."
)


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_YELLOW = "\033[33m"
# Dark grey background (matches Codex CLI styling more closely than ANSI bright-black).
ANSI_BG_GRAY = "\033[48;5;236m"
ANSI_FG_DEFAULT = "\033[39m"

GROUP_NAMES = {
    0: "Group 0",
    1: "Group 1",
}

GROUP_MODELS = {
    # 0: "ft:gpt-4.1-mini-2025-04-14:cornell-university:genai-tutorial:CvawLgpb",
    # 1: "ft:gpt-4.1-mini-2025-04-14:cornell-university:genai-tutorial:CvYIdqz1",
    0: "gpt-4.1-mini-2025-04-14",
    1: "gpt-4.1-mini-2025-04-14",
}

GROUP_TEMPERATURES = {
    0: 1.0,
    1: 1.0,
}


def group_name(group_id: Optional[int]) -> str:
    return GROUP_NAMES.get(
        group_id, f"Group {group_id}" if group_id is not None else "Unknown group"
    )


def supports_color() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    return hasattr(os, "isatty") and os.isatty(1)


def colorize(text: str, color: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color}{text}{ANSI_RESET}"


def load_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def detect_column(df: pd.DataFrame, candidates) -> Optional[str]:
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand in lower_map:
            return lower_map[cand]
    for c in df.columns:
        cl = c.lower()
        for cand in candidates:
            if cand in cl:
                return c
    return None


def load_strategies(path: str, limit: int = 10, include_strategies: bool = True):
    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"No strategies found in {path}.")

    if not include_strategies:
        df = df.head(limit).copy()
        df["__row_id__"] = range(len(df))
        return df, None, None

    buyer_col = "Buyer Negotiation Strategy Prompt"
    seller_col = "Seller Negotiation Strategy Prompt"
    missing = [c for c in (buyer_col, seller_col) if c not in df.columns]
    if missing:
        raise ValueError(
            "Missing required strategy columns in CSV. "
            f"Expected columns: {buyer_col!r} and {seller_col!r}. "
            f"Missing: {missing}. Found columns: {list(df.columns)}"
        )

    df = df.fillna("")
    # Keep rows even if one side's strategy is blank; blank prompts are handled at runtime.
    buyer_has_text = df[buyer_col].astype(str).str.strip() != ""
    seller_has_text = df[seller_col].astype(str).str.strip() != ""
    df = df[buyer_has_text | seller_has_text]
    df = df.head(limit).copy()
    df["__row_id__"] = range(len(df))
    return df, buyer_col, seller_col


def build_prompt(template: str, placeholder: str, strategy: str) -> str:
    return template.replace(placeholder, str(strategy))


def response_text(resp) -> str:
    if hasattr(resp, "output_text"):
        return resp.output_text
    if hasattr(resp, "output"):
        parts = []
        for item in resp.output:
            if getattr(item, "type", None) == "message":
                for content in getattr(item, "content", []):
                    if getattr(content, "type", None) in ("output_text", "text"):
                        parts.append(getattr(content, "text", ""))
        if parts:
            return "".join(parts)
    return str(resp)


def call_model(
    client: OpenAI,
    model: str,
    messages,
    retries=3,
    conversation_id=None,
    instructions=None,
    temperature: Optional[float] = None,
):
    last_err = None
    for attempt in range(retries):
        try:
            params = {
                "model": model,
                "input": messages,
            }
            if conversation_id:
                params["conversation"] = conversation_id
                params["store"] = True
            if instructions:
                params["instructions"] = instructions
            if temperature is not None:
                params["temperature"] = float(temperature)
            resp = client.responses.create(**params)
            return response_text(resp).strip()
        except Exception as exc:  # pragma: no cover - best-effort retries
            last_err = exc
            time.sleep(1 + attempt)
    raise last_err


def accepts_price(client: OpenAI, text: str, model: str = "gpt-4.1-mini") -> bool:
    if not text:
        return False
    system = (
        "You are a strict negotiation classifier. "
        "Return only JSON with key accept_price (boolean). "
        "accept_price is true only if the message explicitly accepts or agrees "
        "to a specific transaction price. It must be a firm acceptance, not a "
        "counteroffer, condition, or a request. If unsure, return false."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]
    result = call_model(client, model, messages)
    result = result.strip()
    if result.startswith("```"):
        result = re.sub(r"^```[a-zA-Z]*\n", "", result).rstrip("`")
    try:
        payload = json.loads(result)
        return bool(payload.get("accept_price"))
    except json.JSONDecodeError:
        lowered = result.lower()
        return lowered.startswith("true") or lowered.startswith("yes")


def format_round_block(r):
    speaker_label = r.get("speaker_label")
    if not speaker_label:
        speaker_group = r.get("speaker_group")
        if speaker_group is not None:
            speaker_label = group_name(int(speaker_group))
        else:
            speaker = r.get("speaker")
            speaker_label = speaker if speaker else "Unknown"
    blocks = [
        f"## Round {r['round']}",
        f"**{speaker_label}:**",
        "```",
        r["text"],
        "```",
        "",
    ]
    return "\n".join(blocks)


def format_stream_round(
    r,
    buyer_id: int,
    seller_id: int,
    buyer_group: Optional[int],
    seller_group: Optional[int],
    use_color: bool,
) -> str:
    speaker_label = r.get("speaker_label")
    if not speaker_label:
        speaker_group = r.get("speaker_group")
        speaker_label = (
            group_name(int(speaker_group)) if speaker_group is not None else r["speaker"]
        )
    speaker_group = r.get("speaker_group")
    if use_color and speaker_group == 0:
        speaker_color = ANSI_CYAN if r["speaker"] == "Buyer" else ANSI_YELLOW
        return (
            f"{ANSI_BG_GRAY}{speaker_color}{speaker_label}:{ANSI_FG_DEFAULT} "
            f"{r['text']}{ANSI_RESET}"
        )

    speaker_color = ANSI_CYAN if r["speaker"] == "Buyer" else ANSI_YELLOW
    speaker = colorize(f"{speaker_label}:", speaker_color, use_color)
    return f"{speaker} {r['text']}"


@contextmanager
def spinner(
    label: str,
    pbar=None,
    enabled: bool = True,
    interval: float = 0.1,
    stream=None,
):
    if not enabled:
        yield
        return

    stop_event = Event()
    frames = "|/-\\"
    if stream is None:
        stream = sys.stderr if pbar else sys.stdout

    def spin():
        i = 0
        while not stop_event.is_set():
            frame = frames[i % len(frames)]
            if pbar:
                pbar.set_postfix_str(f"{label} {frame}")
                pbar.refresh()
            stream.write(f"\r{label} {frame}")
            stream.flush()
            time.sleep(interval)
            i += 1
        if pbar:
            pbar.set_postfix_str("")
            pbar.refresh()
        stream.write("\r" + " " * (len(label) + 2) + "\r")
        stream.flush()

    thread = Thread(target=spin, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join()


def format_transcript(rounds):
    blocks = []
    for r in rounds:
        blocks.append(format_round_block(r))
    return "\n".join(blocks).rstrip("-\n")


def parse_outcome(client: OpenAI, transcript: str, model: str = "gpt-4.1-mini"):
    system = (
        "You are a strict negotiation transcript parser. "
        "Return only JSON with keys: agreement (boolean) and price (number or null). "
        "Agreement is true only if both parties explicitly agree on a transaction. "
        "If there is agreement, price is the agreed numeric dollar value without commas. "
        "If no agreement, price is null. Output only JSON."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": transcript},
    ]
    text = call_model(client, model, messages)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text).rstrip("`")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
        raise


def coerce_price(value):
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


def run_negotiation(
    client: OpenAI,
    buyer_prompt: str,
    seller_prompt: str,
    buyer_model: str,
    seller_model: str,
    max_rounds: int,
    start_message: str,
    buyer_group: Optional[int] = None,
    seller_group: Optional[int] = None,
    progress_cb=None,
    log_round_cb=None,
    stream_round_cb=None,
    accept_model: str = "gpt-4.1-mini",
    spinner_cb=None,
):
    buyer_conversation = client.conversations.create()
    seller_conversation = client.conversations.create()

    buyer_temperature = GROUP_TEMPERATURES.get(buyer_group)
    seller_temperature = GROUP_TEMPERATURES.get(seller_group)

    rounds = []

    spinner_ctx = spinner_cb("Waiting for Buyer response") if spinner_cb else nullcontext()
    with spinner_ctx:
        last_message = call_model(
            client,
            buyer_model,
            [{"role": "user", "content": start_message}],
            conversation_id=buyer_conversation.id,
            instructions=buyer_prompt,
            temperature=buyer_temperature,
        )
    round_entry = {
        "round": 1,
        "speaker": "Buyer",
        "speaker_group": buyer_group,
        "speaker_label": group_name(buyer_group),
        "prompt": start_message,
        "text": last_message,
    }
    rounds.append(round_entry)
    if log_round_cb:
        log_round_cb(round_entry)
    if stream_round_cb:
        stream_round_cb(round_entry)
    if progress_cb:
        progress_cb(1, "Buyer")

    if accepts_price(client, last_message, model=accept_model):
        return rounds

    for round_idx in range(2, max_rounds + 1):
        if round_idx % 2 == 0:
            speaker = "Seller"
            spinner_ctx = (
                spinner_cb("Waiting for Seller response") if spinner_cb else nullcontext()
            )
            with spinner_ctx:
                reply = call_model(
                    client,
                    seller_model,
                    [{"role": "user", "content": last_message}],
                    conversation_id=seller_conversation.id,
                    instructions=seller_prompt,
                    temperature=seller_temperature,
                )
            round_entry = {
                "round": round_idx,
                "speaker": speaker,
                "speaker_group": seller_group,
                "speaker_label": group_name(seller_group),
                "prompt": last_message,
                "text": reply,
            }
            rounds.append(round_entry)
            if log_round_cb:
                log_round_cb(round_entry)
            if stream_round_cb:
                stream_round_cb(round_entry)
            if progress_cb:
                progress_cb(round_idx, speaker)
            if accepts_price(client, reply, model=accept_model):
                break
            last_message = reply
        else:
            speaker = "Buyer"
            spinner_ctx = (
                spinner_cb("Waiting for Buyer response") if spinner_cb else nullcontext()
            )
            with spinner_ctx:
                reply = call_model(
                    client,
                    buyer_model,
                    [{"role": "user", "content": last_message}],
                    conversation_id=buyer_conversation.id,
                    instructions=buyer_prompt,
                    temperature=buyer_temperature,
                )
            round_entry = {
                "round": round_idx,
                "speaker": speaker,
                "speaker_group": buyer_group,
                "speaker_label": group_name(buyer_group),
                "prompt": last_message,
                "text": reply,
            }
            rounds.append(round_entry)
            if log_round_cb:
                log_round_cb(round_entry)
            if stream_round_cb:
                stream_round_cb(round_entry)
            if progress_cb:
                progress_cb(round_idx, speaker)
            if accepts_price(client, reply, model=accept_model):
                break
            last_message = reply

    return rounds


def main():
    global GROUP_NAMES
    parser = argparse.ArgumentParser(description="Run negotiation tournaments.")
    parser.add_argument("--strategies", default="strategies.csv")
    parser.add_argument("--buyer-template", default="buyerprompt.txt")
    parser.add_argument("--seller-template", default="sellerprompt.txt")
    parser.add_argument("--results", default="results.csv")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--max-rounds", type=int, default=12)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--model", default="gpt-4.1-mini")
    parser.add_argument("--group0-model", default=GROUP_MODELS[0])
    parser.add_argument("--group1-model", default=GROUP_MODELS[1])
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable tqdm progress bar and spinners (cleaner conversation output).",
    )
    parser.add_argument(
        "--exclude-strategies",
        action="store_true",
        help="Do not inject per-agent strategies from the CSV into the system prompt templates.",
    )
    parser.add_argument(
        "--group-column",
        default=None,
        help="CSV column name indicating agent group (0/1). If omitted, auto-detects common names.",
    )
    parser.add_argument(
        "--default-group",
        type=int,
        default=0,
        help="Group to assume when no group column is present (default: 0).",
    )
    parser.add_argument("--parser-model", default="gpt-4.1-mini")
    parser.add_argument("--start-message", default=DEFAULT_START_MESSAGE)
    args = parser.parse_args()

    dotenv_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.getenv("OPENAI_API_KEY"):
        load_dotenv(dotenv_path=dotenv_path, override=False)
    client = OpenAI()

    buyer_template = load_template(args.buyer_template)
    seller_template = load_template(args.seller_template)

    df, buyer_col, seller_col = load_strategies(
        args.strategies, limit=args.limit, include_strategies=not args.exclude_strategies
    )

    def coerce_group(value) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, (int, float)) and not pd.isna(value):
            iv = int(value)
            return iv if iv in (0, 1) else None
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("0", "group0", "group 0"):
                return 0
            if v in ("1", "group1", "group 1"):
                return 1
            if v.isdigit():
                iv = int(v)
                return iv if iv in (0, 1) else None
            if GROUP_NAMES[0].lower() in v:
                return 0
            if GROUP_NAMES[1].lower() in v:
                return 1
        return None

    if args.group_column:
        if args.group_column not in df.columns:
            raise ValueError(
                f"--group-column {args.group_column!r} not found in CSV columns: {list(df.columns)}"
            )
        group_col = args.group_column
    else:
        group_col = detect_column(df, ["group", "team", "cohort", "condition"])

    def row_group(row) -> int:
        if group_col is None:
            return int(args.default_group)
        g = coerce_group(row.get(group_col))
        if g is None:
            raise ValueError(
                f"Invalid group value {row.get(group_col)!r} in column {group_col!r}; expected 0 or 1."
            )
        return int(g)

    team_name_col = detect_column(df, ["member names", "team name", "team", "name"])
    group_names_from_csv: dict[int, str] = {}
    if team_name_col:
        for _, row in df.iterrows():
            gid = row_group(row)
            name = str(row.get(team_name_col, "")).strip()
            if name:
                group_names_from_csv.setdefault(gid, name)
    # Prefer CSV-provided names; fall back to generic labels for missing groups.
    GROUP_NAMES = {0: "Group 0", 1: "Group 1", **group_names_from_csv}

    buyers = []
    sellers = []
    for _, row in df.iterrows():
        gid = row_group(row)
        buyer_strategy = "" if buyer_col is None else row[buyer_col]
        seller_strategy = "" if seller_col is None else row[seller_col]

        row_id = int(row["__row_id__"])
        # If we're using per-agent strategies from the CSV, only include rows that
        # actually provide a prompt for that role.
        if buyer_col is None or str(buyer_strategy).strip():
            buyers.append({"id": row_id, "group": gid, "strategy": buyer_strategy})
        if seller_col is None or str(seller_strategy).strip():
            sellers.append({"id": row_id, "group": gid, "strategy": seller_strategy})

    def model_for_group(group_id: Optional[int]) -> str:
        if group_id == 0:
            return args.group0_model
        if group_id == 1:
            return args.group1_model
        return args.model

    results = []
    pairs = [(b, s) for b in buyers for s in sellers if b["id"] != s["id"]]
    # Prefer showing/running matchups where group 1 is the buyer first.
    pairs.sort(
        key=lambda pair: (
            0 if pair[0].get("group") == 1 else 1,
            pair[0]["id"],
            pair[1]["id"],
        )
    )
    total_pairs = len(pairs)
    total_rounds = total_pairs * args.max_rounds

    if total_pairs == 0:
        print(
            "No matchups to run. Need at least one buyer and one seller strategy row to form pairs.",
            flush=True,
        )
        print(
            f"Loaded strategies: {len(df)} row(s); buyers: {len(buyers)}; sellers: {len(sellers)}.",
            flush=True,
        )
        return
    if args.max_rounds <= 0:
        print("No rounds to run (--max-rounds must be > 0).", flush=True)
        return

    print(
        f"Loaded strategies: {len(df)} row(s); buyers: {len(buyers)}; sellers: {len(sellers)}; "
        f"matchups: {total_pairs}; max rounds: {args.max_rounds}.",
        flush=True,
    )
    os.makedirs(args.logs_dir, exist_ok=True)

    for run_idx in range(1, args.repeats + 1):
        run_tag = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(args.logs_dir, f"{run_tag}_run{run_idx}")
        os.makedirs(run_dir, exist_ok=True)

        if tqdm and not args.no_progress:
            pbar = tqdm(
                total=total_rounds, desc=f"Rounds (run {run_idx}/{args.repeats})"
            )
        else:
            pbar = None

        for pair_idx, (buyer, seller) in enumerate(pairs):
            def progress_cb(round_idx, speaker):
                if pbar:
                    pbar.update(1)
                    pbar.set_postfix(
                        buyer=buyer["id"],
                        seller=seller["id"],
                        round=round_idx,
                        speaker=speaker,
                    )

            def emit_notice(message: str) -> None:
                if pbar:
                    pbar.write(message)
                else:
                    print(message, flush=True)

            buyer_strategy_text = str(buyer.get("strategy", "")).strip()
            if buyer_strategy_text:
                buyer_prompt = build_prompt(buyer_template, "buyerPrompt", buyer_strategy_text)
            else:
                buyer_prompt = ""
                emit_notice("No buyer prompt")

            seller_strategy_text = str(seller.get("strategy", "")).strip()
            if seller_strategy_text:
                seller_prompt = build_prompt(
                    seller_template, "sellerPrompt", seller_strategy_text
                )
            else:
                seller_prompt = ""
                emit_notice("No seller prompt")

            log_name = f"negotiation_b{buyer['id']}_s{seller['id']}.md"
            log_path = os.path.join(run_dir, log_name)
            use_color = supports_color()
            with open(log_path, "a", encoding="utf-8") as log_file:
                def log_round_cb(r):
                    log_file.write(format_round_block(r) + "\n")
                    log_file.flush()

                def stream_round_cb(r):
                    text = format_stream_round(
                        r,
                        buyer["id"],
                        seller["id"],
                        buyer.get("group"),
                        seller.get("group"),
                        use_color,
                    )
                    if pbar:
                        pbar.write(text)
                        pbar.write("")
                    else:
                        print(text, flush=True)
                        print("", flush=True)

                def spinner_cb(label: str):
                    return spinner(label, pbar=pbar, enabled=not args.no_progress)

                rounds = run_negotiation(
                    client=client,
                    buyer_prompt=buyer_prompt,
                    seller_prompt=seller_prompt,
                    buyer_model=model_for_group(buyer.get("group")),
                    seller_model=model_for_group(seller.get("group")),
                    max_rounds=args.max_rounds,
                    start_message=args.start_message,
                    buyer_group=buyer.get("group"),
                    seller_group=seller.get("group"),
                    progress_cb=progress_cb,
                    log_round_cb=log_round_cb,
                    stream_round_cb=stream_round_cb,
                    spinner_cb=None if args.no_progress else spinner_cb,
                )

            transcript = format_transcript(rounds)

            with spinner("Grading transcript", pbar=pbar, enabled=not args.no_progress):
                outcome = parse_outcome(client, transcript, model=args.parser_model)
            agreement = bool(outcome.get("agreement"))
            price = coerce_price(outcome.get("price"))
            if not agreement:
                price = None

            buyer_surplus = 0
            seller_surplus = 0
            if agreement and isinstance(price, (int, float)):
                buyer_surplus = 22000 - price
                seller_surplus = price - 18000

            results.append(
                {
                    "run_index": run_idx,
                    "buyer_index": buyer["id"],
                    "seller_index": seller["id"],
                    "buyer_group": buyer.get("group"),
                    "seller_group": seller.get("group"),
                    "agreement": agreement,
                    "price_agreed": price,
                    "buyer_surplus": buyer_surplus,
                    "seller_surplus": seller_surplus,
                    "rounds_completed": len(rounds),
                    "transcript_path": log_path,
                }
            )

            if len(rounds) == args.max_rounds:
                if pbar:
                    pbar.write("END")
                else:
                    print("END")
                if pair_idx < (len(pairs) - 1):
                    if pbar:
                        pbar.write("")
                    else:
                        print("")

        if pbar:
            pbar.close()

    results_df = pd.DataFrame(results)
    write_header = not os.path.exists(args.results)
    results_df.to_csv(args.results, mode="a", header=write_header, index=False)


if __name__ == "__main__":
    main()
