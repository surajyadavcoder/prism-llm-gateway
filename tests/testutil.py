"""
Shared test setup: points PRISM_DB_PATH at a throwaway file so tests never
touch data/prism.db (the one the running demo server uses), and re-inits
the schema fresh for each test module.
"""
import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="prism_test_")
os.environ["PRISM_DB_PATH"] = os.path.join(_tmp_dir, "test_prism.db")

from app.core.db import init_db, get_conn  # noqa: E402
from app.core.seed import seed_keys  # noqa: E402


def fresh_db():
    init_db(reset=True)
    seed_keys()
    return get_conn()
