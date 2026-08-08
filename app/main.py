import os
from flask import Flask, send_from_directory, redirect

from app.core.db import init_db
from app.core.seed import seed_keys
from app.routers import chat, usage, admin

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

app = Flask(__name__)

with app.app_context():
    init_db(reset=False)
    seed_keys()

app.register_blueprint(chat.bp)
app.register_blueprint(usage.bp)
app.register_blueprint(admin.bp)


@app.after_request
def add_standard_headers(response):
    # Minimal CORS support without pulling in flask-cors as a dependency --
    # fine for an API gateway meant to be called from server-side clients
    # and the bundled ops console. Lock PRISM_ALLOWED_ORIGINS down in
    # production instead of leaving it at the wildcard default.
    allowed_origins = os.environ.get("PRISM_ALLOWED_ORIGINS", "*")
    response.headers.setdefault("Access-Control-Allow-Origin", allowed_origins)
    response.headers.setdefault("Access-Control-Allow-Headers", "Authorization, Content-Type")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.route("/")
def root():
    return redirect("/console/")


@app.route("/console/")
def console_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/console/<path:filename>")
def console_static(filename):
    return send_from_directory(STATIC_DIR, filename)


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe. Actually checks the DB connection, not just
    that the process is alive -- a gateway that's up but can't reach its
    own database is not healthy, and a load balancer should know that.
    """
    from app.core.db import get_conn
    try:
        get_conn().execute("SELECT 1").fetchone()
        db_ok = True
    except Exception as e:
        db_ok = False
    status = "ok" if db_ok else "degraded"
    code = 200 if db_ok else 503
    return {"status": status, "db": "ok" if db_ok else "unreachable"}, code


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    # threaded=True is what gives us real OS-thread concurrency for the
    # rate-limit/budget race tests and the load test script.
    app.run(host="0.0.0.0", port=port, threaded=True)
