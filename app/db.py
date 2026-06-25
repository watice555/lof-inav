from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DATA_DIR, DB_PATH


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
    weight real not null,
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
    updated_at text not null
);

create table if not exists daily_prices (
    secid text not null,
    date text not null,
    close real not null,
    pct real,
    primary key (secid, date)
);

create table if not exists mark_prices (
    secid text not null,
    date text not null,
    close real not null,
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


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect() as con:
        con.execute("pragma journal_mode = wal")
        con.executescript(SCHEMA)
        _ensure_column(con, "holdings", "publish_date", "text")
        _ensure_column(con, "navs", "distribution", "real default 0")
        _ensure_column(con, "funds", "fund_type", "text not null default '其他'")
        _ensure_column(con, "backtests", "data_quality", "text not null default 'ok'")


def _ensure_column(con: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = [row["name"] for row in con.execute(f"pragma table_info({table})")]
    if column not in columns:
        con.execute(f"alter table {table} add column {column} {column_type}")


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
