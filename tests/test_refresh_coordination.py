from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date
from unittest.mock import patch

import app.server as server
from app.build import refresh_navs, refresh_quotes
from app.config import FundConfig
from app.db import SCHEMA, set_meta
from app.server import (
    NAV_NON_TRADING_REFRESH_INTERVAL_SECONDS,
    NAV_REFRESH_INTERVAL_SECONDS,
    NAV_UNCHANGED_REFRESH_INTERVAL_SECONDS,
    FAILED_QUOTE_RETRY_INTERVAL_SECONDS,
    MISSING_QUOTE_RETRY_INTERVAL_SECONDS,
    REPORT_REFRESH_INTERVAL_SECONDS,
    REPORT_RETRY_INTERVAL_SECONDS,
    nav_refresh_interval_seconds,
    quote_refresh_due_secids,
    refresh_cooldown_active,
    report_refresh_interval_seconds,
)


def nav_row() -> dict:
    return {
        "date": "2026-07-10",
        "nav": 1.0,
        "distribution": 0.0,
        "return_pct": 0.0,
    }


class RefreshCoordinationTests(unittest.TestCase):
    def test_first_refresh_is_not_blocked_by_process_uptime(self) -> None:
        self.assertFalse(refresh_cooldown_active(0, 60 * 60, now=30))
        self.assertTrue(refresh_cooldown_active(20, 60, now=30))
        self.assertFalse(refresh_cooldown_active(20, 60, now=80))

    def test_quote_refresh_keeps_live_quotes_fresh_and_backs_off_missing_ones(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)
        con.executemany(
            """
            insert into quotes
            (secid, symbol, market, name, price, fetch_status,
             last_attempt_at, last_success_at, updated_at)
            values (?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "0.OK",
                    "OK",
                    "OK",
                    1,
                    "ok",
                    "2026-07-13T09:59:00+00:00",
                    "2026-07-13T09:59:00+00:00",
                    "2026-07-13T09:59:00+00:00",
                ),
                (
                    "0.FAILED",
                    "FAILED",
                    "FAILED",
                    1,
                    "missing",
                    "2026-07-13T09:54:00+00:00",
                    "2026-07-13T09:50:00+00:00",
                    "2026-07-13T09:50:00+00:00",
                ),
                (
                    "0.NEVER",
                    "NEVER",
                    "NEVER",
                    None,
                    "missing",
                    "2026-07-13T09:30:00+00:00",
                    None,
                    "2026-07-13T09:30:00+00:00",
                ),
            ],
        )

        due = quote_refresh_due_secids(
            con,
            ["0.OK", "0.FAILED", "0.NEVER", "0.NEW"],
            now_at="2026-07-13T10:00:00+00:00",
        )

        self.assertEqual(due, ["0.FAILED", "0.NEW", "0.OK"])
        self.assertEqual(FAILED_QUOTE_RETRY_INTERVAL_SECONDS, 5 * 60)
        self.assertEqual(MISSING_QUOTE_RETRY_INTERVAL_SECONDS, 60 * 60)

    def test_nav_refresh_backoff_uses_session_and_change_state(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)

        self.assertEqual(
            nav_refresh_interval_seconds(con, date(2026, 7, 12)),
            NAV_NON_TRADING_REFRESH_INTERVAL_SECONDS,
        )
        set_meta(con, "last_navs_refresh_stats", {"changed_count": 0, "failed_count": 0})
        self.assertEqual(
            nav_refresh_interval_seconds(con, date(2026, 7, 13)),
            NAV_UNCHANGED_REFRESH_INTERVAL_SECONDS,
        )
        set_meta(con, "last_navs_refresh_stats", {"changed_count": 1, "failed_count": 0})
        self.assertEqual(
            nav_refresh_interval_seconds(con, date(2026, 7, 13)),
            NAV_REFRESH_INTERVAL_SECONDS,
        )

    def test_slow_nav_network_fetch_does_not_block_quote_write(self) -> None:
        handle, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        started = threading.Event()
        release = threading.Event()
        failures: list[Exception] = []
        try:
            con = sqlite3.connect(path)
            try:
                con.executescript(SCHEMA)
                con.commit()
            finally:
                con.close()

            def slow_page(_code: str):
                started.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("test release timed out")
                return {"name": "A", "navs": [nav_row()]}

            def nav_worker() -> None:
                con = None
                try:
                    con = sqlite3.connect(path, timeout=1)
                    con.row_factory = sqlite3.Row
                    refresh_navs(con, ["A"], update_backtests=False)
                    con.commit()
                except Exception as exc:  # pragma: no cover - asserted below
                    failures.append(exc)
                finally:
                    if con is not None:
                        con.close()

            quote = {
                "secid": "0.000001",
                "symbol": "000001",
                "market": 0,
                "name": "Index",
                "price": 3500.0,
                "pct": 0.1,
                "previous_close": 3496.5,
                "quote_time": "2026-07-10T07:00:00+00:00",
            }
            with (
                patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
                patch("app.build.fund_page_data", side_effect=slow_page),
                patch("app.build.parse_navs", side_effect=lambda rows: rows),
                patch("app.build.refresh_current_valuation_base_prices"),
                patch("app.build.fetch_realtime_quotes", return_value=[quote]),
            ):
                thread = threading.Thread(target=nav_worker)
                thread.start()
                self.assertTrue(started.wait(timeout=2))

                con = sqlite3.connect(path, timeout=1)
                try:
                    con.row_factory = sqlite3.Row
                    result = refresh_quotes(con, ["0.000001"], attempts=1)
                    con.commit()
                finally:
                    con.close()

                self.assertTrue(thread.is_alive())
                self.assertEqual(result["saved"], 1)
                con = sqlite3.connect(path)
                try:
                    self.assertEqual(con.execute("select count(*) from quotes").fetchone()[0], 1)
                finally:
                    con.close()

                release.set()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
            self.assertEqual(failures, [])
        finally:
            release.set()
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    def test_report_refresh_retries_failures_sooner(self) -> None:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(SCHEMA)

        self.assertEqual(
            report_refresh_interval_seconds(con),
            REPORT_REFRESH_INTERVAL_SECONDS,
        )
        set_meta(con, "last_reports_refresh_stats", {"failed_count": 1})
        self.assertEqual(
            report_refresh_interval_seconds(con),
            REPORT_RETRY_INTERVAL_SECONDS,
        )
        set_meta(con, "last_reports_refresh_stats", {"failed_count": 0})
        set_meta(con, "pending_report_backtests", {"A": "2026-07-20"})
        self.assertEqual(
            report_refresh_interval_seconds(con),
            REPORT_RETRY_INTERVAL_SECONDS,
        )

    def test_report_nav_and_manual_backtest_schedulers_are_mutually_exclusive(self) -> None:
        self.assertTrue(server._nav_refresh_lock.acquire(blocking=False))
        try:
            self.assertFalse(server.schedule_report_refresh())
        finally:
            server._nav_refresh_lock.release()

        self.assertTrue(server._report_refresh_lock.acquire(blocking=False))
        try:
            self.assertFalse(server.schedule_nav_refresh())
            started, message = server.schedule_incremental_backtest_refresh()
            self.assertFalse(started)
            self.assertIn("持仓检查", message)
        finally:
            server._report_refresh_lock.release()


if __name__ == "__main__":
    unittest.main()
