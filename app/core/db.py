"""
SQLite persistence layer for Prism.

Design notes:
- SQLite in WAL mode gives us safe concurrent readers + a single writer queue,
  which is enough for a gateway demo under moderate concurrent load.
- All money/usage mutations (budget spend, rate-limit counters) go through
  short, explicit transactions guarded by SQLite's own locking so that
  concurrent requests can't double-spend a budget or bypass a rate limit
  (the classic check-then-act race). We use BEGIN IMMEDIATE to grab the
  write lock up front instead of optimistic retries.
- Virtual keys are stored as SHA-256 hashes, never plaintext (see
  app/core/security.py). key_prefix is a masked display value only.
"""
import sqlite3
import threading
import time
import os

DB_PATH = os.environ.get("PRISM_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "..", "data", "prism.db"))

_local = threading.local()


def get_conn():
    """Return a thread-local SQLite connection with WAL + busy timeout."""
    if not hasattr(_local, "conn"):
        conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS virtual_keys (
    key_hash TEXT PRIMARY KEY,         -- SHA-256 of the plaintext virtual key
    key_prefix TEXT NOT NULL,          -- masked display value, e.g. sk-prism-team...0001
    tenant TEXT NOT NULL,
    allowed_models TEXT NOT NULL,      -- JSON list
    rpm_limit INTEGER NOT NULL,
    monthly_budget_usd REAL NOT NULL,
    spent_usd REAL NOT NULL DEFAULT 0,
    period_start TEXT NOT NULL         -- ISO date the current budget period started
);

CREATE TABLE IF NOT EXISTS rate_windows (
    key_hash TEXT NOT NULL,
    window_start INTEGER NOT NULL,     -- unix epoch second, floored to minute
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_hash, window_start)
);

CREATE TABLE IF NOT EXISTS request_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    ts REAL NOT NULL,
    key_prefix TEXT NOT NULL,
    tenant TEXT NOT NULL,
    requested_model TEXT NOT NULL,
    resolved_model TEXT,
    provider TEXT,
    status TEXT NOT NULL,              -- ok | error | rejected
    reject_reason TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    cache_hit INTEGER DEFAULT 0,
    fallback_used INTEGER DEFAULT 0,
    latency_ms REAL DEFAULT 0,
    stream INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cache_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    vector TEXT NOT NULL,              -- JSON list[float], bag-of-words embedding
    response_text TEXT NOT NULL,
    resolved_model TEXT NOT NULL,
    provider TEXT NOT NULL,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'up'
);

CREATE INDEX IF NOT EXISTS idx_cache_tenant ON cache_entries(tenant);
CREATE INDEX IF NOT EXISTS idx_logs_key_prefix ON request_logs(key_prefix);
"""


def init_db(reset: bool = False):
    """Initialize schema. `reset=True` drops and recreates tables in place
    (rather than deleting the DB file) so that any already-open thread-local
    connections -- including ones opened by worker threads spawned before
    the reset -- keep pointing at a valid, current file. Deleting the file
    out from under a cached connection would leave that connection attached
    to an orphaned inode with stale schema, while any *new* connection to
    the same path would see an empty, schema-less file -- a real bug we hit
    while writing the concurrency tests for this project.
    """
    conn = get_conn()
    if reset:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for t in tables:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.executescript(SCHEMA)


def now() -> float:
    return time.time()
