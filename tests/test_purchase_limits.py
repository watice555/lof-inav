from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.build import refresh_purchase_limits
from app.config import FundConfig
from app.db import SCHEMA, get_meta, set_meta


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def limit(code: str, display: str = "开放") -> dict:
    return {
        "fund_code": code,
        "purchase_status": "开放申购",
        "redeem_status": "开放赎回",
        "next_open_date": "",
        "min_purchase_amount": 1.0,
        "max_purchase_amount": None,
        "display": display,
        "sort_value": 1.0,
        "source_date": "2026-07-13",
    }


def seed_old_limit(con: sqlite3.Connection, code: str) -> None:
    item = {**limit(code, "旧值"), "updated_at": "old"}
    con.execute(
        """
        insert into fund_purchase_limits
        (fund_code, purchase_status, redeem_status, next_open_date,
         min_purchase_amount, max_purchase_amount, display, sort_value,
         source_date, updated_at)
        values (:fund_code, :purchase_status, :redeem_status, :next_open_date,
                :min_purchase_amount, :max_purchase_amount, :display, :sort_value,
                :source_date, :updated_at)
        """,
        item,
    )
    con.commit()


class PurchaseLimitRefreshTests(unittest.TestCase):
    def test_partial_snapshot_preserves_old_value_and_not_full_success(self) -> None:
        con = make_connection()
        seed_old_limit(con, "B")
        set_meta(con, "last_purchase_limits_refresh_success_at", "old-success")
        configs = {"A": FundConfig("A", 0), "B": FundConfig("B", 0)}

        with (
            patch.dict("app.build.FUNDS", configs, clear=True),
            patch("app.build.fetch_purchase_limits", return_value=[limit("A")]),
        ):
            result = refresh_purchase_limits(con)

        self.assertEqual(result["missing"], ["B"])
        self.assertEqual(get_meta(con, "last_purchase_limits_refresh_success_at"), "old-success")
        self.assertIsNotNone(get_meta(con, "last_purchase_limits_refresh_partial_at"))
        self.assertEqual(get_meta(con, "last_purchase_limits_refresh_missing_codes"), ["B"])
        self.assertEqual(
            con.execute(
                "select display from fund_purchase_limits where fund_code='B'"
            ).fetchone()[0],
            "旧值",
        )

    def test_complete_snapshot_advances_success(self) -> None:
        con = make_connection()
        configs = {"A": FundConfig("A", 0), "B": FundConfig("B", 0)}
        with (
            patch.dict("app.build.FUNDS", configs, clear=True),
            patch(
                "app.build.fetch_purchase_limits",
                return_value=[limit("A"), limit("B")],
            ),
        ):
            result = refresh_purchase_limits(con)

        self.assertEqual(result["missing"], [])
        self.assertEqual(
            get_meta(con, "last_purchase_limits_refresh_success_at"),
            get_meta(con, "last_purchase_limits_refresh_completed_at"),
        )

    def test_fetch_failure_records_failure_and_keeps_cache(self) -> None:
        con = make_connection()
        seed_old_limit(con, "A")
        with (
            patch.dict("app.build.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch(
                "app.build.fetch_purchase_limits",
                side_effect=TimeoutError("upstream timeout"),
            ),
        ):
            with self.assertRaises(TimeoutError):
                refresh_purchase_limits(con)

        self.assertIsNotNone(get_meta(con, "last_purchase_limits_refresh_failed_at"))
        self.assertEqual(get_meta(con, "last_purchase_limits_refresh_missing_codes"), ["A"])
        self.assertIn("TimeoutError", get_meta(con, "last_purchase_limits_refresh_errors")[0]["error"])
        self.assertEqual(
            con.execute(
                "select display from fund_purchase_limits where fund_code='A'"
            ).fetchone()[0],
            "旧值",
        )


if __name__ == "__main__":
    unittest.main()
