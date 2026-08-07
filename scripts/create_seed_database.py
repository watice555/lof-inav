from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import database_readiness, get_meta, set_meta
from app.runtime import db_path
from app.valuation import DEFAULT_BACKTEST_DAYS


@dataclass(frozen=True)
class SeedRetention:
    nav_rows_per_fund: int = 200
    holding_periods_per_fund: int = 5
    price_rows_per_instrument: int = 160
    backtest_rows_per_fund: int = DEFAULT_BACKTEST_DAYS


RANKED_TABLES = (
    ("navs", "fund_code", "date", "nav_rows_per_fund"),
    ("daily_prices", "secid", "date", "price_rows_per_instrument"),
    ("mark_prices", "secid", "date", "price_rows_per_instrument"),
    ("fund_prices", "fund_code", "date", "price_rows_per_instrument"),
    ("backtests", "fund_code", "date", "backtest_rows_per_fund"),
)


def delete_rows_after_rank(
    con: sqlite3.Connection,
    table: str,
    partition_column: str,
    order_column: str,
    keep: int,
) -> None:
    con.execute(
        f"""
        delete from {table}
        where rowid in (
            select rowid
            from (
                select rowid,
                       row_number() over (
                           partition by {partition_column}
                           order by {order_column} desc
                       ) as row_rank
                from {table}
            )
            where row_rank > ?
        )
        """,
        (keep,),
    )


def trim_seed_database(con: sqlite3.Connection, retention: SeedRetention) -> None:
    values = asdict(retention)
    for table, partition_column, order_column, retention_key in RANKED_TABLES:
        delete_rows_after_rank(
            con,
            table,
            partition_column,
            order_column,
            values[retention_key],
        )

    con.execute(
        """
        delete from holdings
        where rowid in (
            select rowid
            from (
                select rowid,
                       dense_rank() over (
                           partition by fund_code
                           order by report_date desc
                       ) as period_rank
                from holdings
            )
            where period_rank > ?
        )
        """,
        (retention.holding_periods_per_fund,),
    )

    # Detailed fetch errors can be large and are machine-specific. Keep the
    # successful refresh timestamps and compact statistics used by the UI.
    con.execute(
        """
        delete from metadata
        where key like 'last_import_fund_%'
           or key like '%diagnostic%'
           or key like '%unresolved%'
           or key like '%_errors'
           or key in ('pending_config_hash', 'schema_repair_issues')
        """
    )
    set_meta(con, "pending_report_backtests", {})
    set_meta(con, "seed_format_version", 1)
    set_meta(con, "seed_generated_at", datetime.now(timezone.utc).isoformat())
    set_meta(con, "seed_retention", values)


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "funds",
        "navs",
        "holdings",
        "quotes",
        "daily_prices",
        "mark_prices",
        "fund_prices",
        "backtests",
        "fund_announcements",
        "fund_purchase_limits",
    )
    return {
        table: int(con.execute(f"select count(*) from {table}").fetchone()[0])
        for table in tables
    }


def create_seed_database(
    source_path: Path,
    output_path: Path,
    retention: SeedRetention,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    source_path = source_path.resolve()
    output_path = output_path.resolve()
    if source_path == output_path:
        raise ValueError("source and output database paths must differ")
    if not source_path.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_path}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"output already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source_path.as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    source.row_factory = sqlite3.Row
    temporary_path: Path | None = None
    try:
        source_status = database_readiness(source)
        if not source_status["ready"]:
            raise RuntimeError(
                "source database is not release-ready: "
                + json.dumps(source_status, ensure_ascii=False)
            )
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        target = sqlite3.connect(temporary_path)
        target.row_factory = sqlite3.Row
        try:
            source.backup(target)
            trim_seed_database(target, retention)
            target.commit()
            target.execute("pragma journal_mode = delete")
            target.execute("vacuum")
            target_status = database_readiness(target)
            integrity = target.execute("pragma integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"seed database integrity check failed: {integrity}")
            if not target_status["ready"]:
                raise RuntimeError(
                    "trimmed seed database is not release-ready: "
                    + json.dumps(target_status, ensure_ascii=False)
                )
            result = {
                "output": str(output_path),
                "bytes": temporary_path.stat().st_size,
                "latest_nav_date": target.execute("select max(date) from navs").fetchone()[0],
                "counts": table_counts(target),
                "config_hash": get_meta(target, "config_hash"),
                "retention": asdict(retention),
            }
        except Exception:
            target.close()
            raise
        else:
            target.close()
        os.replace(temporary_path, output_path)
        temporary_path = None
        return result
    finally:
        source.close()
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a compact, release-ready database for the Windows portable build."
    )
    parser.add_argument("--source", type=Path, default=db_path())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nav-rows", type=positive_int, default=200)
    parser.add_argument("--holding-periods", type=positive_int, default=5)
    parser.add_argument("--price-rows", type=positive_int, default=160)
    parser.add_argument(
        "--backtest-rows",
        type=positive_int,
        default=DEFAULT_BACKTEST_DAYS,
        help=f"backtest rows per fund to retain (default: {DEFAULT_BACKTEST_DAYS})",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = create_seed_database(
            args.source,
            args.output,
            SeedRetention(
                nav_rows_per_fund=args.nav_rows,
                holding_periods_per_fund=args.holding_periods,
                price_rows_per_instrument=args.price_rows,
                backtest_rows_per_fund=args.backtest_rows,
            ),
            overwrite=args.force,
        )
    except (FileNotFoundError, FileExistsError, RuntimeError, ValueError) as exc:
        print(f"seed database build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
