"""
Admin API key -- required for every /admin/* endpoint.

Separate from tenant virtual keys (app/core/auth.py): admins manage the
gateway itself (provider health, key inventory), tenants only ever see
/v1/*. Mixing the two auth systems would let a tenant's leaked virtual key
be used to take providers down for everyone -- a real product would never
allow that, so this is deliberately a distinct credential.

Reads ADMIN_API_KEY from the environment. If unset, a random key is
generated at startup and printed once to stdout/logs -- the gateway never
silently runs with open admin access.
"""
import os
import secrets
import functools

from flask import request, jsonify

from app.core.observability import log_event

_ADMIN_KEY = os.environ.get("ADMIN_API_KEY")
if not _ADMIN_KEY:
    _ADMIN_KEY = secrets.token_urlsafe(24)
    print(f"[prism] ADMIN_API_KEY not set -- generated a random one for this "
          f"process only: {_ADMIN_KEY}\n"
          f"[prism] Set ADMIN_API_KEY in your environment for a stable key "
          f"across restarts.", flush=True)


def require_admin(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        provided = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else None
        if not provided or not secrets.compare_digest(provided, _ADMIN_KEY):
            log_event("warning", "admin_auth_failed", path=request.path)
            return jsonify({"error": {"type": "unauthorized",
                             "message": "Admin endpoints require 'Authorization: Bearer <ADMIN_API_KEY>'."}}), 401
        return view(*args, **kwargs)
    return wrapped
