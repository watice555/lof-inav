from __future__ import annotations

import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app.backtest_state import (
    invalidate_backtests,
    load_backtest_dirty,
    mark_backtest_dirty,
)
from app.build import (
    earliest_backtest_affected_by_holdings,
    import_fund_data,
    upsert_nav_rows,
)
from app.config import FundConfig
from app.db import SCHEMA


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def nav(date: str, value: float) -> dict:
    return {"date": date, "nav": value, "distribution": 0.0, "return_pct": 0.0}


def config() -> FundConfig:
    return FundConfig(
        code="TEST",
        exchange_market=0,
        manual_holdings_mode="replace",
        manual_holdings=(
            {
                "report_date": "2026-06-30",
                "holdings": [
                    {
                        "secid": "0.NEW",
                        "name": "New",
                        "weight": 0.1,
                        "source": "manual",
                    }
                ],
            },
        ),
    )


def seed_old_snapshot(con: sqlite3.Connection) -> None:
    con.execute(
        "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('TEST', 'Old', 0, '其他', 'old')"
    )
    upsert_nav_rows(con, "TEST", [nav("2026-07-09", 1.0)])
    con.execute(
        """
        insert into holdings
        (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
        values ('TEST', '2026-03-31', '2026-04-22', '0.OLD', 'OLD', 'Old', 0.2, 'old')
        """
    )
    con.execute(
        """
        insert into fund_announcements
        (fund_code, title, publish_date, announcement_id, url, updated_at)
        values ('TEST', 'Old report', '2026-04-22', 'old-id', 'old-url', 'old')
        """
    )
    con.commit()


class FundSnapshotTests(unittest.TestCase):
    def mocked_sources(self, con: sqlite3.Connection):
        def report_dates(_code: str):
            self.assertFalse(con.in_transaction)
            return {"2026-06-30": "2026-07-20"}

        return (
            patch.dict("app.build.FUNDS", {"TEST": config()}, clear=True),
            patch(
                "app.build.fund_page_data",
                return_value={
                    "name": "New Fund",
                    "navs": [nav("2026-07-09", 1.0), nav("2026-07-10", 1.01)],
                    "allocation": {},
                    "stock_codes": [],
                },
            ),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch("app.build.fetch_report_publish_dates", side_effect=report_dates),
            patch(
                "app.build.fetch_latest_regular_report",
                return_value={
                    "title": "New report",
                    "publish_date": "2026-07-20",
                    "announcement_id": "new-id",
                    "url": "new-url",
                },
            ),
        )

    def test_apply_failure_rolls_back_entire_fund_snapshot(self) -> None:
        con = make_connection()
        seed_old_snapshot(con)
        contexts = self.mocked_sources(con)
        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            stack.enter_context(
                patch("app.build.add_single_holding", side_effect=RuntimeError("write failed"))
            )
            with self.assertRaises(RuntimeError):
                import_fund_data(con, "TEST")

        self.assertEqual(con.execute("select name from funds where code='TEST'").fetchone()[0], "Old")
        self.assertEqual(con.execute("select count(*) from navs where fund_code='TEST'").fetchone()[0], 1)
        self.assertEqual(con.execute("select secid from holdings where fund_code='TEST'").fetchone()[0], "0.OLD")
        self.assertEqual(
            con.execute("select announcement_id from fund_announcements where fund_code='TEST'").fetchone()[0],
            "old-id",
        )

    def test_success_replaces_snapshot_after_all_fetches(self) -> None:
        con = make_connection()
        seed_old_snapshot(con)
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )
        con.commit()
        contexts = self.mocked_sources(con)
        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            secids = import_fund_data(con, "TEST")

        self.assertEqual(con.execute("select name from funds where code='TEST'").fetchone()[0], "New Fund")
        self.assertEqual(con.execute("select count(*) from navs where fund_code='TEST'").fetchone()[0], 2)
        self.assertEqual(con.execute("select secid from holdings where fund_code='TEST'").fetchone()[0], "0.NEW")
        self.assertEqual(
            con.execute("select announcement_id from fund_announcements where fund_code='TEST'").fetchone()[0],
            "new-id",
        )
        self.assertEqual(secids, {"0.TEST", "0.NEW"})
        self.assertEqual(load_backtest_dirty(con), {"TEST": "2026-07-10"})
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)

    def test_unchanged_snapshot_preserves_existing_dirty_anchor(self) -> None:
        con = make_connection()
        seed_old_snapshot(con)
        mark_backtest_dirty(con, "TEST", "2026-07-01")
        unchanged_cfg = FundConfig(
            code="TEST",
            exchange_market=0,
            manual_holdings_mode="replace",
            manual_holdings=(
                {
                    "report_date": "2026-03-31",
                    "holdings": [
                        {
                            "secid": "0.OLD",
                            "name": "Old",
                            "weight": 0.2,
                            "source": "old",
                        }
                    ],
                },
            ),
        )
        with (
            patch.dict("app.build.FUNDS", {"TEST": unchanged_cfg}, clear=True),
            patch(
                "app.build.fund_page_data",
                return_value={
                    "name": "Old",
                    "navs": [nav("2026-07-09", 1.0)],
                    "allocation": {},
                    "stock_codes": [],
                },
            ),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch(
                "app.build.fetch_report_publish_dates",
                return_value={"2026-03-31": "2026-04-22"},
            ),
            patch("app.build.fetch_latest_regular_report", return_value=None),
        ):
            import_fund_data(con, "TEST")

        self.assertEqual(load_backtest_dirty(con), {"TEST": "2026-07-01"})

    def test_older_report_backfill_without_selected_holding_change_is_clean(self) -> None:
        current = {
            "2026-06-30": (
                ("2026-06-30", "2026-07-01", "0.CURRENT", "0.2", "test"),
            )
        }
        with_backfill = {
            **current,
            "2025-12-31": (
                ("2025-12-31", "2026-01-20", "0.OLD", "0.1", "test"),
            ),
        }

        self.assertIsNone(
            earliest_backtest_affected_by_holdings(
                current,
                with_backfill,
                [("2026-07-10", "2026-07-09")],
            )
        )

    def test_each_valuation_relevant_holding_field_invalidates(self) -> None:
        baseline = {
            "2026-06-30": (
                ("2026-06-30", "2026-07-01", "0.A", "0.2", "test"),
            )
        }
        variants = {
            "report_date": {
                "2026-06-29": (
                    ("2026-06-29", "2026-07-01", "0.A", "0.2", "test"),
                )
            },
            "publish_date": {
                "2026-06-30": (
                    ("2026-06-30", "2026-07-10", "0.A", "0.2", "test"),
                )
            },
            "secid": {
                "2026-06-30": (
                    ("2026-06-30", "2026-07-01", "0.B", "0.2", "test"),
                )
            },
            "weight": {
                "2026-06-30": (
                    ("2026-06-30", "2026-07-01", "0.A", "0.3", "test"),
                )
            },
            "source": {
                "2026-06-30": (
                    ("2026-06-30", "2026-07-01", "0.A", "0.2", "corrected"),
                )
            },
        }

        for field, changed in variants.items():
            with self.subTest(field=field):
                self.assertEqual(
                    earliest_backtest_affected_by_holdings(
                        baseline,
                        changed,
                        [("2026-07-10", "2026-07-09")],
                    ),
                    "2026-07-10",
                )

    def test_announcement_only_change_does_not_invalidate_backtests(self) -> None:
        con = make_connection()
        seed_old_snapshot(con)
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )
        unchanged_cfg = FundConfig(
            code="TEST",
            exchange_market=0,
            manual_holdings_mode="replace",
            manual_holdings=(
                {
                    "report_date": "2026-03-31",
                    "holdings": [
                        {
                            "secid": "0.OLD",
                            "name": "Old",
                            "weight": 0.2,
                            "source": "old",
                        }
                    ],
                },
            ),
        )
        incoming_report = {
            "title": "Test基金2026年第1季度报告（更正）",
            "publish_date": "2026-04-22",
            "announcement_id": "corrected-id",
            "url": "corrected-url",
            "report_date": "2026-03-31",
        }
        with (
            patch.dict("app.build.FUNDS", {"TEST": unchanged_cfg}, clear=True),
            patch(
                "app.build.fund_page_data",
                return_value={
                    "name": "Old",
                    "navs": [nav("2026-07-09", 1.0)],
                    "allocation": {},
                    "stock_codes": [],
                },
            ),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch(
                "app.build.fetch_report_publish_dates",
                return_value={"2026-03-31": "2026-04-22"},
            ),
        ):
            import_fund_data(con, "TEST", latest_report=incoming_report)

        self.assertEqual(load_backtest_dirty(con), {})
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 1)
        self.assertEqual(
            con.execute(
                "select announcement_id from fund_announcements where fund_code='TEST'"
            ).fetchone()[0],
            "corrected-id",
        )

    def test_failure_after_invalidation_rolls_back_snapshot_rows_and_dirty(self) -> None:
        con = make_connection()
        seed_old_snapshot(con)
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('TEST', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )
        con.commit()

        def invalidate_then_fail(connection, code, from_date):
            invalidate_backtests(connection, code, from_date)
            raise RuntimeError("simulated post-invalidation failure")

        contexts = self.mocked_sources(con)
        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            stack.enter_context(
                patch(
                    "app.build.invalidate_backtests",
                    side_effect=invalidate_then_fail,
                )
            )
            with self.assertRaisesRegex(RuntimeError, "post-invalidation"):
                import_fund_data(con, "TEST")

        self.assertEqual(load_backtest_dirty(con), {})
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 1)
        self.assertEqual(
            con.execute("select secid from holdings where fund_code='TEST'").fetchone()[0],
            "0.OLD",
        )

    def test_new_announcement_waits_for_matching_holding_period(self) -> None:
        con = make_connection()
        seed_old_snapshot(con)
        incoming_report = {
            "title": "Test基金2026年第2季度报告",
            "publish_date": "2026-07-20",
            "announcement_id": "new-id",
            "url": "new-url",
            "report_date": "2026-06-30",
        }
        old_period = (
            "2026-03-31",
            [
                {
                    "secid": "0.OLD",
                    "symbol": "OLD",
                    "name": "Old",
                    "weight": 0.2,
                    "source": "disclosed_stock",
                }
            ],
        )
        with (
            patch.dict(
                "app.build.FUNDS",
                {"TEST": FundConfig("TEST", 0, manual_holdings_mode="overlay")},
                clear=True,
            ),
            patch(
                "app.build.fund_page_data",
                return_value={
                    "name": "New Fund",
                    "navs": [nav("2026-07-10", 1.01)],
                    "allocation": {},
                    "stock_codes": ["0.OLD"],
                },
            ),
            patch("app.build.parse_navs", side_effect=lambda rows: rows),
            patch(
                "app.build.fetch_report_publish_dates",
                return_value={"2026-06-30": "2026-07-20"},
            ),
            patch("app.build.fetch_holdings", return_value=[old_period]),
        ):
            with self.assertRaisesRegex(ValueError, "2026-06-30"):
                import_fund_data(con, "TEST", latest_report=incoming_report)

        self.assertEqual(
            con.execute(
                "select announcement_id from fund_announcements where fund_code='TEST'"
            ).fetchone()[0],
            "old-id",
        )
        self.assertEqual(
            con.execute("select secid from holdings where fund_code='TEST'").fetchone()[0],
            "0.OLD",
        )


if __name__ == "__main__":
    unittest.main()
