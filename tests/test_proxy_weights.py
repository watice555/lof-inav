from __future__ import annotations

import unittest

from app.build import MIN_PROXY_WEIGHT, proxy_weight_for_period


class ProxyWeightTests(unittest.TestCase):
    def test_floating_point_gap_residue_is_clamped_to_zero(self) -> None:
        weight = proxy_weight_for_period(
            None,
            "stock_gap",
            ("0.PROXY",),
            {"2026-03-31": {"stock": 0.3}},
            "2026-03-31",
            0.0,
            0.0,
            0.29999999999999993,
        )

        self.assertEqual(weight, 0.0)

    def test_meaningful_small_proxy_weight_is_preserved(self) -> None:
        expected = MIN_PROXY_WEIGHT * 10
        weight = proxy_weight_for_period(
            expected,
            "stock_gap",
            ("0.PROXY",),
            {},
            "2026-03-31",
            0.0,
            0.0,
            0.0,
        )

        self.assertEqual(weight, expected)


if __name__ == "__main__":
    unittest.main()
