from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from app.build import expand_manual_holding, holding_report_years


class HoldingYearTests(unittest.TestCase):
    def test_default_report_window_rolls_forward_with_calendar_year(self) -> None:
        self.assertEqual(
            holding_report_years(as_of=date(2027, 1, 2)),
            [2027, 2026, 2025, 2024],
        )
        self.assertEqual(
            holding_report_years(as_of=date(2027, 1, 2), count=2),
            [2027, 2026],
        )

    def test_lookthrough_fund_uses_current_two_year_window(self) -> None:
        holding = {
            "report_date": "2027-03-31",
            "publish_date": "2027-04-22",
            "secid": "1.520560",
            "symbol": "520560",
            "name": "Lookthrough",
            "weight": 0.1,
            "source": "lookthrough_fund",
        }
        with (
            patch("app.build.app_today", return_value=date(2027, 5, 1)),
            patch("app.build.fund_page_data", return_value={"stock_codes": []}),
            patch("app.build.fetch_holdings", return_value=[]) as fetch,
        ):
            self.assertEqual(expand_manual_holding(holding), [holding])

        self.assertEqual(fetch.call_args.kwargs["years"], [2027, 2026])


if __name__ == "__main__":
    unittest.main()
