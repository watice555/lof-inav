from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

from .config import FUNDS, FX_MIDPOINT_SECIDS, FX_SECID_OVERRIDES, US_EQUITY_CLOSE_MARKS
from .market_calendar import (
    expected_market_closure_gap,
    historical_price_market,
    previous_trading_session,
)
from .sources import quote_session_date


MIN_BACKTEST_PRICED_RATIO = 0.6
BACKTEST_OUTLIER_ABS_ERROR = 0.2
REALTIME_QUOTE_MAX_AGE_SECONDS = 15 * 60
SQLITE_BATCH_SIZE = 400


@dataclass
class IntradayPrefetch:
    """Request-local rows used by the all-funds valuation endpoint."""

    funds: dict[str, sqlite3.Row]
    latest_navs: dict[str, sqlite3.Row]
    quotes: dict[str, sqlite3.Row]
    announcements: dict[str, dict[str, Any]]
    purchase_limits: dict[str, dict[str, Any]]
    holdings: dict[str, list[sqlite3.Row]]
    base_prices: dict[tuple[str, str], float | None]


def prefetch_intraday_inputs(
    con: sqlite3.Connection, codes: list[str] | tuple[str, ...]
) -> IntradayPrefetch:
    """Load all inputs for a funds response in bounded SQLite batches.

    The returned object belongs to one request/connection; no mutable state is
    shared between HTTP worker threads.
    """

    unique_codes = list(dict.fromkeys(codes))
    funds: dict[str, sqlite3.Row] = {}
    latest_navs: dict[str, sqlite3.Row] = {}
    announcements: dict[str, dict[str, Any]] = {}
    purchase_limits: dict[str, dict[str, Any]] = {}
    holdings: dict[str, list[sqlite3.Row]] = {code: [] for code in unique_codes}
    for batch in _batched(unique_codes):
        placeholders = ",".join("?" for _ in batch)
        funds.update(
            (row["code"], row)
            for row in con.execute(
                f"select * from funds where code in ({placeholders})", batch
            )
        )
        latest_navs.update(
            (row["fund_code"], row)
            for row in con.execute(
                f"""
                with targets(code) as (values {",".join("(?)" for _ in batch)})
                select n.* from targets t
                join navs n
                  on n.fund_code = t.code
                 and n.date = (
                     select max(candidate.date) from navs candidate
                     where candidate.fund_code = t.code
                 )
                """,
                batch,
            )
        )
        announcements.update(
            (
                row["fund_code"],
                {
                    key: row[key]
                    for key in ("title", "publish_date", "announcement_id", "url")
                },
            )
            for row in con.execute(
                f"""
                select title, publish_date, announcement_id, url, fund_code
                from fund_announcements where fund_code in ({placeholders})
                """,
                batch,
            )
        )
        purchase_limits.update(
            (
                row["fund_code"],
                {
                    key: row[key]
                    for key in (
                        "purchase_status",
                        "redeem_status",
                        "next_open_date",
                        "min_purchase_amount",
                        "max_purchase_amount",
                        "display",
                        "sort_value",
                        "source_date",
                        "updated_at",
                    )
                },
            )
            for row in con.execute(
                f"""
                select fund_code, purchase_status, redeem_status, next_open_date,
                       min_purchase_amount, max_purchase_amount, display, sort_value,
                       source_date, updated_at
                from fund_purchase_limits where fund_code in ({placeholders})
                """,
                batch,
            )
        )
        for row in con.execute(
            f"""
            with targets(code) as (values {",".join("(?)" for _ in batch)})
            select h.* from targets t
            cross join holdings h indexed by sqlite_autoindex_holdings_1
              on h.fund_code = t.code
             and h.report_date = (
                 select max(candidate.report_date) from holdings candidate
                 where candidate.fund_code = t.code and candidate.weight > 0
             )
            where h.weight > 0
            order by h.fund_code, h.weight desc
            """,
            batch,
        ):
            holdings[row["fund_code"]].append(row)

    quote_secids = {
        f"{fund['exchange_market']}.{code}" for code, fund in funds.items()
    }
    base_candidates: set[tuple[str, str]] = set()
    for code, rows in holdings.items():
        latest_nav = latest_navs.get(code)
        base_date = latest_nav["date"] if latest_nav else None
        for row in rows:
            secid = row["secid"]
            quote_secids.add(secid)
            fx_secid = fx_secid_for_asset(secid)
            if fx_secid:
                quote_secids.add(fx_secid)
            if base_date:
                base_candidates.add((secid, base_date))
                if fx_secid:
                    base_candidates.add((fx_secid, base_date))

    quotes: dict[str, sqlite3.Row] = {}
    for batch in _batched(sorted(quote_secids)):
        placeholders = ",".join("?" for _ in batch)
        quotes.update(
            (row["secid"], row)
            for row in con.execute(
                f"select * from quotes where secid in ({placeholders})", batch
            )
        )

    base_targets = {
        (secid, base_date)
        for secid, base_date in base_candidates
        if (quote := quotes.get(secid))
        and _quote_needs_base_price(secid, quote, base_date)
    }
    base_prices: dict[tuple[str, str], float | None] = {}
    targets_by_date: dict[str, list[str]] = {}
    for secid, base_date in sorted(base_targets):
        targets_by_date.setdefault(base_date, []).append(secid)
    for base_date, secids in targets_by_date.items():
        for batch in _batched(secids):
            placeholders = ",".join("?" for _ in batch)
            for secid in batch:
                base_prices[(secid, base_date)] = None
            rows = con.execute(
                f"""
                select p.secid, p.date as price_date, p.close
                from daily_prices p
                join (
                    select secid, max(date) as date
                    from daily_prices
                    where secid in ({placeholders})
                      and date <= ?
                      and close > 0
                    group by secid
                ) latest on latest.secid = p.secid and latest.date = p.date
                """,
                [*batch, base_date],
            ).fetchall()
            for row in rows:
                price_date = row["price_date"]
                if price_date == base_date or expected_market_closure_gap(
                    row["secid"], base_date, price_date
                ):
                    base_prices[(row["secid"], base_date)] = row["close"]

    return IntradayPrefetch(
        funds=funds,
        latest_navs=latest_navs,
        quotes=quotes,
        announcements=announcements,
        purchase_limits=purchase_limits,
        holdings=holdings,
        base_prices=base_prices,
    )


def _batched(values: list[Any], size: int = SQLITE_BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def estimate_intraday(
    con: sqlite3.Connection, code: str, prefetch: IntradayPrefetch | None = None
) -> dict[str, Any]:
    fund = (
        prefetch.funds.get(code)
        if prefetch is not None
        else con.execute("select * from funds where code = ?", (code,)).fetchone()
    )
    if not fund:
        raise KeyError(code)
    latest_nav = (
        prefetch.latest_navs.get(code)
        if prefetch is not None
        else con.execute(
            "select * from navs where fund_code = ? order by date desc limit 1", (code,)
        ).fetchone()
    )
    trade_secid = f"{fund['exchange_market']}.{code}"
    if prefetch is not None:
        quote = prefetch.quotes.get(trade_secid)
        announcement = prefetch.announcements.get(code)
        purchase_limit = prefetch.purchase_limits.get(code)
        holdings = prefetch.holdings.get(code, [])
    else:
        quote = con.execute("select * from quotes where secid = ?", (trade_secid,)).fetchone()
        announcement = con.execute(
            "select title, publish_date, announcement_id, url from fund_announcements where fund_code = ?", (code,)
        ).fetchone()
        purchase_limit = con.execute(
            """
            select purchase_status, redeem_status, next_open_date, min_purchase_amount,
                   max_purchase_amount, display, sort_value, source_date, updated_at
            from fund_purchase_limits
            where fund_code = ?
            """,
            (code,),
        ).fetchone()
        holdings = latest_holdings(con, code)
    latest_nav_date = latest_nav["date"] if latest_nav else None
    weighted_return, modeled_weight, priced_weight, missing, realtime_warnings = realtime_weighted_return(
        con, holdings, latest_nav_date, prefetch=prefetch
    )
    estimate = latest_nav["nav"] * (1 + weighted_return) if latest_nav else None
    trade_price = quote["price"] if quote and realtime_quote_is_usable(quote, trade_secid) else None
    if trade_price is not None and trade_price <= 0:
        trade_price = None
    premium = trade_price / estimate - 1 if estimate and trade_price else None
    return {
        "code": code,
        "name": fund["name"],
        "type": FUNDS[code].fund_type,
        "trade_secid": trade_secid,
        "previous_nav": latest_nav["nav"] if latest_nav else None,
        "nav_date": latest_nav["date"] if latest_nav else None,
        "trade_price": trade_price,
        "trade_pct": quote["pct"] if trade_price is not None else None,
        "estimated_nav": estimate,
        "premium": premium,
        "covered_weight": priced_weight,
        "modeled_weight": modeled_weight,
        "priced_weight": priced_weight,
        "priced_ratio": priced_weight / modeled_weight if modeled_weight > 0 else None,
        "unmodeled_weight": max(0.0, 1 - modeled_weight),
        "unpriced_weight": max(0.0, modeled_weight - priced_weight),
        "missing_weight": max(0.0, modeled_weight - priced_weight),
        "missing_quotes": missing,
        "realtime_warnings": realtime_warnings,
        "note": fund["note"],
        "quote_time": quote["quote_time"] if quote else None,
        "announcement": dict(announcement) if announcement else None,
        "purchase_limit": dict(purchase_limit) if purchase_limit else None,
    }


def latest_holdings(con: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    date_row = con.execute(
        "select max(report_date) as report_date from holdings where fund_code = ? and weight > 0", (code,)
    ).fetchone()
    if not date_row or not date_row["report_date"]:
        return []
    return con.execute(
        """
        select * from holdings
        where fund_code = ? and report_date = ? and weight > 0
        order by weight desc
        """,
        (code, date_row["report_date"]),
    ).fetchall()


def realtime_weighted_return(
    con: sqlite3.Connection,
    holdings: list[sqlite3.Row],
    base_date: str | None = None,
    prefetch: IntradayPrefetch | None = None,
) -> tuple[float, float, float, list[str], list[dict[str, Any]]]:
    total = 0.0
    modeled = 0.0
    priced = 0.0
    missing = []
    warnings = []
    for holding in holdings:
        modeled += holding["weight"]
        asset_ret, asset_warning = realtime_asset_return_with_warning(
            con, holding["secid"], base_date, prefetch=prefetch
        )
        if asset_warning:
            warnings.append(_holding_warning(holding, "asset", asset_warning))
        if asset_ret is None:
            missing.append(holding["secid"])
            continue
        fx_ret, fx_warning = realtime_fx_return_with_warning(
            con, holding["secid"], base_date, prefetch=prefetch
        )
        if fx_warning:
            warnings.append(_holding_warning(holding, "fx", fx_warning))
        if fx_ret is None:
            continue
        total += holding["weight"] * ((1 + asset_ret) * (1 + fx_ret) - 1)
        priced += holding["weight"]
    return total, modeled, priced, missing, warnings


def realtime_asset_return(con: sqlite3.Connection, secid: str, base_date: str | None = None) -> float | None:
    asset_ret, _warning = realtime_asset_return_with_warning(con, secid, base_date)
    return asset_ret


def realtime_asset_return_with_warning(
    con: sqlite3.Connection,
    secid: str,
    base_date: str | None = None,
    prefetch: IntradayPrefetch | None = None,
) -> tuple[float | None, dict[str, Any] | None]:
    quote = (
        prefetch.quotes.get(secid)
        if prefetch is not None
        else con.execute("select * from quotes where secid = ?", (secid,)).fetchone()
    )
    if not quote:
        return None, {
            "type": "quote_missing",
            "secid": secid,
            "base_date": base_date,
            "message": "资产实时行情缺失",
        }
    return realtime_quote_return_with_warning(
        con, secid, quote, base_date, prefetch=prefetch
    )


def realtime_fx_return(con: sqlite3.Connection, secid: str, base_date: str | None = None) -> float | None:
    fx_ret, _warning = realtime_fx_return_with_warning(con, secid, base_date)
    return fx_ret


def realtime_fx_return_with_warning(
    con: sqlite3.Connection,
    secid: str,
    base_date: str | None = None,
    prefetch: IntradayPrefetch | None = None,
) -> tuple[float | None, dict[str, Any] | None]:
    fx_secid = fx_secid_for_asset(secid)
    if not fx_secid:
        return 0.0, None
    quote = (
        prefetch.quotes.get(fx_secid)
        if prefetch is not None
        else con.execute("select * from quotes where secid = ?", (fx_secid,)).fetchone()
    )
    if not quote:
        return None, {
            "type": "fx_quote_missing",
            "secid": fx_secid,
            "asset_secid": secid,
            "base_date": base_date,
            "message": "汇率实时行情缺失，该仓位未计价",
        }
    fx_ret, warning = realtime_quote_return_with_warning(
        con, fx_secid, quote, base_date, prefetch=prefetch
    )
    if fx_ret is not None:
        return fx_ret, warning
    if warning:
        warning = {**warning, "message": f"{warning['message']}，该仓位未计价"}
    else:
        warning = {
            "type": "fx_return_missing",
            "secid": fx_secid,
            "asset_secid": secid,
            "base_date": base_date,
            "message": "汇率收益无法计算，该仓位未计价",
        }
    return None, warning


def realtime_quote_return(
    con: sqlite3.Connection, secid: str, quote: sqlite3.Row, base_date: str | None = None
) -> float | None:
    quote_ret, _warning = realtime_quote_return_with_warning(con, secid, quote, base_date)
    return quote_ret


def realtime_quote_return_with_warning(
    con: sqlite3.Connection,
    secid: str,
    quote: sqlite3.Row,
    base_date: str | None = None,
    prefetch: IntradayPrefetch | None = None,
) -> tuple[float | None, dict[str, Any] | None]:
    cache_warning = realtime_quote_cache_warning(quote, secid)
    if cache_warning:
        return None, cache_warning
    price = _positive_float(quote["price"])
    previous_close = _positive_float(quote["previous_close"])
    fallback_return, fallback_source = _quote_pct_return_with_source(price, previous_close, quote["pct"])
    if not base_date:
        return fallback_return, _quote_warning(
            "no_base_date",
            secid,
            quote,
            base_date,
            price,
            previous_close,
            fallback_return,
            fallback_source,
            "缺少最新净值日期，使用行情涨跌幅",
        )

    quote_date = _quote_date(secid, quote)
    try:
        base_day = datetime.fromisoformat(base_date).date()
    except ValueError:
        base_day = None
    if quote_date and base_day and quote_date == base_day:
        return 0.0, None
    if quote_date and base_day and quote_date < base_day:
        if expected_market_closure_gap(secid, base_date, quote_date.isoformat()):
            return 0.0, None
        return None, _quote_warning(
            "quote_before_base",
            secid,
            quote,
            base_date,
            price,
            previous_close,
            None,
            None,
            "行情交易日早于最新净值日",
        )
    market = historical_price_market(secid)
    previous_session = (
        previous_trading_session(market, quote_date)
        if quote_date and market
        else (_previous_business_day(quote_date) if quote_date else None)
    )
    if (
        quote_date
        and base_day
        and price is not None
        and previous_close is not None
        and previous_session == base_day
    ):
        return price / previous_close - 1, None

    if price is not None:
        base_price = _realtime_base_price(con, secid, base_date, prefetch=prefetch)
        if base_price:
            return price / base_price - 1, None
    if quote_date and base_day and quote_date > base_day:
        return None, _quote_warning(
            "base_price_missing",
            secid,
            quote,
            base_date,
            price,
            previous_close,
            None,
            None,
            "行情已晚于最新净值日，但缺少同口径基准价",
        )
    return fallback_return, _quote_warning(
        "date_unavailable",
        secid,
        quote,
        base_date,
        price,
        previous_close,
        fallback_return,
        fallback_source,
        "无法判断行情日期或净值日期，使用行情涨跌幅",
    )


def _quote_pct_return(price: float | None, previous_close: float | None, pct: Any) -> float | None:
    quote_ret, _source = _quote_pct_return_with_source(price, previous_close, pct)
    return quote_ret


def _quote_pct_return_with_source(
    price: float | None, previous_close: float | None, pct: Any
) -> tuple[float | None, str | None]:
    if pct is not None:
        return pct / 100, "quote_pct"
    if price is not None and previous_close is not None:
        return price / previous_close - 1, "price_previous_close"
    return None, None


def _holding_warning(holding: sqlite3.Row, kind: str, warning: dict[str, Any]) -> dict[str, Any]:
    return {
        **warning,
        "kind": kind,
        "holding_secid": holding["secid"],
        "holding_name": holding["name"],
        "holding_weight": holding["weight"],
    }


def _quote_warning(
    warning_type: str,
    secid: str,
    quote: sqlite3.Row,
    base_date: str | None,
    price: float | None,
    previous_close: float | None,
    fallback_return: float | None,
    fallback_source: str | None,
    message: str,
) -> dict[str, Any]:
    return {
        "type": warning_type,
        "secid": secid,
        "base_date": base_date,
        "quote_time": quote["quote_time"],
        "price": price,
        "previous_close": previous_close,
        "pct": quote["pct"],
        "fallback_return": fallback_return,
        "fallback_source": fallback_source,
        "message": message,
    }


def _positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 0 and math.isfinite(number):
        return number
    return None


def realtime_quote_is_usable(
    quote: sqlite3.Row | dict[str, Any], secid: str, now: datetime | None = None
) -> bool:
    return realtime_quote_cache_warning(quote, secid, now=now) is None


def realtime_quote_cache_warning(
    quote: sqlite3.Row | dict[str, Any], secid: str, now: datetime | None = None
) -> dict[str, Any] | None:
    status = _row_value(quote, "fetch_status") or "ok"
    if status != "ok":
        return {
            "type": "quote_refresh_missing",
            "secid": secid,
            "fetch_status": status,
            "last_attempt_at": _row_value(quote, "last_attempt_at"),
            "last_success_at": _row_value(quote, "last_success_at"),
            "message": "最近一次行情刷新未返回该标的",
        }
    success_at = _row_value(quote, "last_success_at") or _row_value(quote, "updated_at")
    if not success_at:
        return {
            "type": "quote_success_time_missing",
            "secid": secid,
            "message": "行情缺少成功抓取时间",
        }
    try:
        success_time = datetime.fromisoformat(str(success_at))
    except ValueError:
        return {
            "type": "quote_success_time_invalid",
            "secid": secid,
            "last_success_at": success_at,
            "message": "行情成功抓取时间无效",
        }
    if success_time.tzinfo is None:
        success_time = success_time.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = (now.astimezone(timezone.utc) - success_time.astimezone(timezone.utc)).total_seconds()
    if age_seconds > REALTIME_QUOTE_MAX_AGE_SECONDS:
        return {
            "type": "quote_cache_stale",
            "secid": secid,
            "last_success_at": success_at,
            "age_seconds": age_seconds,
            "message": "行情缓存超过 15 分钟未成功刷新",
        }
    return None


def _quote_needs_base_price(
    secid: str, quote: sqlite3.Row | dict[str, Any], base_date: str
) -> bool:
    """Mirror the fast exits before realtime quote valuation reads daily prices."""

    if realtime_quote_cache_warning(quote, secid):
        return False
    price = _positive_float(quote["price"])
    if price is None:
        return False
    quote_date = _quote_date(secid, quote)
    try:
        base_day = datetime.fromisoformat(base_date).date()
    except ValueError:
        base_day = None
    if quote_date and base_day and quote_date <= base_day:
        return False
    market = historical_price_market(secid)
    previous_session = (
        previous_trading_session(market, quote_date)
        if quote_date and market
        else (_previous_business_day(quote_date) if quote_date else None)
    )
    previous_close = _positive_float(quote["previous_close"])
    if quote_date and base_day and previous_close is not None and previous_session == base_day:
        return False
    return True


def _row_value(row: sqlite3.Row | dict[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def run_backtest(con: sqlite3.Connection, code: str, days: int = 30, lag_days: int = 25) -> list[dict[str, Any]]:
    navs = con.execute(
        "select * from navs where fund_code = ? order by date asc", (code,)
    ).fetchall()
    if len(navs) < 2:
        replace_backtest_rows_atomic(con, code, [], start_date=None)
        return []

    recent = navs[-days - 1 :]
    needed_secids = backtest_secids_for_nav_pairs(con, code, recent, None, lag_days)
    if not needed_secids:
        replace_backtest_rows_atomic(con, code, [], start_date=None)
        return []
    price_cache = {secid: _backtest_price_series(con, secid) for secid in needed_secids}
    results = []
    for prev, curr in zip(recent, recent[1:]):
        row = calculate_backtest_row(con, code, prev, curr, price_cache, lag_days)
        if not row:
            continue
        results.append(row)
    replace_backtest_rows_atomic(con, code, results, start_date=None)
    return results


def run_backtest_incremental(
    con: sqlite3.Connection,
    code: str,
    lag_days: int = 25,
    start_date: str | None = None,
) -> list[dict[str, Any]]:
    latest_backtest = con.execute(
        "select max(date) as date from backtests where fund_code = ?", (code,)
    ).fetchone()
    # A requested start date means recompute that whole window.  This is
    # intentionally different from append-only mode: late prices and corrected
    # NAVs must replace already-persisted derived rows.
    start_after = None if start_date else (latest_backtest["date"] if latest_backtest else None)
    anchor_date = incremental_backtest_anchor_date(con, code, start_after, start_date)
    if anchor_date:
        navs = con.execute(
            """
            select * from navs
            where fund_code = ?
              and date >= (
                select max(date) from navs where fund_code = ? and date <= ?
              )
            order by date asc
            """,
            (code, code, anchor_date),
        ).fetchall()
    else:
        navs = con.execute(
            "select * from navs where fund_code = ? order by date asc", (code,)
        ).fetchall()
    if len(navs) < 2:
        return []

    needed_secids = backtest_secids_for_nav_pairs(
        con,
        code,
        navs,
        start_after,
        lag_days,
        start_date=start_date,
    )
    if not needed_secids:
        if start_date:
            con.execute(
                "delete from backtests where fund_code = ? and date >= ?",
                (code, start_date),
            )
        return []
    price_cache = {secid: _backtest_price_series(con, secid) for secid in needed_secids}
    results = []
    for prev, curr in zip(navs, navs[1:]):
        if start_after and curr["date"] <= start_after:
            continue
        if start_date and curr["date"] < start_date:
            continue
        row = calculate_backtest_row(con, code, prev, curr, price_cache, lag_days)
        if not row:
            continue
        results.append(row)
    replace_backtest_rows_atomic(con, code, results, start_date=start_date)
    return results


def replace_backtest_rows_atomic(
    con: sqlite3.Connection,
    code: str,
    rows: list[dict[str, Any]],
    start_date: str | None,
) -> None:
    con.execute("savepoint replace_backtest_window")
    try:
        if start_date:
            con.execute(
                "delete from backtests where fund_code = ? and date >= ?",
                (code, start_date),
            )
        else:
            con.execute("delete from backtests where fund_code = ?", (code,))
        for row in rows:
            save_backtest_row(con, row)
    except Exception:
        con.execute("rollback to replace_backtest_window")
        con.execute("release replace_backtest_window")
        raise
    else:
        con.execute("release replace_backtest_window")


def backtest_secids_for_nav_pairs(
    con: sqlite3.Connection,
    code: str,
    navs: list[sqlite3.Row],
    start_after: str | None = None,
    lag_days: int = 25,
    start_date: str | None = None,
) -> set[str]:
    secids: set[str] = set()
    for prev, curr in zip(navs, navs[1:]):
        if start_after and curr["date"] <= start_after:
            continue
        if start_date and curr["date"] < start_date:
            continue
        for holding in holdings_available_on(con, code, prev["date"], lag_days):
            secids.add(holding["secid"])
    return secids


def incremental_backtest_anchor_date(
    con: sqlite3.Connection,
    code: str,
    latest_backtest_date: str | None,
    start_date: str | None,
) -> str | None:
    if start_date:
        row = con.execute(
            "select max(date) as date from navs where fund_code = ? and date < ?",
            (code, start_date),
        ).fetchone()
        return row["date"] if row and row["date"] else start_date
    return latest_backtest_date


def calculate_backtest_row(
    con: sqlite3.Connection,
    code: str,
    prev: sqlite3.Row,
    curr: sqlite3.Row,
    price_cache: dict[str, list[tuple[str, float]]],
    lag_days: int,
) -> dict[str, Any] | None:
    holdings = holdings_available_on(con, code, prev["date"], lag_days)
    if not holdings:
        return None
    weighted = 0.0
    modeled = sum(holding["weight"] for holding in holdings)
    covered = 0.0
    for holding in holdings:
        series = price_cache.get(holding["secid"], [])
        prev_close = _fresh_price_on_or_before(series, prev["date"], holding["secid"])
        curr_close = _fresh_price_on_or_before(series, curr["date"], holding["secid"])
        if not prev_close or not curr_close:
            continue
        asset_ret = curr_close / prev_close - 1
        fx_ret = historical_fx_return(con, holding["secid"], prev["date"], curr["date"], require_fresh=True)
        if fx_ret is None:
            continue
        weighted += holding["weight"] * ((1 + asset_ret) * (1 + fx_ret) - 1)
        covered += holding["weight"]
    if covered <= 0:
        return None
    estimated = prev["nav"] * (1 + weighted)
    actual_value = curr["nav"] + (curr["distribution"] or 0.0)
    error_pct = estimated / actual_value - 1 if actual_value else math.nan
    priced_ratio = covered / modeled if modeled > 0 else 0.0
    if priced_ratio < MIN_BACKTEST_PRICED_RATIO:
        data_quality = "low_coverage"
    elif not math.isfinite(error_pct) or abs(error_pct) >= BACKTEST_OUTLIER_ABS_ERROR:
        data_quality = "outlier"
    else:
        data_quality = "ok"
    return {
        "fund_code": code,
        "date": curr["date"],
        "previous_date": prev["date"],
        "previous_nav": prev["nav"],
        "actual_nav": actual_value,
        "estimated_nav": estimated,
        "error_pct": error_pct,
        "covered_weight": covered,
        "modeled_weight": modeled,
        "priced_ratio": priced_ratio,
        "data_quality": data_quality,
    }


def backtest_price_diagnostics(
    con: sqlite3.Connection,
    code: str,
    previous_date: str,
    current_date: str,
    lag_days: int = 25,
    price_lookup_cache: dict[tuple[str, str], tuple[str, float] | None] | None = None,
) -> dict[str, Any]:
    holdings = holdings_available_on(con, code, previous_date, lag_days)
    asset_stale_weight = 0.0
    asset_market_closed_weight = 0.0
    fx_stale_weight = 0.0
    missing_weight = 0.0
    asset_stale = []
    asset_market_closed = []
    fx_stale = []
    missing = []
    for holding in holdings:
        previous = _backtest_price_item_cached(con, holding["secid"], previous_date, price_lookup_cache)
        current = _backtest_price_item_cached(con, holding["secid"], current_date, price_lookup_cache)
        if not previous or not current:
            missing_weight += holding["weight"]
            missing.append(
                {
                    "secid": holding["secid"],
                    "name": holding["name"],
                    "weight": holding["weight"],
                    "reason": "asset_price",
                }
            )
            continue
        if previous[0] != previous_date or current[0] != current_date:
            stale_item = {
                "secid": holding["secid"],
                "name": holding["name"],
                "weight": holding["weight"],
                "previous_date": previous_date,
                "previous_price_date": previous[0],
                "current_date": current_date,
                "current_price_date": current[0],
            }
            closure_market = _expected_asset_closure_market(
                holding["secid"], previous_date, previous[0], current_date, current[0]
            )
            if closure_market:
                asset_market_closed_weight += holding["weight"]
                asset_market_closed.append({**stale_item, "market": closure_market})
            else:
                asset_stale_weight += holding["weight"]
                asset_stale.append(stale_item)
        fx_diag = _historical_fx_diagnostic(
            con,
            holding["secid"],
            previous_date,
            current_date,
            price_lookup_cache=price_lookup_cache,
        )
        if fx_diag["missing"]:
            missing_weight += holding["weight"]
            missing.append(
                {
                    "secid": holding["secid"],
                    "name": holding["name"],
                    "weight": holding["weight"],
                    "reason": "fx_price",
                    "fx_secid": fx_diag["fx_secid"],
                }
            )
        elif fx_diag["stale"]:
            fx_stale_weight += holding["weight"]
            fx_stale.append(
                {
                    "secid": holding["secid"],
                    "name": holding["name"],
                    "weight": holding["weight"],
                    **fx_diag,
                }
            )
    return {
        "asset_stale_weight": asset_stale_weight,
        "asset_market_closed_weight": asset_market_closed_weight,
        "fx_stale_weight": fx_stale_weight,
        "missing_weight": missing_weight,
        "asset_stale": asset_stale,
        "asset_market_closed": asset_market_closed,
        "fx_stale": fx_stale,
        "missing": missing,
    }


def _expected_asset_closure_market(
    secid: str,
    previous_date: str,
    previous_price_date: str,
    current_date: str,
    current_price_date: str,
) -> str | None:
    markets = []
    if previous_price_date != previous_date:
        market = expected_market_closure_gap(secid, previous_date, previous_price_date)
        if market is None:
            return None
        markets.append(market)
    if current_price_date != current_date:
        market = expected_market_closure_gap(secid, current_date, current_price_date)
        if market is None:
            return None
        markets.append(market)
    return markets[0] if markets and all(market == markets[0] for market in markets) else None


def save_backtest_row(con: sqlite3.Connection, row: dict[str, Any]) -> None:
    con.execute(
        """
        insert or replace into backtests
        (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav,
         error_pct, covered_weight, modeled_weight, priced_ratio, data_quality)
        values (:fund_code, :date, :previous_date, :previous_nav, :actual_nav, :estimated_nav,
                :error_pct, :covered_weight, :modeled_weight, :priced_ratio, :data_quality)
        """,
        row,
    )


def holdings_available_on(
    con: sqlite3.Connection, code: str, nav_date: str, lag_days: int = 25
) -> list[sqlite3.Row]:
    published = con.execute(
        """
        select max(report_date) as report_date
        from (
            select report_date
            from holdings
            where fund_code = ? and weight > 0
            group by report_date
            having count(*) = count(publish_date) and max(publish_date) <= ?
        )
        """,
        (code, nav_date),
    ).fetchone()
    if not published or not published["report_date"]:
        return []
    return con.execute(
        """
        select * from holdings
        where fund_code = ? and report_date = ? and weight > 0
        order by weight desc
        """,
        (code, published["report_date"]),
    ).fetchall()


def backtest_summary(con: sqlite3.Connection, code: str) -> dict[str, Any]:
    rows = con.execute(
        "select * from backtests where fund_code = ? order by date desc limit 30", (code,)
    ).fetchall()
    if not rows:
        return {"count": 0}
    usable_rows = [row for row in rows if row["data_quality"] == "ok"]
    abs_errors = [abs(row["error_pct"]) for row in usable_rows if row["error_pct"] is not None]
    errors = [row["error_pct"] for row in usable_rows if row["error_pct"] is not None]
    nav_returns = [
        row["actual_nav"] / row["previous_nav"] - 1
        for row in usable_rows
        if row["actual_nav"] is not None and row["previous_nav"]
    ]
    mae_pct = sum(abs_errors) / len(abs_errors) if abs_errors else None
    nav_volatility_pct = pstdev(nav_returns) if len(nav_returns) >= 2 else None
    latest_usable = usable_rows[0] if usable_rows else None
    return {
        "count": len(rows),
        "quality_sample_count": len(usable_rows),
        "low_coverage_count": sum(1 for row in rows if row["data_quality"] == "low_coverage"),
        "outlier_count": sum(1 for row in rows if row["data_quality"] == "outlier"),
        "mae_pct": mae_pct,
        "nav_volatility_pct": nav_volatility_pct,
        "mae_to_nav_volatility": (
            mae_pct / nav_volatility_pct if mae_pct is not None and nav_volatility_pct else None
        ),
        "std_pct": pstdev(errors) if errors else None,
        "max_abs_error_pct": max(abs_errors) if abs_errors else None,
        "latest_error_pct": latest_usable["error_pct"] if latest_usable else None,
        "latest_date": latest_usable["date"] if latest_usable else None,
        "avg_covered_weight": sum(row["covered_weight"] for row in rows) / len(rows),
        "avg_modeled_weight": sum(row["modeled_weight"] for row in rows) / len(rows),
        "avg_priced_ratio": sum(row["priced_ratio"] for row in rows) / len(rows),
    }


def _price_series(con: sqlite3.Connection, secid: str) -> list[tuple[str, float]]:
    rows = con.execute(
        "select date, close from daily_prices where secid = ? and close > 0 order by date asc", (secid,)
    ).fetchall()
    return [(row["date"], row["close"]) for row in rows]


def _backtest_price_series(con: sqlite3.Connection, secid: str) -> list[tuple[str, float]]:
    series = None
    if secid in US_EQUITY_CLOSE_MARKS:
        rows = con.execute(
            """
            select date, close from mark_prices
            where secid = ? and source = 'yahoo_daily_close' and close > 0
            order by date asc
            """,
            (secid,),
        ).fetchall()
        if rows:
            series = [(row["date"], row["close"]) for row in rows]
    if series is None:
        series = _price_series(con, secid)
    return series


def _backtest_price_item_on_or_before(
    con: sqlite3.Connection, secid: str, date: str, require_fresh: bool = True
) -> tuple[str, float] | None:
    if secid in US_EQUITY_CLOSE_MARKS and _has_yahoo_close_marks(con, secid):
        item = _price_item_on_or_before(
            con,
            "mark_prices",
            "secid = ? and source = 'yahoo_daily_close'",
            (secid,),
            date,
            require_fresh=False,
        )
    else:
        item = _price_item_on_or_before(
            con,
            "daily_prices",
            "secid = ?",
            (secid,),
            date,
            require_fresh=False,
        )

    if not require_fresh or item is None:
        return item
    if item[0] == date or expected_market_closure_gap(secid, date, item[0]):
        return item
    return None


def _backtest_price_item_cached(
    con: sqlite3.Connection,
    secid: str,
    date: str,
    price_lookup_cache: dict[tuple[str, str], tuple[str, float] | None] | None = None,
) -> tuple[str, float] | None:
    if price_lookup_cache is None:
        return _backtest_price_item_on_or_before(con, secid, date)
    key = (secid, date)
    if key not in price_lookup_cache:
        price_lookup_cache[key] = _backtest_price_item_on_or_before(con, secid, date)
    return price_lookup_cache[key]


def _has_yahoo_close_marks(con: sqlite3.Connection, secid: str) -> bool:
    row = con.execute(
        """
        select 1 from mark_prices
        where secid = ? and source = 'yahoo_daily_close' and close > 0
        limit 1
        """,
        (secid,),
    ).fetchone()
    return row is not None


def _price_item_on_or_before(
    con: sqlite3.Connection,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
    date: str,
    require_fresh: bool = True,
    market_secid: str | None = None,
) -> tuple[str, float] | None:
    if table not in {"daily_prices", "mark_prices"}:
        raise ValueError(f"unsupported price table: {table}")
    row = con.execute(
        f"""
        select date, close from {table}
        where {where_sql} and date <= ? and close > 0
        order by date desc
        limit 1
        """,
        (*params, date),
    ).fetchone()
    if not row:
        return None
    item = (row["date"], row["close"])
    if not require_fresh:
        return item
    if item[0] == date or (
        market_secid
        and expected_market_closure_gap(market_secid, date, item[0])
    ):
        return item
    return None


def _realtime_base_price(
    con: sqlite3.Connection,
    secid: str,
    base_date: str,
    prefetch: IntradayPrefetch | None = None,
) -> float | None:
    if prefetch is not None and (secid, base_date) in prefetch.base_prices:
        return prefetch.base_prices[(secid, base_date)]
    item = _price_item_on_or_before(
        con,
        "daily_prices",
        "secid = ?",
        (secid,),
        base_date,
        require_fresh=True,
        market_secid=secid,
    )
    return item[1] if item else None


def _quote_date(secid: str, quote: sqlite3.Row):
    session_date = _row_value(quote, "session_date")
    if session_date:
        try:
            return datetime.fromisoformat(session_date).date()
        except ValueError:
            pass
    derived = quote_session_date(secid, quote["quote_time"])
    if not derived:
        return None
    try:
        return datetime.fromisoformat(derived).date()
    except ValueError:
        return None


def _previous_business_day(date):
    previous = date - timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= timedelta(days=1)
    return previous


def _price_on_or_before(series: list[tuple[str, float]], date: str) -> float | None:
    dates = [item[0] for item in series]
    idx = bisect_right(dates, date) - 1
    if idx < 0:
        return None
    return series[idx][1]


def _fresh_price_on_or_before(
    series: list[tuple[str, float]], date: str, secid: str | None = None
) -> float | None:
    item = _fresh_price_item_on_or_before(series, date, secid)
    return item[1] if item else None


def _fresh_price_item_on_or_before(
    series: list[tuple[str, float]], date: str, secid: str | None = None
) -> tuple[str, float] | None:
    dates = [item[0] for item in series]
    idx = bisect_right(dates, date) - 1
    if idx < 0:
        return None
    price_date = dates[idx]
    if price_date == date or (
        secid and expected_market_closure_gap(secid, date, price_date)
    ):
        return series[idx]
    return None


def historical_fx_return(
    con: sqlite3.Connection,
    asset_secid: str,
    previous_date: str,
    current_date: str,
    require_fresh: bool = False,
) -> float | None:
    fx_secid = fx_secid_for_asset(asset_secid)
    if not fx_secid:
        return 0.0
    if require_fresh:
        previous_item = _backtest_price_item_on_or_before(con, fx_secid, previous_date)
        current_item = _backtest_price_item_on_or_before(con, fx_secid, current_date)
        previous = previous_item[1] if previous_item else None
        current = current_item[1] if current_item else None
    else:
        previous_item = _backtest_price_item_on_or_before(con, fx_secid, previous_date, require_fresh=False)
        current_item = _backtest_price_item_on_or_before(con, fx_secid, current_date, require_fresh=False)
        previous = previous_item[1] if previous_item else None
        current = current_item[1] if current_item else None
    if not previous or not current:
        return None if require_fresh else 0.0
    return current / previous - 1


def _historical_fx_diagnostic(
    con: sqlite3.Connection,
    asset_secid: str,
    previous_date: str,
    current_date: str,
    price_lookup_cache: dict[tuple[str, str], tuple[str, float] | None] | None = None,
) -> dict[str, Any]:
    fx_secid = fx_secid_for_asset(asset_secid)
    if not fx_secid:
        return {"fx_secid": None, "missing": False, "stale": False}
    previous = _backtest_price_item_cached(con, fx_secid, previous_date, price_lookup_cache)
    current = _backtest_price_item_cached(con, fx_secid, current_date, price_lookup_cache)
    if not previous or not current:
        return {"fx_secid": fx_secid, "missing": True, "stale": False}
    return {
        "fx_secid": fx_secid,
        "missing": False,
        "stale": previous[0] != previous_date or current[0] != current_date,
        "previous_date": previous_date,
        "previous_price_date": previous[0],
        "current_date": current_date,
        "current_price_date": current[0],
    }


def fx_secid_for_asset(asset_secid: str) -> str | None:
    if asset_secid in FX_SECID_OVERRIDES:
        return FX_SECID_OVERRIDES[asset_secid]
    market = int(asset_secid.split(".", 1)[0])
    return FX_MIDPOINT_SECIDS.get(market)
