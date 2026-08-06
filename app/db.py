from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH, FUND_RULES_PATH, FUNDS


SCHEMA = """
create table if not exists funds (
    code text primary key,
    name text not null,
    exchange_market integer not null,
    fund_type text not null default '其他',
    note text default '',
    updated_at text not null
);

create table if not exists navs (
    fund_code text not null,
    date text not null,
    nav real not null,
    distribution real default 0,
    return_pct real,
    primary key (fund_code, date)
);

create table if not exists holdings (
    fund_code text not null,
    report_date text not null,
    publish_date text,
    secid text not null,
    symbol text not null,
    name text not null,
    weight real not null check(weight > 0),
    source text not null,
    primary key (fund_code, report_date, secid, source)
);

create table if not exists quotes (
    secid text primary key,
    symbol text not null,
    market integer not null,
    name text not null,
    price real,
    pct real,
    previous_close real,
    quote_time text,
    session_date text,
    last_attempt_at text,
    last_success_at text,
    fetch_status text not null default 'ok',
    updated_at text not null
);

create table if not exists daily_prices (
    secid text not null,
    date text not null,
    close real not null check(close > 0),
    pct real,
    source text not null default 'unknown',
    adjustment text not null default 'unknown',
    updated_at text,
    primary key (secid, date)
);

create table if not exists mark_prices (
    secid text not null,
    date text not null,
    close real not null check(close > 0),
    source text not null,
    primary key (secid, date, source)
);

create table if not exists fund_prices (
    fund_code text not null,
    date text not null,
    close real not null,
    pct real,
    primary key (fund_code, date)
);

create table if not exists backtests (
    fund_code text not null,
    date text not null,
    previous_date text not null,
    previous_nav real not null,
    actual_nav real not null,
    estimated_nav real not null,
    error_pct real not null,
    covered_weight real not null,
    modeled_weight real not null default 0,
    priced_ratio real not null default 0,
    data_quality text not null default 'ok',
    primary key (fund_code, date)
);

create table if not exists metadata (
    key text primary key,
    value text not null
);

create table if not exists fund_announcements (
    fund_code text primary key,
    title text not null,
    publish_date text not null,
    announcement_id text not null,
    url text not null,
    updated_at text not null
);

create table if not exists fund_purchase_limits (
    fund_code text primary key,
    purchase_status text not null,
    redeem_status text,
    next_open_date text,
    min_purchase_amount real,
    max_purchase_amount real,
    display text not null,
    sort_value real,
    source_date text,
    updated_at text not null
);
"""


SCHEMA_CONTRACT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "funds": (
        ("code", "name", "exchange_market", "fund_type", "note", "updated_at"),
        ("code",),
    ),
    "navs": (
        ("fund_code", "date", "nav", "distribution", "return_pct"),
        ("fund_code", "date"),
    ),
    "holdings": (
        (
            "fund_code",
            "report_date",
            "publish_date",
            "secid",
            "symbol",
            "name",
            "weight",
            "source",
        ),
        ("fund_code", "report_date", "secid", "source"),
    ),
    "quotes": (
        (
            "secid",
            "symbol",
            "market",
            "name",
            "price",
            "pct",
            "previous_close",
            "quote_time",
            "session_date",
            "last_attempt_at",
            "last_success_at",
            "fetch_status",
            "updated_at",
        ),
        ("secid",),
    ),
    "daily_prices": (
        ("secid", "date", "close", "pct", "source", "adjustment", "updated_at"),
        ("secid", "date"),
    ),
    "mark_prices": (
        ("secid", "date", "close", "source"),
        ("secid", "date", "source"),
    ),
    "fund_prices": (
        ("fund_code", "date", "close", "pct"),
        ("fund_code", "date"),
    ),
    "backtests": (
        (
            "fund_code",
            "date",
            "previous_date",
            "previous_nav",
            "actual_nav",
            "estimated_nav",
            "error_pct",
            "covered_weight",
            "modeled_weight",
            "priced_ratio",
            "data_quality",
        ),
        ("fund_code", "date"),
    ),
    "metadata": (("key", "value"), ("key",)),
    "fund_announcements": (
        ("fund_code", "title", "publish_date", "announcement_id", "url", "updated_at"),
        ("fund_code",),
    ),
    "fund_purchase_limits": (
        (
            "fund_code",
            "purchase_status",
            "redeem_status",
            "next_open_date",
            "min_purchase_amount",
            "max_purchase_amount",
            "display",
            "sort_value",
            "source_date",
            "updated_at",
        ),
        ("fund_code",),
    ),
}


# Bump this whenever persisted backtest inputs or calculation semantics change.
# A stale derived cache is less safe than an empty cache: it will be rebuilt by
# the normal full/incremental refresh paths.
BACKTEST_CACHE_VERSION = 5
HISTORICAL_PRICE_CACHE_VERSION = 3
SCHEMA_VERSION = 1


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect() as con:
        require_supported_schema_version(con)
        existing_tables = {
            row[0]
            for row in con.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        repaired_contract_issues = (
            schema_contract_issues(con)
            if existing_tables.intersection(SCHEMA_CONTRACT)
            else []
        )
        con.execute("pragma journal_mode = wal")
        con.executescript(SCHEMA)
        _ensure_column(con, "holdings", "publish_date", "text")
        _ensure_column(con, "navs", "distribution", "real default 0")
        _ensure_column(con, "funds", "fund_type", "text not null default '其他'")
        _ensure_column(con, "backtests", "data_quality", "text not null default 'ok'")
        _ensure_column(con, "backtests", "modeled_weight", "real not null default 0")
        _ensure_column(con, "backtests", "priced_ratio", "real not null default 0")
        _ensure_column(con, "quotes", "session_date", "text")
        _ensure_column(con, "quotes", "last_attempt_at", "text")
        _ensure_column(con, "quotes", "last_success_at", "text")
        _ensure_column(con, "quotes", "fetch_status", "text not null default 'ok'")
        _ensure_column(con, "daily_prices", "source", "text not null default 'unknown'")
        _ensure_column(
            con,
            "daily_prices",
            "adjustment",
            "text not null default 'unknown'",
        )
        _ensure_column(con, "daily_prices", "updated_at", "text")
        issues = schema_contract_issues(con)
        if issues:
            raise RuntimeError(f"database schema contract failed: {'; '.join(issues)}")
        ensure_cache_versions(con)
        con.execute(f"pragma user_version = {SCHEMA_VERSION}")
        set_meta(con, "schema_version", SCHEMA_VERSION)
        ensure_config_state(con)
        if repaired_contract_issues:
            # CREATE IF NOT EXISTS / additive column repairs can restore the
            # shape, but they cannot certify that the missing cache contents
            # were rebuilt. Force the normal build workflow before serving.
            set_meta(con, "schema_repair_issues", repaired_contract_issues)
            set_meta(con, "build_state", "schema_changed")


def _ensure_column(con: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = [row["name"] for row in con.execute(f"pragma table_info({table})")]
    if column not in columns:
        con.execute(f"alter table {table} add column {column} {column_type}")


def require_supported_schema_version(con: sqlite3.Connection) -> int:
    version = int(con.execute("pragma user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than supported version {SCHEMA_VERSION}"
        )
    return version


def schema_contract_issues(con: sqlite3.Connection) -> list[str]:
    tables = {
        row[0]
        for row in con.execute(
            "select name from sqlite_master where type = 'table'"
        )
    }
    issues = []
    for table, (required_columns, expected_primary_key) in SCHEMA_CONTRACT.items():
        if table not in tables:
            issues.append(f"missing table: {table}")
            continue
        rows = con.execute(f"pragma table_info({table})").fetchall()
        available = {row[1] for row in rows}
        missing_columns = [column for column in required_columns if column not in available]
        if missing_columns:
            issues.append(f"{table} missing columns: {', '.join(missing_columns)}")
        actual_primary_key = tuple(
            row[1]
            for row in sorted(
                (row for row in rows if row[5]),
                key=lambda row: row[5],
            )
        )
        if actual_primary_key != expected_primary_key:
            issues.append(
                f"{table} primary key: expected {expected_primary_key}, got {actual_primary_key}"
            )
    return issues


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("pragma busy_timeout = 30000")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def set_meta(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "insert or replace into metadata(key, value) values (?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_meta(con: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = con.execute("select value from metadata where key = ?", (key,)).fetchone()
    if not row:
        return default
    return json.loads(row["value"])


def ensure_cache_versions(con: sqlite3.Connection) -> None:
    if get_meta(con, "historical_price_cache_version") != HISTORICAL_PRICE_CACHE_VERSION:
        invalid_price = (
            "close is null or typeof(close) not in ('integer', 'real') "
            "or close <= 0 or close > 1e100 or close != close"
        )
        con.execute(f"delete from daily_prices where {invalid_price}")
        con.execute(f"delete from mark_prices where {invalid_price}")
        con.execute("update daily_prices set pct = null where pct != pct or abs(pct) > 1000")
        con.execute("delete from daily_prices where source = 'unknown'")
        con.execute("delete from backtests")
        set_meta(con, "historical_price_cache_version", HISTORICAL_PRICE_CACHE_VERSION)
    if get_meta(con, "backtest_cache_version") == BACKTEST_CACHE_VERSION:
        return
    con.execute("delete from backtests")
    set_meta(con, "backtest_cache_version", BACKTEST_CACHE_VERSION)


def current_config_hash() -> str:
    with FUND_RULES_PATH.open("r", encoding="utf-8") as file:
        rules = json.load(file)
    canonical = json.dumps(rules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def ensure_config_state(con: sqlite3.Connection) -> None:
    expected_hash = current_config_hash()
    stored_hash = get_meta(con, "config_hash")
    build_state = get_meta(con, "build_state")
    has_cached_core = bool(
        con.execute(
            """
            select exists(select 1 from funds)
                or exists(select 1 from navs)
                or exists(select 1 from holdings)
            """
        ).fetchone()[0]
    )
    if stored_hash is None:
        # A pre-marker database must never be silently certified against the
        # current rules.  It may contain complete-looking rows produced by an
        # older schema or valuation model.
        set_meta(con, "pending_config_hash", expected_hash)
        if build_state is None:
            set_meta(con, "build_state", "legacy" if has_cached_core else "empty")
        return
    if stored_hash != expected_hash:
        set_meta(con, "pending_config_hash", expected_hash)
        con.execute("delete from backtests")
        if build_state != "running":
            set_meta(con, "build_state", "config_changed")
        return
    con.execute("delete from metadata where key = 'pending_config_hash'")
    if build_state is None:
        set_meta(con, "build_state", "legacy" if has_cached_core else "empty")


def missing_core_data_codes(con: sqlite3.Connection) -> dict[str, list[str]]:
    configured = set(FUNDS)
    fund_codes = {row[0] for row in con.execute("select code from funds")}
    nav_codes = {row[0] for row in con.execute("select distinct fund_code from navs")}
    holding_codes = {
        row[0]
        for row in con.execute("select distinct fund_code from holdings where weight > 0")
    }
    return {
        "funds": sorted(configured - fund_codes),
        "navs": sorted(configured - nav_codes),
        "holdings": sorted(configured - holding_codes),
    }


def database_readiness(
    con: sqlite3.Connection,
    *,
    require_backtests: bool = False,
    expected_config_hash: str | None = None,
) -> dict[str, Any]:
    schema_version = con.execute("pragma user_version").fetchone()[0]
    expected_hash = expected_config_hash or current_config_hash()
    schema_issues = schema_contract_issues(con)
    if schema_version > SCHEMA_VERSION:
        schema_issues = [
            f"schema version {schema_version} is newer than supported version {SCHEMA_VERSION}",
            *schema_issues,
        ]
    if schema_issues:
        return {
            "ready": False,
            "schema_version": schema_version,
            "expected_schema_version": SCHEMA_VERSION,
            "schema_issues": schema_issues,
            "config_hash_matches": False,
            "pending_config_hash": None,
            "build_state": "schema_incompatible",
            "missing": {},
            "missing_backtests": [],
        }
    stored_hash = get_meta(con, "config_hash")
    pending_hash = get_meta(con, "pending_config_hash")
    missing = missing_core_data_codes(con)
    build_state = get_meta(con, "build_state", "empty")
    missing_backtests = []
    if require_backtests:
        backtest_codes = {
            row[0]
            for row in con.execute("select distinct fund_code from backtests")
        }
        missing_backtests = sorted(set(FUNDS) - backtest_codes)
    ready = (
        schema_version == SCHEMA_VERSION
        and stored_hash == expected_hash
        and pending_hash is None
        and build_state == "complete"
        and not any(missing.values())
        and not missing_backtests
    )
    return {
        "ready": ready,
        "schema_version": schema_version,
        "expected_schema_version": SCHEMA_VERSION,
        "schema_issues": [],
        "config_hash_matches": stored_hash == expected_hash,
        "pending_config_hash": pending_hash,
        "build_state": build_state,
        "missing": missing,
        "missing_backtests": missing_backtests,
    }


def database_is_ready(con: sqlite3.Connection, *, require_backtests: bool = False) -> bool:
    return bool(database_readiness(con, require_backtests=require_backtests)["ready"])


def mark_build_started(con: sqlite3.Connection, mode: str, started_at: str) -> None:
    set_meta(con, "build_state", "running")
    set_meta(con, "build_mode", mode)
    set_meta(con, "build_started_at", started_at)


def mark_build_finished(
    con: sqlite3.Connection,
    *,
    mode: str,
    completed_at: str,
    import_failed: list[dict[str, Any]],
    backtests_failed: list[dict[str, Any]],
) -> None:
    complete = not import_failed and (mode != "full" or not backtests_failed)
    set_meta(con, "build_state", "complete" if complete else "partial")
    set_meta(con, "build_mode", mode)
    set_meta(con, "build_completed_at", completed_at)
    set_meta(
        con,
        "build_error_counts",
        {"imports": len(import_failed), "backtests": len(backtests_failed)},
    )
    if complete:
        set_meta(con, "config_hash", current_config_hash())
        con.execute("delete from metadata where key = 'pending_config_hash'")
