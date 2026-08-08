import json
import datetime
from app.core.db import get_conn
from app.core.config import settings
from app.core.security import hash_key, key_prefix


def seed_keys():
    """Seed virtual keys from data/seed_keys.json, storing only the hash +
    a masked prefix -- the plaintext key never touches the database.
    """
    conn = get_conn()
    today = datetime.date.today().isoformat()
    for k in settings.seed_keys:
        kh = hash_key(k["key"])
        existing = conn.execute("SELECT key_hash FROM virtual_keys WHERE key_hash = ?", (kh,)).fetchone()
        if existing:
            continue
        conn.execute(
            """INSERT INTO virtual_keys
               (key_hash, key_prefix, tenant, allowed_models, rpm_limit, monthly_budget_usd, spent_usd, period_start)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
            (kh, key_prefix(k["key"]), k["tenant"], json.dumps(k["allowed_models"]),
             k["rpm_limit"], k["monthly_budget_usd"], today),
        )
