"""Security layer: defence-in-depth for an LLM that writes SQL.

Layers (each one independently blocks an attack):
  1. Input guard      - question length, control chars, prompt-injection heuristics.
  2. SQL static guard - single read-only SELECT/WITH statement, denylisted keywords,
                        only known tables referenced.
  3. SQLite authorizer- engine-level allowlist of operations + functions (cannot be
                        bypassed by clever SQL text, even if layer 2 misses something).
  4. Runtime limits   - query_only pragma, wall-clock timeout, row cap.
  5. Output guard     - PII masking on results, CSV/Excel formula-injection neutralising.
  6. Edge             - API keys, rate limiting, security headers, upload validation.
"""
from __future__ import annotations

import hmac
import re
import sqlite3
import threading
import time
import unicodedata
from collections import deque

# ---------------------------------------------------------------------------
# 1. Input guard
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|prompts?|rules)",
    r"disregard (all |the )?(previous|prior|above|system)",
    r"you are now (a|an|in) ",
    r"(reveal|print|show|leak) (me )?(the |your )?(system prompt|instructions|api key|secret)",
    r"\b(drop|delete|truncate|alter)\s+table\b",
    r"\battach\s+database\b",
    r"\bpragma\b",
    r"act as (a |an )?(dan|developer mode|jailbroken)",
    r"</?(system|instruction)>",
]
_INJECTION_RE = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


class SecurityError(ValueError):
    """Raised when a security control blocks a request. Message is safe to show users."""


def clean_text(text: str, max_len: int) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    text = text.strip()
    if not text:
        raise SecurityError("Question is empty.")
    if len(text) > max_len:
        raise SecurityError(f"Question is too long (max {max_len} characters).")
    return text


def detect_prompt_injection(text: str) -> str | None:
    m = _INJECTION_RE.search(text or "")
    return m.group(0) if m else None


def sanitize_identifier(name: str, fallback: str) -> str:
    """Turn arbitrary CSV headers into safe snake_case SQL identifiers."""
    name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    name = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not name:
        name = fallback
    if name[0].isdigit():
        name = f"c_{name}"
    return name[:60]


# ---------------------------------------------------------------------------
# 2. SQL static guard
# ---------------------------------------------------------------------------
_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|"
    r"reindex|analyze|begin|commit|rollback|savepoint|release|load_extension|"
    r"writefile|readfile|sqlite_master|sqlite_schema|sqlite_temp_master)\b",
    re.IGNORECASE,
)
_STRING_LIT = re.compile(r"'(?:[^']|'')*'")


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", sql)


def validate_sql(sql: str, allowed_tables: set[str]) -> str:
    """Static validation. Returns normalised SQL or raises SecurityError."""
    if not sql or not sql.strip():
        raise SecurityError("Empty SQL.")
    sql = _strip_comments(sql).strip().rstrip(";").strip()
    no_strings = _STRING_LIT.sub("''", sql)
    if ";" in no_strings:
        raise SecurityError("Multiple SQL statements are not allowed.")
    if not re.match(r"^\s*(select|with)\b", no_strings, re.IGNORECASE):
        raise SecurityError("Only read-only SELECT queries are allowed.")
    bad = _FORBIDDEN_SQL.search(no_strings)
    if bad:
        raise SecurityError(f"Forbidden SQL keyword: {bad.group(0).upper()}")
    # every FROM/JOIN target must be an allowed table or a CTE defined in this query
    ctes = {m.lower() for m in re.findall(
        r"(?:\bwith(?:\s+recursive)?|,)\s*([a-zA-Z_]\w*)\s*(?:\([^)]*\))?\s+as\s*\(", no_strings, re.IGNORECASE)}
    for tbl in re.findall(r"\b(?:from|join)\s+([a-zA-Z_\"][\w\"]*)", no_strings, re.IGNORECASE):
        t = tbl.strip('"').lower()
        if t not in allowed_tables and t not in ctes:
            raise SecurityError(f"Query references unknown table '{t}'.")
    return sql


# ---------------------------------------------------------------------------
# 3. SQLite engine-level authorizer
# ---------------------------------------------------------------------------
_SAFE_FUNCTIONS = {
    "count", "sum", "avg", "min", "max", "total", "group_concat", "abs", "round", "lower",
    "upper", "length", "substr", "substring", "trim", "ltrim", "rtrim", "replace", "coalesce",
    "ifnull", "nullif", "iif", "cast", "date", "time", "datetime", "julianday", "strftime",
    "instr", "printf", "format", "typeof", "like", "glob", "random", "row_number", "rank",
    "dense_rank", "ntile", "lag", "lead", "first_value", "last_value", "percent_rank",
    "cume_dist", "sqrt", "power", "pow", "log", "log10", "ln", "exp", "floor", "ceil",
    "ceiling", "mod", "sign", "unixepoch", "concat", "concat_ws",
}


def sqlite_authorizer(action, arg1, arg2, dbname, source):  # noqa: ANN001 - sqlite signature
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE):
        if action == sqlite3.SQLITE_READ and arg1 and arg1.lower().startswith("sqlite_"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION:
        return sqlite3.SQLITE_OK if (arg2 or "").lower() in _SAFE_FUNCTIONS else sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_DENY


# ---------------------------------------------------------------------------
# 5. Output guard
# ---------------------------------------------------------------------------
_PII_PATTERNS = {
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?\d{10}(?!\d)"),
    "aadhaar": re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)"),
}


def mask_pii(value):
    """Mask e-mails, phone, Aadhaar-like and card-like numbers inside string cells."""
    if not isinstance(value, str):
        return value, False
    hit = False
    for kind, pat in _PII_PATTERNS.items():
        if pat.search(value):
            hit = True
            if kind == "email":
                value = pat.sub(lambda m: m.group(0)[0] + "***@" + m.group(0).split("@")[-1], value)
            else:
                value = pat.sub(lambda m: "*" * (len(m.group(0)) - 4) + m.group(0)[-4:], value)
    return value, hit


def neutralize_formula(value):
    """Prevent CSV/Excel formula injection when results are exported."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


# ---------------------------------------------------------------------------
# 6. Edge controls
# ---------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window limiter, thread-safe, per client key."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False, int(self.window - (now - q[0])) + 1
            q.append(now)
            if len(self._hits) > 10_000:  # bound memory
                self._hits = {k: v for k, v in self._hits.items() if v}
            return True, 0


def verify_api_key(provided: str | None, valid_keys: list[str]) -> bool:
    if not valid_keys:
        return True  # auth disabled for local demo
    if not provided:
        return False
    return any(hmac.compare_digest(provided, k) for k in valid_keys)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}
