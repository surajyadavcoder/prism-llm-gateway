import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests import testutil  # noqa: E402, sets PRISM_DB_PATH before any app import touches sqlite

from app.core.cost import compute_cost_usd, provider_for


class TestCost(unittest.TestCase):
    def test_known_model_pricing(self):
        # gpt-4o-mini: input 0.00015/1k, output 0.0006/1k
        cost = compute_cost_usd("gpt-4o-mini", input_tokens=1000, output_tokens=1000)
        self.assertAlmostEqual(cost, 0.00015 + 0.0006, places=8)

    def test_zero_tokens_zero_cost(self):
        cost = compute_cost_usd("gpt-4o-mini", input_tokens=0, output_tokens=0)
        self.assertEqual(cost, 0.0)

    def test_scales_linearly_with_tokens(self):
        c1 = compute_cost_usd("gpt-4o", input_tokens=500, output_tokens=0)
        c2 = compute_cost_usd("gpt-4o", input_tokens=1000, output_tokens=0)
        self.assertAlmostEqual(c2, c1 * 2, places=8)

    def test_unknown_model_raises(self):
        with self.assertRaises(ValueError):
            compute_cost_usd("not-a-real-model", 10, 10)

    def test_provider_for_known_models(self):
        self.assertEqual(provider_for("gpt-4o-mini"), "openai")
        self.assertEqual(provider_for("claude-sonnet"), "anthropic")

    def test_provider_for_unknown_model_raises(self):
        with self.assertRaises(ValueError):
            provider_for("nonexistent-model")


if __name__ == "__main__":
    unittest.main()
