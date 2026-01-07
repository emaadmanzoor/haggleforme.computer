import { FormEvent, useEffect, useMemo, useState } from "react";
import "./App.css";

type Role = "buyer" | "seller";

type LeaderboardEntry = {
  username: string;
  matches: number;
  agreements: number;
  total_surplus: number;
};

type MatchRound = {
  round: number;
  speaker: "Buyer" | "Seller";
  text: string;
};

type MatchResult = {
  opponent_role?: Role;
  opponent_label?: string;
  agreement?: boolean;
  price?: number | null;
  rounds?: number;
  surplus?: number;
  opponent_surplus?: number;
  transcript?: MatchRound[];
};

type SubmissionResponse = {
  status: "matched" | "queued";
  entry_id: string;
  role: Role;
  opponent_role?: Role;
  opponent_label?: string;
  user_total_surplus?: number | null;
  agreement?: boolean;
  price?: number | null;
  rounds?: number;
  surplus?: number;
  opponent_surplus?: number;
  total_surplus?: number | null;
  matches?: MatchResult[];
  transcript?: MatchRound[];
  leaderboard?: LeaderboardEntry[];
  message?: string;
};

const AUTH_TOKEN_KEY = "negotiationAuthToken";
const AUTH_USER_KEY = "negotiationAuthUser";

const API_BASE = (() => {
  const explicit =
    typeof import.meta !== "undefined" ? import.meta.env.VITE_API_BASE_URL : undefined;
  if (explicit) {
    return explicit;
  }
  if (typeof window !== "undefined") {
    return "";
  }
  return "https://haggleforme.computer";
})();

const apiUrl = (path: string) => `${API_BASE}${path}`;

const hashPassword = async (password: string): Promise<string> => {
  if (typeof window === "undefined" || !window.crypto?.subtle) {
    throw new Error("Secure hashing is not available in this browser.");
  }
  const encoder = new TextEncoder();
  const data = encoder.encode(password);
  const digest = await window.crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
};

const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const formatCurrency = (value?: number | null) => {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "—";
  }
  return currencyFormatter.format(value);
};

const toNumber = (value: unknown): number => {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string") {
    const parsed = Number.parseFloat(value);
    if (Number.isFinite(parsed)) {
      return parsed;
    }
  }
  return 0;
};

const normalizeLeaderboard = (value: unknown): LeaderboardEntry[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .map((item) => {
      if (!item || typeof item !== "object") {
        return null;
      }
      const source = item as Record<string, unknown>;
      const username = typeof source.username === "string" ? source.username : "";
      const matches = Math.max(0, Math.trunc(toNumber(source.matches)));
      const agreements = Math.max(0, Math.trunc(toNumber(source.agreements)));
      const total_surplus = toNumber(source.total_surplus);
      return {
        username,
        matches,
        agreements,
        total_surplus,
      };
    })
    .filter((entry): entry is LeaderboardEntry => entry !== null);
};

const HEADER_TEXT = "haggle for me, computer";

function App() {
  const [role, setRole] = useState<Role>("buyer");
  const [strategy, setStrategy] = useState<string>("");
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [result, setResult] = useState<SubmissionResponse | null>(null);
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [buyerPrompt, setBuyerPrompt] = useState<string>("");
  const [sellerPrompt, setSellerPrompt] = useState<string>("");
  const [promptOpen, setPromptOpen] = useState<boolean>(false);
  const [resultOpen, setResultOpen] = useState<boolean>(false);

  const [authToken, setAuthToken] = useState<string | null>(null);
  const [authUser, setAuthUser] = useState<string | null>(null);
  const [authMode, setAuthMode] = useState<"signin" | "register" | null>(null);
  const [authFormUsername, setAuthFormUsername] = useState<string>("");
  const [authFormPassword, setAuthFormPassword] = useState<string>("");
  const [authError, setAuthError] = useState<string | null>(null);
  const [authLoading, setAuthLoading] = useState<boolean>(false);
  const [authReady, setAuthReady] = useState<boolean>(false);
  const [strategiesOpen, setStrategiesOpen] = useState<boolean>(false);
  const [strategiesLoading, setStrategiesLoading] = useState<boolean>(false);
  const [strategiesError, setStrategiesError] = useState<string | null>(null);
  const [userStrategies, setUserStrategies] = useState<{
    buyer_strategy?: string | null;
    seller_strategy?: string | null;
  } | null>(null);

  const isSignedIn = useMemo(() => Boolean(authToken && authUser), [authToken, authUser]);

  const persistAuth = (token: string, usernameValue: string) => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(AUTH_TOKEN_KEY, token);
      window.localStorage.setItem(AUTH_USER_KEY, usernameValue);
    }
    setAuthToken(token);
    setAuthUser(usernameValue);
  };

  const clearStoredAuth = () => {
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(AUTH_TOKEN_KEY);
      window.localStorage.removeItem(AUTH_USER_KEY);
    }
    setAuthToken(null);
    setAuthUser(null);
  };

  const resetAuthForm = () => {
    setAuthFormUsername("");
    setAuthFormPassword("");
  };

  const closeAuthOverlay = () => {
    setAuthMode(null);
    setAuthError(null);
    resetAuthForm();
  };

  const openAuth = (mode: "signin" | "register") => {
    setAuthError(null);
    resetAuthForm();
    setAuthMode(mode);
  };

  const loadUserStrategies = async () => {
    if (!authToken) {
      return;
    }
    setStrategiesLoading(true);
    setStrategiesError(null);
    try {
      const response = await fetch(apiUrl("/api/strategies/me"), {
        headers: { Authorization: `Bearer ${authToken}` },
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail =
          typeof payload.detail === "string" ? payload.detail : "Unable to load strategies.";
        throw new Error(detail);
      }
      setUserStrategies({
        buyer_strategy: payload.buyer_strategy ?? null,
        seller_strategy: payload.seller_strategy ?? null,
      });
    } catch (error) {
      setStrategiesError((error as Error).message);
    } finally {
      setStrategiesLoading(false);
    }
  };

  const loadLeaderboard = async () => {
    try {
      const response = await fetch(apiUrl("/api/leaderboard"));
      if (!response.ok) {
        return;
      }
      const payload = await response.json().catch(() => ({}));
      if (Array.isArray(payload.leaderboard)) {
        setLeaderboard(normalizeLeaderboard(payload.leaderboard));
      }
    } catch (err) {
      console.warn("Unable to refresh leaderboard", err);
    }
  };

  useEffect(() => {
    if (typeof window === "undefined") {
      setAuthReady(true);
      return;
    }
    try {
      const storedToken = window.localStorage.getItem(AUTH_TOKEN_KEY);
      const storedUser = window.localStorage.getItem(AUTH_USER_KEY);
      if (storedToken && storedUser) {
        setAuthToken(storedToken);
        setAuthUser(storedUser);
      }
    } finally {
      setAuthReady(true);
    }
  }, []);

  useEffect(() => {
    if (!authReady) {
      return;
    }
    loadLeaderboard();
  }, [authReady]);

  useEffect(() => {
    const loadPrompts = async () => {
      try {
        const response = await fetch(apiUrl("/api/prompts"));
        if (!response.ok) {
          return;
        }
        const payload = await response.json().catch(() => ({}));
        if (typeof payload.buyer_prompt === "string") {
          setBuyerPrompt(payload.buyer_prompt);
        }
        if (typeof payload.seller_prompt === "string") {
          setSellerPrompt(payload.seller_prompt);
        }
      } catch (err) {
        console.warn("Unable to load system prompts", err);
      }
    };

    loadPrompts();
  }, []);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const trimmed = strategy.trim();
    if (!trimmed || isSubmitting) {
      return;
    }

    setIsSubmitting(true);
    setErrorMessage(null);
    setResult(null);

    try {
      const headers: HeadersInit = { "Content-Type": "application/json" };
      if (authToken) {
        headers.Authorization = `Bearer ${authToken}`;
      }
      const response = await fetch(apiUrl("/api/submit"), {
        method: "POST",
        headers,
        body: JSON.stringify({ role, strategy: trimmed }),
      });

      const payload = await response.json().catch(() => ({}));

      if (response.status === 401) {
        clearStoredAuth();
        resetAuthForm();
        setAuthMode("signin");
        setAuthError("Your session expired. Please sign in again.");
        return;
      }
      if (!response.ok) {
        const detail = typeof payload.detail === "string" ? payload.detail : "Submission failed.";
        throw new Error(detail);
      }

      setResult(payload as SubmissionResponse);
      setResultOpen(true);
      if (Array.isArray(payload.leaderboard)) {
        setLeaderboard(normalizeLeaderboard(payload.leaderboard));
      }
    } catch (error) {
      setErrorMessage((error as Error).message);
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleAuthSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!authMode || authLoading) {
      return;
    }
    setAuthLoading(true);
    setAuthError(null);

    try {
      const hashed = await hashPassword(authFormPassword);
      const response = await fetch(
        apiUrl(authMode === "signin" ? "/api/auth/login" : "/api/auth/register"),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: authFormUsername.trim(),
            password_hash: hashed,
          }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof payload.detail === "string" ? payload.detail : "Authentication failed.";
        throw new Error(detail);
      }
      persistAuth(payload.token, payload.username);
      closeAuthOverlay();
      loadLeaderboard();
    } catch (error) {
      setAuthError((error as Error).message);
    } finally {
      setAuthLoading(false);
    }
  };

  const handleLogout = async () => {
    if (authToken) {
      try {
        await fetch(apiUrl("/api/auth/logout"), {
          method: "POST",
          headers: { Authorization: `Bearer ${authToken}` },
        });
      } catch (error) {
        console.warn("Logout failed", error);
      }
    }
    clearStoredAuth();
    setUserStrategies(null);
    setStrategiesOpen(false);
    loadLeaderboard();
  };

  const formDisabled = isSubmitting || strategy.trim().length === 0;
  const roleHint = role === "buyer" ? "buyer" : "seller";
  const matches = useMemo<MatchResult[]>(() => {
    if (!result) {
      return [];
    }
    if (Array.isArray(result.matches) && result.matches.length > 0) {
      return result.matches;
    }
    if (result.status === "matched") {
      return [
        {
          opponent_role: result.opponent_role,
          opponent_label: result.opponent_label,
          agreement: result.agreement,
          price: result.price,
          surplus: result.surplus,
          opponent_surplus: result.opponent_surplus,
          transcript: result.transcript ?? [],
        },
      ];
    }
    return [];
  }, [result]);
  const totalSurplus = useMemo(() => {
    if (!result || result.status !== "matched") {
      return null;
    }
    if (result.total_surplus !== undefined && result.total_surplus !== null) {
      return result.total_surplus;
    }
    return matches.reduce(
      (sum, match) => sum + (match.agreement ? toNumber(match.surplus) : 0),
      0,
    );
  }, [matches, result]);
  const activePrompt =
    role === "buyer"
      ? buyerPrompt || "Loading buyer system prompt…"
      : sellerPrompt || "Loading seller system prompt…";


  return (
    <div className="app-shell">
      <a
        href="https://github.com/emaadmanzoor/haggleforme.computer"
        className="github-corner"
        aria-label="View source on GitHub"
      >
        <svg width="80" height="80" viewBox="0 0 250 250" aria-hidden="true">
          <path d="M0,0 L250,0 L250,250 Z" />
          <path
            className="octo-arm"
            d="M128.3,109.0 C113.8,99.7 119.0,89.6 119.0,89.6 C122.0,82.7 120.5,78.6 120.5,78.6 C119.2,72.0 123.4,76.3 123.4,76.3 C127.3,80.9 125.5,87.3 125.5,87.3 C122.9,97.6 130.6,101.9 134.4,103.2"
            fill="currentColor"
          />
          <path
            className="octo-body"
            d="M115.0,115.0 C114.9,115.1 118.7,116.5 119.8,115.4 L133.7,101.6 C136.9,99.2 139.9,98.4 142.2,98.6 C133.8,88.0 127.5,74.4 143.8,58.0 C148.5,53.4 154.0,51.2 159.7,51.0 C160.3,49.4 163.2,43.6 171.4,40.1 C171.4,40.1 176.1,42.2 178.8,56.2 C183.1,58.7 187.2,62.0 190.9,65.9 C194.6,69.8 197.9,73.9 200.4,78.2 C214.4,80.9 216.5,85.6 216.5,85.6 C213.0,93.8 207.2,96.7 205.6,97.3 C205.4,103.0 203.2,108.5 198.6,113.2 C182.2,129.6 168.6,123.3 158.0,114.9 C158.2,117.2 157.4,120.2 155.0,123.4 L141.2,137.2 C140.1,138.3 141.5,142.1 141.6,142.0"
            fill="currentColor"
          />
        </svg>
      </a>
      <div className="terminal-window">
        <header className="terminal-header">
          <div className="header-inner">
            <pre><span className="header-title">{HEADER_TEXT}<span className="header-dot" aria-hidden="true" /></span></pre>
            <div className="auth-controls">
              {isSignedIn ? (
                <>
                  <span className="auth-user">
                    signed in as {authUser} (
                    <button
                      type="button"
                      className="auth-link"
                      onClick={() => {
                        setStrategiesOpen(true);
                        loadUserStrategies();
                      }}
                    >
                      view my strategies
                    </button>
                    )
                  </span>
                  <button className="auth-button" type="button" onClick={handleLogout}>
                    Sign out
                  </button>
                </>
              ) : (
                <>
                  <span className="auth-user">guest mode</span>
                  <button className="auth-button" type="button" onClick={() => openAuth("signin")}>Sign in</button>
                  <button className="auth-button" type="button" onClick={() => openAuth("register")}>Register</button>
                </>
              )}
            </div>
          </div>
        </header>
        <div className="terminal-content">
          <div className="terminal-columns">
            <div className="terminal-column">
              <form className="strategy-form" onSubmit={handleSubmit}>
                <div className="strategy-header">
                  <span className="strategy-title">Negotiation Strategy</span>
                  <span className="strategy-role" aria-hidden="true"></span>
                  <div className="command-actions">
                    <button
                      type="button"
                      className={`text-button role-button ${role === "buyer" ? "primary" : ""}`}
                      onClick={() => setRole("buyer")}
                    >
                      Buyer
                    </button>
                    <button
                      type="button"
                      className={`text-button role-button ${role === "seller" ? "primary" : ""}`}
                      onClick={() => setRole("seller")}
                    >
                      Seller
                    </button>
                  </div>
                </div>
                <textarea
                  className="strategy-input"
                  placeholder={
                    role === "buyer"
                      ? "Offer a deal a few thousand under the KBB range and go up by a few hundred dollars as needed with the aim of closing as low as possible."
                      : "Start with an offer that’s a few thousand dollars above the midpoint of the blue book value range. Go down by $500–$1000 each round if necessary. Aim to close a deal as far above the trade-in price as possible."
                  }
                  value={strategy}
                  onChange={(event) => setStrategy(event.target.value)}
                  rows={12}
                  maxLength={4000}
                />
                <div className="strategy-header">
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => setPromptOpen(true)}
                  >
                    View system prompt
                  </button>
                  <div className="command-actions">
                    <button className="text-button primary" type="submit" disabled={formDisabled}>
                      {isSubmitting ? "Running tournaments…" : "Submit"}
                    </button>
                  </div>
                </div>
              </form>

              {errorMessage && <div className="alert">{errorMessage}</div>}

              {result && (
                <button
                  type="button"
                  className="text-button"
                  onClick={() => setResultOpen(true)}
                >
                  View results
                </button>
              )}
            </div>

            <div className="terminal-column narrow">
              <p className="terminal-block">Leaderboard Top 10</p>
              {leaderboard.length === 0 ? (
                <p className="terminal-block terminal-empty">
                  No ranked users yet. Register to appear here.
                </p>
              ) : (
                <div className="leaderboard-list">
                  <div className="leaderboard-row header">
                    <span>Player</span>
                    <span>Matches</span>
                    <span>Total</span>
                  </div>
                  {leaderboard.map((entry) => (
                    <div key={entry.username} className="leaderboard-row">
                      <span className="player-name">{entry.username}</span>
                      <span>{entry.matches}</span>
                      <span>{formatCurrency(entry.total_surplus)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {strategiesOpen && (
        <div className="prompt-modal-overlay" role="dialog" aria-modal="true">
          <div className="prompt-modal">
            <div className="prompt-modal-body">
              <div className="modal-header-row">
                <button
                  type="button"
                  className="text-button prompt-close"
                  onClick={() => setStrategiesOpen(false)}
                >
                  Close
                </button>
              </div>
              <div className="result-grid strategies-grid">
                <div>
                  <span className="result-label">Buyer strategy</span>
                  <span className="strategy-value">
                    {strategiesLoading
                      ? "Loading…"
                      : userStrategies?.buyer_strategy || "No strategy submitted"}
                  </span>
                </div>
                <div>
                  <span className="result-label">Seller strategy</span>
                  <span className="strategy-value">
                    {strategiesLoading
                      ? "Loading…"
                      : userStrategies?.seller_strategy || "No strategy submitted"}
                  </span>
                </div>
              </div>
              {strategiesError && (
                <div className="auth-error" role="alert" aria-live="polite">
                  {strategiesError}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {authMode && (
        <div className="auth-overlay" role="dialog" aria-modal="true">
          <form className="auth-panel" onSubmit={handleAuthSubmit}>
            <h2>{authMode === "signin" ? "Sign in" : "Create account"}</h2>
            <label>
              Username
              <input
                className={authError ? "error" : ""}
                value={authFormUsername}
                onChange={(event) => setAuthFormUsername(event.target.value)}
                autoComplete="username"
                required
              />
            </label>
            <label>
              Password
              <input
                type="password"
                className={authError ? "error" : ""}
                value={authFormPassword}
                onChange={(event) => setAuthFormPassword(event.target.value)}
                autoComplete={authMode === "signin" ? "current-password" : "new-password"}
                required
              />
            </label>
            {authError && (
              <div className="auth-error" role="alert" aria-live="polite">
                {authError}
              </div>
            )}
            <div className="auth-actions">
              <button type="button" className="text-button" onClick={closeAuthOverlay}>
                Cancel
              </button>
              <button
                type="submit"
                className="text-button primary"
                disabled={authLoading}
              >
                {authLoading ? "Working…" : authMode === "signin" ? "Sign in" : "Register"}
              </button>
            </div>
          </form>
        </div>
      )}

      {promptOpen && (
        <div className="prompt-modal-overlay" role="dialog" aria-modal="true">
          <div className="prompt-modal">
            <div className="prompt-modal-body">
              <div className="modal-header-row">
                <button
                  type="button"
                  className="text-button prompt-close"
                  onClick={() => setPromptOpen(false)}
                >
                  Close
                </button>
              </div>
              <pre>{activePrompt}</pre>
            </div>
          </div>
        </div>
      )}

      {resultOpen && result && (
        <div className="prompt-modal-overlay" role="dialog" aria-modal="true">
          <div className="prompt-modal">
            <div className="prompt-modal-body">
              <div className="result-header-row">
                <div className="result-stack">
                  <span className="result-label">Your role</span>
                  <strong>{roleHint === "buyer" ? "Buyer" : "Seller"}</strong>
                </div>
                <div className="result-stack">
                  <span className="result-label">Opponent's role</span>
                  <strong>{roleHint === "buyer" ? "Seller" : "Buyer"}</strong>
                </div>
                <div className="result-stack">
                  <span className="result-label">Total surplus</span>
                  <strong>{totalSurplus !== null ? formatCurrency(totalSurplus) : "N/A"}</strong>
                </div>
                <button
                  type="button"
                  className="text-button prompt-close"
                  onClick={() => setResultOpen(false)}
                >
                  Close
                </button>
              </div>
              <div className="result-divider" />
              {result.status === "queued" && (
                <div className="terminal-message">
                  <pre>{result.message ?? "Waiting for an opponent strategy to join the pool."}</pre>
                </div>
              )}
              {result.status === "matched" && matches.length > 0 && (
                <div className="tournament-list">
                  {matches.map((match, idx) => (
                    <div key={`tournament-${idx}`} className="tournament-block">
                      <div className="result-grid">
                        <div>
                          <span className="result-label">Tournament</span>
                          <strong>{idx + 1}</strong>
                        </div>
                        <div>
                          <span className="result-label">Opponent</span>
                          <strong>{match.opponent_label ?? "Anonymous"}</strong>
                        </div>
                        <div>
                          <span className="result-label">Agreed price</span>
                          <strong>{match.agreement ? formatCurrency(match.price) : "N/A"}</strong>
                        </div>
                        <div>
                          <span className="result-label">Your surplus</span>
                          <strong>{match.agreement ? formatCurrency(match.surplus) : "N/A"}</strong>
                        </div>
                        <div>
                          <span className="result-label">Opponent surplus</span>
                          <strong>{match.agreement ? formatCurrency(match.opponent_surplus) : "N/A"}</strong>
                        </div>
                      </div>
                      {match.transcript && match.transcript.length > 0 && (
                        <div className="terminal-messages modal-transcript">
                          {match.transcript.map((round) => (
                            <div
                              key={`${idx}-${round.round}-${round.speaker}`}
                              className={`terminal-message ${round.speaker === "Seller" ? "assistant" : ""}`}
                            >
                              <span className="prefix">{round.speaker.toLowerCase()}</span>
                              <pre>{round.text}</pre>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
