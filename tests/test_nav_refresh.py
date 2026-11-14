from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.backtest_state import load_backtest_dirty, mark_backtest_dirty
from app.build import (
    normalize_nav_rows,
    refresh_incremental_backtests,
    refresh_navs,
    upsert_nav_rows,
)
from app.config import FundConfig
from app.db import SCHEMA, get_meta, set_meta
from app.server import collect_data_alerts


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def nav(date: str, value: float, return_pct: float = 0.0) -> dict:
    return {
        "date": date,
        "nav": value,
        "distribution": 0.0,
        "return_pct": return_pct,
    }


class NavRefreshTests(unittest.TestCase):
    def test_upsert_keeps_history_and_writes_only_changes(self) -> None:
        con = make_connection()
        initial = [nav("2026-07-09", 1.0), nav("2026-07-10", 1.01, 1.0)]
        first = upsert_nav_rows(con, "TEST", initial)
        unchanged_subset = upsert_nav_rows(con, "TEST", [initial[-1]])
        revised = upsert_nav_rows(con, "TEST", [nav("2026-07-10", 1.02, 2.0)])

        self.assertEqual(first["inserted_rows"], 2)
        self.assertEqual(unchanged_subset["changed_rows"], 0)
        self.assertEqual(revised["revised_rows"], 1)
        self.assertEqual(revised["earliest_revised_date"], "2026-07-10")
        self.assertIsNone(revised["earliest_invalidating_date"])
        rows = con.execute(
            "select date, nav from navs where fund_code='TEST' order by date"
        ).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("2026-07-09", 1.0), ("2026-07-10", 1.02)])

    def test_nav_snapshot_validation_is_strict(self) -> None:
        with self.assertRaises(ValueError):
            normalize_nav_rows([nav("2026-07-10", 1), nav("2026-07-09", 1)])
        with self.assertRaises(ValueError):
            normalize_nav_rows([nav("2026-07-10", -1)])

    def test_partial_refresh_does_not_advance_full_success(self) -> None:
        con = make_connection()
        con.execute(
            "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('B', 'B', 0, '其他', 'old')"
        )
        upsert_nav_rows(con, "B", [nav("2026-07-09", 2.0)])
        set_meta(con, "last_navs_refresh_success_at", "2026-07-01T00:00:00+00:00")

        def page_for(code: str):
            if code == "B":
                raise TimeoutError("upstream timeout")
            return {"name": "A", "navs": [nav("2026-07-10", 1.0)]}

        configs = {"A": FundConfig("A", 0), "B": FundConfig("B", 0)}
        with (
            patch.dict("app.build.FUNDS", configs, clear=True),
            patch("app.build.fund_page_data", side_effect=page_for),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch("app.build.refresh_current_valuation_base_prices"),
        ):
            result = refresh_navs(con, ["A", "B"], update_backtests=False)

        self.assertEqual([item["code"] for item in result["checked"]], ["A"])
        self.assertEqual([item["code"] for item in result["failed"]], ["B"])
        self.assertEqual(
            get_meta(con, "last_navs_refresh_success_at"),
            "2026-07-01T00:00:00+00:00",
        )
        self.assertEqual(get_meta(con, "last_navs_refresh_at"), get_meta(con, "last_navs_refresh_completed_at"))
        self.assertIsNotNone(get_meta(con, "last_navs_refresh_partial_at"))
        self.assertEqual(
            con.execute("select max(date) from navs where fund_code='B'").fetchone()[0],
            "2026-07-09",
        )

    def test_complete_refresh_advances_full_success_at_completion(self) -> None:
        con = make_connection()
        incoming = [nav("2026-07-10", 1.0)]
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fund_page_data", return_value={"name": "A", "navs": incoming}),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch("app.build.refresh_current_valuation_base_prices"),
        ):
            refresh_navs(con, ["A"], update_backtests=False)

        self.assertEqual(
            get_meta(con, "last_navs_refresh_success_at"),
            get_meta(con, "last_navs_refresh_completed_at"),
        )

    def test_historical_revision_invalidates_affected_backtests(self) -> None:
        con = make_connection()
        con.execute(
            "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('A', 'A', 0, '其他', 'old')"
        )
        upsert_nav_rows(
            con,
            "A",
            [nav("2026-07-08", 1.0), nav("2026-07-09", 1.01), nav("2026-07-10", 1.02)],
        )
        con.executemany(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', ?, ?, 1, 1, 1, 0, 1, 'ok')
            """,
            [("2026-07-09", "2026-07-08"), ("2026-07-10", "2026-07-09")],
        )
        incoming = [
            nav("2026-07-08", 1.0),
            nav("2026-07-09", 1.011),
            nav("2026-07-10", 1.02),
        ]
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fund_page_data", return_value={"name": "A", "navs": incoming}),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch("app.build.refresh_current_valuation_base_prices"),
        ):
            refresh_navs(con, ["A"], update_backtests=False)

        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-09"})

    def test_revision_dirty_state_is_consumed_by_backtest_refresh(self) -> None:
        con = make_connection()
        con.execute(
            "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('A', 'A', 0, '其他', 'old')"
        )
        upsert_nav_rows(con, "A", [nav("2026-07-09", 1.0), nav("2026-07-10", 1.01)])
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )
        incoming = [nav("2026-07-09", 1.0), nav("2026-07-10", 1.02)]
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fund_page_data", return_value={"name": "A", "navs": incoming}),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch(
                "app.build.refresh_incremental_backtests",
                return_value={"refreshed": [], "failed": []},
            ) as refresh_backtests,
        ):
            refresh_navs(con, ["A"], update_backtests=True)

        refresh_backtests.assert_called_once_with(
            con,
            ["A"],
            progress_callback=None,
        )
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-10"})

    def test_historical_nav_insertion_invalidates_but_latest_append_does_not(self) -> None:
        con = make_connection()
        upsert_nav_rows(
            con,
            "A",
            [nav("2026-07-08", 1.0), nav("2026-07-10", 1.02)],
        )
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-08', 1, 1, 1, 0, 1, 'ok')
            """
        )

        insertion = upsert_nav_rows(con, "A", [nav("2026-07-09", 1.01)])
        append = upsert_nav_rows(con, "A", [nav("2026-07-11", 1.03)])

        self.assertEqual(insertion["historical_inserted_rows"], 1)
        self.assertEqual(insertion["earliest_invalidating_date"], "2026-07-09")
        self.assertIsNone(append["earliest_invalidating_date"])
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-09"})
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)

    def test_standalone_nav_invalidation_remains_caller_rollbackable(self) -> None:
        con = make_connection()
        upsert_nav_rows(
            con,
            "A",
            [nav("2026-07-08", 1.0), nav("2026-07-10", 1.02)],
        )
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-08', 1, 1, 1, 0, 1, 'ok')
            """
        )
        con.commit()

        upsert_nav_rows(con, "A", [nav("2026-07-09", 1.01)])

        self.assertTrue(con.in_transaction)
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-09"})
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)
        con.rollback()

        self.assertEqual(load_backtest_dirty(con), {})
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 1)
        self.assertIsNone(
            con.execute(
                "select nav from navs where fund_code='A' and date='2026-07-09'"
            ).fetchone()
        )

    def test_incremental_refresh_uses_dirty_anchor_before_normal_overlap(self) -> None:
        con = make_connection()
        upsert_nav_rows(
            con,
            "A",
            [nav("2026-07-19", 1.0), nav("2026-07-20", 1.01)],
        )
        mark_backtest_dirty(con, "A", "2026-07-01")
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch(
                "app.build.incremental_backtest_price_targets_for_fund",
                return_value={},
            ),
            patch("app.build.run_backtest_incremental", return_value=[]) as run_incremental,
        ):
            refresh_incremental_backtests(con, ["A"])

        run_incremental.assert_called_once_with(
            con,
            "A",
            start_date="2026-07-01",
        )

    def test_partial_nav_failure_becomes_soft_data_alert(self) -> None:
        con = make_connection()
        funds = [
            {
                "code": "A",
                "name": "A",
                "type": "其他",
                "status": "ok",
                "missing_quotes": [],
                "realtime_warnings": [],
            }
        ]

        alerts = collect_data_alerts(
            con,
            funds,
            include_backtests=False,
            nav_refresh_errors=[{"code": "A", "error": "TimeoutError: timeout"}],
        )

        self.assertEqual(alerts[0]["type"], "nav_refresh_failed")
        self.assertEqual(alerts[0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()
