from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import FUNDS, FX_MIDPOINT_SECIDS, US_EQUITY_CLOSE_MARKS
from .db import connect, init_db, set_meta
from .sources import (
    allocation_by_date,
    fetch_daily_prices,
    fetch_holdings,
    fetch_latest_regular_report,
    fetch_purchase_limits,
    fetch_report_publish_dates,
    fetch_realtime_quotes,
    yahoo_daily_close_marks,
    fund_page_data,
    latest_cash_ratio,
    latest_stock_ratio,
    parse_navs,
    utc_now,
)
from .valuation import (
    fx_secid_for_asset,
    holdings_available_on,
    latest_holdings,
    run_backtest,
    run_backtest_incremental,
)


LOGGER = logging.getLogger(__name__)
PRICE_REFRESH_PROGRESS_INTERVAL = 100
INCREMENTAL_BACKTEST_LOOKBACK_DAYS = 7
ProgressCallback = Callable[[dict[str, Any]], None]


def emit_progress(progress_callback: ProgressCallback | None, **payload: Any) -> None:
    if progress_callback:
        progress_callback(payload)


def build_all(days: int = 30, update_backtests: bool = True) -> dict[str, list[dict[str, Any]]]:
    init_db()
    all_secids: set[str] = set()
    imported = []
    import_failed = []
    total_funds = len(FUNDS)
    for index, code in enumerate(FUNDS, start=1):
        LOGGER.info("Importing fund %s/%s: %s", index, total_funds, code)
        try:
            with connect() as con:
                all_secids.update(import_fund_data(con, code))
            imported.append({"code": code})
        except Exception as exc:
            LOGGER.exception("Fund import failed during full build: code=%s", code)
            import_failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})

    with connect() as con:
        set_meta(con, "last_navs_refresh_at", utc_now())
        if imported:
            set_meta(con, "last_navs_refresh_success_at", utc_now())
        set_meta(con, "last_navs_refresh_errors", import_failed)

    all_secids.update(FX_MIDPOINT_SECIDS.values())
    with connect() as con:
        refresh_quotes(con, sorted(all_secids))

    with connect() as con:
        refresh_purchase_limits(con)

    with connect() as con:
        set_meta(con, "backtests_disabled", not update_backtests)

    backtests_refreshed = []
    backtests_failed = []
    if update_backtests:
        with connect() as con:
            refresh_daily_prices(con, sorted(all_secids))

        with connect() as con:
            refresh_mark_prices(con, sorted(all_secids))

        failed_codes = {item["code"] for item in import_failed}
        backtest_codes = [code for code in FUNDS if code not in failed_codes]
        for index, code in enumerate(backtest_codes, start=1):
            LOGGER.info("Running backtest %s/%s: %s", index, len(backtest_codes), code)
            try:
                with connect() as con:
                    rows = run_backtest(con, code, days=days)
                backtests_refreshed.append({"code": code, "rows": len(rows)})
            except Exception as exc:
                LOGGER.exception("Backtest failed during full build: code=%s", code)
                backtests_failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    else:
        with connect() as con:
            refresh_current_valuation_base_prices(con)

    with connect() as con:
        set_meta(con, "last_build_at", utc_now())
        set_meta(
            con,
            "last_build_errors",
            {"imports": import_failed, "backtests": backtests_failed},
        )
    return {
        "imported": imported,
        "import_failed": import_failed,
        "backtests_refreshed": backtests_refreshed,
        "backtests_failed": backtests_failed,
    }


def refresh_purchase_limits(con) -> None:
    limits = {item["fund_code"]: item for item in fetch_purchase_limits()}
    now = utc_now()
    for code in FUNDS:
        item = limits.get(code)
        if not item:
            continue
        con.execute(
            """
            insert or replace into fund_purchase_limits
            (fund_code, purchase_status, redeem_status, next_open_date,
             min_purchase_amount, max_purchase_amount, display, sort_value, source_date, updated_at)
            values (:fund_code, :purchase_status, :redeem_status, :next_open_date,
                    :min_purchase_amount, :max_purchase_amount, :display, :sort_value, :source_date, :updated_at)
            """,
            {**item, "updated_at": now},
        )
    set_meta(con, "last_purchase_limits_refresh_at", now)


def refresh_navs(
    con,
    codes: list[str] | None = None,
    update_backtests: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, list[dict[str, Any]]]:
    target_codes = codes or list(FUNDS)
    now = utc_now()
    updated = []
    failed = []
    emit_progress(
        progress_callback,
        phase="navs",
        label="净值",
        completed=0,
        total=len(target_codes),
    )
    for index, code in enumerate(target_codes, start=1):
        if code not in FUNDS:
            failed.append({"code": code, "error": "fund is not configured"})
            emit_progress(
                progress_callback,
                phase="navs",
                label="净值",
                completed=index,
                total=len(target_codes),
                message=code,
            )
            continue
        cfg = FUNDS[code]
        try:
            page = fund_page_data(code)
            navs = parse_navs(page["navs"])
            if not navs:
                raise ValueError("empty nav series")
        except Exception as exc:
            failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
            emit_progress(
                progress_callback,
                phase="navs",
                label="净值",
                completed=index,
                total=len(target_codes),
                message=code,
            )
            continue

        con.execute(
            """
            insert or replace into funds(code, name, exchange_market, fund_type, note, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (code, page["name"], cfg.exchange_market, cfg.fund_type, cfg.note, now),
        )
        replace_nav_rows(con, code, navs)
        latest = navs[-1]
        updated.append({"code": code, "date": latest["date"], "nav": latest["nav"]})
        emit_progress(
            progress_callback,
            phase="navs",
            label="净值",
            completed=index,
            total=len(target_codes),
            message=code,
        )

    set_meta(con, "last_navs_refresh_at", now)
    if updated:
        set_meta(con, "last_navs_refresh_success_at", now)
    set_meta(con, "last_navs_refresh_errors", failed)
    backtests = {"refreshed": [], "failed": []}
    if update_backtests and updated:
        backtests = refresh_incremental_backtests(
            con,
            [item["code"] for item in updated],
            progress_callback=progress_callback,
        )
    elif updated:
        refresh_current_valuation_base_prices(
            con,
            [item["code"] for item in updated],
            progress_callback=progress_callback,
        )
    return {
        "updated": updated,
        "failed": failed,
        "backtests_refreshed": backtests["refreshed"],
        "backtests_failed": backtests["failed"],
    }


def refresh_incremental_backtests(
    con,
    codes: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, list[dict[str, Any]]]:
    started = time.monotonic()
    target_codes = codes or list(FUNDS)
    refreshed = []
    failed = []
    stale_codes = []
    price_only_codes = []
    emit_progress(
        progress_callback,
        phase="backtest_scan",
        label="检查",
        completed=0,
        total=len(target_codes),
    )
    for index, code in enumerate(target_codes, start=1):
        if code not in FUNDS:
            failed.append({"code": code, "error": "fund is not configured"})
            emit_progress(
                progress_callback,
                phase="backtest_scan",
                label="检查",
                completed=index,
                total=len(target_codes),
                message=code,
            )
            continue
        try:
            latest_nav_date = latest_nav_date_for_fund(con, code)
            latest_backtest_date = latest_backtest_date_for_fund(con, code)
            if not latest_nav_date:
                continue
            if not latest_backtest_date or latest_backtest_date < latest_nav_date:
                stale_codes.append(code)
            elif latest_backtest_trade_price_missing(con, code, latest_backtest_date):
                price_only_codes.append(code)
        except Exception as exc:
            failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
        emit_progress(
            progress_callback,
            phase="backtest_scan",
            label="检查",
            completed=index,
            total=len(target_codes),
            message=code,
        )

    price_targets: dict[str, set[str]] = {}
    price_target_total = len(stale_codes) + len(price_only_codes)
    emit_progress(
        progress_callback,
        phase="price_targets",
        label="价格",
        completed=0,
        total=price_target_total,
    )
    price_target_completed = 0
    for code in stale_codes:
        try:
            merge_price_targets(
                price_targets,
                incremental_backtest_price_targets_for_fund(
                    con,
                    code,
                    start_date=incremental_backtest_cutoff_for_fund(con, code),
                ),
            )
        except Exception as exc:
            failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
        price_target_completed += 1
        emit_progress(
            progress_callback,
            phase="price_targets",
            label="价格",
            completed=price_target_completed,
            total=price_target_total,
            message=code,
        )
    for code in price_only_codes:
        try:
            backtest_date = latest_backtest_date_for_fund(con, code)
            if backtest_date:
                add_price_target(price_targets, fund_secid(code), backtest_date)
        except Exception as exc:
            failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
        price_target_completed += 1
        emit_progress(
            progress_callback,
            phase="price_targets",
            label="价格",
            completed=price_target_completed,
            total=price_target_total,
            message=code,
        )
    if price_targets:
        LOGGER.info(
            "Refreshing incremental backtest prices: stale_codes=%s price_only_codes=%s secids=%s targets=%s",
            len(stale_codes),
            len(price_only_codes),
            len(price_targets),
            count_price_targets(price_targets),
        )
        refresh_daily_prices_for_targets(con, price_targets, progress_callback=progress_callback)
        refresh_mark_prices_for_targets(con, price_targets, progress_callback=progress_callback)

    emit_progress(
        progress_callback,
        phase="backtests",
        label="回测",
        completed=0,
        total=len(stale_codes),
    )
    for index, code in enumerate(stale_codes, start=1):
        try:
            rows = run_backtest_incremental(
                con,
                code,
                start_date=incremental_backtest_cutoff_for_fund(con, code),
            )
            if rows:
                refreshed.append({"code": code, "rows": len(rows), "latest_date": rows[-1]["date"]})
        except Exception as exc:
            failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
        emit_progress(
            progress_callback,
            phase="backtests",
            label="回测",
            completed=index,
            total=len(stale_codes),
            message=code,
        )

    unresolved_trade_prices = [
        {"code": code, "date": date}
        for code in [*stale_codes, *price_only_codes]
        if (date := latest_backtest_missing_trade_price_date(con, code))
    ]
    if unresolved_trade_prices:
        LOGGER.warning("Backtest trade prices still missing: %s", unresolved_trade_prices)

    now = utc_now()
    set_meta(con, "last_incremental_backtests_refresh_at", now)
    set_meta(con, "last_incremental_backtests_refresh_errors", failed)
    LOGGER.info(
        "Incremental backtests completed in %.1fs: target=%s stale=%s refreshed=%s failed=%s",
        time.monotonic() - started,
        len(target_codes),
        len(stale_codes),
        len(refreshed),
        len(failed),
    )
    return {"refreshed": refreshed, "failed": failed}


def refresh_current_valuation_base_prices(
    con,
    codes: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> None:
    targets = current_valuation_base_price_targets(con, codes)
    emit_progress(
        progress_callback,
        phase="current_base_prices",
        label="基准价",
        completed=0,
        total=len(targets),
        message=f"targets={count_price_targets(targets)}",
    )
    if not targets:
        return
    LOGGER.info(
        "Refreshing current valuation base prices: secids=%s targets=%s",
        len(targets),
        count_price_targets(targets),
    )
    refresh_daily_prices_for_targets(con, targets, progress_callback=progress_callback)


def current_valuation_base_price_targets(
    con,
    codes: list[str] | None = None,
) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {}
    target_codes = codes or list(FUNDS)
    end_date = datetime.now().date().isoformat()
    for code in target_codes:
        if code not in FUNDS:
            continue
        nav_date = latest_nav_date_for_fund(con, code)
        if not nav_date:
            continue
        for holding in latest_holdings(con, code):
            add_base_price_window(targets, holding["secid"], nav_date, end_date)
            fx_secid = fx_secid_for_asset(holding["secid"])
            if fx_secid:
                add_base_price_window(targets, fx_secid, nav_date, end_date)
    return targets


def add_base_price_window(targets: dict[str, set[str]], secid: str, base_date: str, end_date: str) -> None:
    try:
        base_day = datetime.fromisoformat(base_date).date()
        end_day = datetime.fromisoformat(end_date).date()
    except ValueError:
        add_price_target(targets, secid, base_date)
        return
    add_price_target(targets, secid, base_date)
    if end_day > base_day:
        add_price_target(targets, secid, end_date)


def latest_nav_date_for_fund(con, code: str) -> str | None:
    row = con.execute("select max(date) as date from navs where fund_code = ?", (code,)).fetchone()
    return row["date"] if row else None


def latest_backtest_date_for_fund(con, code: str) -> str | None:
    row = con.execute("select max(date) as date from backtests where fund_code = ?", (code,)).fetchone()
    return row["date"] if row else None


def latest_backtest_trade_price_missing(con, code: str, backtest_date: str) -> bool:
    row = con.execute(
        "select 1 from daily_prices where secid = ? and date = ? limit 1",
        (fund_secid(code), backtest_date),
    ).fetchone()
    return row is None


def latest_backtest_missing_trade_price_date(con, code: str) -> str | None:
    backtest_date = latest_backtest_date_for_fund(con, code)
    if not backtest_date:
        return None
    return backtest_date if latest_backtest_trade_price_missing(con, code, backtest_date) else None


def incremental_backtest_cutoff_for_fund(
    con,
    code: str,
    lookback_days: int = INCREMENTAL_BACKTEST_LOOKBACK_DAYS,
) -> str | None:
    latest_nav_date = latest_nav_date_for_fund(con, code)
    if not latest_nav_date:
        return None
    try:
        latest_day = datetime.fromisoformat(latest_nav_date).date()
    except ValueError:
        return None
    return (latest_day - timedelta(days=lookback_days)).isoformat()


def incremental_backtest_price_targets_for_fund(
    con,
    code: str,
    start_date: str | None = None,
) -> dict[str, set[str]]:
    latest_backtest_date = latest_backtest_date_for_fund(con, code)
    anchor_date = incremental_backtest_anchor_date(con, code, latest_backtest_date, start_date)
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

    targets: dict[str, set[str]] = {}
    for prev, curr in zip(navs, navs[1:]):
        if latest_backtest_date and curr["date"] <= latest_backtest_date:
            continue
        if start_date and curr["date"] < start_date:
            continue
        for secid in backtest_secids_for_nav_pair(con, code, prev["date"]):
            add_price_target(targets, secid, prev["date"])
            add_price_target(targets, secid, curr["date"])
            fx_secid = fx_secid_for_asset(secid)
            if fx_secid:
                add_price_target(targets, fx_secid, prev["date"])
                add_price_target(targets, fx_secid, curr["date"])
        add_price_target(targets, fund_secid(code), curr["date"])
    return targets


def backtest_secids_for_nav_pair(con, code: str, previous_date: str) -> set[str]:
    return {
        holding["secid"]
        for holding in holdings_available_on(con, code, previous_date)
    }


def add_price_target(targets: dict[str, set[str]], secid: str, date: str) -> None:
    targets.setdefault(secid, set()).add(date)


def merge_price_targets(left: dict[str, set[str]], right: dict[str, set[str]]) -> None:
    for secid, dates in right.items():
        left.setdefault(secid, set()).update(dates)


def count_price_targets(targets: dict[str, set[str]]) -> int:
    return sum(len(dates) for dates in targets.values())


def incremental_backtest_anchor_date(
    con,
    code: str,
    latest_backtest_date: str | None,
    start_date: str | None,
) -> str | None:
    if latest_backtest_date and (not start_date or latest_backtest_date >= start_date):
        return latest_backtest_date
    if not start_date:
        return latest_backtest_date
    row = con.execute(
        "select max(date) as date from navs where fund_code = ? and date < ?",
        (code, start_date),
    ).fetchone()
    return row["date"] if row and row["date"] else start_date


def import_fund_data(con, code: str, years: list[int] | None = None) -> set[str]:
    if code not in FUNDS:
        raise KeyError(f"fund {code} is not configured")
    cfg = FUNDS[code]
    page = fund_page_data(code)
    con.execute(
        """
        insert or replace into funds(code, name, exchange_market, fund_type, note, updated_at)
        values (?, ?, ?, ?, ?, ?)
        """,
        (code, page["name"], cfg.exchange_market, cfg.fund_type, cfg.note, utc_now()),
    )
    replace_nav_rows(con, code, parse_navs(page["navs"]))

    publish_dates = fetch_report_publish_dates(code)
    latest_report = fetch_latest_regular_report(code)
    if latest_report:
        con.execute(
            """
            insert or replace into fund_announcements
            (fund_code, title, publish_date, announcement_id, url, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                latest_report["title"],
                latest_report["publish_date"],
                latest_report["announcement_id"],
                latest_report["url"],
                utc_now(),
            ),
        )

    stock_ratio = latest_stock_ratio(page["allocation"])
    cash_ratio = latest_cash_ratio(page["allocation"])
    allocations = allocation_by_date(page["allocation"])
    years = years or list(range(2026, 2022, -1))
    periods = []
    if cfg.manual_holdings_mode not in {"replace", "proxy_only"}:
        periods = fetch_holdings(code, page["stock_codes"], years=years)

    secids: set[str] = {fund_secid(code)}
    con.execute("delete from holdings where fund_code = ?", (code,))
    if cfg.manual_holdings_mode == "replace":
        for manual in manual_holdings(cfg, publish_dates):
            for holding in expand_manual_holding(manual):
                add_single_holding(con, code, **holding)
                secids.add(holding["secid"])
        return secids
    if cfg.manual_holdings_mode == "proxy_then_manual_replace":
        manual_items = manual_holdings(cfg, publish_dates)
        manual_dates = {item["report_date"] for item in manual_items}
        add_proxy_periods(con, code, cfg, publish_dates, allocations, cash_ratio, stock_ratio, skip_dates=manual_dates)
        secids.update(cfg.proxy_secids)
        for manual in manual_items:
            for holding in expand_manual_holding(manual):
                add_single_holding(con, code, **holding)
                secids.add(holding["secid"])
        return secids

    if cfg.manual_holdings_mode == "proxy_only":
        add_proxy_periods(con, code, cfg, publish_dates, allocations, cash_ratio, stock_ratio)
        secids.update(cfg.proxy_secids)
        return secids

    if periods:
        period_dates = set()
        for report_date, holdings in periods:
            period_dates.add(report_date)
            for holding in holdings:
                add_single_holding(
                    con,
                    code,
                    report_date,
                    publish_dates.get(report_date),
                    holding["secid"],
                    holding["symbol"],
                    holding["name"],
                    holding["weight"],
                    holding["source"],
                )
                secids.add(holding["secid"])

            disclosed_weight = sum(item["weight"] for item in holdings)
            proxy_weight = proxy_weight_for_period(
                cfg.proxy_weight,
                cfg.proxy_basis,
                cfg.proxy_secids,
                allocations,
                report_date,
                cash_ratio,
                stock_ratio,
                disclosed_weight,
            )
            add_proxy_holdings(con, code, report_date, publish_dates.get(report_date), cfg.proxy_secids, proxy_weight)
            secids.update(cfg.proxy_secids)
        if cfg.proxy_secids:
            add_missing_proxy_periods(con, code, cfg, publish_dates, allocations, period_dates, cash_ratio, stock_ratio)
        for manual in manual_holdings(cfg, publish_dates):
            for holding in expand_manual_holding(manual):
                add_single_holding(con, code, **holding)
                secids.add(holding["secid"])
    elif cfg.proxy_secids:
        add_proxy_periods(con, code, cfg, publish_dates, allocations, cash_ratio, stock_ratio)
        secids.update(cfg.proxy_secids)
    return secids


def replace_nav_rows(con, code: str, navs: list[dict]) -> None:
    con.execute("delete from navs where fund_code = ?", (code,))
    for nav in navs:
        con.execute(
            """
            insert or replace into navs(fund_code, date, nav, distribution, return_pct)
            values (?, ?, ?, ?, ?)
            """,
            (code, nav["date"], nav["nav"], nav["distribution"], nav["return_pct"]),
        )


def fund_secid(code: str) -> str:
    cfg = FUNDS[code]
    return f"{cfg.exchange_market}.{code}"


def add_proxy_periods(
    con,
    code: str,
    cfg,
    publish_dates,
    allocations,
    cash_ratio: float,
    stock_ratio: float,
    skip_dates: set[str] | None = None,
) -> None:
    skip_dates = skip_dates or set()
    allocation_dates = list(allocations) or ["proxy"]
    for report_date in allocation_dates:
        if report_date in skip_dates:
            continue
        proxy_weight = proxy_weight_for_period(
            cfg.proxy_weight,
            cfg.proxy_basis,
            cfg.proxy_secids,
            allocations,
            report_date,
            cash_ratio,
            stock_ratio,
            0.0,
        )
        add_proxy_holdings(con, code, report_date, publish_dates.get(report_date), cfg.proxy_secids, proxy_weight)


def add_missing_proxy_periods(
    con, code: str, cfg, publish_dates, allocations, existing_dates: set[str], cash_ratio: float, stock_ratio: float
) -> None:
    for report_date in allocations:
        if report_date in existing_dates:
            continue
        proxy_weight = proxy_weight_for_period(
            cfg.proxy_weight,
            cfg.proxy_basis,
            cfg.proxy_secids,
            allocations,
            report_date,
            cash_ratio,
            stock_ratio,
            0.0,
        )
        add_proxy_holdings(con, code, report_date, publish_dates.get(report_date), cfg.proxy_secids, proxy_weight)


def add_proxy_holdings(
    con, code: str, report_date: str, publish_date: str | None, secids: tuple[str, ...], total_weight: float
) -> None:
    if not secids or total_weight <= 0:
        return
    per_weight = total_weight / len(secids)
    for secid in secids:
        market, symbol = secid.split(".", 1)
        add_single_holding(
            con,
            code,
            report_date,
            publish_date,
            secid,
            symbol,
            proxy_name(secid),
            per_weight,
            "proxy",
        )


def add_single_holding(
    con,
    fund_code: str,
    report_date: str,
    publish_date: str | None,
    secid: str,
    symbol: str,
    name: str,
    weight: float,
    source: str,
) -> None:
    con.execute(
            """
            insert or replace into holdings
            (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
        (fund_code, report_date, publish_date, secid, symbol, name, weight, source),
    )


def manual_holdings(cfg, publish_dates: dict[str, str]) -> list[dict]:
    result = []
    for period in cfg.manual_holdings:
        report_date = period["report_date"]
        publish_date = publish_dates.get(report_date) or period.get("publish_date")
        if not publish_date:
            continue
        for holding in period.get("holdings", []):
            result.append(
                {
                    "report_date": report_date,
                    "publish_date": publish_date,
                    "secid": holding["secid"],
                    "symbol": holding.get("symbol") or holding["secid"].split(".", 1)[1],
                    "name": holding["name"],
                    "weight": float(holding["weight"]),
                    "source": holding["source"],
                }
            )
    return result


def expand_manual_holding(holding: dict) -> list[dict]:
    if holding["source"] != "lookthrough_fund" or holding["secid"] != "1.520560":
        return [holding]
    target_page = fund_page_data("520560")
    target_periods = dict(fetch_holdings("520560", target_page["stock_codes"], years=[2026, 2025]))
    target_holdings = target_periods.get(holding["report_date"])
    if not target_holdings:
        return [holding]
    expanded = []
    for target in target_holdings:
        expanded.append(
            {
                "report_date": holding["report_date"],
                "publish_date": holding["publish_date"],
                "secid": target["secid"],
                "symbol": target["symbol"],
                "name": f"穿透520560/{target['name']}",
                "weight": holding["weight"] * target["weight"],
                "source": "lookthrough_stock",
            }
        )
    return expanded


def proxy_name(secid: str) -> str:
    return {
        "0.399005": "中小100指数",
        "0.399006": "创业板指",
        "0.399313": "巨潮100指数",
        "0.399330": "深证100指数",
        "0.399368": "国证航天军工指数（行情简称国证军工）",
        "0.399393": "国证地产指数",
        "0.399395": "国证有色金属行业指数",
        "0.399396": "国证食品饮料行业指数",
        "0.399417": "新能源车指数",
        "0.399440": "国证钢铁行业指数（行情简称国证钢铁）",
        "0.399699": "金融科技指数",
        "0.399707": "中证申万证券行业指数（行情简称CSSW证券）",
        "0.399803": "工业4.0指数",
        "0.399804": "中证体育指数",
        "0.399806": "中证环境治理指数",
        "0.399807": "高铁产业指数",
        "0.399811": "中证申万电子行业投资指数（行情简称CSSW电子）",
        "0.399965": "中证800地产指数",
        "0.399966": "中证800证券保险指数（行情简称800非银）",
        "0.399967": "中证军工指数",
        "0.399970": "移动互联指数",
        "0.399971": "中证传媒指数",
        "0.399973": "中证国防指数",
        "0.399974": "国企改革指数",
        "0.399975": "中证全指证券公司指数",
        "0.399976": "新能源汽车指数",
        "0.399986": "中证银行指数",
        "0.399989": "中证医疗指数",
        "0.399990": "煤炭等权指数",
        "0.399991": "中证申万一带一路主题指数",
        "0.399992": "中证万得并购重组指数",
        "0.399993": "中证万得生物科技指数",
        "0.399995": "基建工程指数",
        "0.399998": "中证煤炭指数",
        "1.000015": "红利指数",
        "1.000016": "上证50指数",
        "1.000300": "沪深300指数",
        "1.000688": "科创50指数",
        "1.000863": "中证精准医疗主题指数",
        "0.399805": "中证A股资源产业指数",
        "0.399808": "中证新能指数",
        "1.000808": "中证申万医药生物指数",
        "1.000841": "中证800制药与生物科技指数",
        "1.000961": "中证上游资源产业指数",
        "1.000998": "中证TMT产业主题指数",
        "2.930641": "中证中药指数",
        "2.930713": "中证人工智能主题指数",
        "2.930720": "中证互联网医疗主题指数",
        "2.930721": "中证智能汽车主题指数",
        "2.930743": "中证生物科技主题指数",
        "2.930790": "中证娱乐主题指数",
        "2.930791": "中证医药主题指数",
        "2.930820": "中证高端制造主题指数",
        "2.930875": "中证空天一体军工指数",
        "2.931068": "中证消费龙头指数",
        "2.931136": "深圳科技指数",
        "2.H30094": "中证主要消费红利指数",
        "1.000823": "中证800有色金属指数",
        "1.000827": "中证环保产业指数",
        "1.000852": "中证1000指数",
        "1.000903": "中证A100指数",
        "1.000905": "中证500指数",
        "1.000906": "中证800指数",
        "1.000933": "中证医药卫生指数",
        "1.000935": "中证信息技术指数",
        "1.000974": "中证800金融指数",
        "100.HSCEI": "恒生中国企业指数代理",
        "100.HSI": "恒生指数代理",
        "100.NDX100": "纳斯达克100代理",
        "100.SOX": "费城半导体指数代理",
        "100.SPX": "标普500指数代理",
        "1.000979": "中证大宗商品股票指数",
        "2.930914": "中证港股通高股息投资指数代理",
        "124.HSTECH": "恒生科技指数代理",
        "124.HSHKI": "恒生港股通指数代理",
        "124.HSMI": "恒生综合中型股指数代理",
        "0.159995": "中证芯片产业指数代理",
        "1.513530": "港股通红利 ETF 代理",
        "1.562060": "华宝标普中国A股红利机会ETF",
        "102.CL00Y": "NYMEX原油代理",
        "112.B00Y": "布伦特原油代理",
        "113.agm": "沪银主连",
        "124.HSSI": "恒生综合小型股指数",
        "101.GC00Y": "COMEX黄金代理",
        "122.XAU": "黄金现货代理",
        "107.AIQ": "Global X Artificial Intelligence & Technology ETF",
        "107.ARKG": "ARK Genomic Revolution ETF",
        "107.ARKK": "ARK Innovation ETF",
        "107.ARKQ": "ARK Autonomous Technology & Robotics ETF",
        "107.AGG": "iShares Core U.S. Aggregate Bond ETF",
        "107.BNDX": "Vanguard Total International Bond ETF",
        "107.BOTZ": "Global X Robotics & Artificial Intelligence ETF",
        "107.CPER": "United States Copper Index Fund",
        "107.DBA": "Invesco DB Agriculture Fund",
        "107.DBC": "Invesco DB Commodity Index Tracking Fund",
        "107.EPI": "WisdomTree India Earnings Fund",
        "107.EWH": "iShares MSCI Hong Kong ETF",
        "107.FINX": "Global X FinTech ETF",
        "107.GLIN": "VanEck India Growth Leaders ETF",
        "107.INCO": "Columbia India Consumer ETF",
        "107.INDA": "MSCI India ETF 等效代理",
        "107.INDY": "India 50/SENSEX ETF 等效代理",
        "107.IYE": "iShares U.S. Energy ETF",
        "107.IXC": "iShares Global Energy ETF",
        "107.KWEB": "KraneShares CSI China Internet ETF",
        "107.MCHI": "iShares MSCI China ETF",
        "107.NFTY": "Nifty 50 ETF 等效代理",
        "107.PIN": "Invesco India ETF",
        "107.QQQ": "Invesco QQQ Trust",
        "107.RSPH": "Invesco S&P 500 Equal Weight Health Care ETF",
        "107.SLV": "iShares Silver Trust",
        "107.SMH": "VanEck Semiconductor ETF",
        "107.SOXX": "iShares Semiconductor ETF",
        "107.SMIN": "iShares MSCI India Small-Cap ETF",
        "107.VDE": "Vanguard Energy ETF",
        "107.VNQ": "Vanguard Real Estate ETF",
        "107.XBI": "SPDR S&P Biotech ETF",
        "107.XLE": "Energy Select Sector SPDR Fund",
        "107.XLK": "Technology Select Sector SPDR Fund",
        "107.XLY": "Consumer Discretionary Select Sector SPDR Fund",
        "107.XOP": "SPDR S&P Oil & Gas Exploration & Production ETF",
        "107.CNYB": "ChinaAMC China Bond ETF",
    }.get(secid, secid.split(".", 1)[-1])


def proxy_weight_for_period(
    configured_weight: float | None,
    proxy_basis: str,
    proxy_secids: tuple[str, ...],
    allocations: dict[str, dict[str, float]],
    report_date: str,
    fallback_cash_ratio: float,
    fallback_stock_ratio: float,
    disclosed_weight: float,
) -> float:
    if configured_weight is not None:
        return configured_weight
    allocation = allocations.get(report_date, {})
    cash_ratio = allocation.get("cash", fallback_cash_ratio)
    stock_ratio = allocation.get("stock", fallback_stock_ratio)
    if proxy_secids:
        if proxy_basis == "stock_gap":
            return max(0.0, min(1.0, stock_ratio - disclosed_weight))
        return max(0.0, min(1.0, 1.0 - cash_ratio - disclosed_weight))
    return max(0.0, min(1.0, stock_ratio - disclosed_weight))


def latest_report_date_from_allocation(allocation: dict) -> str | None:
    categories = allocation.get("categories") or []
    return categories[-1] if categories else None


def refresh_quotes(
    con,
    secids: list[str],
    attempts: int = 3,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    requested = sorted(set(secids))
    remaining = requested
    saved: set[str] = set()
    attempts = max(1, attempts)
    emit_progress(
        progress_callback,
        phase="quotes",
        label="行情",
        completed=0,
        total=len(requested),
    )

    for attempt in range(attempts):
        if not remaining:
            break
        base_saved_count = len(saved)
        base_remaining_count = len(remaining)

        def quote_source_progress(progress: dict[str, Any]) -> None:
            try:
                source_completed = float(progress.get("completed") or 0)
                source_total = float(progress.get("total") or 100)
            except (TypeError, ValueError):
                source_completed = 0
                source_total = 100
            source_fraction = 0 if source_total <= 0 else min(1.0, max(0.0, source_completed / source_total))
            completed = base_saved_count + int(base_remaining_count * source_fraction)
            if base_remaining_count:
                completed = min(completed, max(0, len(requested) - 1))
            message = progress.get("message") or ""
            if attempts > 1:
                message = f"第 {attempt + 1}/{attempts} 次 · {message}"
            emit_progress(
                progress_callback,
                phase=progress.get("phase") or "quotes",
                label=progress.get("label") or "行情",
                completed=completed,
                total=len(requested),
                message=message,
            )

        for quote in fetch_realtime_quotes(remaining, progress_callback=quote_source_progress):
            if quote.get("price") is None:
                continue
            con.execute(
                """
                insert or replace into quotes
                (secid, symbol, market, name, price, pct, previous_close, quote_time, updated_at)
                values (:secid, :symbol, :market, :name, :price, :pct, :previous_close, :quote_time, :updated_at)
                """,
                {**quote, "updated_at": utc_now()},
            )
            saved.add(quote["secid"])
        remaining = [secid for secid in remaining if secid not in saved]
        completed = len(requested) if attempt + 1 == attempts or not remaining else len(saved)
        emit_progress(
            progress_callback,
            phase="quotes",
            label="行情",
            completed=completed,
            total=len(requested),
            message=f"saved={len(saved)} missing={len(remaining)}",
        )
        if remaining and attempt + 1 < attempts:
            time.sleep(0.8 * (attempt + 1))

    set_meta(con, "last_realtime_quotes_refresh_at", utc_now())
    set_meta(con, "last_realtime_quotes_fetch_missing_secids", remaining)
    LOGGER.info(
        "Realtime quotes refreshed in %.1fs: requested=%s saved=%s missing=%s",
        time.monotonic() - started,
        len(requested),
        len(saved),
        len(remaining),
    )
    return {"requested": len(requested), "saved": len(saved), "missing": remaining}


def refresh_daily_prices(con, secids: list[str], commit_every: int = 1000) -> None:
    started = time.monotonic()
    tasks = []
    for secid in secids:
        row = con.execute("select max(date) as max_date from daily_prices where secid = ?", (secid,)).fetchone()
        begin = "20240101"
        if row and row["max_date"]:
            begin_date = datetime.fromisoformat(row["max_date"]).date() - timedelta(days=7)
            begin = begin_date.strftime("%Y%m%d")
        tasks.append((secid, begin))
    LOGGER.info("Daily price refresh started: secids=%s", len(tasks))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_daily_prices, secid, begin=begin): secid
            for secid, begin in tasks
        }
        saved_rows = 0
        completed = 0
        failed = 0
        empty = 0
        for future in as_completed(futures):
            secid = futures[future]
            completed += 1
            try:
                rows = future.result()
            except Exception as exc:
                failed += 1
                LOGGER.warning("Daily price refresh failed: secid=%s error=%s", secid, exc)
                continue
            if not rows:
                empty += 1
                LOGGER.warning("Daily price refresh returned no rows: secid=%s", secid)
                continue
            for row in rows:
                if row["close"] is None:
                    continue
                con.execute(
                    """
                    insert or replace into daily_prices(secid, date, close, pct)
                    values (?, ?, ?, ?)
                    """,
                    (secid, row["date"], row["close"], row["pct"]),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
            if completed % PRICE_REFRESH_PROGRESS_INTERVAL == 0 or completed == len(tasks):
                LOGGER.info(
                    "Daily price refresh progress: completed=%s/%s saved_rows=%s failed=%s empty=%s elapsed=%.1fs",
                    completed,
                    len(tasks),
                    saved_rows,
                    failed,
                    empty,
                    time.monotonic() - started,
                )
    LOGGER.info(
        "Daily prices refreshed in %.1fs: secids=%s saved_rows=%s failed=%s empty=%s",
        time.monotonic() - started,
        len(tasks),
        saved_rows,
        failed,
        empty,
    )


def refresh_daily_prices_for_targets(
    con,
    targets: dict[str, set[str]],
    commit_every: int = 1000,
    progress_callback: ProgressCallback | None = None,
) -> None:
    started = time.monotonic()
    missing_targets = missing_daily_price_targets(con, targets)
    if not missing_targets:
        total_targets = count_price_targets(targets)
        emit_progress(
            progress_callback,
            phase="daily_prices",
            label="日线",
            completed=total_targets,
            total=total_targets,
            message="missing_targets=0",
        )
        LOGGER.info(
            "Daily price targeted refresh skipped: secids=%s targets=%s missing_targets=0",
            len(targets),
            count_price_targets(targets),
        )
        return

    tasks = [
        (secid, *price_target_window(dates))
        for secid, dates in sorted(missing_targets.items())
    ]
    emit_progress(
        progress_callback,
        phase="daily_prices",
        label="日线",
        completed=0,
        total=len(tasks),
        message=f"missing_targets={count_price_targets(missing_targets)}",
    )
    LOGGER.info(
        "Daily price targeted refresh started: secids=%s targets=%s missing_targets=%s",
        len(tasks),
        count_price_targets(targets),
        count_price_targets(missing_targets),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_daily_prices, secid, begin=begin, end=end): secid
            for secid, begin, end in tasks
        }
        saved_rows = 0
        completed = 0
        failed = 0
        empty = 0
        for future in as_completed(futures):
            secid = futures[future]
            completed += 1
            try:
                rows = future.result()
            except Exception as exc:
                failed += 1
                LOGGER.warning("Daily price targeted refresh failed: secid=%s error=%s", secid, exc)
                emit_progress(
                    progress_callback,
                    phase="daily_prices",
                    label="日线",
                    completed=completed,
                    total=len(tasks),
                    message=f"saved={saved_rows} failed={failed} empty={empty}",
                )
                continue
            if not rows:
                empty += 1
                LOGGER.warning("Daily price targeted refresh returned no rows: secid=%s", secid)
                emit_progress(
                    progress_callback,
                    phase="daily_prices",
                    label="日线",
                    completed=completed,
                    total=len(tasks),
                    message=f"saved={saved_rows} failed={failed} empty={empty}",
                )
                continue
            for row in rows:
                if row["close"] is None:
                    continue
                con.execute(
                    """
                    insert or replace into daily_prices(secid, date, close, pct)
                    values (?, ?, ?, ?)
                    """,
                    (secid, row["date"], row["close"], row["pct"]),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
            if completed % PRICE_REFRESH_PROGRESS_INTERVAL == 0 or completed == len(tasks):
                LOGGER.info(
                    "Daily price targeted refresh progress: completed=%s/%s saved_rows=%s failed=%s empty=%s elapsed=%.1fs",
                    completed,
                    len(tasks),
                    saved_rows,
                    failed,
                    empty,
                    time.monotonic() - started,
                )
            emit_progress(
                progress_callback,
                phase="daily_prices",
                label="日线",
                completed=completed,
                total=len(tasks),
                message=f"saved={saved_rows} failed={failed} empty={empty}",
            )
    LOGGER.info(
        "Daily price targeted refresh completed in %.1fs: secids=%s saved_rows=%s failed=%s empty=%s",
        time.monotonic() - started,
        len(tasks),
        saved_rows,
        failed,
        empty,
    )


def refresh_mark_prices(con, secids: list[str], commit_every: int = 1000) -> None:
    started = time.monotonic()
    tasks = []
    for secid in secids:
        symbol = US_EQUITY_CLOSE_MARKS.get(secid)
        if not symbol:
            continue
        row = con.execute(
            "select max(date) as max_date from mark_prices where secid = ? and source = 'yahoo_daily_close'",
            (secid,),
        ).fetchone()
        begin = "20240101"
        if row and row["max_date"]:
            begin_date = datetime.fromisoformat(row["max_date"]).date() - timedelta(days=7)
            begin = begin_date.strftime("%Y%m%d")
        tasks.append((secid, symbol, begin))
    if tasks:
        LOGGER.info("Mark price refresh started: secids=%s", len(tasks))

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(yahoo_daily_close_marks, symbol, begin=begin): secid
            for secid, symbol, begin in tasks
        }
        saved_rows = 0
        completed = 0
        failed = 0
        empty = 0
        for future in as_completed(futures):
            secid = futures[future]
            completed += 1
            try:
                rows = future.result()
            except Exception as exc:
                failed += 1
                LOGGER.warning("Mark price refresh failed: secid=%s error=%s", secid, exc)
                continue
            if not rows:
                empty += 1
                LOGGER.warning("Mark price refresh returned no rows: secid=%s", secid)
                continue
            for row in rows:
                con.execute(
                    """
                    insert or replace into mark_prices(secid, date, close, source)
                    values (?, ?, ?, 'yahoo_daily_close')
                    """,
                    (secid, row["date"], row["close"]),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
            if completed % PRICE_REFRESH_PROGRESS_INTERVAL == 0 or completed == len(tasks):
                LOGGER.info(
                    "Mark price refresh progress: completed=%s/%s saved_rows=%s failed=%s empty=%s elapsed=%.1fs",
                    completed,
                    len(tasks),
                    saved_rows,
                    failed,
                    empty,
                    time.monotonic() - started,
                )
    if tasks:
        LOGGER.info(
            "Mark prices refreshed in %.1fs: secids=%s saved_rows=%s failed=%s empty=%s",
            time.monotonic() - started,
            len(tasks),
            saved_rows,
            failed,
            empty,
        )


def refresh_mark_prices_for_targets(
    con,
    targets: dict[str, set[str]],
    commit_every: int = 1000,
    progress_callback: ProgressCallback | None = None,
) -> None:
    mark_targets = {
        secid: dates
        for secid, dates in targets.items()
        if secid in US_EQUITY_CLOSE_MARKS
    }
    started = time.monotonic()
    missing_targets = missing_mark_price_targets(con, mark_targets)
    if not missing_targets:
        total_targets = count_price_targets(mark_targets)
        emit_progress(
            progress_callback,
            phase="mark_prices",
            label="收盘",
            completed=total_targets,
            total=total_targets,
            message="missing_targets=0",
        )
        LOGGER.info(
            "Mark price targeted refresh skipped: secids=%s targets=%s missing_targets=0",
            len(mark_targets),
            count_price_targets(mark_targets),
        )
        return

    tasks = [
        (secid, US_EQUITY_CLOSE_MARKS[secid], *price_target_window(dates))
        for secid, dates in sorted(missing_targets.items())
    ]
    emit_progress(
        progress_callback,
        phase="mark_prices",
        label="收盘",
        completed=0,
        total=len(tasks),
        message=f"missing_targets={count_price_targets(missing_targets)}",
    )
    LOGGER.info(
        "Mark price targeted refresh started: secids=%s targets=%s missing_targets=%s",
        len(tasks),
        count_price_targets(mark_targets),
        count_price_targets(missing_targets),
    )

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(yahoo_daily_close_marks, symbol, begin=begin, end=end): secid
            for secid, symbol, begin, end in tasks
        }
        saved_rows = 0
        completed = 0
        failed = 0
        empty = 0
        for future in as_completed(futures):
            secid = futures[future]
            completed += 1
            try:
                rows = future.result()
            except Exception as exc:
                failed += 1
                LOGGER.warning("Mark price targeted refresh failed: secid=%s error=%s", secid, exc)
                emit_progress(
                    progress_callback,
                    phase="mark_prices",
                    label="收盘",
                    completed=completed,
                    total=len(tasks),
                    message=f"saved={saved_rows} failed={failed} empty={empty}",
                )
                continue
            if not rows:
                empty += 1
                LOGGER.warning("Mark price targeted refresh returned no rows: secid=%s", secid)
                emit_progress(
                    progress_callback,
                    phase="mark_prices",
                    label="收盘",
                    completed=completed,
                    total=len(tasks),
                    message=f"saved={saved_rows} failed={failed} empty={empty}",
                )
                continue
            for row in rows:
                con.execute(
                    """
                    insert or replace into mark_prices(secid, date, close, source)
                    values (?, ?, ?, 'yahoo_daily_close')
                    """,
                    (secid, row["date"], row["close"]),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
            if completed % PRICE_REFRESH_PROGRESS_INTERVAL == 0 or completed == len(tasks):
                LOGGER.info(
                    "Mark price targeted refresh progress: completed=%s/%s saved_rows=%s failed=%s empty=%s elapsed=%.1fs",
                    completed,
                    len(tasks),
                    saved_rows,
                    failed,
                    empty,
                    time.monotonic() - started,
                )
            emit_progress(
                progress_callback,
                phase="mark_prices",
                label="收盘",
                completed=completed,
                total=len(tasks),
                message=f"saved={saved_rows} failed={failed} empty={empty}",
            )
    LOGGER.info(
        "Mark price targeted refresh completed in %.1fs: secids=%s saved_rows=%s failed=%s empty=%s",
        time.monotonic() - started,
        len(tasks),
        saved_rows,
        failed,
        empty,
    )


def missing_daily_price_targets(con, targets: dict[str, set[str]]) -> dict[str, set[str]]:
    return missing_price_targets(con, targets, "daily_prices", "secid = ?", lambda secid: (secid,))


def missing_mark_price_targets(con, targets: dict[str, set[str]]) -> dict[str, set[str]]:
    mark_targets = {
        secid: dates
        for secid, dates in targets.items()
        if secid in US_EQUITY_CLOSE_MARKS
    }
    return missing_price_targets(
        con,
        mark_targets,
        "mark_prices",
        "secid = ? and source = 'yahoo_daily_close'",
        lambda secid: (secid,),
    )


def missing_price_targets(
    con,
    targets: dict[str, set[str]],
    table: str,
    where_sql: str,
    params_for_secid,
) -> dict[str, set[str]]:
    missing: dict[str, set[str]] = {}
    for secid, dates in targets.items():
        for date in dates:
            if price_is_fresh_for_date(con, table, where_sql, params_for_secid(secid), date):
                continue
            add_price_target(missing, secid, date)
    return missing


def price_is_fresh_for_date(
    con,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
    date: str,
) -> bool:
    if table not in {"daily_prices", "mark_prices"}:
        raise ValueError(f"unsupported price table: {table}")
    previous = con.execute(
        f"""
        select date from {table}
        where {where_sql} and date <= ?
        order by date desc
        limit 1
        """,
        (*params, date),
    ).fetchone()
    if not previous:
        return False
    if previous["date"] == date:
        return True
    latest = con.execute(
        f"""
        select max(date) as date from {table}
        where {where_sql}
        """,
        params,
    ).fetchone()
    return bool(latest and latest["date"] and latest["date"] > date)


def price_target_window(dates: set[str]) -> tuple[str, str]:
    ordered = sorted(dates)
    return price_date_param(ordered[0]), price_date_param(ordered[-1])


def price_date_param(date: str) -> str:
    return datetime.fromisoformat(date).strftime("%Y%m%d")
