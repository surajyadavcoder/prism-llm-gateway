from app.core.db import get_conn, now


def log_request(key_prefix, tenant, requested_model, resolved_model, provider, status,
                 request_id=None, reject_reason=None, input_tokens=0, output_tokens=0, cost_usd=0.0,
                 cache_hit=False, fallback_used=False, latency_ms=0.0, stream=False):
    conn = get_conn()
    conn.execute(
        """INSERT INTO request_logs
           (request_id, ts, key_prefix, tenant, requested_model, resolved_model, provider, status, reject_reason,
            input_tokens, output_tokens, cost_usd, cache_hit, fallback_used, latency_ms, stream)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (request_id, now(), key_prefix, tenant, requested_model, resolved_model, provider, status, reject_reason,
         input_tokens, output_tokens, cost_usd, int(cache_hit), int(fallback_used), latency_ms, int(stream)),
    )
