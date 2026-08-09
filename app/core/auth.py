"""
Virtual key authentication, per-key model allowlist, RPM rate limiting, and
monthly budget enforcement.

Concurrency correctness: rate-limit admission and budget admission are both
"check remaining capacity, then consume it" operations. Under concurrent
requests naive read-then-write is a classic TOCTOU race that lets a key
over-admit past its limit. We close that race by running each admission
check as a single SQLite transaction opened with BEGIN IMMEDIATE, which
takes SQLite's write lock *before* reading, serializing all admission
decisions for a given key. Combined with `PRAGMA busy_timeout`, concurrent
requests queue briefly instead of racing.

Keys are authenticated by hash (see app/core/security.py) -- the plaintext
key never touches the database.
"""
import datetime
import json
from dataclasses import dataclass
from typing import Optional

from app.core.db import get_conn
from app.core.security import hash_key


class RejectReason:
    INVALID_KEY = "invalid_key"
    MODEL_NOT_ALLOWED = "model_not_allowed"
    RATE_LIMITED = "rate_limited"
    BUDGET_EXHAUSTED = "budget_exhausted"


class GatewayRejection(Exception):
    def __init__(self, reason: str, message: str, http_status: int):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.http_status = http_status


@dataclass
class KeyRecord:
    key_hash: str
    key_prefix: str
    tenant: str
    allowed_models: list
    rpm_limit: int
    monthly_budget_usd: float
    spent_usd: float
    period_start: str


def _row_to_key(row) -> KeyRecord:
    return KeyRecord(
        key_hash=row["key_hash"],
        key_prefix=row["key_prefix"],
        tenant=row["tenant"],
        allowed_models=json.loads(row["allowed_models"]),
        rpm_limit=row["rpm_limit"],
        monthly_budget_usd=row["monthly_budget_usd"],
        spent_usd=row["spent_usd"],
        period_start=row["period_start"],
    )


def get_key_by_plaintext(plaintext_key: str) -> Optional[KeyRecord]:
    conn = get_conn()
    kh = hash_key(plaintext_key)
    row = conn.execute("SELECT * FROM virtual_keys WHERE key_hash = ?", (kh,)).fetchone()
    if not row:
        return None
    return _row_to_key(row)


def _maybe_reset_period(conn, rec: KeyRecord):
    today = datetime.date.today()
    period_start = datetime.date.fromisoformat(rec.period_start)
    if today.month != period_start.month or today.year != period_start.year:
        conn.execute(
            "UPDATE virtual_keys SET spent_usd = 0, period_start = ? WHERE key_hash = ?",
            (today.isoformat(), rec.key_hash),
        )
        rec.spent_usd = 0.0


def authenticate(plaintext_key: str) -> KeyRecord:
    rec = get_key_by_plaintext(plaintext_key)
    if rec is None:
        raise GatewayRejection(RejectReason.INVALID_KEY, "Virtual key not recognized.", 401)
    return rec


def check_model_allowed(rec: KeyRecord, requested_model: str):
    if requested_model not in rec.allowed_models:
        raise GatewayRejection(
            RejectReason.MODEL_NOT_ALLOWED,
            f"Key is not allowed to use model/alias '{requested_model}'.",
            403,
        )


def admit_rate_limit(key_hash: str, rpm_limit: int):
    """Atomically check-and-increment the current-minute counter for this key.
    Returns (remaining, reset_epoch_seconds) so the caller can surface
    standard X-RateLimit-* response headers.
    """
    conn = get_conn()
    import time
    window_start = int(time.time() // 60 * 60)
    reset_at = window_start + 60
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT count FROM rate_windows WHERE key_hash = ? AND window_start = ?",
            (key_hash, window_start),
        ).fetchone()
        current = row["count"] if row else 0
        if current >= rpm_limit:
            conn.execute("COMMIT")
            raise GatewayRejection(
                RejectReason.RATE_LIMITED,
                f"Rate limit exceeded: {rpm_limit} requests/minute.",
                429,
            )
        if row:
            conn.execute(
                "UPDATE rate_windows SET count = count + 1 WHERE key_hash = ? AND window_start = ?",
                (key_hash, window_start),
            )
        else:
            conn.execute(
                "INSERT INTO rate_windows (key_hash, window_start, count) VALUES (?, ?, 1)",
                (key_hash, window_start),
            )
        conn.execute("COMMIT")
        remaining = rpm_limit - (current + 1)
        return remaining, reset_at
    except GatewayRejection:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


def admit_budget(rec: KeyRecord):
    """Hard-stop check: reject if the key has already reached its monthly cap.
    Actual cost is reconciled after the call via `record_actual_spend`.
    """
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM virtual_keys WHERE key_hash = ?", (rec.key_hash,)).fetchone()
        current = _row_to_key(row)
        _maybe_reset_period(conn, current)
        if current.spent_usd >= current.monthly_budget_usd:
            conn.execute("COMMIT")
            raise GatewayRejection(
                RejectReason.BUDGET_EXHAUSTED,
                f"Monthly budget of ${current.monthly_budget_usd:.4f} exhausted "
                f"(spent ${current.spent_usd:.4f}).",
                402,
            )
        conn.execute("COMMIT")
    except GatewayRejection:
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_actual_spend(key_hash: str, cost_usd: float):
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE virtual_keys SET spent_usd = spent_usd + ? WHERE key_hash = ?",
            (cost_usd, key_hash),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
