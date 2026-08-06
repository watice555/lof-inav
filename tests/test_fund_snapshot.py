from __future__ import annotations

import sqlite3
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app.build import import_fund_data, upsert_nav_rows
from app.config import FundConfig
from app.db import SCHEMA, get_meta


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
        contexts = self.mocked_sources(con)
        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            secids = import_fund_data(
                con,
                "TEST",
                pending_backtest_from="2026-07-20",
            )

        self.assertEqual(con.execute("select name from funds where code='TEST'").fetchone()[0], "New Fund")
        self.assertEqual(con.execute("select count(*) from navs where fund_code='TEST'").fetchone()[0], 2)
        self.assertEqual(con.execute("select secid from holdings where fund_code='TEST'").fetchone()[0], "0.NEW")
        self.assertEqual(
            con.execute("select announcement_id from fund_announcements where fund_code='TEST'").fetchone()[0],
            "new-id",
        )
        self.assertEqual(secids, {"0.TEST", "0.NEW"})
        self.assertEqual(
            get_meta(con, "pending_report_backtests"),
            {"TEST": "2026-07-20"},
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
