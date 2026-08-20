from __future__ import annotations

import errno
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import pstdev
from urllib.parse import urlparse

from requests import RequestException

from .build import refresh_navs, refresh_purchase_limits, refresh_quotes, refresh_reports
from .config import FUNDS, FX_MIDPOINT_SECIDS
from .db import connect, database_readiness, get_meta, init_db, set_meta
from .market_calendar import is_trading_session
from .runtime import data_dir, resource_root
from .sources import app_today, utc_now
from .valuation import (
    BACKTEST_DISPLAY_ROWS,
    IntradayPrefetch,
    backtest_price_diagnostics,
    estimate_intraday,
    latest_holdings,
    prefetch_intraday_inputs,
)


ROOT = resource_root()
PUBLIC = ROOT / "public"
LOG_PATH = data_dir() / "lof_inav.log"
PID_PATH = data_dir() / "lof_inav.pid"
QUOTE_REFRESH_INTERVAL_SECONDS = 60
FAILED_QUOTE_RETRY_INTERVAL_SECONDS = 5 * 60
MISSING_QUOTE_RETRY_INTERVAL_SECONDS = 60 * 60
PURCHASE_LIMIT_REFRESH_INTERVAL_SECONDS = 60 * 60
REPORT_REFRESH_INTERVAL_SECONDS = 24 * 60 * 60
REPORT_RETRY_INTERVAL_SECONDS = 60 * 60
NAV_REFRESH_INTERVAL_SECONDS = 15 * 60
NAV_UNCHANGED_REFRESH_INTERVAL_SECONDS = 60 * 60
NAV_NON_TRADING_REFRESH_INTERVAL_SECONDS = 6 * 60 * 60
FUNDS_PAYLOAD_CACHE_SECONDS = 20.0
REFRESH_PROGRESS_DONE_TTL_SECONDS = 20.0
REFRESH_PROGRESS_STALE_SECONDS = 60 * 60
LOGGER = logging.getLogger(__name__)
CSRF_TOKEN = secrets.token_urlsafe(32)
HOST = os.environ.get("LOF_INAV_HOST", "127.0.0.1")
PORT = int(os.environ.get("LOF_INAV_PORT", "8001"))
PORT_IS_EXPLICIT = "LOF_INAV_PORT" in os.environ
PORT_FALLBACK_ATTEMPTS = 100
URL = f"http://{HOST}:{PORT}"
ALLOWED_LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"{HOST}:{PORT}"}
ALLOWED_LOCAL_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", URL}

_quote_refresh_lock = threading.Lock()
_quote_refresh_started_at = 0.0
_purchase_limit_refresh_lock = threading.Lock()
_purchase_limit_refresh_started_at = 0.0
_report_refresh_lock = threading.Lock()
_report_refresh_started_at = 0.0
_nav_refresh_lock = threading.Lock()
_nav_refresh_started_at = 0.0
_backtest_refresh_lock = threading.Lock()
_funds_payload_cache_lock = threading.Lock()
_funds_payload_cache: dict | None = None
_refresh_progress_lock = threading.Lock()
_refresh_progress_by_kind: dict[str, dict] = {}


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    # HTTPServer enables SO_REUSEADDR, which lets Windows bind duplicate local servers.
    allow_reuse_address = False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def log_message(self, format: str, *args) -> None:
        """Route access logs through configured handlers in windowed builds."""
        LOGGER.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh-status":
            self.handle_refresh_status()
            return
        if parsed.path == "/api/funds":
            self.handle_funds()
            return
        if parsed.path.startswith("/api/funds/") and parsed.path.endswith("/holdings"):
            code = parsed.path.split("/")[3]
            self.handle_holdings(code)
            return
        if parsed.path.startswith("/api/funds/") and parsed.path.endswith("/backtest"):
            code = parsed.path.split("/")[3]
            self.handle_backtest(code)
            return
        super().do_GET()

    def handle_refresh_status(self) -> None:
        with connect() as con:
            payload = {
                "last_realtime_quotes_refresh_at": get_meta(
                    con, "last_realtime_quotes_refresh_success_at"
                ),
                "last_realtime_quotes_completed_at": get_meta(
                    con, "last_realtime_quotes_completed_at"
                ),
                "last_purchase_limits_refresh_at": get_meta(
                    con, "last_purchase_limits_refresh_success_at"
                ),
                "last_purchase_limits_completed_at": get_meta(
                    con, "last_purchase_limits_refresh_completed_at"
                ),
                "last_navs_refresh_at": get_meta(con, "last_navs_refresh_at"),
                "last_navs_refresh_success_at": get_meta(con, "last_navs_refresh_success_at"),
                "last_reports_refresh_success_at": get_meta(
                    con, "last_reports_refresh_success_at"
                ),
                "last_reports_refresh_completed_at": get_meta(
                    con, "last_reports_refresh_completed_at"
                ),
                "last_incremental_backtests_refresh_at": get_meta(
                    con, "last_incremental_backtests_refresh_at"
                ),
            }
        self.json(attach_refresh_runtime_state(payload))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/backtests/incremental":
            if not self.validate_local_post():
                return
            self.handle_incremental_backtest_refresh()
            return
        self.send_error(404, "Not found")

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def handle_funds(self) -> None:
        cached_payload = get_cached_funds_payload()
        if cached_payload is not None:
            self.json(attach_refresh_runtime_state(cached_payload))
            return

        should_refresh_quotes = False
        quote_refresh_secids: list[str] = []
        should_refresh_purchase_limits = False
        should_refresh_navs = False
        should_refresh_reports = False
        with connect() as con:
            intraday_prefetch = prefetch_intraday_inputs(con, list(FUNDS))
            secids = collect_realtime_secids(con, intraday_prefetch)
            last_navs_refresh_at = get_meta(con, "last_navs_refresh_at")
            last_navs_refresh_success_at = get_meta(con, "last_navs_refresh_success_at")
            last_navs_refresh_errors = get_meta(con, "last_navs_refresh_errors", [])
            last_incremental_backtests_refresh_at = get_meta(con, "last_incremental_backtests_refresh_at")
            last_quotes_refresh_at = get_meta(con, "last_realtime_quotes_refresh_at")
            last_quotes_refresh_success_at = get_meta(
                con, "last_realtime_quotes_refresh_success_at"
            )
            last_purchase_limits_refresh_at = get_meta(con, "last_purchase_limits_refresh_at")
            last_purchase_limits_success_at = get_meta(
                con, "last_purchase_limits_refresh_success_at"
            )
            purchase_limit_missing_codes = set(
                get_meta(con, "last_purchase_limits_refresh_missing_codes", []) or []
            )
            last_reports_refresh_success_at = get_meta(
                con, "last_reports_refresh_success_at"
            )
            last_reports_refresh_completed_at = get_meta(
                con, "last_reports_refresh_completed_at"
            )
            last_reports_refresh_errors = get_meta(
                con, "last_reports_refresh_errors", []
            )
            pending_report_backtests = get_meta(
                con, "pending_report_backtests", {}
            ) or {}
            backtests_disabled = bool(get_meta(con, "backtests_disabled", False))
            nav_cache_empty = nav_cache_is_empty(con)
            if nav_cache_empty or not last_navs_refresh_at:
                should_refresh_navs = True
            elif refresh_is_due(last_navs_refresh_at, nav_refresh_interval_seconds(con)):
                should_refresh_navs = True
            if not nav_cache_empty and refresh_is_due(
                last_reports_refresh_completed_at,
                report_refresh_interval_seconds(con),
            ):
                should_refresh_reports = True
            quote_refresh_secids = quote_refresh_due_secids(con, secids)
            should_refresh_quotes = bool(quote_refresh_secids)
            if purchase_limit_cache_is_empty(con):
                should_refresh_purchase_limits = True
            elif refresh_is_due(last_purchase_limits_refresh_at, PURCHASE_LIMIT_REFRESH_INTERVAL_SECONDS):
                should_refresh_purchase_limits = True
            if backtests_disabled:
                backtest_summaries = {}
                latest_backtests = {}
                latest_nav_dates = {}
            else:
                backtest_summaries, latest_backtests = collect_backtest_cache(con)
                latest_nav_dates = {
                    code: row["date"]
                    for code, row in intraday_prefetch.latest_navs.items()
                    if row["date"]
                }
            funds = []
            for code, cfg in FUNDS.items():
                try:
                    item = estimate_intraday(con, code, prefetch=intraday_prefetch)
                    item["status"] = "ok"
                    item["backtest"] = (
                        {"count": 0, "disabled": True}
                        if backtests_disabled
                        else backtest_summaries.get(code, {"count": 0})
                    )
                    if code in purchase_limit_missing_codes:
                        item["purchase_limit"] = {
                            **(item.get("purchase_limit") or {}),
                            "stale": True,
                        }
                except Exception as exc:
                    LOGGER.exception("Fund valuation failed: code=%s", code)
                    item = {
                        "code": code,
                        "name": code,
                        "type": cfg.fund_type,
                        "trade_secid": f"{cfg.exchange_market}.{code}",
                        "previous_nav": None,
                        "nav_date": None,
                        "trade_price": None,
                        "trade_pct": None,
                        "estimated_nav": None,
                        "premium": None,
                        "covered_weight": 0,
                        "modeled_weight": 0,
                        "priced_weight": 0,
                        "priced_ratio": None,
                        "unmodeled_weight": 1,
                        "unpriced_weight": 0,
                        "missing_weight": 0,
                        "missing_quotes": [],
                        "realtime_warnings": [],
                        "note": cfg.note,
                        "quote_time": None,
                        "announcement": None,
                        "purchase_limit": None,
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "backtest": (
                            {"count": 0, "disabled": True}
                            if backtests_disabled
                            else {"count": 0, "error": "valuation failed"}
                        ),
                    }
                funds.append(item)
            backtest_status = (
                {"disabled": True}
                if backtests_disabled
                else collect_backtest_status(con, latest_nav_dates, latest_backtests)
            )
            data_alerts = collect_data_alerts(
                con,
                funds,
                include_backtests=not backtests_disabled,
                latest_backtests=latest_backtests,
                nav_refresh_errors=last_navs_refresh_errors,
                report_refresh_errors=last_reports_refresh_errors,
                pending_report_backtests=pending_report_backtests,
            )
            payload = {
                "last_realtime_quotes_refresh_at": last_quotes_refresh_success_at,
                "last_realtime_quotes_completed_at": last_quotes_refresh_at,
                "last_purchase_limits_refresh_at": last_purchase_limits_success_at,
                "last_purchase_limits_completed_at": last_purchase_limits_refresh_at,
                "last_navs_refresh_at": last_navs_refresh_at,
                "last_navs_refresh_success_at": last_navs_refresh_success_at,
                "last_reports_refresh_success_at": last_reports_refresh_success_at,
                "last_reports_refresh_completed_at": last_reports_refresh_completed_at,
                "last_incremental_backtests_refresh_at": last_incremental_backtests_refresh_at,
                "backtest_status": backtest_status,
                "backtests_disabled": backtests_disabled,
                "quotes_refreshing": should_refresh_quotes,
                "purchase_limits_refreshing": should_refresh_purchase_limits,
                "navs_refreshing": should_refresh_navs,
                "reports_refreshing": should_refresh_reports,
                "backtests_refreshing": _backtest_refresh_lock.locked(),
                "data_alerts": data_alerts,
                "data_alert_count": len(data_alerts),
                "csrf_token": CSRF_TOKEN,
                "funds": compact_funds_payload(funds),
            }
        if should_refresh_navs:
            payload["navs_refreshing"] = schedule_nav_refresh(update_backtests=False)
        if should_refresh_reports:
            payload["reports_refreshing"] = schedule_report_refresh()
        if should_refresh_quotes:
            payload["quotes_refreshing"] = schedule_quote_refresh(quote_refresh_secids or secids)
        if should_refresh_purchase_limits:
            payload["purchase_limits_refreshing"] = schedule_purchase_limit_refresh()
        attach_refresh_runtime_state(payload)
        cache_funds_payload(payload)
        self.json(payload)

    def handle_incremental_backtest_refresh(self) -> None:
        started, message = schedule_incremental_backtest_refresh()
        with connect() as con:
            payload = {
                "started": started,
                "message": message,
                "navs_refreshing": _nav_refresh_lock.locked(),
                "reports_refreshing": _report_refresh_lock.locked(),
                "backtests_refreshing": _backtest_refresh_lock.locked(),
                "last_incremental_backtests_refresh_at": get_meta(
                    con, "last_incremental_backtests_refresh_at"
                ),
            }
        payload["refresh_progress"] = get_active_refresh_progress()
        self.json(payload)

    def handle_holdings(self, code: str) -> None:
        with connect() as con:
            rows = [
                dict(row)
                for row in con.execute(
                    """
                    select h.*,
                           case when q.fetch_status = 'ok' then q.price else null end as quote_price,
                           q.quote_time as quote_time,
                           q.updated_at as quote_updated_at
                    from holdings h
                    left join quotes q on q.secid = h.secid
                    where h.fund_code = ? and h.weight > 0
                      and h.report_date = (
                        select max(report_date) from holdings where fund_code = ?
                      )
                    order by h.weight desc
                    """,
                    (code, code),
                )
            ]
        self.json({"code": code, "holdings": rows})

    def handle_backtest(self, code: str) -> None:
        with connect() as con:
            if bool(get_meta(con, "backtests_disabled", False)):
                self.json({"code": code, "rows": [], "disabled": True})
                return
            rows = [
                dict(row)
                for row in con.execute(
                    """
                    select b.*,
                           p.close as trade_close,
                           case
                             when n.nav is not null and n.nav != 0 and p.close is not null and p.close > 0
                             then p.close / n.nav - 1
                             else null
                           end as close_premium
                    from backtests b
                    left join funds f on f.code = b.fund_code
                    left join navs n on n.fund_code = b.fund_code and n.date = b.date
                    left join daily_prices p
                      on p.secid = cast(f.exchange_market as text) || '.' || b.fund_code
                     and p.date = b.date
                     and p.close > 0
                    where b.fund_code = ?
                    order by b.date desc
                    limit ?
                    """,
                    (code, BACKTEST_DISPLAY_ROWS),
                )
            ]
            price_lookup_cache: dict[
                tuple[str, str], tuple[str, float] | None
            ] = {}
            for row in rows:
                row["price_diagnostics"] = backtest_price_diagnostics(
                    con,
                    code,
                    row["previous_date"],
                    row["date"],
                    price_lookup_cache=price_lookup_cache,
                )
        self.json({"code": code, "rows": rows})

    def json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def validate_local_post(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        origin = self.headers.get("Origin")
        token = self.headers.get("X-LOF-CSRF")
        if host not in ALLOWED_LOCAL_HOSTS:
            self.send_error(403, "Invalid local host")
            return False
        if origin and origin not in ALLOWED_LOCAL_ORIGINS:
            self.send_error(403, "Invalid origin")
            return False
        if not secrets.compare_digest(token or "", CSRF_TOKEN):
            self.send_error(403, "Invalid CSRF token")
            return False
        return True


def main() -> None:
    global PORT, URL, ALLOWED_LOCAL_HOSTS, ALLOWED_LOCAL_ORIGINS

    configure_logging()
    require_loopback_host(HOST)
    init_db()
    require_database_ready()
    requested_port = PORT
    server = create_http_server()
    actual_port = int(server.server_address[1])
    PORT = actual_port
    URL = f"http://{HOST}:{PORT}"
    ALLOWED_LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"{HOST}:{PORT}"}
    ALLOWED_LOCAL_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", URL}
    if actual_port != requested_port:
        message = (
            f"Port {requested_port} is in use by another program; "
            f"using {actual_port} instead."
        )
        LOGGER.warning(message)
        print(message)
    LOGGER.info("LOF iNAV server started at %s", URL)
    print(f"LOF iNAV server: {URL}")
    pid_token = write_server_pid()
    if should_open_browser():
        threading.Thread(target=webbrowser.open, args=(URL,), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        remove_server_pid(pid_token)


def create_http_server() -> SingleInstanceHTTPServer:
    if PORT_IS_EXPLICIT:
        candidate_ports = [PORT]
    else:
        last_fallback_port = min(PORT + PORT_FALLBACK_ATTEMPTS, 65_535)
        candidate_ports = range(PORT, last_fallback_port + 1)

    last_error: OSError | None = None
    for candidate_port in candidate_ports:
        try:
            return SingleInstanceHTTPServer((HOST, candidate_port), Handler)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE and getattr(exc, "winerror", None) != 10048:
                raise
            last_error = exc

    if PORT_IS_EXPLICIT:
        message = f"Cannot start LOF iNAV: port {PORT} is already used by another program."
    else:
        message = (
            f"Cannot start LOF iNAV: ports {PORT}-{min(PORT + PORT_FALLBACK_ATTEMPTS, 65_535)} "
            "are already in use."
        )
    raise SystemExit(message) from last_error


def should_open_browser() -> bool:
    return (
        os.environ.get("LOF_INAV_OPEN_BROWSER") == "1"
        and os.environ.get("LOF_INAV_NO_BROWSER") != "1"
    )


def require_loopback_host(host: str) -> None:
    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return
    try:
        is_loopback = ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise SystemExit(
            "LOF iNAV only supports loopback binding. "
            "Set LOF_INAV_HOST to 127.0.0.1 or localhost."
        )


def write_server_pid() -> str:
    token = secrets.token_hex(16)
    payload = {
        "pid": os.getpid(),
        "port": PORT,
        "token": token,
        "started_at": utc_now(),
    }
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = PID_PATH.with_suffix(".pid.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, PID_PATH)
    return token


def remove_server_pid(token: str) -> None:
    try:
        payload = json.loads(PID_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not secrets.compare_digest(str(payload.get("token") or ""), token):
        return
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass


def require_database_ready() -> dict:
    with connect() as con:
        status = database_readiness(con)
    if status["ready"]:
        return status
    raise SystemExit(
        "LOF iNAV database is incomplete or incompatible. "
        "Run `python build.py --current-only` before starting the server. "
        f"Status: {json.dumps(status, ensure_ascii=False)}"
    )


def configure_logging() -> None:
    if getattr(configure_logging, "_configured", False):
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(file_handler)
    if sys.stderr is not None:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)
    configure_logging._configured = True


def get_cached_funds_payload() -> dict | None:
    now = time.monotonic()
    with _funds_payload_cache_lock:
        if not _funds_payload_cache:
            return None
        if _funds_payload_cache["expires_at"] <= now:
            return None
        return dict(_funds_payload_cache["payload"])


def cache_funds_payload(payload: dict) -> None:
    global _funds_payload_cache
    with _funds_payload_cache_lock:
        _funds_payload_cache = {
            "expires_at": time.monotonic() + FUNDS_PAYLOAD_CACHE_SECONDS,
            "payload": dict(payload),
        }


def invalidate_funds_payload_cache() -> None:
    global _funds_payload_cache
    with _funds_payload_cache_lock:
        _funds_payload_cache = None


def attach_refresh_runtime_state(payload: dict) -> dict:
    payload["quotes_refreshing"] = _quote_refresh_lock.locked()
    payload["purchase_limits_refreshing"] = _purchase_limit_refresh_lock.locked()
    payload["navs_refreshing"] = _nav_refresh_lock.locked()
    payload["reports_refreshing"] = _report_refresh_lock.locked()
    payload["backtests_refreshing"] = _backtest_refresh_lock.locked()
    payload["refresh_progress"] = get_active_refresh_progress()
    return payload


def begin_refresh_progress(
    kind: str,
    label: str,
    phase: str,
    total: int | None = None,
) -> None:
    update_refresh_progress(
        kind,
        phase=phase,
        label=label,
        completed=0,
        total=total,
        message="",
        status="running",
        started_monotonic=time.monotonic(),
    )


def update_refresh_progress(kind: str, **updates) -> None:
    now = time.monotonic()
    with _refresh_progress_lock:
        current = dict(_refresh_progress_by_kind.get(kind, {}))
        started = updates.pop("started_monotonic", current.get("_started_monotonic", now))
        if (
            "phase" in updates
            and updates["phase"] != current.get("phase")
            and "message" not in updates
        ):
            current.pop("message", None)
        current.update(updates)
        current["kind"] = kind
        current["status"] = current.get("status") or "running"
        current["_started_monotonic"] = started
        current["_updated_monotonic"] = now
        current["updated_at"] = utc_now()
        current["elapsed_seconds"] = round(now - started, 1)
        completed = current.get("completed")
        total = current.get("total")
        current["percent"] = progress_percent(completed, total)
        _refresh_progress_by_kind[kind] = current


def finish_refresh_progress(
    kind: str,
    label: str,
    status: str = "done",
    message: str | None = None,
    completed: int | None = None,
    total: int | None = None,
) -> None:
    updates = {"label": label, "status": status, "phase": status}
    if message is not None:
        updates["message"] = message
    if completed is not None:
        updates["completed"] = completed
    if total is not None:
        updates["total"] = total
    update_refresh_progress(kind, **updates)


def progress_percent(completed, total) -> int | None:
    try:
        completed_value = int(completed)
        total_value = int(total)
    except (TypeError, ValueError):
        return None
    if total_value <= 0:
        return None
    return max(0, min(100, int(completed_value * 100 / total_value)))


def get_refresh_progresses() -> dict[str, dict]:
    now = time.monotonic()
    progresses: dict[str, dict] = {}
    with _refresh_progress_lock:
        expired = []
        for kind, progress in _refresh_progress_by_kind.items():
            updated = progress.get("_updated_monotonic", now)
            status = progress.get("status")
            if status in {"done", "partial", "error"} and now - updated > REFRESH_PROGRESS_DONE_TTL_SECONDS:
                expired.append(kind)
                continue
            if status == "running" and now - updated > REFRESH_PROGRESS_STALE_SECONDS:
                expired.append(kind)
                continue
            public_progress = {
                key: value
                for key, value in progress.items()
                if not key.startswith("_")
            }
            public_progress["elapsed_seconds"] = round(
                now - progress.get("_started_monotonic", now),
                1,
            )
            progresses[kind] = public_progress
        for kind in expired:
            _refresh_progress_by_kind.pop(kind, None)
    return progresses


def get_active_refresh_progress() -> dict | None:
    progresses = get_refresh_progresses()
    if _backtest_refresh_lock.locked() and "backtests" in progresses:
        return progresses["backtests"]
    if _nav_refresh_lock.locked() and "navs" in progresses:
        return progresses["navs"]
    if _report_refresh_lock.locked() and "reports" in progresses:
        return progresses["reports"]
    if _quote_refresh_lock.locked() and "quotes" in progresses:
        return progresses["quotes"]
    if _purchase_limit_refresh_lock.locked() and "purchase_limits" in progresses:
        return progresses["purchase_limits"]
    if (
        _nav_refresh_lock.locked()
        or _report_refresh_lock.locked()
        or _purchase_limit_refresh_lock.locked()
    ):
        return None
    if not progresses:
        return None
    return max(progresses.values(), key=lambda item: item.get("updated_at") or "")


def collect_realtime_secids(
    con, prefetch: IntradayPrefetch | None = None
) -> list[str]:
    secids = [f"{cfg.exchange_market}.{code}" for code, cfg in FUNDS.items()]
    secids.extend(FX_MIDPOINT_SECIDS.values())
    for code in FUNDS:
        rows = (
            prefetch.holdings.get(code, [])
            if prefetch is not None
            else latest_holdings(con, code)
        )
        for row in rows:
            secids.append(row["secid"])
    return sorted(set(secids))


def quote_cache_missing_secids(con, secids: list[str]) -> list[str]:
    unique_secids = sorted(set(secids))
    if not unique_secids:
        return []
    placeholders = ",".join("?" for _ in unique_secids)
    rows = con.execute(
        f"""
        select secid from quotes
        where secid in ({placeholders})
          and price is not null
          and price > 0
          and fetch_status = 'ok'
        """,
        unique_secids,
    ).fetchall()
    present = {row["secid"] for row in rows}
    return [secid for secid in unique_secids if secid not in present]


def quote_refresh_due_secids(
    con,
    secids: list[str],
    now_at: str | None = None,
) -> list[str]:
    unique_secids = sorted(set(secids))
    if not unique_secids:
        return []
    placeholders = ",".join("?" for _ in unique_secids)
    rows = con.execute(
        f"""
        select secid, fetch_status, last_attempt_at, last_success_at
        from quotes
        where secid in ({placeholders})
        """,
        unique_secids,
    ).fetchall()
    cached = {row["secid"]: row for row in rows}
    due = []
    for secid in unique_secids:
        row = cached.get(secid)
        if row is None:
            due.append(secid)
            continue
        if row["fetch_status"] == "ok":
            interval = QUOTE_REFRESH_INTERVAL_SECONDS
        elif row["last_success_at"]:
            interval = FAILED_QUOTE_RETRY_INTERVAL_SECONDS
        else:
            interval = MISSING_QUOTE_RETRY_INTERVAL_SECONDS
        if refresh_is_due(row["last_attempt_at"], interval, now_at=now_at):
            due.append(secid)
    return due


def purchase_limit_cache_is_empty(con) -> bool:
    row = con.execute("select count(*) as count from fund_purchase_limits").fetchone()
    return not row or row["count"] == 0


def compact_funds_payload(funds: list[dict]) -> list[dict]:
    return [compact_fund_payload(fund) for fund in funds]


def compact_fund_payload(fund: dict) -> dict:
    item = pick_present(
        fund,
        (
            "code",
            "name",
            "type",
            "trade_secid",
            "previous_nav",
            "nav_date",
            "trade_price",
            "estimated_nav",
            "premium",
            "covered_weight",
            "modeled_weight",
            "priced_weight",
            "priced_ratio",
            "unmodeled_weight",
            "unpriced_weight",
            "note",
            "quote_time",
            "status",
        ),
    )
    if fund.get("error"):
        item["error"] = fund["error"]
    item["announcement"] = compact_nested_payload(
        fund.get("announcement"),
        ("title", "publish_date", "announcement_id", "url"),
    )
    item["purchase_limit"] = compact_nested_payload(
        fund.get("purchase_limit"),
        ("display", "sort_value", "stale"),
    )
    item["backtest"] = compact_nested_payload(
        fund.get("backtest"),
        ("count", "disabled", "error", "mae_pct", "mae_to_nav_volatility"),
    )
    return item


def compact_nested_payload(value: dict | None, fields: tuple[str, ...]) -> dict | None:
    if not value:
        return None
    return pick_present(value, fields)


def pick_present(source: dict, fields: tuple[str, ...]) -> dict:
    return {field: source[field] for field in fields if field in source}


def collect_latest_nav_dates(con) -> dict[str, str]:
    dates = {}
    for code in FUNDS:
        row = con.execute(
            "select date from navs where fund_code = ? order by date desc limit 1",
            (code,),
        ).fetchone()
        if row and row["date"]:
            dates[code] = row["date"]
    return dates


def collect_backtest_cache(con) -> tuple[dict[str, dict], dict[str, object]]:
    rows = con.execute(
        """
        select *
        from (
          select b.*, row_number() over (partition by fund_code order by date desc) as rn
          from backtests b
        )
        where rn <= 30
        order by fund_code, date desc
        """
    ).fetchall()
    rows_by_code: dict[str, list] = {}
    latest_by_code = {}
    for row in rows:
        code = row["fund_code"]
        latest_by_code.setdefault(code, row)
        code_rows = rows_by_code.setdefault(code, [])
        if len(code_rows) < 30:
            code_rows.append(row)
    return (
        {code: summarize_backtest_rows(rows) for code, rows in rows_by_code.items()},
        latest_by_code,
    )


def summarize_backtest_rows(rows: list) -> dict:
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


def collect_backtest_status(
    con,
    latest_nav_dates: dict[str, str] | None = None,
    latest_backtests: dict[str, object] | None = None,
) -> dict:
    latest_nav_dates = latest_nav_dates if latest_nav_dates is not None else collect_latest_nav_dates(con)
    latest_backtests = latest_backtests if latest_backtests is not None else collect_backtest_cache(con)[1]
    counts: dict[str, int] = {}
    stale_count = 0
    missing_count = 0
    latest_dates = []
    for code, nav_date in latest_nav_dates.items():
        latest = latest_backtests.get(code)
        backtest_date = latest["date"] if latest else None
        if backtest_date:
            counts[backtest_date] = counts.get(backtest_date, 0) + 1
            latest_dates.append(backtest_date)
        else:
            missing_count += 1
        if not backtest_date or nav_date > backtest_date:
            stale_count += 1
    return {
        "fund_count": len(latest_nav_dates),
        "latest_date": max(latest_dates) if latest_dates else None,
        "oldest_latest_date": min(latest_dates) if latest_dates else None,
        "date_counts": dict(sorted(counts.items(), reverse=True)),
        "stale_count": stale_count,
        "missing_count": missing_count,
    }


def collect_data_alerts(
    con,
    funds: list[dict],
    include_backtests: bool = True,
    latest_backtests: dict[str, object] | None = None,
    nav_refresh_errors: list[dict] | None = None,
    report_refresh_errors: list[dict] | None = None,
    pending_report_backtests: dict[str, str] | None = None,
) -> list[dict]:
    if include_backtests and latest_backtests is None:
        latest_backtests = collect_backtest_cache(con)[1]
    alerts = []
    nav_errors_by_code = {
        item.get("code"): item.get("error") or "unknown error"
        for item in (nav_refresh_errors or [])
        if item.get("code")
    }
    report_errors_by_code: dict[str, list[str]] = {}
    for item in report_refresh_errors or []:
        code = item.get("code")
        if code:
            report_errors_by_code.setdefault(code, []).append(
                item.get("error") or "unknown error"
            )
    pending_report_backtests = pending_report_backtests or {}
    price_lookup_cache: dict[tuple[str, str], tuple[str, float] | None] = {}
    for fund in funds:
        if (
            fund["code"] in report_errors_by_code
            or fund["code"] in pending_report_backtests
        ):
            details = report_errors_by_code.get(fund["code"], [])
            if fund["code"] in pending_report_backtests:
                details = [
                    *details,
                    f"回测将从 {pending_report_backtests[fund['code']]} 起重算",
                ]
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "report_refresh_pending",
                    "severity": "warning",
                    "weight": 1,
                    "message": (
                        f"{fund['code']} {fund['name']} 持仓或回测更新待重试，"
                        "当前保留上一版可用缓存"
                    ),
                    "details": details,
                }
            )
        if fund["code"] in nav_errors_by_code:
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "nav_refresh_failed",
                    "severity": "warning",
                    "weight": 1,
                    "message": f"{fund['code']} {fund['name']} 最近净值刷新失败，当前保留旧净值",
                    "details": [nav_errors_by_code[fund["code"]]],
                }
            )
        if fund.get("status") == "error":
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "valuation_error",
                    "severity": "error",
                    "weight": 1,
                    "message": f"{fund['code']} {fund['name']} 估值数据不可用",
                    "details": [fund.get("error") or "unknown error"],
                }
            )
            continue
        missing_quotes = fund.get("missing_quotes") or []
        if missing_quotes:
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "realtime_missing_quotes",
                    "severity": "warning",
                    "weight": fund.get("missing_weight") or 0,
                    "message": f"{fund['code']} {fund['name']} 实时资产收益无法计算 {len(missing_quotes)} 个",
                    "details": missing_quotes[:20],
                }
            )
        realtime_warnings = fund.get("realtime_warnings") or []
        if realtime_warnings:
            asset_weight = sum(
                item.get("holding_weight") or 0
                for item in realtime_warnings
                if item.get("kind") == "asset"
            )
            fx_weight = sum(
                item.get("holding_weight") or 0
                for item in realtime_warnings
                if item.get("kind") == "fx"
            )
            parts = []
            if asset_weight:
                parts.append(f"资产 {asset_weight:.2%}")
            if fx_weight:
                parts.append(f"汇率 {fx_weight:.2%}")
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "realtime_fallback",
                    "severity": "warning",
                    "weight": max(asset_weight, fx_weight),
                    "message": f"{fund['code']} {fund['name']} 实时估值降级：{' / '.join(parts) or '待检查'}",
                    "details": [
                        format_realtime_warning(item)
                        for item in realtime_warnings[:20]
                    ],
                }
            )
        if not include_backtests:
            continue
        latest = latest_backtests.get(fund["code"]) if latest_backtests else None
        if not latest:
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "missing_backtest",
                    "severity": "warning",
                    "weight": 1,
                    "message": f"{fund['code']} {fund['name']} 缺少回测数据",
                    "details": [],
                }
            )
            continue
        nav_date = fund.get("nav_date")
        if nav_date and latest["date"] < nav_date:
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "stale_backtest",
                    "severity": "warning",
                    "weight": 1,
                    "message": f"{fund['code']} {fund['name']} 回测滞后：净值 {nav_date} / 回测 {latest['date']}",
                    "details": {
                        "nav_date": nav_date,
                        "backtest_date": latest["date"],
                        "previous_date": latest["previous_date"],
                    },
                }
            )
            continue
        if latest["data_quality"] == "outlier":
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "fund_type": fund.get("type") or "其他",
                    "type": "backtest_outlier",
                    "severity": "warning",
                    "weight": abs(latest["error_pct"] or 0),
                    "message": (
                        f"{fund['code']} {fund['name']} 最新回测 {latest['date']} "
                        f"误差异常 {latest['error_pct']:.2%}"
                    ),
                    "details": {
                        "date": latest["date"],
                        "previous_date": latest["previous_date"],
                        "error_pct": latest["error_pct"],
                        "covered_weight": latest["covered_weight"],
                    },
                }
            )
            continue
        diagnostics = backtest_price_diagnostics(
            con,
            fund["code"],
            latest["previous_date"],
            latest["date"],
            price_lookup_cache=price_lookup_cache,
        )
        asset_weight = diagnostics["asset_stale_weight"]
        fx_weight = diagnostics["fx_stale_weight"]
        missing_weight = diagnostics["missing_weight"]
        if not any(value > 0.0001 for value in (asset_weight, fx_weight, missing_weight)):
            continue
        parts = []
        if asset_weight:
            parts.append(f"价格回退 {asset_weight:.2%}")
        if fx_weight:
            parts.append(f"汇率回退 {fx_weight:.2%}")
        if missing_weight:
            parts.append(f"数据缺失 {missing_weight:.2%}")
        alerts.append(
            {
                "code": fund["code"],
                "name": fund["name"],
                "fund_type": fund.get("type") or "其他",
                "type": "backtest_data_quality",
                "severity": "warning",
                "weight": max(asset_weight, fx_weight, missing_weight),
                "message": f"{fund['code']} {fund['name']} 最新回测 {latest['date']} {' / '.join(parts)}",
                "details": {
                    "date": latest["date"],
                    "previous_date": latest["previous_date"],
                    **diagnostics,
                },
            }
        )
    return sorted(alerts, key=alert_sort_key)


def alert_sort_key(item: dict) -> tuple:
    severity_priority = {"error": 0, "warning": 1}.get(item.get("severity"), 2)
    type_priority = {
        "valuation_error": 0,
        "nav_refresh_failed": 0,
        "report_refresh_pending": 0,
        "realtime_missing_quotes": 0,
        "realtime_fallback": 1,
        "backtest_data_quality": 2,
        "backtest_outlier": 2,
        "stale_backtest": 3,
        "missing_backtest": 4,
    }.get(item.get("type"), 9)
    return (
        severity_priority,
        type_priority,
        alert_fund_type_priority(item.get("fund_type")),
        -item.get("weight", 0),
        item["code"],
    )


def alert_fund_type_priority(fund_type: str | None) -> int:
    type_priority = {
        "QDII-港股": 0,
        "QDII-美股": 1,
        "QDII-多市场": 2,
        "商品-贵金属": 3,
        "商品-原油": 4,
        "商品-混合": 5,
        "债券-美元债": 6,
        "债券-境内债": 7,
        "FOF": 8,
        "A股-指数": 90,
        "A股-主动": 91,
    }
    if fund_type in type_priority:
        return type_priority[fund_type]
    if str(fund_type or "").startswith("A股-"):
        return 90
    return 50


def format_realtime_warning(item: dict) -> str:
    kind = "汇率" if item.get("kind") == "fx" else "资产"
    holding = item.get("holding_name") or item.get("holding_secid") or ""
    pieces = [
        f"{kind} {item.get('secid')}",
        f"持仓 {holding}",
        item.get("message") or "触发实时估值降级",
        f"净值日 {item.get('base_date') or '--'}",
        f"行情时间 {item.get('quote_time') or '--'}",
    ]
    if item.get("price") is not None:
        pieces.append(f"价格 {item['price']}")
    if item.get("previous_close") is not None:
        pieces.append(f"昨收 {item['previous_close']}")
    if item.get("fallback_return") is not None:
        pieces.append(f"fallback收益 {item['fallback_return']:.2%}")
    if item.get("fallback_source"):
        pieces.append(f"来源 {item['fallback_source']}")
    if item.get("holding_weight") is not None:
        pieces.append(f"权重 {item['holding_weight']:.2%}")
    return "；".join(pieces)


def nav_cache_is_empty(con) -> bool:
    row = con.execute("select count(*) as count from navs").fetchone()
    return not row or row["count"] == 0


def quote_refresh_is_due(last_refresh_at: str | None) -> bool:
    return refresh_is_due(last_refresh_at, QUOTE_REFRESH_INTERVAL_SECONDS)


def refresh_is_due(
    last_refresh_at: str | None,
    interval_seconds: int,
    now_at: str | None = None,
) -> bool:
    if not last_refresh_at:
        return True
    try:
        from datetime import datetime

        last_refresh = datetime.fromisoformat(last_refresh_at)
        now = datetime.fromisoformat(now_at or utc_now())
    except ValueError:
        return True
    return (now - last_refresh).total_seconds() >= interval_seconds


def nav_refresh_interval_seconds(con, today=None) -> int:
    today = today or app_today()
    if not is_trading_session("CN", today):
        return NAV_NON_TRADING_REFRESH_INTERVAL_SECONDS
    stats = get_meta(con, "last_navs_refresh_stats", {}) or {}
    if stats.get("changed_count") == 0 and stats.get("failed_count") == 0:
        return NAV_UNCHANGED_REFRESH_INTERVAL_SECONDS
    return NAV_REFRESH_INTERVAL_SECONDS


def report_refresh_interval_seconds(con) -> int:
    stats = get_meta(con, "last_reports_refresh_stats", {}) or {}
    pending = get_meta(con, "pending_report_backtests", {}) or {}
    if stats.get("failed_count") or pending:
        return REPORT_RETRY_INTERVAL_SECONDS
    return REPORT_REFRESH_INTERVAL_SECONDS


def refresh_cooldown_active(
    last_started_at: float,
    interval_seconds: int,
    now: float | None = None,
) -> bool:
    if last_started_at <= 0:
        return False
    now = time.monotonic() if now is None else now
    return now - last_started_at < interval_seconds


def schedule_quote_refresh(secids: list[str]) -> bool:
    global _quote_refresh_started_at
    now = time.monotonic()
    if _quote_refresh_lock.locked():
        return True
    if refresh_cooldown_active(
        _quote_refresh_started_at,
        QUOTE_REFRESH_INTERVAL_SECONDS,
        now,
    ):
        return False
    if not _quote_refresh_lock.acquire(blocking=False):
        return True
    _quote_refresh_started_at = now
    begin_refresh_progress("quotes", "行情", "queued", total=len(set(secids)))
    invalidate_funds_payload_cache()
    thread = threading.Thread(target=refresh_quotes_in_background, args=(secids,), daemon=True)
    thread.start()
    return True


def schedule_nav_refresh(update_backtests: bool = True) -> bool:
    global _nav_refresh_started_at
    now = time.monotonic()
    if _nav_refresh_lock.locked():
        return True
    if _report_refresh_lock.locked():
        return False
    if refresh_cooldown_active(
        _nav_refresh_started_at,
        NAV_REFRESH_INTERVAL_SECONDS,
        now,
    ):
        return False
    if not _nav_refresh_lock.acquire(blocking=False):
        return True
    if _report_refresh_lock.locked():
        _nav_refresh_lock.release()
        return False
    _nav_refresh_started_at = now
    begin_refresh_progress("navs", "净值", "queued", total=len(FUNDS))
    invalidate_funds_payload_cache()
    thread = threading.Thread(target=refresh_navs_in_background, args=(update_backtests,), daemon=True)
    thread.start()
    return True


def schedule_incremental_backtest_refresh() -> tuple[bool, str]:
    global _nav_refresh_started_at
    if _backtest_refresh_lock.locked():
        return False, "增量回测已在刷新中"
    if _nav_refresh_lock.locked():
        return False, "净值刷新中，稍后再启动增量回测"
    if _report_refresh_lock.locked():
        return False, "持仓检查中，稍后再启动增量回测"
    if not _backtest_refresh_lock.acquire(blocking=False):
        return False, "增量回测已在刷新中"
    if not _nav_refresh_lock.acquire(blocking=False):
        _backtest_refresh_lock.release()
        return False, "净值刷新中，稍后再启动增量回测"
    if _report_refresh_lock.locked():
        _nav_refresh_lock.release()
        _backtest_refresh_lock.release()
        return False, "持仓检查中，稍后再启动增量回测"
    now = time.monotonic()
    _nav_refresh_started_at = now
    begin_refresh_progress("backtests", "增量回测", "queued")
    invalidate_funds_payload_cache()
    thread = threading.Thread(target=refresh_incremental_backtests_in_background, daemon=True)
    thread.start()
    return True, "增量回测已开始"


def refresh_navs_in_background(update_backtests: bool = True) -> None:
    global _nav_refresh_started_at
    try:
        LOGGER.info("Nav refresh started: update_backtests=%s", update_backtests)
        started = time.monotonic()
        with connect() as con:
            result = refresh_navs(
                con,
                update_backtests=update_backtests,
                progress_callback=lambda progress: update_refresh_progress("navs", **progress),
            )
        LOGGER.info(
            "Nav refresh completed in %.1fs: checked=%s changed=%s failed=%s backtests=%s backtest_failed=%s",
            time.monotonic() - started,
            len(result["checked"]),
            len(result["updated"]),
            len(result["failed"]),
            len(result["backtests_refreshed"]),
            len(result["backtests_failed"]),
        )
        finish_refresh_progress(
            "navs",
            "净值" if not result["failed"] else "净值部分完成",
            status="done" if not result["failed"] else "partial",
            completed=len(result["checked"]) + len(result["failed"]),
            total=len(FUNDS),
            message=(
                f"checked={len(result['checked'])} changed={len(result['updated'])} "
                f"failed={len(result['failed'])}"
            ),
        )
    except Exception:
        LOGGER.exception("Nav refresh failed")
        finish_refresh_progress("navs", "净值失败", status="error")
    finally:
        invalidate_funds_payload_cache()
        _nav_refresh_started_at = time.monotonic()
        _nav_refresh_lock.release()


def refresh_incremental_backtests_in_background() -> None:
    global _nav_refresh_started_at
    try:
        LOGGER.info("Incremental backtest refresh started")
        started = time.monotonic()
        with connect() as con:
            set_meta(con, "backtests_disabled", False)
            con.commit()
            result = refresh_navs(
                con,
                update_backtests=True,
                progress_callback=lambda progress: update_refresh_progress("backtests", **progress),
            )
        LOGGER.info(
            "Incremental backtest refresh completed in %.1fs: nav_updated=%s nav_failed=%s backtests=%s backtest_failed=%s",
            time.monotonic() - started,
            len(result["updated"]),
            len(result["failed"]),
            len(result["backtests_refreshed"]),
            len(result["backtests_failed"]),
        )
        finish_refresh_progress(
            "backtests",
            "完成",
            completed=1,
            total=1,
            message=(
                f"nav_updated={len(result['updated'])} "
                f"backtests={len(result['backtests_refreshed'])} "
                f"failed={len(result['failed']) + len(result['backtests_failed'])}"
            ),
        )
    except Exception:
        LOGGER.exception("Incremental backtest refresh failed")
        finish_refresh_progress("backtests", "失败", status="error")
    finally:
        invalidate_funds_payload_cache()
        _nav_refresh_started_at = time.monotonic()
        _nav_refresh_lock.release()
        _backtest_refresh_lock.release()


def refresh_quotes_in_background(secids: list[str]) -> None:
    try:
        LOGGER.info("Quote refresh started: secids=%s", len(set(secids)))
        started = time.monotonic()
        with connect() as con:
            result = refresh_quotes(
                con,
                secids,
                progress_callback=lambda progress: update_refresh_progress("quotes", **progress),
            )
        LOGGER.info(
            "Quote refresh completed in %.1fs: requested=%s saved=%s missing=%s",
            time.monotonic() - started,
            result["requested"],
            result["saved"],
            len(result["missing"]),
        )
        finish_refresh_progress(
            "quotes",
            "行情",
            completed=result["requested"],
            total=result["requested"],
            message=f"saved={result['saved']} missing={len(result['missing'])}",
        )
    except Exception:
        LOGGER.exception("Quote refresh failed")
        finish_refresh_progress("quotes", "行情失败", status="error")
    finally:
        invalidate_funds_payload_cache()
        _quote_refresh_lock.release()


def schedule_report_refresh() -> bool:
    global _report_refresh_started_at
    now = time.monotonic()
    if _report_refresh_lock.locked():
        return True
    if _nav_refresh_lock.locked() or _backtest_refresh_lock.locked():
        return False
    if refresh_cooldown_active(
        _report_refresh_started_at,
        REPORT_RETRY_INTERVAL_SECONDS,
        now,
    ):
        return False
    if not _report_refresh_lock.acquire(blocking=False):
        return True
    if _nav_refresh_lock.locked() or _backtest_refresh_lock.locked():
        _report_refresh_lock.release()
        return False
    _report_refresh_started_at = now
    begin_refresh_progress("reports", "持仓检查", "queued", total=len(FUNDS))
    invalidate_funds_payload_cache()
    thread = threading.Thread(target=refresh_reports_in_background, daemon=True)
    thread.start()
    return True


def refresh_reports_in_background() -> None:
    global _report_refresh_started_at
    try:
        LOGGER.info("Report refresh started: funds=%s", len(FUNDS))
        started = time.monotonic()
        with connect() as con:
            result = refresh_reports(
                con,
                progress_callback=lambda progress: update_refresh_progress(
                    "reports", **progress
                ),
            )
        failure_count = len(result["failed_codes"])
        LOGGER.info(
            "Report refresh completed in %.1fs: checked=%s changed=%s refreshed=%s failed=%s backtests=%s",
            time.monotonic() - started,
            len(result["checked"]),
            len(result["changed"]),
            len(result["refreshed"]),
            failure_count,
            len(result["backtests_refreshed"]),
        )
        finish_refresh_progress(
            "reports",
            "持仓检查" if not failure_count else "持仓检查部分完成",
            status="done" if not failure_count else "partial",
            completed=len(FUNDS),
            total=len(FUNDS),
            message=(
                f"checked={len(result['checked'])} changed={len(result['changed'])} "
                f"refreshed={len(result['refreshed'])} failed={failure_count}"
            ),
        )
    except Exception:
        LOGGER.exception("Report refresh failed")
        finish_refresh_progress("reports", "持仓检查失败", status="error")
    finally:
        invalidate_funds_payload_cache()
        _report_refresh_started_at = time.monotonic()
        _report_refresh_lock.release()


def schedule_purchase_limit_refresh() -> bool:
    global _purchase_limit_refresh_started_at
    now = time.monotonic()
    if _purchase_limit_refresh_lock.locked():
        return True
    if refresh_cooldown_active(
        _purchase_limit_refresh_started_at,
        PURCHASE_LIMIT_REFRESH_INTERVAL_SECONDS,
        now,
    ):
        return False
    if not _purchase_limit_refresh_lock.acquire(blocking=False):
        return True
    _purchase_limit_refresh_started_at = now
    begin_refresh_progress(
        "purchase_limits",
        "申购限额",
        "queued",
        total=len(FUNDS),
    )
    invalidate_funds_payload_cache()
    thread = threading.Thread(target=refresh_purchase_limits_in_background, daemon=True)
    thread.start()
    return True


def refresh_purchase_limits_in_background() -> None:
    try:
        LOGGER.info("Purchase limit refresh started")
        started = time.monotonic()
        with connect() as con:
            result = refresh_purchase_limits(con)
        LOGGER.info(
            "Purchase limit refresh completed in %.1fs: saved=%s missing=%s",
            time.monotonic() - started,
            result["saved"],
            len(result["missing"]),
        )
        finish_refresh_progress(
            "purchase_limits",
            "申购限额" if not result["missing"] else "申购限额部分完成",
            status="done" if not result["missing"] else "partial",
            completed=len(FUNDS),
            total=len(FUNDS),
            message=f"saved={result['saved']} missing={len(result['missing'])}",
        )
    except (RequestException, ValueError):
        LOGGER.exception("Purchase limit refresh failed")
        finish_refresh_progress("purchase_limits", "申购限额失败", status="error")
    except Exception:
        LOGGER.exception("Purchase limit refresh failed unexpectedly")
        finish_refresh_progress("purchase_limits", "申购限额失败", status="error")
    finally:
        invalidate_funds_payload_cache()
        _purchase_limit_refresh_lock.release()


if __name__ == "__main__":
    main()
