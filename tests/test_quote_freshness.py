from __future__ import annotations

import sqlite3
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.build import refresh_quotes, realtime_source_diagnostic_stats
from app.config import FundConfig
from app.db import SCHEMA, get_meta, set_meta
from app.sources import normalize_realtime_quote, record_realtime_source_error, utc_now
from app.valuation import (
    estimate_intraday,
    realtime_asset_return_with_warning,
    realtime_quote_cache_warning,
)


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def quote(secid: str, price: float, quote_time: str) -> dict:
    market, symbol = secid.split(".", 1)
    return {
        "secid": secid,
        "symbol": symbol,
        "market": int(market),
        "name": symbol,
        "price": price,
        "pct": 1.0,
        "previous_close": price / 1.01,
        "quote_time": quote_time,
    }


def insert_quote(
    con: sqlite3.Connection,
    secid: str,
    *,
    price: float = 100.0,
    session_date: str = "2026-07-10",
    fetch_status: str = "ok",
    last_success_at: str | None = None,
) -> None:
    market, symbol = secid.split(".", 1)
    timestamp = last_success_at or utc_now()
    con.execute(
        """
        insert into quotes
        (secid, symbol, market, name, price, pct, previous_close, quote_time,
         session_date, last_attempt_at, last_success_at, fetch_status, updated_at)
        values (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            secid,
            symbol,
            int(market),
            symbol,
            price,
            price / 1.01,
            f"{session_date}T20:00:00+00:00",
            session_date,
            utc_now(),
            timestamp,
            fetch_status,
            timestamp,
        ),
    )


class QuoteFreshnessTests(unittest.TestCase):
    def test_quote_normalization_rejects_bad_price_and_sets_market_session(self) -> None:
        self.assertIsNone(
            normalize_realtime_quote(quote("107.QQQ", -1, "2026-07-11T00:30:00+00:00"))
        )
        normalized = normalize_realtime_quote(
            quote("107.QQQ", 600, "2026-07-11T00:30:00+00:00")
        )
        self.assertEqual(normalized["session_date"], "2026-07-10")

    def test_partial_refresh_marks_missing_row_without_reusing_it(self) -> None:
        con = make_connection()
        insert_quote(con, "0.A", price=10)
        insert_quote(con, "0.B", price=20)
        set_meta(con, "last_realtime_quotes_refresh_success_at", "old-success")

        with patch(
            "app.build.fetch_realtime_quotes",
            return_value=[quote("0.A", 11, "2026-07-10T07:00:00+00:00")],
        ):
            result = refresh_quotes(con, ["0.A", "0.B"], attempts=1)

        rows = {
            row["secid"]: row
            for row in con.execute("select * from quotes where secid in ('0.A', '0.B')")
        }
        self.assertEqual(result["missing"], ["0.B"])
        self.assertEqual(rows["0.A"]["fetch_status"], "ok")
        self.assertEqual(rows["0.A"]["price"], 11)
        self.assertEqual(rows["0.B"]["fetch_status"], "missing")
        self.assertEqual(rows["0.B"]["price"], 20)
        self.assertEqual(get_meta(con, "last_realtime_quotes_refresh_success_at"), "old-success")
        self.assertIsNotNone(get_meta(con, "last_realtime_quotes_partial_at"))

    def test_refresh_persists_structured_source_diagnostics(self) -> None:
        con = make_connection()

        def source(_secids, progress_callback=None, diagnostics=None):
            self.assertIsNotNone(diagnostics)
            diagnostics.append(
                {
                    "source": "test_source",
                    "secids": ["0.B"],
                    "error": "Timeout: injected",
                }
            )
            return [quote("0.A", 11, "2026-07-10T07:00:00+00:00")]

        with patch("app.build.fetch_realtime_quotes", side_effect=source):
            refresh_quotes(con, ["0.A", "0.B"], attempts=1)

        self.assertEqual(
            get_meta(con, "last_realtime_quotes_source_diagnostics"),
            [
                {
                    "source": "test_source",
                    "secids": ["0.B"],
                    "error": "Timeout: injected",
                }
            ],
        )
        self.assertEqual(
            get_meta(con, "last_realtime_quotes_source_diagnostic_stats"),
            {
                "count": 1,
                "stored_count": 1,
                "truncated": False,
                "affected_secid_count": 1,
                "error_types": {"Timeout": 1},
                "by_source": {
                    "test_source": {
                        "event_count": 1,
                        "secid_count": 1,
                    }
                },
            },
        )

    def test_realtime_source_diagnostics_are_aggregated_by_source(self) -> None:
        stats = realtime_source_diagnostic_stats(
            [
                {"source": "batch", "secids": ["0.A", "0.B"], "error": "Timeout: one"},
                {"source": "batch", "secids": ["0.B", "0.C"], "error": "Timeout: two"},
                {"source": "single", "secids": ["0.D"], "error": "ValueError: bad"},
            ]
        )

        self.assertEqual(
            stats["by_source"],
            {
                "batch": {
                    "event_count": 2,
                    "secid_count": 3,
                },
                "single": {
                    "event_count": 1,
                    "secid_count": 1,
                },
            },
        )
        self.assertEqual(stats["affected_secid_count"], 4)
        self.assertEqual(stats["error_types"], {"Timeout": 2, "ValueError": 1})

    def test_source_error_recording_is_thread_safe_and_silent(self) -> None:
        diagnostics = []
        threads = [
            threading.Thread(
                target=record_realtime_source_error,
                args=(diagnostics, "batch", [f"0.{index}"], TimeoutError("injected")),
            )
            for index in range(500)
        ]

        with patch("app.sources.LOGGER.warning") as warning:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        warning.assert_not_called()
        self.assertEqual(len(diagnostics), 500)
        self.assertEqual(len({item["secids"][0] for item in diagnostics}), 500)

    def test_refresh_logs_one_bounded_source_summary(self) -> None:
        con = make_connection()

        def source(_secids, progress_callback=None, diagnostics=None):
            diagnostics.extend(
                {
                    "source": "batch",
                    "secids": [f"0.{index}"],
                    "error": "ProxyError: https://secret.example/detail",
                }
                for index in range(40)
            )
            return [quote("0.A", 11, "2026-07-10T07:00:00+00:00")]

        with (
            patch("app.build.fetch_realtime_quotes", side_effect=source),
            patch("app.build.LOGGER.warning") as warning,
        ):
            refresh_quotes(con, ["0.A"], attempts=1)

        warning.assert_called_once()
        message = warning.call_args.args[0] % warning.call_args.args[1:]
        self.assertIn("events=40", message)
        self.assertIn("final_missing=0", message)
        self.assertNotIn("secret.example", message)
        self.assertLess(len(message), 300)

    def test_missing_or_old_quote_is_not_usable_for_asset_return(self) -> None:
        con = make_connection()
        insert_quote(con, "107.QQQ", fetch_status="missing")

        value, warning = realtime_asset_return_with_warning(con, "107.QQQ", "2026-07-09")

        self.assertIsNone(value)
        self.assertEqual(warning["type"], "quote_refresh_missing")
        stale = {
            "fetch_status": "ok",
            "last_success_at": "2026-07-10T00:00:00+00:00",
            "updated_at": "2026-07-10T00:00:00+00:00",
        }
        warning = realtime_quote_cache_warning(
            stale,
            "107.QQQ",
            now=datetime(2026, 7, 10, 0, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(warning["type"], "quote_cache_stale")

    def test_quote_before_base_is_only_zero_on_known_market_closure(self) -> None:
        con = make_connection()
        insert_quote(con, "107.QQQ", session_date="2026-07-08")

        value, warning = realtime_asset_return_with_warning(con, "107.QQQ", "2026-07-09")

        self.assertIsNone(value)
        self.assertEqual(warning["type"], "quote_before_base")
        con.execute("delete from quotes")
        insert_quote(con, "107.QQQ", session_date="2026-07-02")
        value, warning = realtime_asset_return_with_warning(con, "107.QQQ", "2026-07-03")
        self.assertEqual(value, 0.0)
        self.assertIsNone(warning)

    def test_stale_trade_quote_is_not_presented_as_current_price(self) -> None:
        con = make_connection()
        con.execute(
            "insert into funds(code, name, exchange_market, fund_type, updated_at) values ('TEST', 'Test', 0, '其他', 'now')"
        )
        con.execute(
            "insert into navs(fund_code, date, nav, distribution) values ('TEST', '2026-07-10', 1, 0)"
        )
        insert_quote(con, "0.TEST", fetch_status="missing")

        with patch.dict("app.valuation.FUNDS", {"TEST": FundConfig("TEST", 0)}):
            result = estimate_intraday(con, "TEST")

        self.assertIsNone(result["trade_price"])
        self.assertIsNone(result["premium"])


if __name__ == "__main__":
    unittest.main()
