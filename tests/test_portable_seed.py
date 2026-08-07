from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.config import FundConfig
from app.db import SCHEMA, SCHEMA_VERSION, get_meta, set_meta
from app.runtime import install_seed_database
from scripts.create_seed_database import SeedRetention, create_seed_database


def create_ready_source(path: Path) -> None:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute(f"pragma user_version = {SCHEMA_VERSION}")
    for code in ("A", "B"):
        con.execute(
            """
            insert into funds(code, name, exchange_market, fund_type, updated_at)
            values (?, ?, 0, '其他', 'now')
            """,
            (code, code),
        )
        for day in range(1, 7):
            date = f"2026-07-{day:02d}"
            con.execute(
                "insert into navs(fund_code, date, nav) values (?, ?, ?)",
                (code, date, 1 + day / 100),
            )
            con.execute(
                """
                insert into daily_prices(secid, date, close, source, adjustment)
                values (?, ?, ?, 'test', 'none')
                """,
                (f"0.{code}", date, 10 + day),
            )
        for period in range(1, 5):
            con.execute(
                """
                insert into holdings
                (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
                values (?, ?, ?, ?, ?, ?, 0.1, 'test')
                """,
                (
                    code,
                    f"2026-0{period}-28",
                    f"2026-0{period}-29",
                    f"0.{code}{period}",
                    f"{code}{period}",
                    f"{code}{period}",
                ),
            )
    set_meta(con, "schema_version", SCHEMA_VERSION)
    set_meta(con, "historical_price_cache_version", 3)
    set_meta(con, "backtest_cache_version", 5)
    set_meta(con, "config_hash", "expected")
    set_meta(con, "build_state", "complete")
    set_meta(con, "last_navs_refresh_errors", [{"error": "private machine detail"}])
    set_meta(con, "last_navs_refresh_success_at", "2026-07-06T00:00:00+00:00")
    con.commit()
    con.close()


class PortableSeedInstallTests(unittest.TestCase):
    def test_seed_is_installed_once_without_overwriting_user_data(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            seed = root / "seed.sqlite3"
            target = root / "data" / "lof_inav.sqlite3"
            seed.write_bytes(b"seed")

            self.assertTrue(install_seed_database(seed, target))
            self.assertEqual(target.read_bytes(), b"seed")
            seed.write_bytes(b"new seed")

            self.assertFalse(install_seed_database(seed, target))
            self.assertEqual(target.read_bytes(), b"seed")

    def test_missing_packaged_seed_leaves_target_absent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "data" / "lof_inav.sqlite3"

            self.assertFalse(install_seed_database(root / "missing.sqlite3", target))
            self.assertFalse(target.exists())


class SeedDatabaseBuildTests(unittest.TestCase):
    def test_default_seed_keeps_seven_backtest_rows(self) -> None:
        self.assertEqual(SeedRetention().backtest_rows_per_fund, 7)

    def test_ready_source_is_trimmed_and_remains_ready(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            output = root / "seed.sqlite3"
            create_ready_source(source)
            funds = {code: FundConfig(code, 0) for code in ("A", "B")}

            with (
                patch.dict("app.db.FUNDS", funds, clear=True),
                patch("app.db.current_config_hash", return_value="expected"),
            ):
                result = create_seed_database(
                    source,
                    output,
                    SeedRetention(
                        nav_rows_per_fund=3,
                        holding_periods_per_fund=2,
                        price_rows_per_instrument=4,
                        backtest_rows_per_fund=2,
                    ),
                )

            self.assertEqual(result["counts"]["navs"], 6)
            self.assertEqual(result["counts"]["holdings"], 4)
            self.assertEqual(result["counts"]["daily_prices"], 8)
            con = sqlite3.connect(output)
            con.row_factory = sqlite3.Row
            try:
                self.assertIsNone(get_meta(con, "last_navs_refresh_errors"))
                self.assertEqual(
                    get_meta(con, "last_navs_refresh_success_at"),
                    "2026-07-06T00:00:00+00:00",
                )
                self.assertEqual(get_meta(con, "seed_format_version"), 1)
                self.assertEqual(con.execute("pragma integrity_check").fetchone()[0], "ok")
            finally:
                con.close()

    def test_unready_source_is_rejected_without_leaving_output(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            output = root / "seed.sqlite3"
            create_ready_source(source)
            con = sqlite3.connect(source)
            con.execute("delete from metadata where key = 'config_hash'")
            con.commit()
            con.close()
            funds = {code: FundConfig(code, 0) for code in ("A", "B")}

            with (
                patch.dict("app.db.FUNDS", funds, clear=True),
                patch("app.db.current_config_hash", return_value="expected"),
                self.assertRaisesRegex(RuntimeError, "not release-ready"),
            ):
                create_seed_database(source, output, SeedRetention())

            self.assertFalse(output.exists())

    def test_failed_forced_rebuild_preserves_previous_seed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            output = root / "seed.sqlite3"
            create_ready_source(source)
            output.write_bytes(b"previous seed")
            con = sqlite3.connect(source)
            con.execute("delete from metadata where key = 'config_hash'")
            con.commit()
            con.close()
            funds = {code: FundConfig(code, 0) for code in ("A", "B")}

            with (
                patch.dict("app.db.FUNDS", funds, clear=True),
                patch("app.db.current_config_hash", return_value="expected"),
                self.assertRaisesRegex(RuntimeError, "not release-ready"),
            ):
                create_seed_database(
                    source,
                    output,
                    SeedRetention(),
                    overwrite=True,
                )

            self.assertEqual(output.read_bytes(), b"previous seed")


if __name__ == "__main__":
    unittest.main()
