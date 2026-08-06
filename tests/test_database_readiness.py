from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.build import prepare_build
from app.config import FundConfig
from app.db import (
    SCHEMA,
    SCHEMA_VERSION,
    database_readiness,
    ensure_config_state,
    get_meta,
    init_db,
    mark_build_finished,
    mark_build_started,
    require_supported_schema_version,
    schema_contract_issues,
    set_meta,
)


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute(f"pragma user_version = {SCHEMA_VERSION}")
    return con


def seed_core(con: sqlite3.Connection, code: str = "A") -> None:
    con.execute(
        "insert into funds(code, name, exchange_market, fund_type, updated_at) values (?, ?, 0, '其他', 'now')",
        (code, code),
    )
    con.execute(
        "insert into navs(fund_code, date, nav, distribution) values (?, '2026-07-10', 1, 0)",
        (code,),
    )
    con.execute(
        """
        insert into holdings
        (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
        values (?, '2026-06-30', '2026-07-01', '0.A', 'A', 'A', 0.1, 'test')
        """,
        (code,),
    )


class DatabaseReadinessTests(unittest.TestCase):
    def test_legacy_cache_is_not_silently_certified(self) -> None:
        con = make_connection()
        seed_core(con)

        with (
            patch.dict("app.db.FUNDS", {"A": FundConfig("A", 0)}, clear=True),
            patch("app.db.current_config_hash", return_value="expected"),
        ):
            ensure_config_state(con)
            status = database_readiness(con, expected_config_hash="expected")

        self.assertFalse(status["ready"])
        self.assertEqual(status["build_state"], "legacy")
        self.assertEqual(status["pending_config_hash"], "expected")
        self.assertIsNone(get_meta(con, "config_hash"))

    def test_config_change_invalidates_old_backtests(self) -> None:
        con = make_connection()
        seed_core(con)
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )
        set_meta(con, "config_hash", "old")
        set_meta(con, "build_state", "complete")

        with patch("app.db.current_config_hash", return_value="new"):
            ensure_config_state(con)

        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)
        self.assertEqual(get_meta(con, "build_state"), "config_changed")
        self.assertEqual(get_meta(con, "pending_config_hash"), "new")

    def test_current_only_build_discards_incompatible_backtests(self) -> None:
        con = make_connection()
        con.execute(
            """
            insert into backtests
            (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
             error_pct, covered_weight, data_quality)
            values ('A', '2026-07-10', '2026-07-09', 1, 1, 1, 0, 1, 'ok')
            """
        )

        prepare_build(con, "current_only", False, "start")

        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)
        self.assertTrue(get_meta(con, "backtests_disabled"))

    def test_core_complete_current_only_cache_is_ready(self) -> None:
        con = make_connection()
        seed_core(con)
        set_meta(con, "config_hash", "expected")
        set_meta(con, "build_state", "complete")

        with patch.dict("app.db.FUNDS", {"A": FundConfig("A", 0)}, clear=True):
            status = database_readiness(con, expected_config_hash="expected")

        self.assertTrue(status["ready"])
        self.assertEqual(status["schema_issues"], [])
        self.assertEqual(status["missing_backtests"], [])

    def test_missing_required_table_is_reported_before_business_queries(self) -> None:
        con = make_connection()
        seed_core(con)
        set_meta(con, "config_hash", "expected")
        set_meta(con, "build_state", "complete")
        con.execute("drop table quotes")

        with patch.dict("app.db.FUNDS", {"A": FundConfig("A", 0)}, clear=True):
            status = database_readiness(con, expected_config_hash="expected")

        self.assertFalse(status["ready"])
        self.assertEqual(status["build_state"], "schema_incompatible")
        self.assertIn("missing table: quotes", status["schema_issues"])

    def test_missing_columns_and_wrong_primary_key_fail_schema_contract(self) -> None:
        con = make_connection()
        con.execute("drop table quotes")
        con.execute("create table quotes (secid text, symbol text)")

        issues = schema_contract_issues(con)

        self.assertTrue(any("quotes missing columns" in issue for issue in issues))
        self.assertIn("quotes primary key: expected ('secid',), got ()", issues)

    def test_future_schema_version_is_never_downgraded(self) -> None:
        con = make_connection()
        future_version = SCHEMA_VERSION + 1
        con.execute(f"pragma user_version = {future_version}")

        with self.assertRaisesRegex(RuntimeError, "newer than supported"):
            require_supported_schema_version(con)

        self.assertEqual(con.execute("pragma user_version").fetchone()[0], future_version)

    def test_repaired_existing_schema_requires_a_rebuild_before_serving(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "cache.sqlite3"
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            con.executescript(SCHEMA)
            con.execute(f"pragma user_version = {SCHEMA_VERSION}")
            set_meta(con, "config_hash", "expected")
            set_meta(con, "build_state", "complete")
            con.execute("drop table quotes")
            con.commit()
            con.close()

            with (
                patch("app.db.DB_PATH", db_path),
                patch("app.db.DATA_DIR", Path(directory)),
                patch("app.db.current_config_hash", return_value="expected"),
            ):
                init_db()
                con = sqlite3.connect(db_path)
                con.row_factory = sqlite3.Row
                try:
                    self.assertIsNotNone(
                        con.execute(
                            "select 1 from sqlite_master where type='table' and name='quotes'"
                        ).fetchone()
                    )
                    self.assertEqual(get_meta(con, "build_state"), "schema_changed")
                    self.assertIn(
                        "missing table: quotes",
                        get_meta(con, "schema_repair_issues"),
                    )
                finally:
                    con.close()

    def test_config_mismatch_or_interrupted_build_is_not_ready(self) -> None:
        con = make_connection()
        seed_core(con)
        set_meta(con, "config_hash", "old")
        set_meta(con, "build_state", "complete")
        with patch.dict("app.db.FUNDS", {"A": FundConfig("A", 0)}, clear=True):
            self.assertFalse(
                database_readiness(con, expected_config_hash="new")["ready"]
            )
            set_meta(con, "config_hash", "new")
            set_meta(con, "build_state", "running")
            self.assertFalse(
                database_readiness(con, expected_config_hash="new")["ready"]
            )

    def test_backtests_are_only_required_when_requested(self) -> None:
        con = make_connection()
        seed_core(con)
        set_meta(con, "config_hash", "expected")
        set_meta(con, "build_state", "complete")
        with patch.dict("app.db.FUNDS", {"A": FundConfig("A", 0)}, clear=True):
            self.assertTrue(
                database_readiness(con, expected_config_hash="expected")["ready"]
            )
            self.assertFalse(
                database_readiness(
                    con,
                    require_backtests=True,
                    expected_config_hash="expected",
                )["ready"]
            )

    def test_build_markers_only_publish_config_on_complete_import(self) -> None:
        con = make_connection()
        set_meta(con, "config_hash", "old")
        mark_build_started(con, "current_only", "start")
        self.assertEqual(
            con.execute("select value from metadata where key='build_state'").fetchone()[0],
            '"running"',
        )
        with patch("app.db.current_config_hash", return_value="new"):
            mark_build_finished(
                con,
                mode="current_only",
                completed_at="done",
                import_failed=[{"code": "A"}],
                backtests_failed=[],
            )
            self.assertEqual(
                con.execute("select value from metadata where key='config_hash'").fetchone()[0],
                '"old"',
            )
            mark_build_finished(
                con,
                mode="current_only",
                completed_at="done",
                import_failed=[],
                backtests_failed=[],
            )
            self.assertEqual(
                con.execute("select value from metadata where key='config_hash'").fetchone()[0],
                '"new"',
            )

    def test_full_build_with_backtest_failures_is_partial(self) -> None:
        con = make_connection()
        with patch("app.db.current_config_hash", return_value="new"):
            mark_build_finished(
                con,
                mode="full",
                completed_at="done",
                import_failed=[],
                backtests_failed=[{"code": "A"}],
            )

        self.assertEqual(get_meta(con, "build_state"), "partial")
        self.assertIsNone(get_meta(con, "config_hash"))


if __name__ == "__main__":
    unittest.main()
