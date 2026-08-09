import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import testutil  # noqa: E402

from app.core.cache import lookup, store


class TestSemanticCache(unittest.TestCase):
    def setUp(self):
        testutil.fresh_db()

    def _msgs(self, text):
        return [{"role": "user", "content": text}]

    def test_miss_on_empty_cache(self):
        result = lookup("team-alpha", self._msgs("What is the capital of France?"))
        self.assertIsNone(result)

    def test_exact_match_hits(self):
        store("team-alpha", self._msgs("What is the capital of France?"), "Paris.", "gpt-4o-mini", "openai", 10, 5)
        result = lookup("team-alpha", self._msgs("What is the capital of France?"))
        self.assertIsNotNone(result)
        self.assertEqual(result["response_text"], "Paris.")

    def test_paraphrase_hits(self):
        store("team-alpha", self._msgs("What is the capital of France?"), "Paris.", "gpt-4o-mini", "openai", 10, 5)
        result = lookup("team-alpha", self._msgs("Tell me France's capital city"))
        self.assertIsNotNone(result, "paraphrased query should hit the semantic cache")

    def test_unrelated_prompt_misses(self):
        store("team-alpha", self._msgs("What is the capital of France?"), "Paris.", "gpt-4o-mini", "openai", 10, 5)
        result = lookup("team-alpha", self._msgs("Write a haiku about the ocean at sunset"))
        self.assertIsNone(result, "unrelated prompt must not hit the cache (no false positive)")

    def test_tenant_isolation(self):
        store("team-alpha", self._msgs("What is our refund policy?"), "30 days.", "gpt-4o-mini", "openai", 10, 5)
        result = lookup("team-beta", self._msgs("What is our refund policy?"))
        self.assertIsNone(result, "one tenant must never read another tenant's cached response")

    def test_tenant_scoped_hit_still_works(self):
        store("team-alpha", self._msgs("What is our refund policy?"), "30 days.", "gpt-4o-mini", "openai", 10, 5)
        result = lookup("team-alpha", self._msgs("What is our refund policy?"))
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
