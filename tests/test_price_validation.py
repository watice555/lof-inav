from __future__ import annotations

import math
import sqlite3
import unittest
from unittest.mock import patch

from app.build import add_single_holding, refresh_daily_prices_for_targets
from app.db import SCHEMA
from app.sources import _best_daily_price_rows, normalize_daily_price_row


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


class PriceValidationTests(unittest.TestCase):
    def test_all_invalid_sources_return_no_rows(self) -> None:
        invalid = [{"date": "2026-07-10", "close": -3.71, "pct": 1}]

        rows = _best_daily_price_rows(
            [lambda: invalid], require_weekday_continuity=False, attempts_per_source=1
        )

        self.assertEqual(rows, [])

    def test_index_rows_do_not_require_every_weekday(self) -> None:
        rows = [
            {"date": "2026-07-02", "close": 100, "pct": None},
            {"date": "2026-07-06", "close": 101, "pct": 1},
        ]

        selected = _best_daily_price_rows(
            [lambda: rows], require_weekday_continuity=False, attempts_per_source=1
        )

        self.assertEqual(selected, rows)

    def test_normalizer_rejects_bad_close_and_sanitizes_pct(self) -> None:
        for close in (0, -1, math.nan, math.inf, -math.inf, None):
            self.assertIsNone(
                normalize_daily_price_row(
                    {"date": "2026-07-10", "close": close, "pct": 0}
                )
            )
        self.assertEqual(
            normalize_daily_price_row(
                {"date": "2026-07-10", "close": 12.5, "pct": math.inf}
            ),
            {
                "date": "2026-07-10",
                "close": 12.5,
                "pct": None,
                "source": "unknown",
                "adjustment": "unknown",
            },
        )

    def test_targeted_writer_skips_invalid_rows(self) -> None:
        con = make_connection()
        rows = [
            {"date": "2026-07-09", "close": -3.71, "pct": 1},
            {"date": "2026-07-10", "close": 10.5, "pct": 2},
        ]

        with patch("app.build.fetch_daily_prices", return_value=rows):
            result = refresh_daily_prices_for_targets(
                con, {"0.TEST": {"2026-07-10"}}, commit_every=0
            )

        stored = con.execute(
            "select date, close, pct, source, adjustment from daily_prices order by date"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in stored],
            [("2026-07-10", 10.5, 2.0, "unknown", "unknown")],
        )
        self.assertEqual(
            result,
            {"requested": 1, "saved": 1, "unresolved": [], "saved_rows": 1},
        )

    def test_schema_and_holding_writer_reject_zero_values(self) -> None:
        con = make_connection()
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute(
                "insert into daily_prices(secid, date, close, pct) values ('0.TEST', '2026-07-10', 0, 0)"
            )

        add_single_holding(
            con,
            "TEST",
            "2026-06-30",
            "2026-07-01",
            "0.TEST",
            "TEST",
            "Test",
            0,
            "test",
        )
        self.assertEqual(con.execute("select count(*) from holdings").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
