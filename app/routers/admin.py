from flask import Blueprint, request, jsonify
from app.providers.mock import health
from app.core.db import get_conn
from app.core.admin_auth import require_admin

bp = Blueprint("admin", __name__)


@bp.route("/admin/provider_health", methods=["POST"])
@require_admin
def set_provider_health():
    body = request.get_json(force=True, silent=True) or {}
    provider = body.get("provider")
    state = body.get("state")
    if state not in ("up", "down", "degraded") or not provider:
        return jsonify({"error": "provider and state (up|down|degraded) required"}), 400
    health.set(provider, state)
    return jsonify({"provider": provider, "state": state})


@bp.route("/admin/provider_health", methods=["GET"])
@require_admin
def get_provider_health():
    data = health.all()
    for name in ("openai", "anthropic"):
        data.setdefault(name, "up")
    return jsonify({"providers": data})


@bp.route("/admin/keys", methods=["GET"])
@require_admin
def list_keys():
    conn = get_conn()
    rows = conn.execute(
        "SELECT key_prefix, tenant, allowed_models, rpm_limit, monthly_budget_usd, spent_usd FROM virtual_keys"
    ).fetchall()
    return jsonify({"keys": [dict(r) for r in rows]})
