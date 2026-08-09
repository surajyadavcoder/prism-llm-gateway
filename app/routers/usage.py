from flask import Blueprint, request, jsonify
from app.core.db import get_conn
from app.core.auth import get_key_by_plaintext

bp = Blueprint("usage", __name__)


@bp.route("/v1/usage/summary", methods=["GET"])
def usage_summary():
    plaintext_key = request.args.get("key")
    conn = get_conn()
    q = """SELECT key_prefix, tenant, COUNT(*) as requests,
                  SUM(cost_usd) as total_cost,
                  SUM(input_tokens) as total_input_tokens,
                  SUM(output_tokens) as total_output_tokens,
                  SUM(cache_hit) as cache_hits,
                  SUM(fallback_used) as fallbacks,
                  AVG(latency_ms) as avg_latency_ms
           FROM request_logs WHERE status='ok' {filt} GROUP BY key_prefix, tenant"""
    if plaintext_key:
        rec = get_key_by_plaintext(plaintext_key)
        if rec is None:
            return jsonify({"usage": []})
        rows = conn.execute(q.format(filt="AND key_prefix = ?"), (rec.key_prefix,)).fetchall()
    else:
        rows = conn.execute(q.format(filt="")).fetchall()

    result = []
    for r in rows:
        requests_n = r["requests"] or 0
        cache_hits = r["cache_hits"] or 0
        result.append({
            "key_prefix": r["key_prefix"],
            "tenant": r["tenant"],
            "requests": requests_n,
            "total_cost_usd": round(r["total_cost"] or 0.0, 6),
            "total_input_tokens": r["total_input_tokens"] or 0,
            "total_output_tokens": r["total_output_tokens"] or 0,
            "cache_hit_rate": round(cache_hits / requests_n, 4) if requests_n else 0.0,
            "fallback_count": r["fallbacks"] or 0,
            "avg_latency_ms": round(r["avg_latency_ms"] or 0.0, 2),
        })
    return jsonify({"usage": result})


@bp.route("/v1/usage/requests", methods=["GET"])
def recent_requests():
    limit = min(int(request.args.get("limit", 50)), 500)
    conn = get_conn()
    rows = conn.execute("SELECT * FROM request_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return jsonify({"requests": [dict(r) for r in rows]})
