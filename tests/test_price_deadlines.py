from __future__ import annotations

import sqlite3
import time
import unittest
from unittest.mock import patch

import requests

from app.build import refresh_daily_prices, refresh_daily_prices_for_targets
from app.db import SCHEMA, get_meta
from app.sources import (
    DailyPriceDeadlineExceeded,
    _get,
    fetch_daily_prices,
)


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


class PriceDeadlineTests(unittest.TestCase):
    def test_instrument_deadline_clamps_request_timeout_and_retry_sleep(self) -> None:
        observed_timeouts: list[float] = []

        def timeout_request(*_args, timeout, **_kwargs):
            observed_timeouts.append(timeout)
            time.sleep(timeout + 0.002)
            raise requests.Timeout("injected timeout")

        def timed_source(*_args, **_kwargs):
            return _get("https://example.invalid/slow", timeout=20, attempts=5)

        started = time.monotonic()
        with (
            patch("app.sources.requests.get", side_effect=timeout_request),
            patch("app.sources.eastmoney_daily_prices", side_effect=timed_source),
            self.assertRaises(DailyPriceDeadlineExceeded),
        ):
            fetch_daily_prices("2.TEST", deadline=started + 0.03)

        self.assertLess(time.monotonic() - started, 0.12)
        self.assertTrue(observed_timeouts)
        self.assertLessEqual(max(observed_timeouts), 0.04)

    def test_fetch_daily_prices_keeps_normal_source_fallback(self) -> None:
        expected = [
            {
                "date": "2026-07-10",
                "close": 12.5,
                "pct": 1.0,
                "source": "fallback",
                "adjustment": "raw",
            }
        ]
        with (
            patch("app.sources.hang_seng_index_daily_prices", return_value=[]) as primary,
            patch("app.sources.eastmoney_index_daily_prices", return_value=expected) as fallback,
            patch("app.sources._sleep_within_daily_price_deadline"),
        ):
            rows = fetch_daily_prices("124.HSTECH")

        self.assertEqual(rows, expected)
        self.assertEqual(primary.call_count, 3)
        fallback.assert_called_once()

    def test_batch_deadline_returns_without_waiting_for_executor_shutdown(self) -> None:
        con = make_connection()

        def slow_fetch(*_args, **_kwargs):
            time.sleep(0.2)
            return []

        started = time.monotonic()
        with patch("app.build.fetch_daily_prices", side_effect=slow_fetch):
            refresh_daily_prices(
                con,
                ["0.SLOW", "0.QUEUED"],
                commit_every=0,
                batch_deadline_seconds=0.03,
                instrument_deadline_seconds=0.5,
            )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.12)
        diagnostics = get_meta(con, "last_daily_prices_refresh_diagnostics")
        self.assertEqual(
            diagnostics,
            [
                {
                    "secid": "0.QUEUED",
                    "type": "batch_deadline",
                    "error": "historical price batch deadline exceeded",
                },
                {
                    "secid": "0.SLOW",
                    "type": "batch_deadline",
                    "error": "historical price batch deadline exceeded",
                },
            ],
        )
        self.assertEqual(
            get_meta(con, "last_daily_prices_refresh_stats")["deadline_count"],
            2,
        )

    def test_targeted_refresh_records_instrument_deadline(self) -> None:
        con = make_connection()
        error = DailyPriceDeadlineExceeded("injected instrument deadline")

        with patch("app.build.fetch_daily_prices", side_effect=error):
            result = refresh_daily_prices_for_targets(
                con,
                {"0.SLOW": {"2026-07-10"}},
                commit_every=0,
                batch_deadline_seconds=1,
                instrument_deadline_seconds=0.5,
            )

        diagnostics = [
            {
                "secid": "0.SLOW",
                "type": "instrument_deadline",
                "error": "DailyPriceDeadlineExceeded: injected instrument deadline",
            }
        ]
        self.assertEqual(result["diagnostics"], diagnostics)
        self.assertEqual(
            get_meta(con, "last_daily_prices_targeted_diagnostics"),
            diagnostics,
        )
        self.assertEqual(
            result["unresolved"],
            [{"secid": "0.SLOW", "date": "2026-07-10"}],
        )


if __name__ == "__main__":
    unittest.main()
