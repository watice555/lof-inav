from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from html import unescape
from typing import Any, Callable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from requests import RequestException
from bs4 import BeautifulSoup

from .config import EASTMONEY_HEADERS, SINA_PRICE_SYMBOLS, YAHOO_PRICE_SYMBOLS
from .market_calendar import historical_price_market


RealtimeProgressCallback = Callable[[dict[str, Any]], None]
RealtimeDiagnostics = list[dict[str, Any]]
LOGGER = logging.getLogger(__name__)
_REALTIME_DIAGNOSTICS_LOCK = threading.Lock()


def _positive_env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value > 0 else default


DAILY_PRICE_INSTRUMENT_DEADLINE_SECONDS = _positive_env_seconds(
    "LOF_INAV_PRICE_INSTRUMENT_DEADLINE_SECONDS",
    90.0,
)
DAILY_PRICE_BATCH_DEADLINE_SECONDS = _positive_env_seconds(
    "LOF_INAV_PRICE_BATCH_DEADLINE_SECONDS",
    15 * 60.0,
)
_DAILY_PRICE_DEADLINE: ContextVar[float | None] = ContextVar(
    "daily_price_deadline",
    default=None,
)


class DailyPriceDeadlineExceeded(TimeoutError):
    """A historical-price operation exhausted its absolute time budget."""


@contextmanager
def daily_price_deadline(deadline: float):
    current = _DAILY_PRICE_DEADLINE.get()
    effective = min(current, deadline) if current is not None else deadline
    token = _DAILY_PRICE_DEADLINE.set(effective)
    try:
        yield effective
    finally:
        _DAILY_PRICE_DEADLINE.reset(token)


def _daily_price_deadline_remaining() -> float | None:
    deadline = _DAILY_PRICE_DEADLINE.get()
    return None if deadline is None else deadline - time.monotonic()


def _raise_if_daily_price_deadline_exceeded(operation: str) -> None:
    remaining = _daily_price_deadline_remaining()
    if remaining is not None and remaining <= 0:
        raise DailyPriceDeadlineExceeded(f"{operation} exceeded its absolute deadline")


def _request_timeout_within_deadline(timeout: float) -> float:
    remaining = _daily_price_deadline_remaining()
    if remaining is None:
        return timeout
    if remaining <= 0:
        raise DailyPriceDeadlineExceeded("historical-price request exceeded its absolute deadline")
    return max(0.001, min(timeout, remaining))


def _sleep_within_daily_price_deadline(seconds: float, operation: str) -> None:
    remaining = _daily_price_deadline_remaining()
    if remaining is None:
        time.sleep(seconds)
        return
    if remaining <= 0:
        raise DailyPriceDeadlineExceeded(f"{operation} exceeded its absolute deadline")
    time.sleep(min(seconds, remaining))
    _raise_if_daily_price_deadline_exceeded(operation)


@contextmanager
def _lock_within_daily_price_deadline(lock: threading.Lock, operation: str):
    remaining = _daily_price_deadline_remaining()
    if remaining is None:
        acquired = lock.acquire()
    elif remaining <= 0:
        acquired = False
    else:
        acquired = lock.acquire(timeout=remaining)
    if not acquired:
        raise DailyPriceDeadlineExceeded(f"{operation} exceeded its absolute deadline")
    try:
        yield
    finally:
        lock.release()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def app_today():
    return datetime.now(CHINA_TZ).date()


CHINA_TZ = timezone(timedelta(hours=8))
EASTMONEY_INDEX_SECIDS = {
    "1.000979",
    "2.930641",
    "2.930713",
    "2.930720",
    "2.930721",
    "2.930743",
    "2.930790",
    "2.930791",
    "2.930820",
    "2.930875",
    "2.930914",
    "2.931068",
    "2.931136",
    "2.H30094",
    "124.HSHKI",
    "124.HSMI",
    "124.HSSI",
    "124.HSTECH",
}
CSINDEX_INDEX_SECIDS = {
    "1.000808",
    "1.000841",
    "1.000961",
    "1.000979",
    "1.000998",
    "2.930641",
    "2.930713",
    "2.930720",
    "2.930721",
    "2.930743",
    "2.930790",
    "2.930791",
    "2.930820",
    "2.930875",
    "2.930914",
    "2.931068",
    "2.931136",
    "2.H30094",
}
CSINDEX_REQUEST_INTERVAL_SECONDS = 0.25
_CSINDEX_REQUEST_LOCK = threading.Lock()
_csindex_last_request_at = 0.0
HANG_SENG_INDEX_SERIES = {
    "124.HSFML25": ("hschk25", "00021.00"),
    "124.HSHKI": ("hshkis", "02019.00"),
    "124.HSMI": ("sizeindexes", "00013.00"),
    "124.HSSI": ("sizeindexes", "00016.00"),
    "124.HSTECH": ("hstech", "02083.00"),
}
PURCHASE_LIMIT_UNBOUNDED_SORT_VALUE = 1_000_000_000_000_000.0


def _get(url: str, **kwargs: Any) -> requests.Response:
    headers = dict(EASTMONEY_HEADERS)
    headers.update(kwargs.pop("headers", {}))
    timeout = kwargs.pop("timeout", 20)
    attempts = kwargs.pop("attempts", 3)
    last_error: Exception | None = None
    for attempt in range(attempts):
        _raise_if_daily_price_deadline_exceeded(f"GET {url}")
        try:
            response = requests.get(
                url,
                headers=headers,
                timeout=_request_timeout_within_deadline(timeout),
                **kwargs,
            )
            response.raise_for_status()
            _raise_if_daily_price_deadline_exceeded(f"GET {url}")
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                _sleep_within_daily_price_deadline(
                    0.5 * (attempt + 1),
                    f"GET {url}",
                )
    if last_error:
        raise last_error
    raise RuntimeError(f"request failed: {url}")


def _post(url: str, **kwargs: Any) -> requests.Response:
    headers = dict(EASTMONEY_HEADERS)
    headers.update(kwargs.pop("headers", {}))
    timeout = kwargs.pop("timeout", 20)
    attempts = kwargs.pop("attempts", 3)
    last_error: Exception | None = None
    for attempt in range(attempts):
        _raise_if_daily_price_deadline_exceeded(f"POST {url}")
        try:
            response = requests.post(
                url,
                headers=headers,
                timeout=_request_timeout_within_deadline(timeout),
                **kwargs,
            )
            response.raise_for_status()
            _raise_if_daily_price_deadline_exceeded(f"POST {url}")
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt + 1 < attempts:
                _sleep_within_daily_price_deadline(
                    0.5 * (attempt + 1),
                    f"POST {url}",
                )
    if last_error:
        raise last_error
    raise RuntimeError(f"request failed: {url}")


def _extract_var(script: str, name: str) -> str | None:
    match = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*(.*?);", script, re.S)
    return match.group(1).strip() if match else None


def fund_page_data(code: str) -> dict[str, Any]:
    text = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js").text
    name_raw = _extract_var(text, "fS_name")
    nav_raw = _extract_var(text, "Data_netWorthTrend")
    allocation_raw = _extract_var(text, "Data_assetAllocation")
    stock_codes_raw = _extract_var(text, "stockCodesNew")
    if not name_raw or not nav_raw:
        raise ValueError(f"missing fund page data for {code}")
    return {
        "name": json.loads(name_raw),
        "navs": json.loads(nav_raw),
        "allocation": json.loads(allocation_raw) if allocation_raw else {},
        "stock_codes": json.loads(stock_codes_raw) if stock_codes_raw else [],
    }


def parse_navs(raw_navs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in raw_navs:
        date = datetime.fromtimestamp(item["x"] / 1000, tz=CHINA_TZ).date().isoformat()
        rows.append(
            {
                "date": date,
                "nav": float(item["y"]),
                "distribution": parse_distribution(item.get("unitMoney") or ""),
                "return_pct": float(item.get("equityReturn") or 0),
            }
        )
    return rows


def parse_distribution(text: str) -> float:
    match = re.search(r"每份派现金\s*([0-9.]+)\s*元", text)
    return float(match.group(1)) if match else 0.0


def latest_stock_ratio(allocation: dict[str, Any]) -> float:
    series = allocation.get("series") or []
    for item in series:
        if item.get("name") == "股票占净比" and item.get("data"):
            return float(item["data"][-1]) / 100
    return 0.0


def latest_cash_ratio(allocation: dict[str, Any]) -> float:
    series = allocation.get("series") or []
    for item in series:
        if item.get("name") == "现金占净比" and item.get("data"):
            return float(item["data"][-1]) / 100
    return 0.0


def allocation_by_date(allocation: dict[str, Any]) -> dict[str, dict[str, float]]:
    categories = allocation.get("categories") or []
    result = {date: {"stock": 0.0, "bond": 0.0, "cash": 0.0} for date in categories}
    key_map = {"股票占净比": "stock", "债券占净比": "bond", "现金占净比": "cash"}
    for item in allocation.get("series") or []:
        key = key_map.get(item.get("name"))
        if not key:
            continue
        for date, value in zip(categories, item.get("data") or []):
            result[date][key] = float(value) / 100
    return result


def _market_for_symbol(symbol: str, stock_codes: list[str]) -> int:
    for secid in stock_codes:
        parts = secid.split(".", 1)
        if len(parts) == 2 and parts[1] == symbol:
            return int(parts[0])
    if re.fullmatch(r"6\d{5}", symbol):
        return 1
    if re.fullmatch(r"(00|20|30)\d{4}", symbol):
        return 0
    if re.fullmatch(r"\d{5}", symbol):
        return 116
    if re.fullmatch(r"[A-Z.]+", symbol):
        return 105
    return 0


def fetch_holdings(
    code: str, stock_codes: list[str], years: list[int] | None = None
) -> list[tuple[str, list[dict[str, Any]]]]:
    if years is None:
        current_year = app_today().year
        years = [current_year, current_year - 1]
    periods: list[tuple[str, list[dict[str, Any]]]] = []
    seen: set[str] = set()
    for year in years:
        for report_date, holdings in _fetch_holdings_year(code, stock_codes, year):
            if report_date not in seen:
                periods.append((report_date, holdings))
                seen.add(report_date)
    return sorted(periods, key=lambda item: item[0], reverse=True)


def _fetch_holdings_year(
    code: str, stock_codes: list[str], year: int
) -> list[tuple[str, list[dict[str, Any]]]]:
    url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
    params = {
        "type": "jjcc",
        "code": code,
        "topline": "80",
        "year": str(year),
        "month": "",
        "rt": "0.1",
    }
    text = _get(
        url,
        params=params,
        headers={"Referer": f"https://fundf10.eastmoney.com/ccmx_{code}.html"},
    ).text
    content_match = re.search(r'content:"(.*)",arryear:', text, re.S)
    if not content_match:
        return []
    html = unescape(content_match.group(1).replace('\\"', '"'))
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    periods: list[tuple[str, list[dict[str, Any]]]] = []
    for box in soup.select(".boxitem"):
        title = box.find("h4")
        if not title:
            continue
        match = re.search(r"(20\d{2})年(\d)季度", title.get_text(" ", strip=True))
        if not match:
            continue
        quarter_end = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}[match.group(2)]
        report_date = f"{match.group(1)}-{quarter_end}"
        holdings: list[dict[str, Any]] = []
        for row in box.select("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
            if len(cells) < 7 or not cells[0].isdigit():
                continue
            symbol = cells[1].strip()
            name = cells[2].strip()
            weight_cell = next((cell for cell in cells[3:] if "%" in cell), "")
            weight_text = weight_cell.replace("%", "").replace("--", "").replace(",", "").strip()
            if not weight_text:
                continue
            market = _market_for_symbol(symbol, stock_codes)
            holdings.append(
                {
                    "secid": f"{market}.{symbol}",
                    "symbol": symbol,
                    "name": name,
                    "weight": float(weight_text) / 100,
                    "source": "disclosed_stock",
                }
            )
        if holdings:
            periods.append((report_date, holdings))
    return periods


def emit_realtime_progress(
    progress_callback: RealtimeProgressCallback | None,
    phase: str,
    completed: int,
    message: str,
) -> None:
    if progress_callback:
        progress_callback(
            {
                "phase": phase,
                "label": "行情",
                "completed": max(0, min(98, completed)),
                "total": 100,
                "message": message,
            }
        )


def record_realtime_source_error(
    diagnostics: RealtimeDiagnostics | None,
    source: str,
    secids: list[str] | tuple[str, ...],
    exc: Exception,
) -> None:
    error = f"{type(exc).__name__}: {exc}"[:500]
    if diagnostics is not None:
        entry = {
            "source": source,
            "secids": sorted(set(secids)),
            "error": error,
        }
        with _REALTIME_DIAGNOSTICS_LOCK:
            diagnostics.append(entry)


def fetch_realtime_quotes(
    secids: list[str],
    progress_callback: RealtimeProgressCallback | None = None,
    diagnostics: RealtimeDiagnostics | None = None,
) -> list[dict[str, Any]]:
    if not secids:
        return []
    rows: list[dict[str, Any]] = []
    special = {
        "113.agm",
        *EASTMONEY_INDEX_SECIDS,
        *CSINDEX_INDEX_SECIDS,
        *HANG_SENG_INDEX_SERIES,
    }
    special_secids = [secid for secid in secids if secid in special]
    emit_realtime_progress(progress_callback, "quotes_start", 0, f"准备 {len(secids)} 个标的")
    with ThreadPoolExecutor(max_workers=min(8, len(special_secids) or 1)) as executor:
        futures = {
            executor.submit(special_realtime_quote, secid, diagnostics): secid
            for secid in special_secids
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                quote = future.result()
                if quote:
                    rows.append(quote)
            except RequestException as exc:
                record_realtime_source_error(
                    diagnostics,
                    "special_realtime",
                    [futures[future]],
                    exc,
                )
            emit_realtime_progress(
                progress_callback,
                "quotes_special",
                round(8 * completed / len(special_secids)),
                f"特殊行情 {completed}/{len(special_secids)}",
            )
    batches = [
        [secid for secid in secids[i : i + 30] if secid not in special]
        for i in range(0, len(secids), 30)
    ]
    batches = [batch for batch in batches if batch]
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(eastmoney_realtime_batch, batch): batch for batch in batches}
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                rows.extend(future.result())
            except RequestException as exc:
                record_realtime_source_error(
                    diagnostics,
                    "eastmoney_batch",
                    futures[future],
                    exc,
                )
            emit_realtime_progress(
                progress_callback,
                "quotes_eastmoney",
                8 + round(62 * completed / len(batches)),
                f"东方财富 {completed}/{len(batches)} 批",
            )
    seen = {row["secid"] for row in rows}
    cn_fallback = [secid for secid in secids if secid not in seen and is_cn_exchange_secid(secid)]
    cn_batches = [cn_fallback[i : i + 100] for i in range(0, len(cn_fallback), 100)]
    for index, batch in enumerate(cn_batches, start=1):
        try:
            rows.extend(sina_cn_realtime_quotes(batch))
        except (RequestException, ValueError, IndexError) as exc:
            record_realtime_source_error(diagnostics, "sina_cn_batch", batch, exc)
        emit_realtime_progress(
            progress_callback,
            "quotes_sina_cn",
            70 + round(12 * index / len(cn_batches)),
            f"新浪A股 {index}/{len(cn_batches)} 批",
        )
    seen = {row["secid"] for row in rows}
    fallback_secids = [secid for secid in secids if secid not in seen]
    with ThreadPoolExecutor(max_workers=min(4, len(fallback_secids) or 1)) as executor:
        futures = {
            executor.submit(fallback_realtime_quote, secid, diagnostics): secid
            for secid in fallback_secids
        }
        completed = 0
        for future in as_completed(futures):
            completed += 1
            try:
                quote = future.result()
            except Exception as exc:
                record_realtime_source_error(
                    diagnostics,
                    "fallback_realtime",
                    [futures[future]],
                    exc,
                )
                quote = None
            if quote:
                rows.append(quote)
            emit_realtime_progress(
                progress_callback,
                "quotes_fallback",
                82 + round(16 * completed / len(fallback_secids)),
                f"备用源 {completed}/{len(fallback_secids)}",
            )
    emit_realtime_progress(progress_callback, "quotes_source_done", 98, f"源请求完成，返回 {len(rows)} 条")
    return rows


def special_realtime_quote(
    secid: str,
    diagnostics: RealtimeDiagnostics | None = None,
) -> dict[str, Any] | None:
    if secid == "113.agm":
        return eastmoney_futures_quote(secid)
    if secid in CSINDEX_INDEX_SECIDS:
        try:
            quote = csindex_realtime_quote(secid)
            if quote:
                return quote
        except (RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            record_realtime_source_error(diagnostics, "csindex", [secid], exc)
        return eastmoney_index_quote(secid)
    if secid in HANG_SENG_INDEX_SERIES:
        try:
            quote = hang_seng_index_realtime_quote(secid)
            if quote:
                return quote
        except (RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            record_realtime_source_error(diagnostics, "hang_seng", [secid], exc)
        return eastmoney_index_quote(secid)
    if secid in EASTMONEY_INDEX_SECIDS:
        return eastmoney_index_quote(secid)
    return None


def fallback_realtime_quote(
    secid: str,
    diagnostics: RealtimeDiagnostics | None = None,
) -> dict[str, Any] | None:
    symbol = yahoo_price_symbol(secid)
    quote = None
    if symbol:
        try:
            quote = yahoo_realtime_quote(secid, symbol)
        except (RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            record_realtime_source_error(diagnostics, "yahoo", [secid], exc)
            quote = None
    if not quote:
        try:
            quote = sina_realtime_quote(secid)
        except (RequestException, ValueError, IndexError) as exc:
            record_realtime_source_error(diagnostics, "sina", [secid], exc)
            quote = None
    if not quote:
        try:
            quote = sina_cn_realtime_quote(secid)
        except (RequestException, ValueError, IndexError) as exc:
            record_realtime_source_error(diagnostics, "sina_cn", [secid], exc)
            quote = None
    return quote


def eastmoney_realtime_batch(secids: list[str]) -> list[dict[str, Any]]:
    response = _get(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        params={
            "fltt": "2",
            "invt": "2",
            "fields": "f12,f13,f14,f2,f3,f4,f18,f124",
            "secids": ",".join(secids),
        },
        timeout=3,
        attempts=1,
    ).json()
    rows = []
    for item in (response.get("data") or {}).get("diff") or []:
        quote_time = None
        if item.get("f124"):
            quote_time = datetime.fromtimestamp(item["f124"], tz=timezone.utc).isoformat(timespec="seconds")
        price = _to_float(item.get("f2"))
        if price is None:
            continue
        rows.append(
            {
                "secid": f"{item['f13']}.{item['f12']}",
                "symbol": item["f12"],
                "market": int(item["f13"]),
                "name": item.get("f14") or item["f12"],
                "price": price,
                "pct": _to_float(item.get("f3")),
                "previous_close": _to_float(item.get("f18")),
                "quote_time": quote_time,
            }
        )
    return rows


def fetch_daily_prices(
    secid: str,
    begin: str = "20240101",
    end: str = "20500101",
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    active_deadline = _DAILY_PRICE_DEADLINE.get()
    deadline = deadline or active_deadline or (
        time.monotonic() + DAILY_PRICE_INSTRUMENT_DEADLINE_SECONDS
    )
    with daily_price_deadline(deadline):
        try:
            return _fetch_daily_prices_with_deadline(secid, begin, end)
        except DailyPriceDeadlineExceeded as exc:
            raise DailyPriceDeadlineExceeded(
                f"historical price deadline exceeded for {secid}"
            ) from exc


def _fetch_daily_prices_with_deadline(
    secid: str,
    begin: str,
    end: str,
) -> list[dict[str, Any]]:
    if secid == "113.agm":
        return _best_daily_price_rows(
            [
                lambda: sina_futures_daily_prices(secid, "AG0", begin, end),
            ],
            require_weekday_continuity=False,
        )
    if secid in HANG_SENG_INDEX_SERIES:
        return _best_daily_price_rows(
            [
                lambda: hang_seng_index_daily_prices(secid, begin, end),
                lambda: eastmoney_index_daily_prices(secid, begin, end),
            ],
            require_weekday_continuity=False,
        )
    if secid in CSINDEX_INDEX_SECIDS:
        return _best_daily_price_rows(
            [
                lambda: csindex_daily_prices(secid, begin, end),
                lambda: eastmoney_index_daily_prices(secid, begin, end),
                lambda: sina_cn_daily_prices(secid, begin, end),
            ],
            require_weekday_continuity=False,
        )
    if secid in EASTMONEY_INDEX_SECIDS or is_cn_index_secid(secid):
        return _best_daily_price_rows(
            [
                lambda: eastmoney_index_daily_prices(secid, begin, end),
                lambda: sina_cn_daily_prices(secid, begin, end),
            ],
            require_weekday_continuity=False,
        )
    yahoo_symbol = yahoo_price_symbol(secid)
    if yahoo_symbol and is_a_share_stock_secid(secid):
        return _best_daily_price_rows(
            [
                lambda: yahoo_daily_prices(secid, yahoo_symbol, begin, end),
                lambda: sina_cn_daily_prices(secid, begin, end),
            ],
            require_weekday_continuity=False,
        )
    if secid in YAHOO_PRICE_SYMBOLS:
        return _best_daily_price_rows(
            [
                lambda: yahoo_daily_prices(secid, yahoo_symbol, begin, end),
            ],
            require_weekday_continuity=False,
        ) if yahoo_symbol else []
    candidates = [lambda: eastmoney_daily_prices(secid, begin, end)]
    if is_cn_exchange_secid(secid):
        candidates.append(lambda: sina_cn_daily_prices(secid, begin, end))
    if yahoo_symbol:
        candidates.append(lambda: yahoo_daily_prices(secid, yahoo_symbol, begin, end))
    return _best_daily_price_rows(candidates, require_weekday_continuity=False)


def _best_daily_price_rows(
    candidates: list,
    require_weekday_continuity: bool,
    attempts_per_source: int = 3,
) -> list[dict[str, Any]]:
    for fetch in candidates:
        for attempt in range(attempts_per_source):
            _raise_if_daily_price_deadline_exceeded("historical price source retries")
            try:
                rows = fetch()
            except DailyPriceDeadlineExceeded:
                raise
            except Exception:
                rows = []
            if _daily_price_rows_are_valid(rows, require_weekday_continuity):
                return rows
            if attempt + 1 < attempts_per_source:
                _sleep_within_daily_price_deadline(
                    0.5 * (attempt + 1),
                    "historical price source retries",
                )
    return []


def _daily_price_rows_are_valid(rows: list[dict[str, Any]], require_weekday_continuity: bool) -> bool:
    if not rows:
        return False
    dates = []
    last_date = None
    for row in rows:
        normalized = normalize_daily_price_row(row)
        if normalized is None:
            return False
        date = datetime.fromisoformat(normalized["date"]).date()
        if last_date and date <= last_date:
            return False
        dates.append(date)
        last_date = date
    return not require_weekday_continuity or not _has_missing_weekday_between_rows(dates)


def _has_missing_weekday_between_rows(dates) -> bool:
    for previous, current in zip(dates, dates[1:]):
        day = previous + timedelta(days=1)
        while day < current:
            if day.weekday() < 5:
                return True
            day += timedelta(days=1)
    return False


def normalize_daily_price_row(row: dict[str, Any]) -> dict[str, Any] | None:
    date_text = row.get("date")
    if not isinstance(date_text, str):
        return None
    try:
        parsed_date = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return None
    close = _to_float(row.get("close"))
    if close is None or close <= 0 or not math.isfinite(close):
        return None
    pct = _to_float(row.get("pct"))
    if pct is not None and (not math.isfinite(pct) or abs(pct) > 1000):
        pct = None
    return {
        "date": parsed_date.isoformat(),
        "close": close,
        "pct": pct,
        "source": str(row.get("source") or "unknown"),
        "adjustment": str(row.get("adjustment") or "unknown"),
    }


def normalize_realtime_quote(row: dict[str, Any]) -> dict[str, Any] | None:
    secid = row.get("secid")
    if not isinstance(secid, str) or "." not in secid:
        return None
    try:
        market_text, default_symbol = secid.split(".", 1)
        market = int(row.get("market", market_text))
    except (TypeError, ValueError):
        return None
    price = _to_float(row.get("price"))
    if price is None or price <= 0 or not math.isfinite(price):
        return None
    previous_close = _to_float(row.get("previous_close"))
    if previous_close is not None and (
        previous_close <= 0 or not math.isfinite(previous_close)
    ):
        previous_close = None
    pct = _to_float(row.get("pct"))
    if pct is not None and (not math.isfinite(pct) or abs(pct) > 1000):
        pct = None
    quote_time = row.get("quote_time")
    session_date = quote_session_date(secid, quote_time)
    return {
        "secid": secid,
        "symbol": str(row.get("symbol") or default_symbol),
        "market": market,
        "name": str(row.get("name") or default_symbol),
        "price": price,
        "pct": pct,
        "previous_close": previous_close,
        "quote_time": quote_time,
        "session_date": session_date,
    }


def quote_session_date(secid: str, quote_time: Any) -> str | None:
    if not quote_time:
        return None
    try:
        timestamp = datetime.fromisoformat(str(quote_time).replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    market = historical_price_market(secid)
    if market == "US":
        try:
            target_tz = ZoneInfo("America/New_York")
        except ZoneInfoNotFoundError:
            target_tz = timezone(timedelta(hours=-5))
    else:
        target_tz = CHINA_TZ
    return timestamp.astimezone(target_tz).date().isoformat()


def is_cn_exchange_secid(secid: str) -> bool:
    market, symbol = secid.split(".", 1)
    return market in {"0", "1"} and re.fullmatch(r"\d{6}", symbol) is not None


def is_cn_index_secid(secid: str) -> bool:
    market, symbol = secid.split(".", 1)
    return (market == "0" and re.fullmatch(r"399\d{3}", symbol) is not None) or (
        market == "1" and re.fullmatch(r"000\d{3}", symbol) is not None
    )


def is_a_share_stock_secid(secid: str) -> bool:
    market, symbol = secid.split(".", 1)
    return (
        market == "0"
        and re.fullmatch(r"(00|20|30)\d{4}", symbol) is not None
    ) or (
        market == "1"
        and re.fullmatch(r"(60|68|90)\d{4}", symbol) is not None
    )


def yahoo_price_symbol(secid: str) -> str | None:
    if secid in YAHOO_PRICE_SYMBOLS:
        return YAHOO_PRICE_SYMBOLS[secid]
    market, symbol = secid.split(".", 1)
    if market == "0" and re.fullmatch(r"(00|20|30)\d{4}", symbol):
        return f"{symbol}.SZ"
    if market == "1" and re.fullmatch(r"(60|68|90)\d{4}", symbol):
        return f"{symbol}.SS"
    if market == "116" and re.fullmatch(r"\d+", symbol):
        return f"{str(int(symbol)).zfill(4)}.HK"
    return None


def eastmoney_futures_quote(secid: str) -> dict[str, Any] | None:
    response = _get(
        f"https://futsseapi.eastmoney.com/static/{secid.replace('.', '_')}_qt",
        params={
            "field": "name,sc,dm,p,zdf,zde,utime,zjsj",
            "token": "1101ffec61617c99be287c1bec3085ff",
        },
        headers={"Referer": f"https://quote.eastmoney.com/unify/r/{secid}"},
        timeout=3,
        attempts=1,
    ).json()
    item = response.get("qt") or {}
    price = _to_float(item.get("p"))
    previous = _to_float(item.get("zjsj"))
    pct = _to_float(item.get("zdf"))
    quote_time = None
    if item.get("utime"):
        quote_time = datetime.fromtimestamp(int(item["utime"]), tz=timezone.utc).isoformat(timespec="seconds")
    if price is None:
        return None
    return {
        "secid": secid,
        "symbol": secid.split(".", 1)[1],
        "market": int(secid.split(".", 1)[0]),
        "name": item.get("name") or secid,
        "price": price,
        "pct": pct,
        "previous_close": previous,
        "quote_time": quote_time,
    }


def eastmoney_index_quote(secid: str) -> dict[str, Any] | None:
    try:
        response = _get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            params={
                "secid": secid,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fields": "f43,f57,f58,f60,f86,f107,f169,f170",
            },
            headers={"Referer": f"https://quote.eastmoney.com/unify/r/{secid}"},
            timeout=3,
            attempts=1,
        ).json()
    except RequestException:
        return eastmoney_index_trends_quote(secid)
    item = response.get("data") or {}
    price = _to_float(item.get("f43"))
    previous = _to_float(item.get("f60"))
    quote_time = None
    if item.get("f86"):
        quote_time = datetime.fromtimestamp(int(item["f86"]), tz=timezone.utc).isoformat(timespec="seconds")
    if price is None:
        return eastmoney_index_trends_quote(secid)
    market, code = secid.split(".", 1)
    return {
        "secid": secid,
        "symbol": item.get("f57") or code,
        "market": int(item.get("f107") or market),
        "name": item.get("f58") or secid,
        "price": price,
        "pct": _to_float(item.get("f170")),
        "previous_close": previous,
        "quote_time": quote_time,
    }


def eastmoney_index_trends_quote(secid: str) -> dict[str, Any] | None:
    response = _get(
        "https://push2his.eastmoney.com/api/qt/stock/trends2/get",
        params={
            "secid": secid,
            "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
            "ndays": "1",
            "iscr": "0",
            "iscca": "0",
        },
        headers={"Referer": f"https://quote.eastmoney.com/unify/r/{secid}"},
        timeout=3,
        attempts=1,
    ).json()
    data = response.get("data") or {}
    trends = data.get("trends") or []
    if not trends:
        return None
    parts = trends[-1].split(",")
    price = _to_float(parts[2] if len(parts) > 2 else None)
    previous = _to_float(data.get("preClose"))
    if price is None:
        return None
    quote_time = None
    if data.get("time"):
        quote_time = datetime.fromtimestamp(int(data["time"]), tz=timezone.utc).isoformat(timespec="seconds")
    market, code = secid.split(".", 1)
    return {
        "secid": secid,
        "symbol": data.get("code") or code,
        "market": int(data.get("market") or market),
        "name": data.get("name") or secid,
        "price": price,
        "pct": (price / previous - 1) * 100 if previous else None,
        "previous_close": previous,
        "quote_time": quote_time,
    }


def _hang_seng_index_item(document: dict[str, Any], secid: str) -> dict[str, Any]:
    mapping = HANG_SENG_INDEX_SERIES.get(secid)
    if not mapping:
        raise ValueError(f"unsupported Hang Seng index: {secid}")
    target_code = mapping[1]
    matches: list[dict[str, Any]] = []

    def visit(items: Any) -> None:
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("indexCode") == target_code:
                matches.append(item)
            visit(item.get("subIndexList"))

    for series in document.get("indexSeriesList") or []:
        if isinstance(series, dict):
            visit(series.get("indexList"))
    if len(matches) != 1:
        raise ValueError(
            f"expected one Hang Seng index {target_code}, found {len(matches)}"
        )
    return matches[0]


def _positive_finite_float(value: Any, label: str) -> float:
    parsed = _to_float(value)
    if parsed is None or parsed <= 0 or not math.isfinite(parsed):
        raise ValueError(f"invalid {label}: {value!r}")
    return parsed


def hang_seng_index_realtime_quote(secid: str) -> dict[str, Any] | None:
    mapping = HANG_SENG_INDEX_SERIES.get(secid)
    if not mapping:
        return None
    series, _ = mapping
    document = _get(
        f"https://www.hsi.com.hk/data/eng/rt/index-series/{series}/performance.do",
        headers={"Referer": "https://www.hsi.com.hk/"},
        timeout=10,
        attempts=2,
    ).json()
    item = _hang_seng_index_item(document, secid)
    price = _positive_finite_float(item.get("indexValue"), "index value")
    previous = _positive_finite_float(item.get("previousClose"), "previous close")
    last_update = datetime.strptime(
        str(item.get("lastUpdate")), "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=CHINA_TZ)
    market, symbol = secid.split(".", 1)
    return {
        "secid": secid,
        "symbol": symbol,
        "market": int(market),
        "name": str(item.get("indexName") or symbol),
        "price": price,
        "pct": (price / previous - 1) * 100,
        "previous_close": previous,
        "quote_time": last_update.astimezone(timezone.utc).isoformat(timespec="seconds"),
    }


def hang_seng_index_daily_prices(
    secid: str,
    begin: str = "20240101",
    end: str = "20500101",
) -> list[dict[str, Any]]:
    mapping = HANG_SENG_INDEX_SERIES.get(secid)
    if not mapping:
        return []
    begin_date = datetime.strptime(begin, "%Y%m%d").date()
    end_date = min(datetime.strptime(end, "%Y%m%d").date(), app_today())
    if begin_date > end_date:
        return []
    series, _ = mapping
    document = _get(
        f"https://www.hsi.com.hk/data/eng/index-series/{series}/chart-rebased.json",
        headers={"Referer": "https://www.hsi.com.hk/"},
        timeout=20,
        attempts=2,
    ).json()
    item = _hang_seng_index_item(document, secid)
    points = item.get("indexLevels-5y")
    if not isinstance(points, list) or not points:
        raise ValueError(f"missing Hang Seng chart points for {secid}")

    parsed_points: list[tuple[str, float]] = []
    previous_timestamp: float | None = None
    for point in points:
        if not isinstance(point, list) or len(point) < 2:
            raise ValueError(f"invalid Hang Seng chart point for {secid}")
        timestamp = float(point[0])
        value = _positive_finite_float(point[1], "rebased index value")
        if not math.isfinite(timestamp) or (
            previous_timestamp is not None and timestamp <= previous_timestamp
        ):
            raise ValueError(f"unordered Hang Seng chart for {secid}")
        previous_timestamp = timestamp
        date = (
            datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc)
            .astimezone(CHINA_TZ)
            .date()
            .isoformat()
        )
        parsed_points.append((date, value))
    if not math.isclose(parsed_points[0][1], 100.0, abs_tol=1e-9):
        raise ValueError(f"unexpected Hang Seng chart base for {secid}")
    last_update = datetime.strptime(
        str(item.get("lastUpdate")), "%Y-%m-%d %H:%M:%S"
    ).date().isoformat()
    if parsed_points[-1][0] != last_update:
        raise ValueError(f"mismatched Hang Seng chart date for {secid}")

    latest_close = _positive_finite_float(item.get("previousClose"), "latest close")
    scale = latest_close / parsed_points[-1][1]
    rows = []
    previous_value = None
    begin_iso = begin_date.isoformat()
    end_iso = end_date.isoformat()
    for date, value in parsed_points:
        pct = (value / previous_value - 1) * 100 if previous_value else None
        previous_value = value
        if date < begin_iso or date > end_iso:
            continue
        rows.append(
            {
                "date": date,
                "close": value * scale,
                "pct": pct,
                "source": "hang_seng_indexes_chart",
                "adjustment": "rebased_scaled",
            }
        )
    return rows


def eastmoney_daily_prices(secid: str, begin: str = "20240101", end: str = "20500101") -> list[dict[str, Any]]:
    response = _get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={
            "secid": secid,
            "klt": "101",
            "fqt": "1",
            "beg": begin,
            "end": end,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        },
    ).json()
    return _parse_eastmoney_daily_rows(response)


def eastmoney_index_daily_prices(secid: str, begin: str = "20240101", end: str = "20500101") -> list[dict[str, Any]]:
    params = {
        "secid": secid,
        "klt": "101",
        "fqt": "1",
        "beg": begin,
        "end": end,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
    }
    last_error: RequestException | None = None
    for _ in range(6):
        try:
            response = _get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params=params,
                headers={"Referer": f"https://quote.eastmoney.com/unify/r/{secid}"},
            ).json()
            rows = _parse_eastmoney_daily_rows(response)
            if rows:
                return rows
        except RequestException as exc:
            last_error = exc
        _sleep_within_daily_price_deadline(0.8, f"Eastmoney index {secid}")
    if last_error:
        raise last_error
    return []


def csindex_daily_prices(
    secid: str,
    begin: str = "20240101",
    end: str = "20500101",
) -> list[dict[str, Any]]:
    if secid not in CSINDEX_INDEX_SECIDS:
        return []
    begin_date = datetime.strptime(begin, "%Y%m%d").date()
    end_date = min(datetime.strptime(end, "%Y%m%d").date(), app_today())
    if begin_date > end_date:
        return []

    # The CSI export may synthesize a row on an initial non-trading day using
    # the next session's close. Start earlier, then discard the padded range.
    request_begin = begin_date - timedelta(days=14)
    payload = [
        {
            "startDate": request_begin.strftime("%Y%m%d"),
            "endDate": end_date.strftime("%Y%m%d"),
            "indexCode": secid.split(".", 1)[1],
        }
    ]
    global _csindex_last_request_at
    with _lock_within_daily_price_deadline(
        _CSINDEX_REQUEST_LOCK,
        f"CSI index {secid} rate limit",
    ):
        wait_seconds = CSINDEX_REQUEST_INTERVAL_SECONDS - (
            time.monotonic() - _csindex_last_request_at
        )
        if wait_seconds > 0:
            _sleep_within_daily_price_deadline(
                wait_seconds,
                f"CSI index {secid} rate limit",
            )
        try:
            response = _post(
                "https://www.csindex.com.cn/csindex-home/exportExcel/downloadindex-perf?language=CH",
                json=payload,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "Referer": "https://www.csindex.com.cn/",
                },
                timeout=30,
                attempts=2,
            )
        finally:
            _csindex_last_request_at = time.monotonic()
    return _parse_csindex_daily_workbook(
        response.content,
        begin_date.isoformat(),
        end_date.isoformat(),
        secid.split(".", 1)[1],
    )


def csindex_realtime_quote(secid: str) -> dict[str, Any] | None:
    if secid not in CSINDEX_INDEX_SECIDS:
        return None
    index_code = secid.split(".", 1)[1]
    global _csindex_last_request_at
    with _CSINDEX_REQUEST_LOCK:
        wait_seconds = CSINDEX_REQUEST_INTERVAL_SECONDS - (
            time.monotonic() - _csindex_last_request_at
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            document = _get(
                "https://www.csindex.com.cn/csindex-home/perf/index-perf-oneday",
                params={"indexCode": index_code},
                headers={"Referer": "https://www.csindex.com.cn/"},
                timeout=10,
                attempts=2,
            ).json()
        finally:
            _csindex_last_request_at = time.monotonic()
    header = ((document.get("data") or {}).get("intraDayHeader") or {})
    if str(header.get("indexCode") or "") != index_code:
        return None
    price = _to_float(header.get("current"))
    previous_close = _to_float(header.get("closePre"))
    if price is None or price <= 0:
        return None
    trade_date = str(header.get("tradeDate") or "")
    trade_time = str(header.get("tradeTime") or "")
    quote_time = None
    try:
        quote_time = (
            datetime.fromisoformat(f"{trade_date}T{trade_time}")
            .replace(tzinfo=CHINA_TZ)
            .astimezone(timezone.utc)
            .isoformat(timespec="seconds")
        )
    except ValueError:
        pass
    pct = _to_float(header.get("changePct"))
    if pct is None and previous_close and previous_close > 0:
        pct = (price / previous_close - 1) * 100
    name = index_code
    for item in (document.get("data") or {}).get("intraDayPerfList") or []:
        if str(item.get("indexCode") or "") == index_code and item.get("indexName"):
            name = str(item["indexName"])
            break
    return {
        "secid": secid,
        "symbol": index_code,
        "market": int(secid.split(".", 1)[0]),
        "name": name,
        "price": price,
        "pct": pct,
        "previous_close": previous_close,
        "quote_time": quote_time,
    }


def _parse_csindex_daily_workbook(
    content: bytes,
    begin_date: str,
    end_date: str,
    index_code: str,
) -> list[dict[str, Any]]:
    if not content.startswith(b"PK"):
        return []
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        sheet = ElementTree.fromstring(workbook.read("xl/worksheets/sheet1.xml"))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows = []
    for row in sheet.findall(".//x:sheetData/x:row", namespace):
        values: dict[str, str] = {}
        for cell in row.findall("x:c", namespace):
            reference = cell.get("r") or ""
            column_match = re.match(r"[A-Z]+", reference)
            if not column_match:
                continue
            value = cell.findtext("x:is/x:t", default="", namespaces=namespace)
            if not value:
                value = cell.findtext("x:v", default="", namespaces=namespace)
            values[column_match.group(0)] = value
        raw_date = values.get("A", "")
        if values.get("B") != index_code or not re.fullmatch(r"\d{8}", raw_date):
            continue
        date = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
        if date < begin_date or date > end_date:
            continue
        close = _to_float(values.get("J"))
        if close is None:
            continue
        rows.append(
            {
                "date": date,
                "close": close,
                "pct": _to_float(values.get("L")),
                "source": "csindex",
                "adjustment": "raw",
            }
        )
    return rows


def _parse_eastmoney_daily_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    data = response.get("data") or {}
    rows = []
    for line in data.get("klines") or []:
        parts = line.split(",")
        rows.append(
            {
                "date": parts[0],
                "close": float(parts[2]),
                "pct": _to_float(parts[8]),
                "source": "eastmoney",
                "adjustment": "forward",
            }
        )
    return rows


def sina_futures_daily_prices(secid: str, symbol: str, begin: str = "20240101", end: str = "20500101") -> list[dict[str, Any]]:
    response = _get(
        "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_/InnerFuturesNewService.getDailyKLine",
        params={"symbol": symbol},
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    match = re.search(r"var\s+_\((.*)\)\s*;?\s*$", response.text, re.S)
    if not match:
        return []
    data = json.loads(match.group(1))
    begin_date = datetime.strptime(begin, "%Y%m%d").date().isoformat()
    end_date = datetime.strptime(end, "%Y%m%d").date().isoformat()
    rows = []
    previous_close = None
    for item in data:
        date = item.get("d")
        if not date or date < begin_date or date > end_date:
            continue
        close = _to_float(item.get("c"))
        if close is None:
            continue
        pct = (close / previous_close - 1) * 100 if previous_close else None
        rows.append(
            {
                "date": date,
                "close": close,
                "pct": pct,
                "source": "sina_futures",
                "adjustment": "raw",
            }
        )
        previous_close = close
    return rows


def yahoo_realtime_quote(secid: str, symbol: str) -> dict[str, Any] | None:
    response = _get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={"range": "5d", "interval": "1d"},
        timeout=8,
        attempts=2,
    ).json()
    result = response["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    closes = [item for item in result["indicators"]["quote"][0]["close"] if item is not None]
    if len(closes) < 2:
        return None
    meta = result.get("meta") or {}
    price = float(meta.get("regularMarketPrice") or closes[-1])
    previous = float(closes[-2])
    quote_time = None
    regular_time = meta.get("regularMarketTime")
    if regular_time:
        quote_time = datetime.fromtimestamp(regular_time, tz=timezone.utc).isoformat(timespec="seconds")
    market, code = secid.split(".", 1)
    return {
        "secid": secid,
        "symbol": code,
        "market": int(market),
        "name": meta.get("shortName") or code,
        "price": price,
        "pct": (price / previous - 1) * 100 if previous else None,
        "previous_close": previous,
        "quote_time": quote_time,
    }


def sina_realtime_quote(secid: str) -> dict[str, Any] | None:
    symbol = SINA_PRICE_SYMBOLS.get(secid)
    if not symbol:
        return None
    response = _get(
        f"https://hq.sinajs.cn/list={symbol}",
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=6,
        attempts=1,
    )
    match = re.search(r'="(.*)"\s*;', response.text)
    if not match or not match.group(1):
        return None
    parts = match.group(1).split(",")
    if symbol.startswith("gb_"):
        return _sina_us_quote(secid, symbol, parts)
    if symbol.startswith("hf_"):
        return _sina_global_futures_quote(secid, symbol, parts)
    if symbol.startswith("fx_"):
        return _sina_fx_quote(secid, symbol, parts)
    if symbol.startswith("rt_hk"):
        return _sina_hk_index_quote(secid, symbol, parts)
    return None


def _sina_us_quote(secid: str, symbol: str, parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 4:
        return None
    price = _to_float(parts[1])
    pct = _to_float(parts[2])
    previous = _to_float(parts[27] if len(parts) > 27 else None)
    if previous is None and price is not None and pct is not None and pct != -100:
        previous = price / (1 + pct / 100)
    quote_time = _parse_sina_us_time(parts)
    return _quote_from_parts(secid, symbol.removeprefix("gb_").upper(), parts[0], price, pct, previous, quote_time)


def _sina_global_futures_quote(secid: str, symbol: str, parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 14:
        return None
    price = _to_float(parts[0])
    previous = _to_float(parts[7])
    pct = (price / previous - 1) * 100 if price is not None and previous else None
    quote_time = None
    if parts[12] and parts[6]:
        quote_time = datetime.strptime(f"{parts[12]} {parts[6]}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CHINA_TZ
        ).astimezone(timezone.utc).isoformat(timespec="seconds")
    name = parts[13] or symbol
    return _quote_from_parts(secid, symbol, name, price, pct, previous, quote_time)


def _sina_fx_quote(secid: str, symbol: str, parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 18:
        return None
    price = _to_float(parts[1])
    change = _to_float(parts[10])
    previous = price - change if price is not None and change is not None else None
    pct = (price / previous - 1) * 100 if price is not None and previous else None
    quote_time = None
    if parts[17] and parts[0]:
        quote_time = datetime.strptime(f"{parts[17]} {parts[0]}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CHINA_TZ
        ).astimezone(timezone.utc).isoformat(timespec="seconds")
    name = parts[9] or symbol
    return _quote_from_parts(secid, symbol, name, price, pct, previous, quote_time)


def _sina_hk_index_quote(secid: str, symbol: str, parts: list[str]) -> dict[str, Any] | None:
    if len(parts) < 19:
        return None
    price = _to_float(parts[6])
    previous = _to_float(parts[3])
    pct = _to_float(parts[8])
    quote_time = None
    if parts[17] and parts[18]:
        quote_time = datetime.strptime(f"{parts[17]} {parts[18]}", "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=CHINA_TZ
        ).astimezone(timezone.utc).isoformat(timespec="seconds")
    return _quote_from_parts(secid, parts[0] or symbol, parts[1] or symbol, price, pct, previous, quote_time)


def _parse_sina_us_time(parts: list[str]) -> str | None:
    if len(parts) > 31 and parts[24] and parts[31]:
        match = re.fullmatch(r"([A-Za-z]{3} \d{1,2} \d{2}:\d{2}[AP]M) (EDT|EST)", parts[24])
        if match:
            offset = timezone(timedelta(hours=-4 if match.group(2) == "EDT" else -5))
            value = f"{parts[31]} {match.group(1)}"
            return datetime.strptime(value, "%Y %b %d %I:%M%p").replace(
                tzinfo=offset
            ).astimezone(timezone.utc).isoformat(timespec="seconds")
    if len(parts) > 3 and parts[3]:
        return datetime.strptime(parts[3], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CHINA_TZ
        ).astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


def _quote_from_parts(
    secid: str,
    symbol: str,
    name: str,
    price: float | None,
    pct: float | None,
    previous: float | None,
    quote_time: str | None,
) -> dict[str, Any] | None:
    if price is None:
        return None
    market, code = secid.split(".", 1)
    return {
        "secid": secid,
        "symbol": symbol or code,
        "market": int(market),
        "name": name or code,
        "price": price,
        "pct": pct,
        "previous_close": previous,
        "quote_time": quote_time,
    }


def sina_cn_realtime_quote(secid: str) -> dict[str, Any] | None:
    quotes = sina_cn_realtime_quotes([secid])
    return quotes[0] if quotes else None


def sina_cn_realtime_quotes(secids: list[str]) -> list[dict[str, Any]]:
    symbols = {}
    for secid in secids:
        market, symbol = secid.split(".", 1)
        if market not in {"0", "1"} or not re.fullmatch(r"\d{6}", symbol):
            continue
        prefix = "bj" if market == "0" and symbol.startswith("920") else "sz" if market == "0" else "sh"
        sina_symbol = f"{prefix}{symbol}"
        symbols[sina_symbol] = secid
    if not symbols:
        return []
    response = _get(
        f"https://hq.sinajs.cn/list={','.join(symbols)}",
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=6,
        attempts=1,
    )
    rows = []
    for match in re.finditer(r'var hq_str_([a-z]{2}\d{6})="(.*?)"\s*;', response.text):
        secid = symbols.get(match.group(1))
        if not secid or not match.group(2):
            continue
        quote = _sina_cn_quote_from_parts(secid, match.group(2).split(","))
        if quote:
            rows.append(quote)
    return rows


def _sina_cn_quote_from_parts(secid: str, parts: list[str]) -> dict[str, Any] | None:
    market, symbol = secid.split(".", 1)
    if len(parts) < 32:
        return None
    previous = _to_float(parts[2])
    price = _to_float(parts[3])
    if price is None or previous is None or previous <= 0:
        return None
    quote_time = None
    if parts[30] and parts[31]:
        quote_time = datetime.strptime(f"{parts[30]} {parts[31]}", "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=CHINA_TZ
        ).astimezone(timezone.utc).isoformat(timespec="seconds")
    return {
        "secid": secid,
        "symbol": symbol,
        "market": int(market),
        "name": parts[0] or symbol,
        "price": price,
        "pct": (price / previous - 1) * 100,
        "previous_close": previous,
        "quote_time": quote_time,
    }


def sina_cn_daily_prices(secid: str, begin: str = "20240101", end: str = "20500101") -> list[dict[str, Any]]:
    market, symbol = secid.split(".", 1)
    if not is_cn_exchange_secid(secid):
        return []
    prefix = "bj" if market == "0" and symbol.startswith("920") else "sz" if market == "0" else "sh"
    response = _get(
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_/CN_MarketDataService.getKLineData",
        params={"symbol": f"{prefix}{symbol}", "scale": "240", "ma": "no", "datalen": "600"},
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    match = re.search(r"var\s+_\((\[.*\])\)\s*;?\s*$", response.text, re.S)
    if not match:
        return []
    data = json.loads(match.group(1))
    begin_date = datetime.strptime(begin, "%Y%m%d").date().isoformat()
    end_date = datetime.strptime(end, "%Y%m%d").date().isoformat()
    rows = []
    previous_close = None
    for item in data:
        date = item.get("day")
        if not date or date < begin_date or date > end_date:
            continue
        close = _to_float(item.get("close"))
        if close is None:
            continue
        pct = (close / previous_close - 1) * 100 if previous_close else None
        rows.append(
            {
                "date": date,
                "close": close,
                "pct": pct,
                "source": "sina",
                "adjustment": "raw",
            }
        )
        previous_close = close
    return rows


def yahoo_daily_prices(secid: str, symbol: str, begin: str = "20240101", end: str = "20500101") -> list[dict[str, Any]]:
    begin_dt = datetime.strptime(begin, "%Y%m%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
    max_end = datetime.now(timezone.utc) + timedelta(days=1)
    end_dt = min(end_dt, max_end) + timedelta(days=1)
    response = _get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={
            "period1": int(begin_dt.timestamp()),
            "period2": int(end_dt.timestamp()),
            "interval": "1d",
        },
    ).json()
    result = response["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0]["close"]
    timezone_name = (result.get("meta") or {}).get("exchangeTimezoneName")
    quote_tz = timezone.utc
    if timezone_name:
        try:
            quote_tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            quote_tz = timezone.utc
    rows = []
    previous_close = None
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date = datetime.fromtimestamp(ts, tz=quote_tz).date().isoformat()
        close = float(close)
        pct = (close / previous_close - 1) * 100 if previous_close else None
        rows.append(
            {
                "date": date,
                "close": close,
                "pct": pct,
                "source": "yahoo",
                "adjustment": "raw",
            }
        )
        previous_close = close
    return rows


def fetch_report_publish_dates(code: str) -> dict[str, str]:
    response = _get(
        "https://api.fund.eastmoney.com/f10/JJGG",
        params={"fundcode": code, "pageIndex": 1, "pageSize": 100, "type": 3},
        headers={"Referer": f"https://fundf10.eastmoney.com/jjgg_{code}_3.html"},
    ).json()
    dates: dict[str, str] = {}
    for item in response.get("Data") or []:
        title = item.get("TITLE") or ""
        publish_date = item.get("PUBLISHDATEDesc")
        if not publish_date:
            continue
        report_date = regular_report_date(title)
        if not report_date:
            continue
        dates[report_date] = publish_date
    return dates


def regular_report_date(title: str) -> str | None:
    year_pattern = r"(20\d{2}|[〇零○0一二三四五六七八九]{4})"
    quarter = re.search(
        rf"{year_pattern}年(?:度)?第([1-4一二三四])季度报告",
        title or "",
    )
    if quarter:
        year = _regular_report_year(quarter.group(1))
        quarter_end = {
            "1": "03-31",
            "2": "06-30",
            "3": "09-30",
            "4": "12-31",
            "一": "03-31",
            "二": "06-30",
            "三": "09-30",
            "四": "12-31",
        }[quarter.group(2)]
        return f"{year}-{quarter_end}" if year else None
    half_year = re.search(
        rf"{year_pattern}年(?:度)?(?:半年度|中期)报告",
        title or "",
    )
    if half_year:
        year = _regular_report_year(half_year.group(1))
        return f"{year}-06-30" if year else None
    annual = re.search(
        rf"{year_pattern}年(?:年度报告|度报告|年报)",
        title or "",
    )
    if annual:
        year = _regular_report_year(annual.group(1))
        return f"{year}-12-31" if year else None
    return None


def _regular_report_year(value: str) -> str | None:
    if re.fullmatch(r"20\d{2}", value):
        return value
    digits = {
        "〇": "0",
        "零": "0",
        "○": "0",
        "0": "0",
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
    }
    normalized = "".join(digits.get(char, "") for char in value)
    return normalized if re.fullmatch(r"20\d{2}", normalized) else None


def fetch_latest_regular_report(code: str) -> dict[str, str] | None:
    response = _get(
        "https://api.fund.eastmoney.com/f10/JJGG",
        params={"fundcode": code, "pageIndex": 1, "pageSize": 20, "type": 3},
        headers={"Referer": f"https://fundf10.eastmoney.com/jjgg_{code}_3.html"},
    ).json()
    for item in response.get("Data") or []:
        announcement_id = item.get("ID")
        if not announcement_id:
            continue
        return {
            "title": item.get("TITLE") or "",
            "publish_date": item.get("PUBLISHDATEDesc") or "",
            "announcement_id": announcement_id,
            "url": f"http://fund.eastmoney.com/gonggao/{code},{announcement_id}.html",
        }
    return None


def fetch_purchase_limits() -> list[dict[str, Any]]:
    response = _get(
        "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx",
        params={
            "t": "8",
            "page": "1,30000",
            "js": "reData",
            "sort": "fcode,asc",
        },
        headers={"Referer": "https://fund.eastmoney.com/Fund_sgzt.html"},
    )
    text = response.text
    data_match = re.search(r"datas:(\[.*?\]),record:", text, re.S)
    showday_match = re.search(r"showday:(\[.*?\])", text, re.S)
    if not data_match:
        raise ValueError("missing purchase limit data")
    rows = json.loads(data_match.group(1))
    showdays = json.loads(showday_match.group(1)) if showday_match else []
    source_date = showdays[0] if showdays else None
    return [parse_purchase_limit_row(row, source_date) for row in rows]


def parse_purchase_limit_row(row: list[Any], source_date: str | None = None) -> dict[str, Any]:
    purchase_status = str(row[5] or "")
    max_amount = _to_float(row[9])
    is_suspended = purchase_status not in {"开放申购", "限大额"}
    is_open_without_limit = purchase_status == "开放申购" or (max_amount is not None and max_amount >= 800000000)
    if is_suspended:
        display = "暂停"
        sort_value = 0.0
    elif is_open_without_limit:
        display = "开放"
        sort_value = PURCHASE_LIMIT_UNBOUNDED_SORT_VALUE
    else:
        display = format_purchase_amount(max_amount)
        sort_value = max_amount
    return {
        "fund_code": str(row[0]),
        "purchase_status": purchase_status,
        "redeem_status": str(row[6] or ""),
        "next_open_date": str(row[7] or ""),
        "min_purchase_amount": _to_float(row[8]),
        "max_purchase_amount": max_amount,
        "display": display,
        "sort_value": sort_value,
        "source_date": source_date,
    }


def format_purchase_amount(amount: float | None) -> str:
    if amount is None or amount < 0:
        return "--"
    if amount == 0:
        return "0元"
    if amount.is_integer():
        return f"{int(amount)}元"
    return f"{amount:g}元"


def yahoo_daily_close_marks(symbol: str, begin: str = "20240101", end: str = "20500101") -> list[dict[str, Any]]:
    requested_begin = datetime.strptime(begin, "%Y%m%d").replace(tzinfo=timezone.utc)
    min_begin = datetime.now(timezone.utc) - timedelta(days=720)
    begin_dt = max(requested_begin, min_begin)
    requested_end = datetime.strptime(end, "%Y%m%d").replace(tzinfo=timezone.utc)
    max_end = datetime.now(timezone.utc) + timedelta(days=1)
    end_dt = min(requested_end, max_end) + timedelta(days=1)
    response = _get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        params={
            "period1": int(begin_dt.timestamp()),
            "period2": int(end_dt.timestamp()),
            "interval": "1d",
        },
    ).json()
    result = response["chart"]["result"][0]
    timestamps = result.get("timestamp") or []
    closes = result["indicators"]["quote"][0]["close"]
    rows = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        rows.append({"date": date, "close": float(close)})
    return rows


def _to_float(value: Any) -> float | None:
    if value in (None, "-", "--"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
