from __future__ import annotations

import unittest

from app.server import alert_sort_key


class AlertSortingTests(unittest.TestCase):
    def test_errors_sort_before_warnings(self) -> None:
        alerts = [
            {
                "code": "000001",
                "type": "realtime_missing_quotes",
                "severity": "warning",
                "weight": 1,
                "fund_type": "QDII-港股",
            },
            {
                "code": "000002",
                "type": "valuation_error",
                "severity": "error",
                "weight": 0,
                "fund_type": "其他",
            },
        ]

        ordered = sorted(alerts, key=alert_sort_key)

        self.assertEqual(ordered[0]["type"], "valuation_error")


if __name__ == "__main__":
    unittest.main()
