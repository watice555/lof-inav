from __future__ import annotations

import inspect
import sqlite3
import unittest
from unittest.mock import patch

from app.build import (
    incremental_backtest_cutoff_for_fund,
    incremental_backtest_price_targets_for_fund,
)
from app.config import FundConfig
from app.db import BACKTEST_CACHE_VERSION, SCHEMA, ensure_cache_versions, get_meta, set_meta
from app.valuation import (
    DEFAULT_BACKTEST_DAYS,
    _backtest_price_series,
    backtest_summary,
    calculate_backtest_row,
    holdings_available_on,
    run_backtest,
    run_backtest_incremental,
    save_backtest_row,
)


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def seed_fund(con: sqlite3.Connection) -> None:
    con.execute(
        "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('TEST', 'Test', 0, '其他', 'now')"
    )
    con.execute(
        """
        insert into holdings
        (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
        values ('TEST', '2026-06-30', '2026-07-01', '0.ASSET', 'ASSET', 'Asset', 1.0, 'test')
        """
    )
    con.executemany(
        "insert into navs(fund_code, date, nav, distribution) values ('TEST', ?, ?, 0)",
        [
            ("2026-07-08", 1.00),
            ("2026-07-09", 1.01),
            ("2026-07-10", 1.02),
        ],
    )


class BacktestCacheTests(unittest.TestCase):
    def test_full_backtest_default_is_seven_rows(self) -> None:
        self.assertEqual(DEFAULT_BACKTEST_DAYS, 7)
        self.assertEqual(
            inspect.signature(run_backtest).parameters["days"].default,
            DEFAULT_BACKTEST_DAYS,
        )

    def test_first_incremental_backtest_uses_seven_calendar_day_window(self) -> None:
        con = make_connection()
        con.executemany(
            "insert into navs(fund_code, date, nav, distribution) values ('TEST', ?, 1, 0)",
            [("2026-07-01",), ("2026-07-29",), ("2026-08-05",)],
        )

        self.assertEqual(
            incremental_backtest_cutoff_for_fund(con, "TEST"),
            "2026-07-29",
        )

    def test_holdings_without_publish_date_are_never_used_by_backtests(self) -> None:
        con = make_connection()
        con.execute(
            """
            insert into holdings
            (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
            values ('TEST', '2026-03-31', null, '0.ASSET', 'ASSET', 'Asset', 1.0, 'test')
            """
        )

        self.assertEqual(
            holdings_available_on(con, "TEST", "2026-07-10", lag_days=1),
            [],
        )

    def test_backtests_use_latest_fully_published_report_only(self) -> None:
        con = make_connection()
        con.executemany(
            """
            insert into holdings
            (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
            values ('TEST', ?, ?, ?, ?, ?, ?, 'test')
            """,
            [
                ("2026-03-31", "2026-04-20", "0.OLD", "OLD", "Old", 1.0),
                ("2026-06-30", "2026-07-20", "0.NEW", "NEW", "New", 0.6),
                ("2026-06-30", None, "0.UNPUBLISHED", "UNPUBLISHED", "Unpublished", 0.4),
            ],
        )

        holdings = holdings_available_on(con, "TEST", "2026-07-25")

        self.assertEqual([holding["secid"] for holding in holdings], ["0.OLD"])

    def test_persisted_backtest_series_ignores_realtime_quote_marks(self) -> None:
        con = make_connection()
        con.executemany(
            "insert into daily_prices(secid, date, close, pct) values ('124.HSTECH', ?, ?, null)",
            [("2026-07-09", 4700.0), ("2026-07-10", 4721.66)],
        )
        con.execute(
            """
            insert into quotes
            (secid, symbol, market, name, price, pct, previous_close, quote_time, updated_at)
            values ('124.HSTECH', 'HSTECH', 124, 'proxy', 4.634, 0, 4.64,
                    '2026-07-10T15:00:00+08:00', 'now')
            """
        )

        self.assertEqual(
            _backtest_price_series(con, "124.HSTECH"),
            [("2026-07-09", 4700.0), ("2026-07-10", 4721.66)],
        )

    def test_incremental_backtest_replaces_existing_overlap(self) -> None:
        con = make_connection()
        seed_fund(con)
        con.executemany(
            "insert into daily_prices(secid, date, close, pct) values ('0.ASSET', ?, ?, null)",
            [("2026-07-08", 100), ("2026-07-09", 101), ("2026-07-10", 102)],
        )
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-10', '2026-07-09', 1.01, 1.02, 999, 978, 1, 'ok')
            """
        )

        rows = run_backtest_incremental(con, "TEST", start_date="2026-07-10")

        self.assertEqual(len(rows), 1)
        stored = con.execute(
            "select * from backtests where fund_code='TEST' and date='2026-07-10'"
        ).fetchone()
        self.assertAlmostEqual(stored["estimated_nav"], 1.02)
        self.assertAlmostEqual(stored["error_pct"], 0.0)

    def test_price_targets_include_existing_overlap(self) -> None:
        con = make_connection()
        seed_fund(con)
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-10', '2026-07-09', 1.01, 1.02, 1.02, 0, 1, 'ok')
            """
        )

        with patch.dict("app.build.FUNDS", {"TEST": FundConfig("TEST", 0)}):
            targets = incremental_backtest_price_targets_for_fund(
                con, "TEST", start_date="2026-07-09"
            )

        self.assertEqual(targets["0.ASSET"], {"2026-07-08", "2026-07-09", "2026-07-10"})
        self.assertEqual(targets["0.TEST"], {"2026-07-09", "2026-07-10"})

    def test_incremental_window_replacement_rolls_back_as_a_unit(self) -> None:
        con = make_connection()
        seed_fund(con)
        con.executemany(
            "insert into daily_prices(secid, date, close, pct) values ('0.ASSET', ?, ?, null)",
            [("2026-07-08", 100), ("2026-07-09", 101), ("2026-07-10", 102)],
        )
        for date, previous_date, previous_nav, actual_nav in (
            ("2026-07-09", "2026-07-08", 1.00, 1.01),
            ("2026-07-10", "2026-07-09", 1.01, 1.02),
        ):
            con.execute(
                """
                insert into backtests
                (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
                 error_pct, covered_weight, data_quality)
                values ('TEST', ?, ?, ?, ?, 777, 700, 1, 'ok')
                """,
                (date, previous_date, previous_nav, actual_nav),
            )
        calls = 0

        def fail_on_second_write(connection, row):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("simulated write failure")
            save_backtest_row(connection, row)

        with patch("app.valuation.save_backtest_row", side_effect=fail_on_second_write):
            with self.assertRaises(sqlite3.OperationalError):
                run_backtest_incremental(con, "TEST", start_date="2026-07-09")

        estimates = [
            row[0]
            for row in con.execute(
                "select estimated_nav from backtests where fund_code='TEST' order by date"
            )
        ]
        self.assertEqual(estimates, [777, 777])

    def test_full_backtest_calculation_failure_preserves_previous_cache(self) -> None:
        con = make_connection()
        seed_fund(con)
        con.executemany(
            "insert into daily_prices(secid, date, close, pct) values ('0.ASSET', ?, ?, null)",
            [("2026-07-08", 100), ("2026-07-09", 101), ("2026-07-10", 102)],
        )
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-10', '2026-07-09', 1.01, 1.02, 777, 700, 1, 'ok')
            """
        )

        with patch("app.valuation.calculate_backtest_row", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                run_backtest(con, "TEST")

        stored = con.execute(
            "select estimated_nav from backtests where fund_code='TEST' and date='2026-07-10'"
        ).fetchone()
        self.assertEqual(stored["estimated_nav"], 777)

    def test_large_error_is_excluded_from_summary(self) -> None:
        con = make_connection()
        seed_fund(con)
        con.executemany(
            "insert into daily_prices(secid, date, close, pct) values ('0.ASSET', ?, ?, null)",
            [("2026-07-08", 100), ("2026-07-09", 150)],
        )
        navs = con.execute(
            "select * from navs where fund_code='TEST' and date <= '2026-07-09' order by date"
        ).fetchall()
        row = calculate_backtest_row(
            con,
            "TEST",
            navs[0],
            navs[1],
            {"0.ASSET": [("2026-07-08", 100), ("2026-07-09", 150)]},
            25,
        )
        self.assertEqual(row["data_quality"], "outlier")
        save_backtest_row(con, row)
        summary = backtest_summary(con, "TEST")
        self.assertEqual(summary["quality_sample_count"], 0)
        self.assertEqual(summary["outlier_count"], 1)
        self.assertIsNone(summary["mae_pct"])

    def test_cache_version_invalidates_old_derived_rows(self) -> None:
        con = make_connection()
        seed_fund(con)
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-09', '2026-07-08', 1, 1.01, 999, 988, 1, 'ok')
            """
        )
        set_meta(con, "backtest_cache_version", 1)

        ensure_cache_versions(con)

        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)
        self.assertEqual(get_meta(con, "backtest_cache_version"), BACKTEST_CACHE_VERSION)


if __name__ == "__main__":
    unittest.main()
