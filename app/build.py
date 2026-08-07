from __future__ import annotations

import logging
import math
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .config import FUNDS, US_EQUITY_CLOSE_MARKS
from .db import connect, get_meta, init_db, mark_build_finished, mark_build_started, set_meta
from .market_calendar import expected_market_closure_gap
from .sources import (
    DAILY_PRICE_BATCH_DEADLINE_SECONDS,
    DAILY_PRICE_INSTRUMENT_DEADLINE_SECONDS,
    DailyPriceDeadlineExceeded,
    allocation_by_date,
    app_today,
    daily_price_deadline,
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
    regular_report_date,
    normalize_daily_price_row,
    normalize_realtime_quote,
    utc_now,
)
from .valuation import (
    DEFAULT_BACKTEST_DAYS,
    fx_secid_for_asset,
    holdings_available_on,
    latest_holdings,
    run_backtest,
    run_backtest_incremental,
)


LOGGER = logging.getLogger(__name__)
PRICE_REFRESH_PROGRESS_INTERVAL = 100
INCREMENTAL_BACKTEST_LOOKBACK_DAYS = 7
PRICE_TARGET_LOOKBACK_DAYS = 14
MIN_PROXY_WEIGHT = 1e-12
ProgressCallback = Callable[[dict[str, Any]], None]
DailyPriceResultCallback = Callable[[str, list[dict[str, Any]] | None, Exception | None], None]


def emit_progress(progress_callback: ProgressCallback | None, **payload: Any) -> None:
    if progress_callback:
        progress_callback(payload)


def execute_daily_price_fetches(
    tasks: list[tuple[str, str, str | None]],
    result_callback: DailyPriceResultCallback,
    *,
    batch_deadline_seconds: float = DAILY_PRICE_BATCH_DEADLINE_SECONDS,
    instrument_deadline_seconds: float = DAILY_PRICE_INSTRUMENT_DEADLINE_SECONDS,
    max_workers: int = 8,
) -> list[dict[str, str]]:
    """Fetch tasks without allowing executor shutdown to overrun the batch deadline."""
    if not tasks:
        return []
    batch_deadline = time.monotonic() + max(0.0, batch_deadline_seconds)
    executor = ThreadPoolExecutor(max_workers=max_workers)

    def fetch_one(secid: str, begin: str, end: str | None):
        deadline = min(
            batch_deadline,
            time.monotonic() + max(0.0, instrument_deadline_seconds),
        )
        with daily_price_deadline(deadline):
            if end is None:
                return fetch_daily_prices(secid, begin=begin)
            return fetch_daily_prices(secid, begin=begin, end=end)

    futures = {
        executor.submit(fetch_one, secid, begin, end): secid
        for secid, begin, end in tasks
    }
    pending = set(futures)
    try:
        while pending:
            remaining = batch_deadline - time.monotonic()
            if remaining <= 0:
                break
            done, pending = wait(
                pending,
                timeout=remaining,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                break
            for future in done:
                secid = futures[future]
                try:
                    rows = future.result()
                except Exception as exc:
                    result_callback(secid, None, exc)
                else:
                    result_callback(secid, rows, None)
    finally:
        deadline_secids = sorted(futures[future] for future in pending)
        for future in pending:
            future.cancel()
        # Running requests inherit the same absolute deadline. Do not let the
        # executor context manager add an unbounded wait after our budget ends.
        executor.shutdown(wait=False, cancel_futures=True)
    return [
        {
            "secid": secid,
            "type": "batch_deadline",
            "error": "historical price batch deadline exceeded",
        }
        for secid in deadline_secids
    ]


def daily_price_fetch_diagnostic(secid: str, exc: Exception) -> dict[str, str]:
    return {
        "secid": secid,
        "type": (
            "instrument_deadline"
            if isinstance(exc, DailyPriceDeadlineExceeded)
            else "fetch_error"
        ),
        "error": f"{type(exc).__name__}: {exc}",
    }


def build_all(
    days: int = DEFAULT_BACKTEST_DAYS,
    update_backtests: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    init_db()
    build_mode = "full" if update_backtests else "current_only"
    with connect() as con:
        prepare_build(con, build_mode, update_backtests, utc_now())
    imported = []
    import_failed = []
    total_funds = len(FUNDS)
    for index, code in enumerate(FUNDS, start=1):
        LOGGER.info("Importing fund %s/%s: %s", index, total_funds, code)
        try:
            with connect() as con:
                import_fund_data(con, code)
            imported.append({"code": code})
        except Exception as exc:
            LOGGER.exception("Fund import failed during full build: code=%s", code)
            import_failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})

    with connect() as con:
        completed_at = utc_now()
        set_meta(con, "last_navs_refresh_at", completed_at)
        set_meta(con, "last_navs_refresh_completed_at", completed_at)
        if len(imported) == total_funds and not import_failed:
            set_meta(con, "last_navs_refresh_success_at", completed_at)
        elif imported:
            set_meta(con, "last_navs_refresh_partial_at", completed_at)
        else:
            set_meta(con, "last_navs_refresh_failed_at", completed_at)
        set_meta(con, "last_navs_refresh_errors", import_failed)
        set_meta(con, "last_reports_refresh_completed_at", completed_at)
        if not import_failed and len(imported) == total_funds:
            set_meta(con, "last_reports_refresh_success_at", completed_at)
        elif imported:
            set_meta(con, "last_reports_refresh_partial_at", completed_at)
        else:
            set_meta(con, "last_reports_refresh_failed_at", completed_at)
        set_meta(con, "last_reports_refresh_errors", import_failed)
        set_meta(
            con,
            "last_reports_refresh_stats",
            {
                "target_count": total_funds,
                "checked_count": len(imported),
                "changed_count": len(imported),
                "refreshed_count": len(imported),
                "failed_count": len(import_failed),
            },
        )

    with connect() as con:
        refresh_quotes(con, current_realtime_secids(con))

    with connect() as con:
        refresh_purchase_limits(con)

    backtests_refreshed = []
    backtests_failed = []
    if update_backtests:
        failed_codes = {item["code"] for item in import_failed}
        backtest_codes = [code for code in FUNDS if code not in failed_codes]
        with connect() as con:
            full_targets = full_backtest_price_targets(con, backtest_codes, days)
            targeted_result = refresh_daily_prices_for_targets(con, full_targets)
            refresh_mark_prices_for_targets(con, full_targets)
            set_meta(
                con,
                "last_full_backtest_price_targets",
                {
                    "target_count": count_price_targets(full_targets),
                    "secid_count": len(full_targets),
                    "requested_count": targeted_result["requested"],
                    "unresolved_count": len(targeted_result["unresolved"]),
                    "unresolved": targeted_result["unresolved"],
                    "completed_at": utc_now(),
                },
            )
        for index, code in enumerate(backtest_codes, start=1):
            LOGGER.info("Running backtest %s/%s: %s", index, len(backtest_codes), code)
            try:
                with connect() as con:
                    rows = run_backtest(con, code, days=days)
                if not rows:
                    raise ValueError("backtest produced no rows")
                backtests_refreshed.append({"code": code, "rows": len(rows)})
            except Exception as exc:
                LOGGER.exception("Backtest failed during full build: code=%s", code)
                backtests_failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
    else:
        with connect() as con:
            refresh_current_valuation_base_prices(con)

    with connect() as con:
        completed_at = utc_now()
        set_meta(con, "last_build_at", completed_at)
        set_meta(
            con,
            "last_build_errors",
            {"imports": import_failed, "backtests": backtests_failed},
        )
        mark_build_finished(
            con,
            mode=build_mode,
            completed_at=completed_at,
            import_failed=import_failed,
            backtests_failed=backtests_failed,
        )
    return {
        "imported": imported,
        "import_failed": import_failed,
        "backtests_refreshed": backtests_refreshed,
        "backtests_failed": backtests_failed,
    }


def prepare_build(
    con,
    build_mode: str,
    update_backtests: bool,
    started_at: str,
) -> None:
    mark_build_started(con, build_mode, started_at)
    set_meta(con, "last_navs_refresh_attempt_at", started_at)
    set_meta(con, "last_reports_refresh_attempt_at", started_at)
    set_meta(con, "backtests_disabled", not update_backtests)
    if not update_backtests:
        con.execute("delete from backtests")


def refresh_purchase_limits(con) -> dict[str, Any]:
    attempt_at = utc_now()
    set_meta(con, "last_purchase_limits_refresh_attempt_at", attempt_at)
    con.commit()
    try:
        limits = {
            item["fund_code"]: item
            for item in fetch_purchase_limits()
            if item.get("fund_code") in FUNDS
        }
    except Exception as exc:
        completed_at = utc_now()
        error = f"{type(exc).__name__}: {exc}"
        set_meta(con, "last_purchase_limits_refresh_at", completed_at)
        set_meta(con, "last_purchase_limits_refresh_completed_at", completed_at)
        set_meta(con, "last_purchase_limits_refresh_failed_at", completed_at)
        set_meta(
            con,
            "last_purchase_limits_refresh_missing_codes",
            sorted(FUNDS),
        )
        set_meta(con, "last_purchase_limits_refresh_errors", [{"error": error}])
        set_meta(
            con,
            "last_purchase_limits_refresh_stats",
            {"target_count": len(FUNDS), "saved_count": 0, "missing_count": len(FUNDS)},
        )
        con.commit()
        raise
    completed_at = utc_now()
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
            {**item, "updated_at": completed_at},
        )
    missing = sorted(set(FUNDS) - set(limits))
    errors = [{"code": code, "error": "missing from purchase-limit snapshot"} for code in missing]
    set_meta(con, "last_purchase_limits_refresh_at", completed_at)
    set_meta(con, "last_purchase_limits_refresh_completed_at", completed_at)
    set_meta(con, "last_purchase_limits_refresh_missing_codes", missing)
    set_meta(con, "last_purchase_limits_refresh_errors", errors)
    set_meta(
        con,
        "last_purchase_limits_refresh_stats",
        {
            "target_count": len(FUNDS),
            "saved_count": len(limits),
            "missing_count": len(missing),
        },
    )
    if not missing:
        set_meta(con, "last_purchase_limits_refresh_success_at", completed_at)
    elif limits:
        set_meta(con, "last_purchase_limits_refresh_partial_at", completed_at)
    else:
        set_meta(con, "last_purchase_limits_refresh_failed_at", completed_at)
    return {"saved": len(limits), "missing": missing, "errors": errors}


def refresh_reports(
    con,
    codes: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    target_codes = list(dict.fromkeys(codes if codes is not None else FUNDS))
    attempt_at = utc_now()
    set_meta(con, "last_reports_refresh_attempt_at", attempt_at)
    con.commit()
    existing_reports = {
        row["fund_code"]: (
            str(row["announcement_id"]),
            row["title"],
            row["publish_date"],
        )
        for row in con.execute(
            "select fund_code, announcement_id, title, publish_date from fund_announcements"
        )
    }
    raw_pending = get_meta(con, "pending_report_backtests", {}) or {}
    pending_backtests = (
        {
            str(code): str(recompute_from)
            for code, recompute_from in raw_pending.items()
            if code in FUNDS and recompute_from
        }
        if isinstance(raw_pending, dict)
        else {}
    )
    reports: dict[str, dict[str, str]] = {}
    failed: list[dict[str, str]] = []
    emit_progress(
        progress_callback,
        phase="report_scan",
        label="报告检查",
        completed=0,
        total=len(target_codes),
    )
    valid_codes = []
    for code in target_codes:
        if code not in FUNDS:
            failed.append(
                {"code": code, "stage": "scan", "error": "fund is not configured"}
            )
        else:
            valid_codes.append(code)

    completed = len(target_codes) - len(valid_codes)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(fetch_latest_regular_report, code): code
            for code in valid_codes
        }
        for future in as_completed(futures):
            code = futures[future]
            completed += 1
            try:
                report = future.result()
                if not report or not report.get("announcement_id"):
                    raise ValueError("latest regular report is missing")
                title = str(report.get("title") or "").strip()
                publish_date = str(report.get("publish_date") or "").strip()
                report_date = regular_report_date(title)
                if not title or not publish_date or not report_date:
                    raise ValueError("latest regular report metadata is incomplete")
                datetime.fromisoformat(publish_date)
                reports[code] = {
                    **report,
                    "title": title,
                    "publish_date": publish_date,
                    "announcement_id": str(report["announcement_id"]),
                    "report_date": report_date,
                }
            except Exception as exc:
                failed.append(
                    {
                        "code": code,
                        "stage": "scan",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            emit_progress(
                progress_callback,
                phase="report_scan",
                label="报告检查",
                completed=completed,
                total=len(target_codes),
                message=code,
            )

    changed_codes = [
        code
        for code in valid_codes
        if code in reports
        and (
            existing_reports.get(code)
            != (
                reports[code]["announcement_id"],
                reports[code]["title"],
                reports[code]["publish_date"],
            )
            or report_snapshot_needs_repair(
                con,
                code,
                reports[code]["report_date"],
            )
        )
    ]
    refreshed = []
    emit_progress(
        progress_callback,
        phase="holding_refresh",
        label="持仓更新",
        completed=0,
        total=len(changed_codes),
    )
    for index, code in enumerate(changed_codes, start=1):
        try:
            import_fund_data(
                con,
                code,
                latest_report=reports[code],
                pending_backtest_from=reports[code]["publish_date"],
            )
            con.commit()
            refreshed.append(code)
            pending_backtests[code] = reports[code]["publish_date"]
        except Exception as exc:
            con.rollback()
            failed.append(
                {
                    "code": code,
                    "stage": "import",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        emit_progress(
            progress_callback,
            phase="holding_refresh",
            label="持仓更新",
            completed=index,
            total=len(changed_codes),
            message=code,
        )

    backtests = {"refreshed": [], "failed": []}
    backtest_codes = [code for code in valid_codes if code in pending_backtests]
    backtest_failed: list[dict[str, str]] = []
    if backtest_codes:
        backtests_disabled = bool(get_meta(con, "backtests_disabled", False))
        try:
            if backtests_disabled:
                refresh_current_valuation_base_prices(
                    con,
                    backtest_codes,
                    progress_callback=progress_callback,
                )
            else:
                backtests = refresh_incremental_backtests(
                    con,
                    backtest_codes,
                    recompute_from_by_code={
                        code: pending_backtests[code] for code in backtest_codes
                    },
                    progress_callback=progress_callback,
                )
                backtest_failed = [
                    {**item, "stage": "backtest"}
                    for item in backtests["failed"]
                ]
        except Exception as exc:
            stage = "current_prices" if backtests_disabled else "backtest"
            backtest_failed = [
                {
                    "code": code,
                    "stage": stage,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                for code in backtest_codes
            ]
        failed_backtest_codes = {item["code"] for item in backtest_failed}
        for code in backtest_codes:
            if code not in failed_backtest_codes:
                pending_backtests.pop(code, None)

    all_failed = [*failed, *backtest_failed]
    failed_codes = sorted({item["code"] for item in all_failed})

    completed_at = utc_now()
    set_meta(con, "pending_report_backtests", pending_backtests)
    set_meta(con, "last_reports_refresh_completed_at", completed_at)
    set_meta(con, "last_reports_refresh_errors", all_failed)
    set_meta(
        con,
        "last_reports_refresh_stats",
        {
            "target_count": len(target_codes),
            "checked_count": len(reports),
            "changed_count": len(changed_codes),
            "refreshed_count": len(refreshed),
            "failed_count": len(failed_codes),
            "issue_count": len(all_failed),
            "pending_count": sum(code in pending_backtests for code in valid_codes),
        },
    )
    if (
        not all_failed
        and len(reports) == len(target_codes)
        and not any(code in pending_backtests for code in valid_codes)
    ):
        set_meta(con, "last_reports_refresh_success_at", completed_at)
    elif reports:
        set_meta(con, "last_reports_refresh_partial_at", completed_at)
    else:
        set_meta(con, "last_reports_refresh_failed_at", completed_at)
    return {
        "checked": [code for code in valid_codes if code in reports],
        "changed": changed_codes,
        "refreshed": refreshed,
        "failed": failed,
        "failed_codes": failed_codes,
        "backtests_refreshed": backtests["refreshed"],
        "backtests_failed": backtest_failed,
        "pending_backtests": pending_backtests,
    }


def report_snapshot_needs_repair(con, code: str, report_date: str) -> bool:
    row = con.execute(
        """
        select count(*) as holding_count,
               sum(
                   case when publish_date is null or trim(publish_date) = ''
                        then 1 else 0 end
               ) as missing_publish_dates
        from holdings
        where fund_code = ? and report_date = ? and weight > 0
        """,
        (code, report_date),
    ).fetchone()
    return (
        not row
        or not row["holding_count"]
        or bool(row["missing_publish_dates"])
    )


def refresh_navs(
    con,
    codes: list[str] | None = None,
    update_backtests: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, list[dict[str, Any]]]:
    target_codes = codes or list(FUNDS)
    set_meta(con, "last_navs_refresh_attempt_at", utc_now())
    con.commit()
    checked = []
    updated = []
    failed = []
    revised_from_by_code: dict[str, str] = {}
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
            navs = normalize_nav_rows(parse_navs(page["navs"]))
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
            (code, page["name"], cfg.exchange_market, cfg.fund_type, cfg.note, utc_now()),
        )
        change = upsert_nav_rows(con, code, navs)
        latest = navs[-1]
        checked_item = {"code": code, "date": latest["date"], "nav": latest["nav"]}
        checked.append(checked_item)
        if change["changed_rows"]:
            updated.append({**checked_item, **change})
        if change["earliest_revised_date"]:
            revised_from_by_code[code] = change["earliest_revised_date"]
        con.commit()
        emit_progress(
            progress_callback,
            phase="navs",
            label="净值",
            completed=index,
            total=len(target_codes),
            message=code,
        )

    backtests = {"refreshed": [], "failed": []}
    if update_backtests and checked:
        backtests = refresh_incremental_backtests(
            con,
            [item["code"] for item in checked],
            recompute_from_by_code=revised_from_by_code,
            progress_callback=progress_callback,
        )
    elif checked:
        for code, revised_from in revised_from_by_code.items():
            con.execute(
                "delete from backtests where fund_code = ? and date >= ?",
                (code, revised_from),
            )
        refresh_current_valuation_base_prices(
            con,
            [item["code"] for item in checked],
            progress_callback=progress_callback,
        )
    completed_at = utc_now()
    set_meta(con, "last_navs_refresh_at", completed_at)
    set_meta(con, "last_navs_refresh_completed_at", completed_at)
    if len(checked) == len(target_codes) and not failed:
        set_meta(con, "last_navs_refresh_success_at", completed_at)
    elif checked:
        set_meta(con, "last_navs_refresh_partial_at", completed_at)
    else:
        set_meta(con, "last_navs_refresh_failed_at", completed_at)
    set_meta(con, "last_navs_refresh_errors", failed)
    set_meta(
        con,
        "last_navs_refresh_stats",
        {
            "target_count": len(target_codes),
            "checked_count": len(checked),
            "changed_count": len(updated),
            "failed_count": len(failed),
        },
    )
    return {
        "checked": checked,
        "updated": updated,
        "failed": failed,
        "backtests_refreshed": backtests["refreshed"],
        "backtests_failed": backtests["failed"],
    }


def refresh_incremental_backtests(
    con,
    codes: list[str] | None = None,
    progress_callback: ProgressCallback | None = None,
    recompute_from_by_code: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    started = time.monotonic()
    target_codes = codes or list(FUNDS)
    refreshed = []
    failed = []
    stale_codes = []
    price_only_codes = []
    cutoff_by_code: dict[str, str | None] = {}
    recompute_from_by_code = recompute_from_by_code or {}
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
            if not latest_nav_date:
                continue
            # Recompute a small overlap even when the latest dates already
            # match.  Upstream daily prices and NAVs can be corrected after the
            # first import, so an append-only cache is not sufficient.
            stale_codes.append(code)
            normal_cutoff = incremental_backtest_cutoff_for_fund(con, code)
            revised_from = recompute_from_by_code.get(code)
            cutoff_by_code[code] = (
                revised_from
                if revised_from and (not normal_cutoff or revised_from < normal_cutoff)
                else normal_cutoff
            )
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
                    start_date=cutoff_by_code.get(code),
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
                start_date=cutoff_by_code.get(code),
            )
            if rows:
                refreshed.append({"code": code, "rows": len(rows), "latest_date": rows[-1]["date"]})
        except Exception as exc:
            failed.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
        con.commit()
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
    for code in target_codes:
        if code not in FUNDS:
            continue
        nav_date = latest_nav_date_for_fund(con, code)
        if not nav_date:
            continue
        for holding in latest_holdings(con, code):
            if holding["weight"] <= 0:
                continue
            add_price_target(targets, holding["secid"], nav_date)
            fx_secid = fx_secid_for_asset(holding["secid"])
            if fx_secid:
                add_price_target(targets, fx_secid, nav_date)
    return targets


def current_realtime_secids(
    con,
    codes: list[str] | None = None,
) -> list[str]:
    target_codes = codes if codes is not None else list(FUNDS)
    secids: set[str] = set()
    for code in target_codes:
        if code not in FUNDS:
            continue
        secids.add(fund_secid(code))
        for holding in latest_holdings(con, code):
            secids.add(holding["secid"])
            fx_secid = fx_secid_for_asset(holding["secid"])
            if fx_secid:
                secids.add(fx_secid)
    return sorted(secids)


def full_backtest_price_targets(
    con,
    codes: list[str],
    days: int,
) -> dict[str, set[str]]:
    targets: dict[str, set[str]] = {}
    for code in codes:
        if code in FUNDS:
            merge_price_targets(
                targets,
                full_backtest_price_targets_for_fund(con, code, days),
            )
    return targets


def full_backtest_price_targets_for_fund(
    con,
    code: str,
    days: int,
) -> dict[str, set[str]]:
    rows = con.execute(
        """
        select * from navs
        where fund_code = ?
        order by date desc
        limit ?
        """,
        (code, max(0, days) + 1),
    ).fetchall()
    navs = list(reversed(rows))
    targets: dict[str, set[str]] = {}
    for prev, curr in zip(navs, navs[1:]):
        for secid in backtest_secids_for_nav_pair(con, code, prev["date"]):
            add_price_target(targets, secid, prev["date"])
            add_price_target(targets, secid, curr["date"])
            fx_secid = fx_secid_for_asset(secid)
            if fx_secid:
                add_price_target(targets, fx_secid, prev["date"])
                add_price_target(targets, fx_secid, curr["date"])
        add_price_target(targets, fund_secid(code), curr["date"])
    return targets


def latest_nav_date_for_fund(con, code: str) -> str | None:
    row = con.execute("select max(date) as date from navs where fund_code = ?", (code,)).fetchone()
    return row["date"] if row else None


def latest_backtest_date_for_fund(con, code: str) -> str | None:
    row = con.execute("select max(date) as date from backtests where fund_code = ?", (code,)).fetchone()
    return row["date"] if row else None


def latest_backtest_trade_price_missing(con, code: str, backtest_date: str) -> bool:
    row = con.execute(
        "select 1 from daily_prices where secid = ? and date = ? and close > 0 limit 1",
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
        if not start_date and latest_backtest_date and curr["date"] <= latest_backtest_date:
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


def price_target_rows(targets: dict[str, set[str]]) -> list[dict[str, str]]:
    return [
        {"secid": secid, "date": target_date}
        for secid, dates in sorted(targets.items())
        for target_date in sorted(dates)
    ]


def incremental_backtest_anchor_date(
    con,
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


def import_fund_data(
    con,
    code: str,
    years: list[int] | None = None,
    latest_report: dict[str, str] | None = None,
    pending_backtest_from: str | None = None,
) -> set[str]:
    if code not in FUNDS:
        raise KeyError(f"fund {code} is not configured")
    cfg = FUNDS[code]

    # Complete every network-dependent step before opening the replacement
    # savepoint.  A slow or failed source must not hold a SQLite write lock or
    # expose a half-replaced fund snapshot.
    page = fund_page_data(code)
    navs = normalize_nav_rows(parse_navs(page["navs"]))
    publish_dates = fetch_report_publish_dates(code)
    latest_report = latest_report or fetch_latest_regular_report(code)
    if latest_report and latest_report.get("announcement_id"):
        latest_report = {
            **latest_report,
            "announcement_id": str(latest_report["announcement_id"]),
            "report_date": latest_report.get("report_date")
            or regular_report_date(latest_report.get("title") or ""),
        }
    stock_ratio = latest_stock_ratio(page["allocation"])
    cash_ratio = latest_cash_ratio(page["allocation"])
    allocations = allocation_by_date(page["allocation"])
    years = years or holding_report_years()
    periods = []
    if cfg.manual_holdings_mode not in {"replace", "proxy_only"}:
        periods = fetch_holdings(code, page["stock_codes"], years=years)
    manual_items = manual_holdings(cfg, publish_dates)
    expanded_manual_items = [
        expanded
        for manual in manual_items
        for expanded in expand_manual_holding(manual)
    ]
    expected_report_date = latest_report.get("report_date") if latest_report else None
    if expected_report_date:
        available_report_dates = {item[0] for item in periods}
        available_report_dates.update(item["report_date"] for item in manual_items)
        if cfg.proxy_secids or cfg.manual_holdings_mode in {
            "proxy_only",
            "proxy_then_manual_replace",
        }:
            available_report_dates.update(allocations)
        if expected_report_date not in available_report_dates:
            raise ValueError(
                f"holding snapshot for report {expected_report_date} is not available"
            )

    secids: set[str] = {fund_secid(code)}
    con.execute("savepoint replace_fund_snapshot")
    try:
        con.execute(
            """
            insert or replace into funds(code, name, exchange_market, fund_type, note, updated_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (code, page["name"], cfg.exchange_market, cfg.fund_type, cfg.note, utc_now()),
        )
        upsert_nav_rows(con, code, navs)
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
        if pending_backtest_from:
            pending = get_meta(con, "pending_report_backtests", {}) or {}
            if not isinstance(pending, dict):
                pending = {}
            set_meta(
                con,
                "pending_report_backtests",
                {**pending, code: pending_backtest_from},
            )

        con.execute("delete from holdings where fund_code = ?", (code,))
        if cfg.manual_holdings_mode == "replace":
            for holding in expanded_manual_items:
                add_single_holding(con, code, **holding)
                secids.add(holding["secid"])
        elif cfg.manual_holdings_mode == "proxy_then_manual_replace":
            manual_dates = {item["report_date"] for item in manual_items}
            add_proxy_periods(
                con,
                code,
                cfg,
                publish_dates,
                allocations,
                cash_ratio,
                stock_ratio,
                skip_dates=manual_dates,
            )
            secids.update(cfg.proxy_secids)
            for holding in expanded_manual_items:
                add_single_holding(con, code, **holding)
                secids.add(holding["secid"])
        elif cfg.manual_holdings_mode == "proxy_only":
            add_proxy_periods(con, code, cfg, publish_dates, allocations, cash_ratio, stock_ratio)
            secids.update(cfg.proxy_secids)
        elif periods:
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
                add_proxy_holdings(
                    con,
                    code,
                    report_date,
                    publish_dates.get(report_date),
                    cfg.proxy_secids,
                    proxy_weight,
                )
                secids.update(cfg.proxy_secids)
            if cfg.proxy_secids:
                add_missing_proxy_periods(
                    con,
                    code,
                    cfg,
                    publish_dates,
                    allocations,
                    period_dates,
                    cash_ratio,
                    stock_ratio,
                )
        elif cfg.proxy_secids:
            add_proxy_periods(con, code, cfg, publish_dates, allocations, cash_ratio, stock_ratio)
            secids.update(cfg.proxy_secids)

        if cfg.manual_holdings_mode not in {"replace", "proxy_only", "proxy_then_manual_replace"}:
            for holding in expanded_manual_items:
                add_single_holding(con, code, **holding)
                secids.add(holding["secid"])

        holding_count = con.execute(
            "select count(*) from holdings where fund_code = ? and weight > 0",
            (code,),
        ).fetchone()[0]
        if not holding_count:
            raise ValueError(f"empty holding snapshot for {code}")
        if expected_report_date:
            expected_holding_count = con.execute(
                """
                select count(*) from holdings
                where fund_code = ? and report_date = ? and weight > 0
                """,
                (code, expected_report_date),
            ).fetchone()[0]
            if not expected_holding_count:
                raise ValueError(
                    f"empty holding snapshot for report {expected_report_date}"
                )
    except Exception:
        con.execute("rollback to replace_fund_snapshot")
        con.execute("release replace_fund_snapshot")
        raise
    else:
        con.execute("release replace_fund_snapshot")
    return secids


def upsert_nav_rows(con, code: str, navs: list[dict]) -> dict[str, Any]:
    normalized = normalize_nav_rows(navs)
    existing = {
        row["date"]: row
        for row in con.execute(
            """
            select date, nav, distribution, return_pct
            from navs where fund_code = ?
            """,
            (code,),
        )
    }
    changed = []
    revised_dates = []
    inserted_rows = 0
    for nav in normalized:
        old = existing.get(nav["date"])
        if old and all(
            old[field] == nav[field]
            for field in ("nav", "distribution", "return_pct")
        ):
            continue
        if old:
            revised_dates.append(nav["date"])
        else:
            inserted_rows += 1
        changed.append(nav)

    for nav in changed:
        con.execute(
            """
            insert into navs(fund_code, date, nav, distribution, return_pct)
            values (?, ?, ?, ?, ?)
            on conflict(fund_code, date) do update set
              nav = excluded.nav,
              distribution = excluded.distribution,
              return_pct = excluded.return_pct
            """,
            (code, nav["date"], nav["nav"], nav["distribution"], nav["return_pct"]),
        )
    return {
        "changed_rows": len(changed),
        "inserted_rows": inserted_rows,
        "revised_rows": len(revised_dates),
        "earliest_revised_date": min(revised_dates) if revised_dates else None,
    }


def normalize_nav_rows(navs: list[dict]) -> list[dict[str, Any]]:
    if not navs:
        raise ValueError("empty nav series")
    result = []
    previous_date = None
    for item in navs:
        date_text = item.get("date")
        try:
            parsed_date = datetime.strptime(date_text, "%Y-%m-%d").date().isoformat()
            nav = float(item["nav"])
            distribution = float(item.get("distribution") or 0)
            return_pct = item.get("return_pct")
            return_pct = float(return_pct) if return_pct is not None else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid nav row: {item!r}") from exc
        if nav <= 0 or not math.isfinite(nav):
            raise ValueError(f"invalid nav value: {item!r}")
        if distribution < 0 or not math.isfinite(distribution):
            raise ValueError(f"invalid distribution: {item!r}")
        if return_pct is not None and not math.isfinite(return_pct):
            raise ValueError(f"invalid nav return: {item!r}")
        if previous_date and parsed_date <= previous_date:
            raise ValueError("nav dates must be strictly increasing")
        result.append(
            {
                "date": parsed_date,
                "nav": nav,
                "distribution": distribution,
                "return_pct": return_pct,
            }
        )
        previous_date = parsed_date
    return result


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
    if not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight <= 0:
        return
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
    target_periods = dict(
        fetch_holdings("520560", target_page["stock_codes"], years=holding_report_years(count=2))
    )
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


def holding_report_years(as_of: date | None = None, count: int = 4) -> list[int]:
    if count < 1:
        raise ValueError("count must be at least 1")
    current_year = (as_of or app_today()).year
    return [current_year - offset for offset in range(count)]


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
        return 0.0 if 0 < configured_weight < MIN_PROXY_WEIGHT else configured_weight
    allocation = allocations.get(report_date, {})
    cash_ratio = allocation.get("cash", fallback_cash_ratio)
    stock_ratio = allocation.get("stock", fallback_stock_ratio)
    if proxy_secids:
        if proxy_basis == "stock_gap":
            weight = max(0.0, min(1.0, stock_ratio - disclosed_weight))
        else:
            weight = max(0.0, min(1.0, 1.0 - cash_ratio - disclosed_weight))
    else:
        weight = max(0.0, min(1.0, stock_ratio - disclosed_weight))
    return 0.0 if weight < MIN_PROXY_WEIGHT else weight


def latest_report_date_from_allocation(allocation: dict) -> str | None:
    categories = allocation.get("categories") or []
    return categories[-1] if categories else None


def realtime_source_diagnostic_stats(
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    by_source: dict[str, dict[str, Any]] = {}
    affected_secids: set[str] = set()
    error_types: dict[str, int] = {}
    for diagnostic in diagnostics:
        source = str(diagnostic.get("source") or "unknown")
        state = by_source.setdefault(
            source,
            {"event_count": 0, "secids": set()},
        )
        state["event_count"] += 1
        secids = {str(secid) for secid in diagnostic.get("secids") or []}
        state["secids"].update(secids)
        affected_secids.update(secids)
        error = str(diagnostic.get("error") or "UnknownError")
        error_type = error.split(":", 1)[0] or "UnknownError"
        error_types[error_type] = error_types.get(error_type, 0) + 1
    return {
        "count": len(diagnostics),
        "stored_count": min(200, len(diagnostics)),
        "truncated": len(diagnostics) > 200,
        "affected_secid_count": len(affected_secids),
        "error_types": dict(sorted(error_types.items())),
        "by_source": {
            source: {
                "event_count": state["event_count"],
                "secid_count": len(state["secids"]),
            }
            for source, state in sorted(by_source.items())
        },
    }


def log_realtime_source_diagnostic_summary(
    stats: dict[str, Any],
    *,
    final_missing_count: int,
) -> None:
    by_source = stats.get("by_source") or {}
    if not by_source:
        return
    sources = sorted(
        by_source.items(),
        key=lambda item: (-item[1]["event_count"], item[0]),
    )
    source_summary = ",".join(
        f"{source}:{details['event_count']}/{details['secid_count']}"
        for source, details in sources[:5]
    )
    if len(sources) > 5:
        source_summary += f",+{len(sources) - 5}"
    errors = sorted(
        (stats.get("error_types") or {}).items(),
        key=lambda item: (-item[1], item[0]),
    )
    error_summary = ",".join(f"{name}:{count}" for name, count in errors[:3])
    if len(errors) > 3:
        error_summary += f",+{len(errors) - 3}"
    LOGGER.warning(
        "Realtime quote sources degraded: events=%s affected_secids=%s "
        "sources=%s errors=%s final_missing=%s details=metadata",
        stats["count"],
        stats["affected_secid_count"],
        source_summary,
        error_summary,
        final_missing_count,
    )


def refresh_quotes(
    con,
    secids: list[str],
    attempts: int = 3,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    attempt_at = utc_now()
    requested = sorted(set(secids))
    remaining = requested
    saved: set[str] = set()
    quotes_by_secid: dict[str, dict[str, Any]] = {}
    source_diagnostics: list[dict[str, Any]] = []
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

        for quote in fetch_realtime_quotes(
            remaining,
            progress_callback=quote_source_progress,
            diagnostics=source_diagnostics,
        ):
            normalized = normalize_realtime_quote(quote)
            if normalized is None:
                continue
            saved.add(normalized["secid"])
            quotes_by_secid[normalized["secid"]] = normalized
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

    completed_at = utc_now()
    set_meta(con, "last_realtime_quotes_attempt_at", attempt_at)
    for secid in requested:
        market_text, symbol = secid.split(".", 1)
        con.execute(
            """
            insert into quotes
            (secid, symbol, market, name, price, pct, previous_close, quote_time,
             session_date, last_attempt_at, last_success_at, fetch_status, updated_at)
            values (?, ?, ?, ?, null, null, null, null, null, ?, null, 'missing', ?)
            on conflict(secid) do update set
              last_attempt_at = excluded.last_attempt_at,
              fetch_status = 'missing'
            """,
            (secid, symbol, int(market_text), symbol, completed_at, completed_at),
        )
    for quote in quotes_by_secid.values():
        con.execute(
            """
            insert into quotes
            (secid, symbol, market, name, price, pct, previous_close, quote_time,
             session_date, last_attempt_at, last_success_at, fetch_status, updated_at)
            values (:secid, :symbol, :market, :name, :price, :pct, :previous_close, :quote_time,
                    :session_date, :last_attempt_at, :last_success_at, 'ok', :updated_at)
            on conflict(secid) do update set
              symbol = excluded.symbol,
              market = excluded.market,
              name = excluded.name,
              price = excluded.price,
              pct = excluded.pct,
              previous_close = excluded.previous_close,
              quote_time = excluded.quote_time,
              session_date = excluded.session_date,
              last_attempt_at = excluded.last_attempt_at,
              last_success_at = excluded.last_success_at,
              fetch_status = 'ok',
              updated_at = excluded.updated_at
            """,
            {
                **quote,
                "last_attempt_at": completed_at,
                "last_success_at": completed_at,
                "updated_at": completed_at,
            },
        )
    set_meta(con, "last_realtime_quotes_refresh_at", completed_at)
    set_meta(con, "last_realtime_quotes_completed_at", completed_at)
    if not remaining:
        set_meta(con, "last_realtime_quotes_refresh_success_at", completed_at)
    elif saved:
        set_meta(con, "last_realtime_quotes_partial_at", completed_at)
    else:
        set_meta(con, "last_realtime_quotes_failed_at", completed_at)
    set_meta(con, "last_realtime_quotes_fetch_missing_secids", remaining)
    sorted_diagnostics = sorted(
        source_diagnostics,
        key=lambda item: (
            item.get("source") or "",
            ",".join(item.get("secids") or []),
            item.get("error") or "",
        ),
    )
    set_meta(con, "last_realtime_quotes_source_diagnostics", sorted_diagnostics[:200])
    diagnostic_stats = realtime_source_diagnostic_stats(sorted_diagnostics)
    set_meta(con, "last_realtime_quotes_source_diagnostic_stats", diagnostic_stats)
    log_realtime_source_diagnostic_summary(
        diagnostic_stats,
        final_missing_count=len(remaining),
    )
    LOGGER.info(
        "Realtime quotes refreshed in %.1fs: requested=%s saved=%s missing=%s",
        time.monotonic() - started,
        len(requested),
        len(saved),
        len(remaining),
    )
    return {"requested": len(requested), "saved": len(saved), "missing": remaining}


def refresh_daily_prices(
    con,
    secids: list[str],
    commit_every: int = 1000,
    *,
    batch_deadline_seconds: float = DAILY_PRICE_BATCH_DEADLINE_SECONDS,
    instrument_deadline_seconds: float = DAILY_PRICE_INSTRUMENT_DEADLINE_SECONDS,
) -> None:
    started = time.monotonic()
    tasks = []
    for secid in secids:
        row = con.execute(
            "select max(date) as max_date from daily_prices where secid = ? and close > 0",
            (secid,),
        ).fetchone()
        begin = "20240101"
        if row and row["max_date"]:
            begin_date = datetime.fromisoformat(row["max_date"]).date() - timedelta(days=7)
            begin = begin_date.strftime("%Y%m%d")
        tasks.append((secid, begin, None))
    LOGGER.info("Daily price refresh started: secids=%s", len(tasks))

    saved_rows = 0
    completed = 0
    failed = 0
    empty = 0
    diagnostics: list[dict[str, str]] = []

    def handle_result(
        secid: str,
        rows: list[dict[str, Any]] | None,
        error: Exception | None,
    ) -> None:
        nonlocal saved_rows, completed, failed, empty
        completed += 1
        if error is not None:
            failed += 1
            diagnostic = daily_price_fetch_diagnostic(secid, error)
            diagnostics.append(diagnostic)
            LOGGER.warning(
                "Daily price refresh failed: secid=%s type=%s error=%s",
                secid,
                diagnostic["type"],
                diagnostic["error"],
            )
            return
        if not rows:
            empty += 1
            LOGGER.warning("Daily price refresh returned no rows: secid=%s", secid)
            return
        for row in rows:
            normalized = normalize_daily_price_row(row)
            if normalized is None:
                continue
            con.execute(
                """
                insert or replace into daily_prices
                (secid, date, close, pct, source, adjustment, updated_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    secid,
                    normalized["date"],
                    normalized["close"],
                    normalized["pct"],
                    normalized["source"],
                    normalized["adjustment"],
                    utc_now(),
                ),
            )
            saved_rows += 1
            if commit_every and saved_rows % commit_every == 0:
                con.commit()
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

    batch_diagnostics = execute_daily_price_fetches(
        tasks,
        handle_result,
        batch_deadline_seconds=batch_deadline_seconds,
        instrument_deadline_seconds=instrument_deadline_seconds,
    )
    diagnostics.extend(batch_diagnostics)
    failed += len(batch_diagnostics)
    completed += len(batch_diagnostics)
    set_meta(con, "last_daily_prices_refresh_diagnostics", diagnostics)
    set_meta(
        con,
        "last_daily_prices_refresh_stats",
        {
            "requested_count": len(tasks),
            "completed_count": completed,
            "saved_rows": saved_rows,
            "failed_count": failed,
            "empty_count": empty,
            "deadline_count": sum(
                item["type"] in {"instrument_deadline", "batch_deadline"}
                for item in diagnostics
            ),
        },
    )
    con.commit()
    LOGGER.info(
        "Daily prices refreshed in %.1fs: secids=%s saved_rows=%s failed=%s empty=%s deadlines=%s",
        time.monotonic() - started,
        len(tasks),
        saved_rows,
        failed,
        empty,
        sum(
            item["type"] in {"instrument_deadline", "batch_deadline"}
            for item in diagnostics
        ),
    )


def refresh_daily_prices_for_targets(
    con,
    targets: dict[str, set[str]],
    commit_every: int = 1000,
    progress_callback: ProgressCallback | None = None,
    *,
    batch_deadline_seconds: float = DAILY_PRICE_BATCH_DEADLINE_SECONDS,
    instrument_deadline_seconds: float = DAILY_PRICE_INSTRUMENT_DEADLINE_SECONDS,
) -> dict[str, Any]:
    started = time.monotonic()
    missing_targets = missing_daily_price_targets(con, targets)
    requested = count_price_targets(missing_targets)
    if not missing_targets:
        total_targets = count_price_targets(targets)
        completed_at = utc_now()
        set_meta(con, "last_daily_prices_targeted_refresh_at", completed_at)
        set_meta(con, "last_daily_prices_targeted_unresolved", [])
        set_meta(con, "last_daily_prices_targeted_diagnostics", [])
        con.commit()
        emit_progress(
            progress_callback,
            phase="daily_prices",
            label="日线",
            completed=total_targets,
            total=total_targets,
            message="requested=0 saved=0 unresolved=0",
        )
        LOGGER.info(
            "Daily price targeted refresh skipped: secids=%s targets=%s missing_targets=0",
            len(targets),
            count_price_targets(targets),
        )
        return {"requested": 0, "saved": 0, "unresolved": [], "saved_rows": 0}

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

    saved_rows = 0
    completed = 0
    failed = 0
    empty = 0
    diagnostics: list[dict[str, str]] = []

    def handle_result(
        secid: str,
        rows: list[dict[str, Any]] | None,
        error: Exception | None,
    ) -> None:
        nonlocal saved_rows, completed, failed, empty
        completed += 1
        if error is not None:
            failed += 1
            diagnostic = daily_price_fetch_diagnostic(secid, error)
            diagnostics.append(diagnostic)
            LOGGER.warning(
                "Daily price targeted refresh failed: secid=%s type=%s error=%s",
                secid,
                diagnostic["type"],
                diagnostic["error"],
            )
        elif not rows:
            empty += 1
            LOGGER.warning("Daily price targeted refresh returned no rows: secid=%s", secid)
        else:
            for row in rows:
                normalized = normalize_daily_price_row(row)
                if normalized is None:
                    continue
                con.execute(
                    """
                    insert or replace into daily_prices
                    (secid, date, close, pct, source, adjustment, updated_at)
                    values (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        secid,
                        normalized["date"],
                        normalized["close"],
                        normalized["pct"],
                        normalized["source"],
                        normalized["adjustment"],
                        utc_now(),
                    ),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
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

    batch_diagnostics = execute_daily_price_fetches(
        tasks,
        handle_result,
        batch_deadline_seconds=batch_deadline_seconds,
        instrument_deadline_seconds=instrument_deadline_seconds,
    )
    diagnostics.extend(batch_diagnostics)
    failed += len(batch_diagnostics)
    completed += len(batch_diagnostics)

    unresolved_targets = missing_daily_price_targets(con, missing_targets)
    unresolved = price_target_rows(unresolved_targets)
    saved = requested - len(unresolved)
    completed_at = utc_now()
    set_meta(con, "last_daily_prices_targeted_refresh_at", completed_at)
    set_meta(con, "last_daily_prices_targeted_unresolved", unresolved)
    set_meta(con, "last_daily_prices_targeted_diagnostics", diagnostics)
    con.commit()
    emit_progress(
        progress_callback,
        phase="daily_prices",
        label="日线",
        completed=len(tasks),
        total=len(tasks),
        message=(
            f"requested={requested} saved={saved} saved_rows={saved_rows} "
            f"unresolved={len(unresolved)}"
        ),
    )
    if unresolved:
        LOGGER.warning(
            "Daily price targeted refresh left unresolved targets: count=%s targets=%s",
            len(unresolved),
            unresolved,
        )
    LOGGER.info(
        "Daily price targeted refresh completed in %.1fs: secids=%s requested=%s saved=%s "
        "saved_rows=%s unresolved=%s failed=%s empty=%s deadlines=%s",
        time.monotonic() - started,
        len(tasks),
        requested,
        saved,
        saved_rows,
        len(unresolved),
        failed,
        empty,
        sum(
            item["type"] in {"instrument_deadline", "batch_deadline"}
            for item in diagnostics
        ),
    )
    result = {
        "requested": requested,
        "saved": saved,
        "unresolved": unresolved,
        "saved_rows": saved_rows,
    }
    if diagnostics:
        result["diagnostics"] = diagnostics
    return result


def refresh_mark_prices(con, secids: list[str], commit_every: int = 1000) -> None:
    started = time.monotonic()
    tasks = []
    for secid in secids:
        symbol = US_EQUITY_CLOSE_MARKS.get(secid)
        if not symbol:
            continue
        row = con.execute(
            """
            select max(date) as max_date from mark_prices
            where secid = ? and source = 'yahoo_daily_close' and close > 0
            """,
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
                normalized = normalize_daily_price_row(row)
                if normalized is None:
                    continue
                con.execute(
                    """
                    insert or replace into mark_prices(secid, date, close, source)
                    values (?, ?, ?, 'yahoo_daily_close')
                    """,
                    (secid, normalized["date"], normalized["close"]),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
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
                normalized = normalize_daily_price_row(row)
                if normalized is None:
                    continue
                con.execute(
                    """
                    insert or replace into mark_prices(secid, date, close, source)
                    values (?, ?, ?, 'yahoo_daily_close')
                    """,
                    (secid, normalized["date"], normalized["close"]),
                )
                saved_rows += 1
                if commit_every and saved_rows % commit_every == 0:
                    con.commit()
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
            if price_is_fresh_for_date(
                con,
                table,
                where_sql,
                params_for_secid(secid),
                date,
                secid=secid,
            ):
                continue
            add_price_target(missing, secid, date)
    return missing


def price_is_fresh_for_date(
    con,
    table: str,
    where_sql: str,
    params: tuple[Any, ...],
    date: str,
    secid: str | None = None,
) -> bool:
    if table not in {"daily_prices", "mark_prices"}:
        raise ValueError(f"unsupported price table: {table}")
    previous = con.execute(
        f"""
        select date from {table}
        where {where_sql} and date <= ? and close > 0
        order by date desc
        limit 1
        """,
        (*params, date),
    ).fetchone()
    if not previous:
        return False
    if previous["date"] == date:
        return True
    return bool(
        secid
        and expected_market_closure_gap(secid, date, previous["date"])
    )


def price_target_window(dates: set[str]) -> tuple[str, str]:
    ordered = sorted(dates)
    begin = datetime.fromisoformat(ordered[0]).date() - timedelta(
        days=PRICE_TARGET_LOOKBACK_DAYS
    )
    return begin.strftime("%Y%m%d"), price_date_param(ordered[-1])


def price_date_param(date: str) -> str:
    return datetime.fromisoformat(date).strftime("%Y%m%d")
