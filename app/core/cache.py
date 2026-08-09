"""
Semantic response cache, scoped per tenant so one tenant can never read
another tenant's cached responses even if their prompts are near-identical
(e.g. two tenants both asking "what's your refund policy").

Lookup is O(n) over that tenant's cache entries -- fine for a demo/capstone
scale; a real deployment would use an ANN index (faiss/pgvector) but the
interface (embed -> nearest neighbor above threshold) is unchanged.
"""
import json
import time
from typing import Optional

from app.core.db import get_conn
from app.core.semantic import embed, cosine
from app.core.config import settings


def _cache_key_text(messages) -> str:
    return " ".join(m.get("content", "") for m in messages if m.get("role") == "user")


def lookup(tenant: str, messages) -> Optional[dict]:
    conn = get_conn()
    threshold = settings.gateway_config["cache"]["similarity_threshold"]
    text = _cache_key_text(messages)
    if not text.strip():
        return None
    query_vec = embed(text)

    now = time.time()
    rows = conn.execute(
        "SELECT * FROM cache_entries WHERE tenant = ? AND expires_at > ?", (tenant, now)
    ).fetchall()

    best = None
    best_sim = 0.0
    for row in rows:
        vec = json.loads(row["vector"])
        sim = cosine(query_vec, vec)
        if sim > best_sim:
            best_sim = sim
            best = row

    if best is not None and best_sim >= threshold:
        return {
            "response_text": best["response_text"],
            "resolved_model": best["resolved_model"],
            "provider": best["provider"],
            "input_tokens": best["input_tokens"],
            "output_tokens": best["output_tokens"],
            "similarity": best_sim,
        }
    return None


def store(tenant: str, messages, response_text: str, resolved_model: str, provider: str,
          input_tokens: int, output_tokens: int):
    conn = get_conn()
    ttl = settings.gateway_config["cache"]["ttl_seconds"]
    text = _cache_key_text(messages)
    if not text.strip():
        return
    vec = embed(text)
    now = time.time()
    conn.execute(
        """INSERT INTO cache_entries
           (tenant, prompt_text, vector, response_text, resolved_model, provider,
            input_tokens, output_tokens, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tenant, text, json.dumps(vec), response_text, resolved_model, provider,
         input_tokens, output_tokens, now, now + ttl),
    )
