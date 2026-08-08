import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import testutil  # noqa: E402

from app.core.router import resolve_candidates, resolve_auto
from app.core.difficulty import classify, score_difficulty


class TestAliasResolution(unittest.TestCase):
    def test_fast_alias_resolves_with_fallback_chain(self):
        plan = resolve_candidates("fast")
        self.assertEqual(plan.candidates[0], "gpt-4o-mini")
        self.assertIn("claude-haiku", plan.candidates)

    def test_smart_alias_resolves_with_fallback_chain(self):
        plan = resolve_candidates("smart")
        self.assertEqual(plan.candidates[0], "gpt-4o")
        self.assertIn("claude-sonnet", plan.candidates)

    def test_concrete_model_has_no_fallback_chain(self):
        plan = resolve_candidates("claude-sonnet")
        self.assertEqual(plan.candidates, ["claude-sonnet"])

    def test_unknown_alias_raises(self):
        with self.assertRaises(ValueError):
            resolve_candidates("not-a-real-alias")


class TestAutoRouting(unittest.TestCase):
    def test_easy_prompt_routes_fast(self):
        plan = resolve_auto("hello there")
        self.assertEqual(plan.auto_classified_as, "fast")

    def test_hard_prompt_routes_smart(self):
        plan = resolve_auto("Prove that the square root of 2 is irrational, step by step.")
        self.assertEqual(plan.auto_classified_as, "smart")

    def test_short_but_hard_trap(self):
        # Short prompt, but conceptually hard -- a length baseline would get this wrong.
        self.assertEqual(classify("Why does time dilation occur near a black hole?", threshold=0.5), "smart")

    def test_long_but_trivial_trap(self):
        long_trivial = "List the following items in a table: " + ", ".join(f"item{i}" for i in range(30))
        self.assertEqual(classify(long_trivial, threshold=0.5), "fast")

    def test_difficulty_score_bounded(self):
        for prompt in ["hi", "Prove P != NP", "a" * 5000]:
            score = score_difficulty(prompt)
            self.assertGreaterEqual(score, 0.0)
            self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
