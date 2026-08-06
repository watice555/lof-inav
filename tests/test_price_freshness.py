from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.build import (
    current_valuation_base_price_targets,
    current_realtime_secids,
    full_backtest_price_targets_for_fund,
    price_is_fresh_for_date,
    refresh_daily_prices_for_targets,
)
from app.config import FundConfig
from app.db import SCHEMA, get_meta
from app.valuation import _fresh_price_item_on_or_before


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


class PriceFreshnessTests(unittest.TestCase):
    def test_realtime_build_scope_excludes_old_historical_holdings(self) -> None:
        con = make_connection()
        con.executemany(
            """
            insert into holdings
            (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
            values ('TEST', ?, ?, ?, ?, ?, 0.1, 'test')
            """,
            [
                ("2025-12-31", "2026-01-20", "105.OLD", "OLD", "Old"),
                ("2026-03-31", "2026-04-22", "105.NVDA", "NVDA", "Nvidia"),
            ],
        )

        with patch.dict("app.build.FUNDS", {"TEST": FundConfig("TEST", 0)}, clear=True):
            secids = current_realtime_secids(con)

        self.assertEqual(secids, ["0.TEST", "105.NVDA", "120.USDCNYC"])

    def test_full_backtest_targets_cover_the_entire_requested_window(self) -> None:
        con = make_connection()
        con.executemany(
            "insert into navs(fund_code, date, nav, distribution) values ('TEST', ?, 1, 0)",
            [("2026-07-08",), ("2026-07-09",), ("2026-07-10",)],
        )
        con.execute(
            """
            insert into holdings
            (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
            values ('TEST', '2026-03-31', '2026-04-22',
                    '0.ASSET', 'ASSET', 'Asset', 0.1, 'test')
            """
        )

        with patch.dict("app.build.FUNDS", {"TEST": FundConfig("TEST", 0)}, clear=True):
            targets = full_backtest_price_targets_for_fund(con, "TEST", days=2)

        self.assertEqual(
            targets,
            {
                "0.ASSET": {"2026-07-08", "2026-07-09", "2026-07-10"},
                "0.TEST": {"2026-07-09", "2026-07-10"},
            },
        )

    def test_normal_us_session_gap_is_not_fresh_even_with_later_data(self) -> None:
        con = make_connection()
        con.executemany(
            "insert into daily_prices(secid, date, close, pct) values ('107.QQQ', ?, ?, null)",
            [("2026-07-07", 610), ("2026-07-09", 615)],
        )

        self.assertFalse(
            price_is_fresh_for_date(
                con,
                "daily_prices",
                "secid = ?",
                ("107.QQQ",),
                "2026-07-08",
                secid="107.QQQ",
            )
        )
        self.assertIsNone(
            _fresh_price_item_on_or_before(
                [("2026-07-07", 610), ("2026-07-09", 615)],
                "2026-07-08",
                "107.QQQ",
            )
        )

    def test_known_us_holiday_accepts_previous_session(self) -> None:
        con = make_connection()
        con.execute(
            "insert into daily_prices(secid, date, close, pct) values ('107.QQQ', '2026-07-02', 610, null)"
        )

        self.assertTrue(
            price_is_fresh_for_date(
                con,
                "daily_prices",
                "secid = ?",
                ("107.QQQ",),
                "2026-07-03",
                secid="107.QQQ",
            )
        )

    def test_targeted_refresh_does_not_request_known_us_holiday(self) -> None:
        con = make_connection()
        con.execute(
            "insert into daily_prices(secid, date, close, pct) values ('107.QQQ', '2026-07-02', 610, null)"
        )

        with patch("app.build.fetch_daily_prices") as fetch:
            result = refresh_daily_prices_for_targets(
                con,
                {"107.QQQ": {"2026-07-03"}},
                commit_every=0,
            )

        fetch.assert_not_called()
        self.assertEqual(
            result,
            {"requested": 0, "saved": 0, "unresolved": [], "saved_rows": 0},
        )
        self.assertEqual(get_meta(con, "last_daily_prices_targeted_unresolved"), [])

    def test_targeted_refresh_fetches_the_previous_session_for_a_holiday(self) -> None:
        con = make_connection()

        with patch(
            "app.build.fetch_daily_prices",
            return_value=[
                {
                    "date": "2026-07-02",
                    "close": 610,
                    "pct": 0.5,
                    "source": "test",
                    "adjustment": "raw",
                }
            ],
        ) as fetch:
            result = refresh_daily_prices_for_targets(
                con,
                {"107.QQQ": {"2026-07-03"}},
                commit_every=0,
            )

        fetch.assert_called_once_with("107.QQQ", begin="20260619", end="20260703")
        self.assertEqual(
            result,
            {"requested": 1, "saved": 1, "unresolved": [], "saved_rows": 1},
        )
        stored = con.execute(
            "select date, close from daily_prices where secid = '107.QQQ'"
        ).fetchone()
        self.assertEqual(tuple(stored), ("2026-07-02", 610.0))

    def test_targeted_refresh_exposes_normal_session_gap_that_remains_missing(self) -> None:
        con = make_connection()
        con.execute(
            "insert into daily_prices(secid, date, close, pct) values ('107.QQQ', '2026-07-07', 610, null)"
        )
        progress: list[dict] = []

        with patch(
            "app.build.fetch_daily_prices",
            return_value=[{"date": "2026-07-09", "close": 615, "pct": 0.5}],
        ) as fetch:
            result = refresh_daily_prices_for_targets(
                con,
                {"107.QQQ": {"2026-07-08"}},
                commit_every=0,
                progress_callback=progress.append,
            )

        fetch.assert_called_once_with("107.QQQ", begin="20260624", end="20260708")
        unresolved = [{"secid": "107.QQQ", "date": "2026-07-08"}]
        self.assertEqual(
            result,
            {"requested": 1, "saved": 0, "unresolved": unresolved, "saved_rows": 1},
        )
        self.assertEqual(
            get_meta(con, "last_daily_prices_targeted_unresolved"),
            unresolved,
        )
        self.assertIn("unresolved=1", progress[-1]["message"])

    def test_domestic_futures_use_cn_weekend_calendar(self) -> None:
        con = make_connection()
        con.execute(
            "insert into daily_prices(secid, date, close, pct) values ('113.agm', '2026-07-10', 14656, null)"
        )

        self.assertTrue(
            price_is_fresh_for_date(
                con,
                "daily_prices",
                "secid = ?",
                ("113.agm",),
                "2026-07-12",
                secid="113.agm",
            )
        )

    def test_current_base_targets_only_require_latest_nav_date(self) -> None:
        con = make_connection()
        con.execute(
            "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('TEST', 'Test', 0, '其他', 'now')"
        )
        con.execute(
            "insert into navs(fund_code, date, nav, distribution) values ('TEST', '2026-07-10', 1, 0)"
        )
        con.execute(
            """
            insert into holdings
            (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
            values ('TEST', '2026-06-30', '2026-07-01', ?, ?, ?, ?, 'test')
            """,
            ("105.NVDA", "NVDA", "Nvidia", 0.2),
        )

        with patch.dict("app.build.FUNDS", {"TEST": FundConfig("TEST", 0)}):
            targets = current_valuation_base_price_targets(con, ["TEST"])

        self.assertEqual(
            targets,
            {
                "105.NVDA": {"2026-07-10"},
                "120.USDCNYC": {"2026-07-10"},
            },
        )


if __name__ == "__main__":
    unittest.main()
