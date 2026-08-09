import os
import sys
import unittest
import concurrent.futures

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import testutil  # noqa: E402

from app.core.auth import authenticate, admit_rate_limit, admit_budget, record_actual_spend, \
    GatewayRejection, check_model_allowed

ALPHA_KEY = "sk-prism-team-alpha-0001"
BETA_KEY = "sk-prism-team-beta-0002"
GAMMA_KEY = "sk-prism-team-gamma-0003"


class TestAuth(unittest.TestCase):
    def setUp(self):
        testutil.fresh_db()

    def test_valid_key_authenticates(self):
        rec = authenticate(ALPHA_KEY)
        self.assertEqual(rec.tenant, "team-alpha")

    def test_invalid_key_rejected(self):
        with self.assertRaises(GatewayRejection) as ctx:
            authenticate("totally-fake-key")
        self.assertEqual(ctx.exception.reason, "invalid_key")

    def test_plaintext_key_not_in_db(self):
        # The virtual_keys table must never contain the raw key string.
        from app.core.db import get_conn
        conn = get_conn()
        rows = conn.execute("SELECT * FROM virtual_keys").fetchall()
        for row in rows:
            self.assertNotIn(ALPHA_KEY, str(dict(row)))
            self.assertNotIn(BETA_KEY, str(dict(row)))

    def test_model_allowlist_enforced(self):
        rec = authenticate(BETA_KEY)  # beta cannot use "smart"
        with self.assertRaises(GatewayRejection) as ctx:
            check_model_allowed(rec, "smart")
        self.assertEqual(ctx.exception.reason, "model_not_allowed")

    def test_model_allowlist_allows_permitted_model(self):
        rec = authenticate(BETA_KEY)
        check_model_allowed(rec, "fast")  # should not raise


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        testutil.fresh_db()

    def test_admits_up_to_limit(self):
        rec = authenticate(GAMMA_KEY)  # rpm_limit = 5
        for _ in range(5):
            admit_rate_limit(rec.key_hash, rec.rpm_limit)  # should not raise

    def test_rejects_after_limit(self):
        rec = authenticate(GAMMA_KEY)
        for _ in range(5):
            admit_rate_limit(rec.key_hash, rec.rpm_limit)
        with self.assertRaises(GatewayRejection) as ctx:
            admit_rate_limit(rec.key_hash, rec.rpm_limit)
        self.assertEqual(ctx.exception.reason, "rate_limited")

    def test_no_overadmission_under_concurrency(self):
        """The core correctness property: rpm_limit concurrent callers racing
        for the same window must not all be admitted past the limit.
        """
        rec = authenticate(GAMMA_KEY)  # rpm_limit = 5
        admitted = []
        errors = []

        def try_admit():
            try:
                admit_rate_limit(rec.key_hash, rec.rpm_limit)
                admitted.append(1)
            except GatewayRejection:
                errors.append(1)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            list(ex.map(lambda _: try_admit(), range(20)))

        self.assertEqual(len(admitted), 5, f"expected exactly 5 admitted, got {len(admitted)}")
        self.assertEqual(len(errors), 15)


class TestBudget(unittest.TestCase):
    def setUp(self):
        testutil.fresh_db()

    def test_admits_when_under_budget(self):
        rec = authenticate(ALPHA_KEY)  # $25 budget
        admit_budget(rec)  # should not raise

    def test_rejects_when_budget_exhausted(self):
        rec = authenticate(GAMMA_KEY)  # $0.05 budget
        record_actual_spend(rec.key_hash, 0.06)  # push over budget
        rec2 = authenticate(GAMMA_KEY)
        with self.assertRaises(GatewayRejection) as ctx:
            admit_budget(rec2)
        self.assertEqual(ctx.exception.reason, "budget_exhausted")

    def test_spend_accumulates_correctly_under_concurrency(self):
        rec = authenticate(ALPHA_KEY)

        def spend():
            record_actual_spend(rec.key_hash, 0.01)

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            list(ex.map(lambda _: spend(), range(50)))

        rec2 = authenticate(ALPHA_KEY)
        self.assertAlmostEqual(rec2.spent_usd, 0.5, places=6,
                                msg="lost updates under concurrent spend recording")


if __name__ == "__main__":
    unittest.main()
