from __future__ import annotations

import sqlite3
import unittest

from app.backtest_state import (
    BACKTEST_DIRTY_META_KEY,
    BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY,
    BACKTEST_REBUILD_REQUIRED_META_KEY,
    LEGACY_BACKTEST_DIRTY_META_KEY,
    backtest_refresh_lease_active,
    claim_backtest_refresh_lease,
    clear_backtest_rebuild_required_if_resolved,
    invalidate_backtests,
    load_backtest_dirty,
    mark_backtest_dirty,
    resolve_backtest_dirty,
    release_backtest_refresh_lease,
)
from app.db import SCHEMA, get_meta, set_meta


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def seed_backtest(con: sqlite3.Connection, code: str, backtest_date: str) -> None:
    con.execute(
        """
        insert into backtests
        (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
         error_pct, covered_weight, modeled_weight, priced_ratio, data_quality)
        values (?, ?, '2026-01-01', 1, 1, 1, 0, 1, 1, 1, 'ok')
        """,
        (code, backtest_date),
    )


class BacktestDirtyStateTests(unittest.TestCase):
    def test_load_merges_new_and_legacy_state_then_mark_migrates_it(self) -> None:
        con = make_connection()
        set_meta(
            con,
            LEGACY_BACKTEST_DIRTY_META_KEY,
            {"A": "2026-07-20", "B": "2026-06-01"},
        )
        set_meta(
            con,
            BACKTEST_DIRTY_META_KEY,
            {"A": "2026-07-10", "C": "2026-08-01"},
        )

        self.assertEqual(
            load_backtest_dirty(con),
            {"A": "2026-07-10", "B": "2026-06-01", "C": "2026-08-01"},
        )
        self.assertIsNotNone(get_meta(con, LEGACY_BACKTEST_DIRTY_META_KEY))

        self.assertEqual(mark_backtest_dirty(con, "D", "2026-05-01"), "2026-05-01")
        self.assertEqual(
            get_meta(con, BACKTEST_DIRTY_META_KEY),
            {
                "A": "2026-07-10",
                "B": "2026-06-01",
                "C": "2026-08-01",
                "D": "2026-05-01",
            },
        )
        self.assertIsNone(get_meta(con, LEGACY_BACKTEST_DIRTY_META_KEY))

    def test_mark_only_moves_anchor_earlier(self) -> None:
        con = make_connection()

        self.assertEqual(mark_backtest_dirty(con, "A", "2026-07-10"), "2026-07-10")
        self.assertEqual(mark_backtest_dirty(con, "A", "2026-07-20"), "2026-07-10")
        self.assertEqual(mark_backtest_dirty(con, "A", "2026-07-01"), "2026-07-01")
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-01"})

    def test_rejects_non_iso_and_impossible_dates(self) -> None:
        con = make_connection()
        for invalid in ("2026-7-01", "2026-02-30", "2026-07-01T00:00:00", "", None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    mark_backtest_dirty(con, "A", invalid)  # type: ignore[arg-type]

        set_meta(con, BACKTEST_DIRTY_META_KEY, {"A": "2026-13-01"})
        with self.assertRaises(ValueError):
            load_backtest_dirty(con)

    def test_resolve_strictly_validates_processing_dates(self) -> None:
        con = make_connection()
        mark_backtest_dirty(con, "A", "2026-07-10")

        with self.assertRaises(ValueError):
            resolve_backtest_dirty(
                con,
                "A",
                full_replace=True,
                processed_from=None,
                processed_through="2026-7-10",
            )
        with self.assertRaises(ValueError):
            resolve_backtest_dirty(
                con,
                "A",
                full_replace=False,
                processed_from="2026-02-30",
                processed_through="2026-07-10",
            )

    def test_invalidate_deletes_from_final_merged_anchor(self) -> None:
        con = make_connection()
        mark_backtest_dirty(con, "A", "2026-07-10")
        for backtest_date in ("2026-07-09", "2026-07-10", "2026-07-20"):
            seed_backtest(con, "A", backtest_date)
        seed_backtest(con, "B", "2026-07-20")

        self.assertEqual(invalidate_backtests(con, "A", "2026-07-15"), "2026-07-10")
        self.assertEqual(
            [row[0] for row in con.execute("select date from backtests where fund_code='A'")],
            ["2026-07-09"],
        )
        self.assertEqual(
            con.execute("select count(*) from backtests where fund_code='B'").fetchone()[0],
            1,
        )

    def test_invalidate_state_and_deletion_roll_back_together(self) -> None:
        con = make_connection()
        seed_backtest(con, "A", "2026-07-10")
        seed_backtest(con, "A", "2026-07-20")
        con.commit()

        invalidate_backtests(con, "A", "2026-07-10")
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 0)
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-10"})

        con.rollback()
        self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 2)
        self.assertEqual(load_backtest_dirty(con), {})

    def test_full_resolve_requires_processed_through_to_reach_anchor(self) -> None:
        con = make_connection()
        mark_backtest_dirty(con, "A", "2026-07-10")

        self.assertFalse(
            resolve_backtest_dirty(
                con,
                "A",
                full_replace=True,
                processed_from="2026-07-20",
                processed_through="2026-07-09",
            )
        )
        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-10"})
        self.assertTrue(
            resolve_backtest_dirty(
                con,
                "A",
                full_replace=True,
                processed_from="2026-07-20",
                processed_through="2026-07-10",
            )
        )
        self.assertEqual(load_backtest_dirty(con), {})

    def test_incremental_resolve_requires_both_bounds_to_cover_anchor(self) -> None:
        con = make_connection()
        mark_backtest_dirty(con, "A", "2026-07-10")

        for processed_from, processed_through in (
            (None, "2026-07-20"),
            ("2026-07-11", "2026-07-20"),
            ("2026-07-01", None),
            ("2026-07-01", "2026-07-09"),
        ):
            with self.subTest(
                processed_from=processed_from,
                processed_through=processed_through,
            ):
                self.assertFalse(
                    resolve_backtest_dirty(
                        con,
                        "A",
                        full_replace=False,
                        processed_from=processed_from,
                        processed_through=processed_through,
                    )
                )

        self.assertEqual(load_backtest_dirty(con), {"A": "2026-07-10"})
        self.assertTrue(
            resolve_backtest_dirty(
                con,
                "A",
                full_replace=False,
                processed_from="2026-07-10",
                processed_through="2026-07-10",
            )
        )
        self.assertEqual(load_backtest_dirty(con), {})

    def test_resolve_keeps_other_funds_and_migrates_legacy_state(self) -> None:
        con = make_connection()
        set_meta(
            con,
            LEGACY_BACKTEST_DIRTY_META_KEY,
            {"A": "2026-07-10", "B": "2026-06-01"},
        )

        self.assertTrue(
            resolve_backtest_dirty(
                con,
                "A",
                full_replace=True,
                processed_from=None,
                processed_through="2026-07-10",
            )
        )
        self.assertEqual(get_meta(con, BACKTEST_DIRTY_META_KEY), {"B": "2026-06-01"})
        self.assertIsNone(get_meta(con, LEGACY_BACKTEST_DIRTY_META_KEY))

    def test_shared_refresh_lease_enforces_owner_expiry_and_retry_cooldown(self) -> None:
        con = make_connection()
        set_meta(con, BACKTEST_REBUILD_REQUIRED_META_KEY, {"target_count": 1})

        self.assertTrue(
            claim_backtest_refresh_lease(
                con,
                "owner-a",
                require_rebuild=True,
                now_at="2026-09-02T08:00:00+00:00",
                lease_seconds=7200,
                retry_interval_seconds=3600,
            )
        )
        self.assertTrue(
            backtest_refresh_lease_active(
                con,
                now_at="2026-09-02T08:30:00+00:00",
            )
        )
        self.assertFalse(
            claim_backtest_refresh_lease(
                con,
                "owner-b",
                require_rebuild=False,
                now_at="2026-09-02T08:30:00+00:00",
                lease_seconds=7200,
                retry_interval_seconds=3600,
            )
        )
        self.assertFalse(release_backtest_refresh_lease(con, "owner-b"))
        self.assertTrue(release_backtest_refresh_lease(con, "owner-a"))
        self.assertFalse(
            claim_backtest_refresh_lease(
                con,
                "owner-b",
                require_rebuild=True,
                now_at="2026-09-02T08:30:00+00:00",
                lease_seconds=7200,
                retry_interval_seconds=3600,
            )
        )
        self.assertTrue(
            claim_backtest_refresh_lease(
                con,
                "owner-b",
                require_rebuild=True,
                now_at="2026-09-02T09:00:00+00:00",
                lease_seconds=7200,
                retry_interval_seconds=3600,
            )
        )

    def test_rebuild_marker_clears_only_after_configured_dirty_funds_resolve(self) -> None:
        con = make_connection()
        set_meta(con, BACKTEST_REBUILD_REQUIRED_META_KEY, {"target_count": 1})
        set_meta(
            con,
            BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY,
            "2026-09-02T08:00:00+00:00",
        )
        set_meta(con, BACKTEST_DIRTY_META_KEY, {"A": "2026-08-20", "REMOVED": "2026-01-01"})

        self.assertFalse(
            clear_backtest_rebuild_required_if_resolved(con, {"A"})
        )
        set_meta(con, BACKTEST_DIRTY_META_KEY, {"REMOVED": "2026-01-01"})
        self.assertTrue(
            clear_backtest_rebuild_required_if_resolved(con, {"A"})
        )
        self.assertIsNone(get_meta(con, BACKTEST_REBUILD_REQUIRED_META_KEY))
        self.assertIsNone(get_meta(con, BACKTEST_REBUILD_LAST_ATTEMPT_META_KEY))


if __name__ == "__main__":
    unittest.main()
