from __future__ import annotations

import math
import sqlite3
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from statistics import pstdev
from typing import Any

from .config import FUNDS, FX_MIDPOINT_SECIDS, FX_SECID_OVERRIDES, US_EQUITY_CLOSE_MARKS
from .market_calendar import expected_market_closure_gap


def estimate_intraday(con: sqlite3.Connection, code: str) -> dict[str, Any]:
    fund = con.execute("select * from funds where code = ?", (code,)).fetchone()
    if not fund:
        raise KeyError(code)
    latest_nav = con.execute(
        "select * from navs where fund_code = ? order by date desc limit 1", (code,)
    ).fetchone()
    trade_secid = f"{fund['exchange_market']}.{code}"
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
    weighted_return, covered_weight, missing = realtime_weighted_return(con, holdings, latest_nav_date)
    estimate = latest_nav["nav"] * (1 + weighted_return) if latest_nav else None
    trade_price = quote["price"] if quote else None
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
        "trade_pct": quote["pct"] if quote else None,
        "estimated_nav": estimate,
        "premium": premium,
        "covered_weight": covered_weight,
        "missing_weight": max(0.0, 1 - covered_weight),
        "missing_quotes": missing,
        "note": fund["note"],
        "quote_time": quote["quote_time"] if quote else None,
        "announcement": dict(announcement) if announcement else None,
        "purchase_limit": dict(purchase_limit) if purchase_limit else None,
    }


def latest_holdings(con: sqlite3.Connection, code: str) -> list[sqlite3.Row]:
    date_row = con.execute(
        "select max(report_date) as report_date from holdings where fund_code = ?", (code,)
    ).fetchone()
    if not date_row or not date_row["report_date"]:
        return []
    return con.execute(
        """
        select * from holdings
        where fund_code = ? and report_date = ?
        order by weight desc
        """,
        (code, date_row["report_date"]),
    ).fetchall()


def realtime_weighted_return(
    con: sqlite3.Connection, holdings: list[sqlite3.Row], base_date: str | None = None
) -> tuple[float, float, list[str]]:
    total = 0.0
    covered = 0.0
    missing = []
    for holding in holdings:
        asset_ret = realtime_asset_return(con, holding["secid"], base_date)
        if asset_ret is None:
            missing.append(holding["secid"])
            continue
        fx_ret = realtime_fx_return(con, holding["secid"], base_date)
        total += holding["weight"] * ((1 + asset_ret) * (1 + fx_ret) - 1)
        covered += holding["weight"]
    return total, covered, missing


def realtime_asset_return(con: sqlite3.Connection, secid: str, base_date: str | None = None) -> float | None:
    quote = con.execute("select * from quotes where secid = ?", (secid,)).fetchone()
    if not quote:
        return None
    return realtime_quote_return(con, secid, quote, base_date)


def realtime_fx_return(con: sqlite3.Connection, secid: str, base_date: str | None = None) -> float:
    fx_secid = fx_secid_for_asset(secid)
    if not fx_secid:
        return 0.0
    quote = con.execute("select * from quotes where secid = ?", (fx_secid,)).fetchone()
    if not quote:
        return 0.0
    fx_ret = realtime_quote_return(con, fx_secid, quote, base_date)
    return fx_ret if fx_ret is not None else 0.0


def realtime_quote_return(
    con: sqlite3.Connection, secid: str, quote: sqlite3.Row, base_date: str | None = None
) -> float | None:
    price = _positive_float(quote["price"])
    previous_close = _positive_float(quote["previous_close"])
    if not base_date:
        return quote["pct"] / 100 if quote["pct"] is not None else None

    quote_date = _quote_date(quote)
    try:
        base_day = datetime.fromisoformat(base_date).date()
    except ValueError:
        base_day = None
    if quote_date and base_day and quote_date <= base_day:
        return 0.0
    if (
        quote_date
        and base_day
        and price is not None
        and previous_close is not None
        and _previous_business_day(quote_date) == base_day
    ):
        return price / previous_close - 1

    if price is not None:
        base_price = _realtime_base_price(con, secid, base_date)
        if base_price:
            return price / base_price - 1
    return quote["pct"] / 100 if quote["pct"] is not None else None


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


def run_backtest(con: sqlite3.Connection, code: str, days: int = 30, lag_days: int = 25) -> list[dict[str, Any]]:
    con.execute("delete from backtests where fund_code = ?", (code,))
    navs = con.execute(
        "select * from navs where fund_code = ? order by date asc", (code,)
    ).fetchall()
    if len(navs) < 2:
        return []

    recent = navs[-days - 1 :]
    needed_secids = backtest_secids_for_nav_pairs(con, code, recent, None, lag_days)
    if not needed_secids:
        return []
    price_cache = {secid: _backtest_price_series(con, secid) for secid in needed_secids}
    results = []
    for prev, curr in zip(recent, recent[1:]):
        row = calculate_backtest_row(con, code, prev, curr, price_cache, lag_days)
        if not row:
            continue
        save_backtest_row(con, row)
        results.append(row)
    return results


def run_backtest_incremental(con: sqlite3.Connection, code: str, lag_days: int = 25) -> list[dict[str, Any]]:
    latest_backtest = con.execute(
        "select max(date) as date from backtests where fund_code = ?", (code,)
    ).fetchone()
    start_after = latest_backtest["date"] if latest_backtest else None
    if start_after:
        navs = con.execute(
            """
            select * from navs
            where fund_code = ?
              and date >= (
                select max(date) from navs where fund_code = ? and date <= ?
              )
            order by date asc
            """,
            (code, code, start_after),
        ).fetchall()
    else:
        navs = con.execute(
            "select * from navs where fund_code = ? order by date asc", (code,)
        ).fetchall()
    if len(navs) < 2:
        return []

    needed_secids = backtest_secids_for_nav_pairs(con, code, navs, start_after, lag_days)
    if not needed_secids:
        return []
    price_cache = {secid: _backtest_price_series(con, secid) for secid in needed_secids}
    results = []
    for prev, curr in zip(navs, navs[1:]):
        if start_after and curr["date"] <= start_after:
            continue
        row = calculate_backtest_row(con, code, prev, curr, price_cache, lag_days)
        if not row:
            continue
        save_backtest_row(con, row)
        results.append(row)
    return results


def backtest_secids_for_nav_pairs(
    con: sqlite3.Connection,
    code: str,
    navs: list[sqlite3.Row],
    start_after: str | None = None,
    lag_days: int = 25,
) -> set[str]:
    secids: set[str] = set()
    for prev, curr in zip(navs, navs[1:]):
        if start_after and curr["date"] <= start_after:
            continue
        for holding in holdings_available_on(con, code, prev["date"], lag_days):
            secids.add(holding["secid"])
    return secids


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
    covered = 0.0
    for holding in holdings:
        series = price_cache.get(holding["secid"], [])
        prev_close = _fresh_price_on_or_before(series, prev["date"])
        curr_close = _fresh_price_on_or_before(series, curr["date"])
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
    return {
        "fund_code": code,
        "date": curr["date"],
        "previous_date": prev["date"],
        "previous_nav": prev["nav"],
        "actual_nav": actual_value,
        "estimated_nav": estimated,
        "error_pct": error_pct,
        "covered_weight": covered,
    }


def backtest_price_diagnostics(
    con: sqlite3.Connection,
    code: str,
    previous_date: str,
    current_date: str,
    lag_days: int = 25,
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
        previous = _backtest_price_item_on_or_before(con, holding["secid"], previous_date)
        current = _backtest_price_item_on_or_before(con, holding["secid"], current_date)
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
        fx_diag = _historical_fx_diagnostic(con, holding["secid"], previous_date, current_date)
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
        (fund_code, date, previous_date, previous_nav, actual_nav, estimated_nav, error_pct, covered_weight)
        values (:fund_code, :date, :previous_date, :previous_nav, :actual_nav, :estimated_nav, :error_pct, :covered_weight)
        """,
        row,
    )


def holdings_available_on(
    con: sqlite3.Connection, code: str, nav_date: str, lag_days: int = 25
) -> list[sqlite3.Row]:
    published = con.execute(
        """
        select max(report_date) as report_date
        from holdings
        where fund_code = ? and publish_date is not null and publish_date <= ?
        """,
        (code, nav_date),
    ).fetchone()
    if published and published["report_date"]:
        return con.execute(
            """
            select * from holdings
            where fund_code = ? and report_date = ?
            order by weight desc
            """,
            (code, published["report_date"]),
        ).fetchall()

    usable_report_date = (
        datetime.fromisoformat(nav_date).date() - timedelta(days=lag_days)
    ).isoformat()
    row = con.execute(
        """
        select max(report_date) as report_date
        from holdings
        where fund_code = ? and report_date <= ?
        """,
        (code, usable_report_date),
    ).fetchone()
    if not row or not row["report_date"]:
        return []
    return con.execute(
        """
        select * from holdings
        where fund_code = ? and report_date = ?
        order by weight desc
        """,
        (code, row["report_date"]),
    ).fetchall()


def backtest_summary(con: sqlite3.Connection, code: str) -> dict[str, Any]:
    rows = con.execute(
        "select * from backtests where fund_code = ? order by date desc limit 30", (code,)
    ).fetchall()
    if not rows:
        return {"count": 0}
    abs_errors = [abs(row["error_pct"]) for row in rows if row["error_pct"] is not None]
    errors = [row["error_pct"] for row in rows if row["error_pct"] is not None]
    nav_returns = [
        row["actual_nav"] / row["previous_nav"] - 1
        for row in rows
        if row["actual_nav"] is not None and row["previous_nav"]
    ]
    mae_pct = sum(abs_errors) / len(abs_errors) if abs_errors else None
    nav_volatility_pct = pstdev(nav_returns) if len(nav_returns) >= 2 else None
    return {
        "count": len(rows),
        "mae_pct": mae_pct,
        "nav_volatility_pct": nav_volatility_pct,
        "mae_to_nav_volatility": (
            mae_pct / nav_volatility_pct if mae_pct is not None and nav_volatility_pct else None
        ),
        "std_pct": pstdev(errors) if errors else None,
        "max_abs_error_pct": max(abs_errors) if abs_errors else None,
        "latest_error_pct": rows[0]["error_pct"],
        "latest_date": rows[0]["date"],
        "avg_covered_weight": sum(row["covered_weight"] for row in rows) / len(rows),
    }


def _price_series(con: sqlite3.Connection, secid: str) -> list[tuple[str, float]]:
    rows = con.execute(
        "select date, close from daily_prices where secid = ? order by date asc", (secid,)
    ).fetchall()
    return [(row["date"], row["close"]) for row in rows]


def _backtest_price_series(con: sqlite3.Connection, secid: str) -> list[tuple[str, float]]:
    series = None
    if secid in US_EQUITY_CLOSE_MARKS:
        rows = con.execute(
            """
            select date, close from mark_prices
            where secid = ? and source = 'yahoo_daily_close'
            order by date asc
            """,
            (secid,),
        ).fetchall()
        if rows:
            series = [(row["date"], row["close"]) for row in rows]
    if series is None:
        series = _price_series(con, secid)
    return _with_quote_mark(con, secid, series)


def _with_quote_mark(
    con: sqlite3.Connection, secid: str, series: list[tuple[str, float]]
) -> list[tuple[str, float]]:
    if not _allow_quote_mark(secid):
        return series
    quote = con.execute("select price, previous_close, quote_time from quotes where secid = ?", (secid,)).fetchone()
    price = _positive_float(quote["price"]) if quote else None
    previous_close = _positive_float(quote["previous_close"]) if quote else None
    if not quote or not quote["quote_time"] or price is None:
        return series
    try:
        quote_date = datetime.fromisoformat(quote["quote_time"]).astimezone(timezone(timedelta(hours=8))).date()
    except ValueError:
        return series
    prices = dict(series)
    quote_date_text = quote_date.isoformat()
    if quote_date_text not in prices:
        prices[quote_date_text] = price
    if previous_close is not None:
        previous_trading_date = _previous_business_day(quote_date).isoformat()
        if previous_trading_date and previous_trading_date not in prices:
            prices[previous_trading_date] = previous_close
    return sorted(prices.items())


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
        latest_date = _latest_price_date(
            con,
            "mark_prices",
            "secid = ? and source = 'yahoo_daily_close'",
            (secid,),
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
        latest_date = _latest_price_date(con, "daily_prices", "secid = ?", (secid,))

    if _allow_quote_mark(secid):
        for quote_item in _quote_mark_items(con, secid):
            quote_date = quote_item[0]
            if latest_date is None or quote_date > latest_date:
                latest_date = quote_date
            if quote_date <= date and (item is None or quote_date > item[0]):
                item = quote_item

    if not require_fresh or item is None:
        return item
    if item[0] == date or (latest_date is not None and latest_date > date):
        return item
    return None


def _has_yahoo_close_marks(con: sqlite3.Connection, secid: str) -> bool:
    row = con.execute(
        "select 1 from mark_prices where secid = ? and source = 'yahoo_daily_close' limit 1",
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
) -> tuple[str, float] | None:
    if table not in {"daily_prices", "mark_prices"}:
        raise ValueError(f"unsupported price table: {table}")
    row = con.execute(
        f"""
        select date, close from {table}
        where {where_sql} and date <= ?
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
    latest_date = _latest_price_date(con, table, where_sql, params)
    if item[0] == date or (latest_date is not None and latest_date > date):
        return item
    return None


def _latest_price_date(
    con: sqlite3.Connection, table: str, where_sql: str, params: tuple[Any, ...]
) -> str | None:
    if table not in {"daily_prices", "mark_prices"}:
        raise ValueError(f"unsupported price table: {table}")
    row = con.execute(
        f"select max(date) as date from {table} where {where_sql}",
        params,
    ).fetchone()
    return row["date"] if row and row["date"] else None


def _quote_mark_items(con: sqlite3.Connection, secid: str) -> list[tuple[str, float]]:
    quote = con.execute(
        "select price, previous_close, quote_time from quotes where secid = ?", (secid,)
    ).fetchone()
    price = _positive_float(quote["price"]) if quote else None
    previous_close = _positive_float(quote["previous_close"]) if quote else None
    if not quote or not quote["quote_time"] or price is None:
        return []
    try:
        quote_date = datetime.fromisoformat(quote["quote_time"]).astimezone(timezone(timedelta(hours=8))).date()
    except ValueError:
        return []
    items = [(quote_date.isoformat(), price)]
    if previous_close is not None:
        items.append((_previous_business_day(quote_date).isoformat(), previous_close))
    return items


def _realtime_base_price(con: sqlite3.Connection, secid: str, base_date: str) -> float | None:
    item = _price_item_on_or_before(con, "daily_prices", "secid = ?", (secid,), base_date, require_fresh=True)
    return item[1] if item else None


def _quote_date(quote: sqlite3.Row):
    if not quote["quote_time"]:
        return None
    try:
        return datetime.fromisoformat(quote["quote_time"]).astimezone(timezone.utc).date()
    except ValueError:
        return None


def _allow_quote_mark(secid: str) -> bool:
    market, symbol = secid.split(".", 1)
    if market in {"0", "1", "116", "120", "124"}:
        return True
    return secid in {"100.HSI", "100.HSCEI"}


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


def _fresh_price_on_or_before(series: list[tuple[str, float]], date: str) -> float | None:
    item = _fresh_price_item_on_or_before(series, date)
    return item[1] if item else None


def _fresh_price_item_on_or_before(series: list[tuple[str, float]], date: str) -> tuple[str, float] | None:
    dates = [item[0] for item in series]
    idx = bisect_right(dates, date) - 1
    if idx < 0:
        return None
    price_date = dates[idx]
    if price_date == date or dates[-1] > date:
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
) -> dict[str, Any]:
    fx_secid = fx_secid_for_asset(asset_secid)
    if not fx_secid:
        return {"fx_secid": None, "missing": False, "stale": False}
    previous = _backtest_price_item_on_or_before(con, fx_secid, previous_date)
    current = _backtest_price_item_on_or_before(con, fx_secid, current_date)
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
