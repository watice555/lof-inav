from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.build import refresh_reports
from app.config import FundConfig
from app.db import SCHEMA, get_meta, set_meta
from app.sources import regular_report_date
from app.server import collect_data_alerts


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def report(announcement_id: str) -> dict[str, str]:
    return {
        "title": "A基金2026年第2季度报告",
        "publish_date": "2026-07-20",
        "announcement_id": announcement_id,
        "url": f"https://example.invalid/{announcement_id}",
    }


def seed_announcement(con: sqlite3.Connection, code: str, announcement_id: str) -> None:
    con.execute(
        """
        insert into fund_announcements
        (fund_code, title, publish_date, announcement_id, url, updated_at)
        values (?, ?, '2026-07-20', ?, 'old-url', 'old')
        """,
        (code, report(announcement_id)["title"], announcement_id),
    )
    con.execute(
        """
        insert into holdings
        (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
        values (?, '2026-06-30', '2026-07-20', '0.A', 'A', 'A', 0.1, 'test')
        """,
        (code,),
    )
    con.commit()


class ReportRefreshTests(unittest.TestCase):
    def test_regular_report_titles_map_to_snapshot_dates(self) -> None:
        self.assertEqual(regular_report_date("基金2026年第2季度报告"), "2026-06-30")
        self.assertEqual(regular_report_date("基金2026年度第1季度报告"), "2026-03-31")
        self.assertEqual(regular_report_date("基金二0二六年第1季度报告"), "2026-03-31")
        self.assertEqual(regular_report_date("基金二〇二六年第二季度报告"), "2026-06-30")
        self.assertEqual(regular_report_date("基金2025年半年度报告"), "2025-06-30")
        self.assertEqual(regular_report_date("基金2025年年度报告"), "2025-12-31")

    def test_unchanged_report_only_advances_check_metadata(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "same-id")
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report", return_value=report("same-id")),
            patch("app.build.import_fund_data") as import_fund,
        ):
            result = refresh_reports(con)

        import_fund.assert_not_called()
        self.assertEqual(result["checked"], ["A"])
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["failed"], [])
        self.assertEqual(
            get_meta(con, "last_reports_refresh_success_at"),
            get_meta(con, "last_reports_refresh_completed_at"),
        )
        stats = get_meta(con, "last_reports_refresh_stats")
        self.assertEqual(stats["target_count"], 1)
        self.assertEqual(stats["checked_count"], 1)
        self.assertEqual(stats["changed_count"], 0)
        self.assertEqual(stats["refreshed_count"], 0)
        self.assertEqual(stats["failed_count"], 0)
        self.assertEqual(stats["pending_count"], 0)

    def test_changed_report_replaces_fund_and_rebuilds_its_backtests(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "old-id")
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )
        con.commit()
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report", return_value=report("new-id")),
            patch("app.build.import_fund_data", return_value={"0.A"}) as import_fund,
            patch(
                "app.build.refresh_incremental_backtests",
                return_value={"refreshed": [{"code": "A", "rows": 2}], "failed": []},
            ) as refresh_backtests,
        ):
            result = refresh_reports(con)

        import_fund.assert_called_once_with(
            con,
            "A",
            latest_report={
                **report("new-id"),
                "report_date": "2026-06-30",
            },
            pending_backtest_from="2026-07-20",
        )
        refresh_backtests.assert_called_once()
        self.assertEqual(refresh_backtests.call_args.args[:2], (con, ["A"]))
        self.assertEqual(
            refresh_backtests.call_args.kwargs["recompute_from_by_code"],
            {"A": "2026-07-20"},
        )
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 1)
        self.assertEqual(result["changed"], ["A"])
        self.assertEqual(result["refreshed"], ["A"])
        self.assertEqual(result["backtests_refreshed"], [{"code": "A", "rows": 2}])

    def test_unchanged_announcement_repairs_missing_holding_publish_dates(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "same-id")
        con.execute("update holdings set publish_date = null where fund_code = 'A'")
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report", return_value=report("same-id")),
            patch("app.build.import_fund_data", return_value={"0.A"}) as import_fund,
            patch(
                "app.build.refresh_incremental_backtests",
                return_value={"refreshed": [], "failed": []},
            ),
        ):
            result = refresh_reports(con)

        import_fund.assert_called_once()
        self.assertEqual(result["changed"], ["A"])
        self.assertEqual(result["refreshed"], ["A"])

    def test_scan_failure_preserves_old_snapshot_and_success_timestamp(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "old-id")
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-09', 1, 1, 7, 6, 1, 'ok')
            """
        )
        set_meta(con, "last_reports_refresh_success_at", "2026-07-01T00:00:00+00:00")
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch(
                "app.build.fetch_latest_regular_report",
                side_effect=TimeoutError("upstream timeout"),
            ),
            patch("app.build.import_fund_data") as import_fund,
        ):
            result = refresh_reports(con)

        import_fund.assert_not_called()
        self.assertEqual(result["failed"][0]["stage"], "scan")
        self.assertEqual(
            get_meta(con, "last_reports_refresh_success_at"),
            "2026-07-01T00:00:00+00:00",
        )
        self.assertEqual(
            con.execute(
                "select announcement_id from fund_announcements where fund_code='A'"
            ).fetchone()[0],
            "old-id",
        )

    def test_backtest_failure_is_reported_without_counting_as_import_failure(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "old-id")
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-09', 1, 1, 7, 6, 1, 'ok')
            """
        )
        set_meta(con, "last_reports_refresh_success_at", "2026-07-01T00:00:00+00:00")
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report", return_value=report("new-id")),
            patch("app.build.import_fund_data", return_value={"0.A"}),
            patch(
                "app.build.refresh_incremental_backtests",
                side_effect=RuntimeError("price refresh failed"),
            ),
        ):
            result = refresh_reports(con)

        self.assertEqual(result["failed"], [])
        self.assertEqual(result["backtests_failed"][0]["stage"], "backtest")
        self.assertEqual(get_meta(con, "last_reports_refresh_stats")["failed_count"], 1)
        self.assertEqual(
            get_meta(con, "last_reports_refresh_success_at"),
            "2026-07-01T00:00:00+00:00",
        )
        self.assertIsNotNone(get_meta(con, "last_reports_refresh_partial_at"))
        self.assertEqual(get_meta(con, "pending_report_backtests"), {"A": "2026-07-20"})
        self.assertEqual(
            con.execute("select estimated_nav from backtests where fund_code='A'").fetchone()[0],
            7,
        )

    def test_pending_backtest_retries_when_announcement_is_unchanged(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "same-id")
        set_meta(con, "pending_report_backtests", {"A": "2026-07-20"})
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report", return_value=report("same-id")),
            patch("app.build.import_fund_data") as import_fund,
            patch(
                "app.build.refresh_incremental_backtests",
                return_value={"refreshed": [], "failed": []},
            ) as refresh_backtests,
        ):
            result = refresh_reports(con)

        import_fund.assert_not_called()
        refresh_backtests.assert_called_once()
        self.assertEqual(result["changed"], [])
        self.assertEqual(get_meta(con, "pending_report_backtests"), {})

    def test_empty_code_list_does_not_expand_to_all_funds(self) -> None:
        con = make_connection()
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report") as fetch_report,
        ):
            result = refresh_reports(con, [])

        fetch_report.assert_not_called()
        self.assertEqual(result["checked"], [])
        self.assertEqual(result["failed_codes"], [])

    def test_disabled_backtests_refresh_current_valuation_prices(self) -> None:
        con = make_connection()
        seed_announcement(con, "A", "old-id")
        set_meta(con, "backtests_disabled", True)
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.build.fetch_latest_regular_report", return_value=report("new-id")),
            patch("app.build.import_fund_data", return_value={"0.A"}),
            patch("app.build.refresh_current_valuation_base_prices") as refresh_prices,
            patch("app.build.refresh_incremental_backtests") as refresh_backtests,
        ):
            result = refresh_reports(con)

        refresh_prices.assert_called_once()
        refresh_backtests.assert_not_called()
        self.assertEqual(result["pending_backtests"], {})

    def test_pending_report_refresh_becomes_a_soft_alert(self) -> None:
        con = make_connection()
        funds = [{"code": "A", "name": "A", "type": "其他", "status": "ok"}]

        alerts = collect_data_alerts(
            con,
            funds,
            include_backtests=False,
            report_refresh_errors=[
                {"code": "A", "stage": "backtest", "error": "price gap"}
            ],
            pending_report_backtests={"A": "2026-07-20"},
        )

        self.assertEqual(alerts[0]["type"], "report_refresh_pending")
        self.assertEqual(alerts[0]["severity"], "warning")
        self.assertIn("上一版可用缓存", alerts[0]["message"])


if __name__ == "__main__":
    unittest.main()
