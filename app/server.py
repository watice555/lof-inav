from __future__ import annotations

import errno
import json
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from requests import RequestException

from .build import refresh_navs, refresh_purchase_limits, refresh_quotes
from .config import FUNDS, FX_MIDPOINT_SECIDS
from .db import connect, get_meta, init_db, set_meta
from .sources import utc_now
from .valuation import backtest_price_diagnostics, backtest_summary, estimate_intraday, latest_holdings


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
QUOTE_REFRESH_INTERVAL_SECONDS = 60
PURCHASE_LIMIT_REFRESH_INTERVAL_SECONDS = 60 * 60
NAV_REFRESH_INTERVAL_SECONDS = 15 * 60

_quote_refresh_lock = threading.Lock()
_quote_refresh_started_at = 0.0
_purchase_limit_refresh_lock = threading.Lock()
_purchase_limit_refresh_started_at = 0.0
_nav_refresh_lock = threading.Lock()
_nav_refresh_started_at = 0.0


class SingleInstanceHTTPServer(ThreadingHTTPServer):
    # HTTPServer enables SO_REUSEADDR, which lets Windows bind duplicate local servers.
    allow_reuse_address = False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PUBLIC), **kwargs)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
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

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def handle_funds(self) -> None:
        should_refresh_quotes = False
        should_refresh_purchase_limits = False
        should_refresh_navs = False
        with connect() as con:
            secids = collect_realtime_secids(con)
            last_navs_refresh_at = get_meta(con, "last_navs_refresh_at")
            last_navs_refresh_success_at = get_meta(con, "last_navs_refresh_success_at")
            last_incremental_backtests_refresh_at = get_meta(con, "last_incremental_backtests_refresh_at")
            last_quotes_refresh_at = get_meta(con, "last_realtime_quotes_refresh_at")
            last_purchase_limits_refresh_at = get_meta(con, "last_purchase_limits_refresh_at")
            backtests_disabled = bool(get_meta(con, "backtests_disabled", False))
            if nav_cache_is_empty(con) or not last_navs_refresh_at:
                refresh_navs(con, update_backtests=not backtests_disabled)
                last_navs_refresh_at = get_meta(con, "last_navs_refresh_at")
                last_navs_refresh_success_at = get_meta(con, "last_navs_refresh_success_at")
                last_incremental_backtests_refresh_at = get_meta(con, "last_incremental_backtests_refresh_at")
            elif refresh_is_due(last_navs_refresh_at, NAV_REFRESH_INTERVAL_SECONDS):
                should_refresh_navs = True
            if quote_cache_is_empty(con, secids):
                refresh_quotes(con, secids)
                last_quotes_refresh_at = utc_now()
                set_meta(con, "last_realtime_quotes_refresh_at", last_quotes_refresh_at)
            elif quote_refresh_is_due(last_quotes_refresh_at):
                should_refresh_quotes = True
            if purchase_limit_cache_is_empty(con):
                try:
                    refresh_purchase_limits(con)
                    last_purchase_limits_refresh_at = get_meta(con, "last_purchase_limits_refresh_at")
                except (RequestException, ValueError):
                    last_purchase_limits_refresh_at = get_meta(con, "last_purchase_limits_refresh_at")
            elif refresh_is_due(last_purchase_limits_refresh_at, PURCHASE_LIMIT_REFRESH_INTERVAL_SECONDS):
                should_refresh_purchase_limits = True
            funds = []
            for code in FUNDS:
                item = estimate_intraday(con, code)
                item["backtest"] = (
                    {"count": 0, "disabled": True}
                    if backtests_disabled
                    else backtest_summary(con, code)
                )
                funds.append(item)
            data_alerts = collect_data_alerts(con, funds, include_backtests=not backtests_disabled)
            payload = {
                "last_realtime_quotes_refresh_at": last_quotes_refresh_at,
                "last_purchase_limits_refresh_at": last_purchase_limits_refresh_at,
                "last_navs_refresh_at": last_navs_refresh_at,
                "last_navs_refresh_success_at": last_navs_refresh_success_at,
                "last_incremental_backtests_refresh_at": last_incremental_backtests_refresh_at,
                "backtests_disabled": backtests_disabled,
                "quotes_refreshing": should_refresh_quotes,
                "purchase_limits_refreshing": should_refresh_purchase_limits,
                "navs_refreshing": should_refresh_navs,
                "data_alerts": data_alerts[:50],
                "data_alert_count": len(data_alerts),
                "funds": funds,
            }
        self.json(payload)
        if should_refresh_navs:
            schedule_nav_refresh(update_backtests=not backtests_disabled)
        if should_refresh_quotes:
            schedule_quote_refresh(secids)
        if should_refresh_purchase_limits:
            schedule_purchase_limit_refresh()

    def handle_holdings(self, code: str) -> None:
        with connect() as con:
            rows = [
                dict(row)
                for row in con.execute(
                    """
                    select h.*, q.price as quote_price, q.quote_time as quote_time,
                           q.updated_at as quote_updated_at
                    from holdings h
                    left join quotes q on q.secid = h.secid
                    where h.fund_code = ?
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
                    where b.fund_code = ?
                    order by b.date desc
                    limit 30
                    """,
                    (code,),
                )
            ]
            for row in rows:
                row["price_diagnostics"] = backtest_price_diagnostics(
                    con, code, row["previous_date"], row["date"]
                )
        self.json({"code": code, "rows": rows})

    def json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    init_db()
    try:
        server = SingleInstanceHTTPServer(("127.0.0.1", 8000), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
            raise SystemExit("LOF iNAV server is already running at http://127.0.0.1:8000") from exc
        raise
    print("LOF iNAV server: http://127.0.0.1:8000")
    server.serve_forever()


def collect_realtime_secids(con) -> list[str]:
    secids = [f"{cfg.exchange_market}.{code}" for code, cfg in FUNDS.items()]
    secids.extend(FX_MIDPOINT_SECIDS.values())
    for code in FUNDS:
        for row in latest_holdings(con, code):
            secids.append(row["secid"])
    return sorted(set(secids))


def quote_cache_is_empty(con, secids: list[str]) -> bool:
    if not secids:
        return False
    placeholders = ",".join("?" for _ in secids)
    row = con.execute(f"select count(*) as count from quotes where secid in ({placeholders})", secids).fetchone()
    return not row or row["count"] == 0


def purchase_limit_cache_is_empty(con) -> bool:
    row = con.execute("select count(*) as count from fund_purchase_limits").fetchone()
    return not row or row["count"] == 0


def collect_data_alerts(con, funds: list[dict], include_backtests: bool = True) -> list[dict]:
    alerts = []
    for fund in funds:
        missing_quotes = fund.get("missing_quotes") or []
        if missing_quotes:
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "type": "realtime_missing_quotes",
                    "severity": "warning",
                    "weight": fund.get("missing_weight") or 0,
                    "message": f"{fund['code']} {fund['name']} 实时行情缺失 {len(missing_quotes)} 个",
                    "details": missing_quotes[:20],
                }
            )
        if not include_backtests:
            continue
        latest = con.execute(
            """
            select date, previous_date
            from backtests
            where fund_code = ?
            order by date desc
            limit 1
            """,
            (fund["code"],),
        ).fetchone()
        if not latest:
            alerts.append(
                {
                    "code": fund["code"],
                    "name": fund["name"],
                    "type": "missing_backtest",
                    "severity": "warning",
                    "weight": 1,
                    "message": f"{fund['code']} {fund['name']} 缺少回测数据",
                    "details": [],
                }
            )
            continue
        diagnostics = backtest_price_diagnostics(con, fund["code"], latest["previous_date"], latest["date"])
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
    return sorted(alerts, key=lambda item: (item["severity"] != "warning", -item.get("weight", 0), item["code"]))


def nav_cache_is_empty(con) -> bool:
    row = con.execute("select count(*) as count from navs").fetchone()
    return not row or row["count"] == 0


def quote_refresh_is_due(last_refresh_at: str | None) -> bool:
    return refresh_is_due(last_refresh_at, QUOTE_REFRESH_INTERVAL_SECONDS)


def refresh_is_due(last_refresh_at: str | None, interval_seconds: int) -> bool:
    if not last_refresh_at:
        return True
    try:
        from datetime import datetime

        last_refresh = datetime.fromisoformat(last_refresh_at)
        now = datetime.fromisoformat(utc_now())
    except ValueError:
        return True
    return (now - last_refresh).total_seconds() >= interval_seconds


def schedule_quote_refresh(secids: list[str]) -> None:
    global _quote_refresh_started_at
    now = time.monotonic()
    if now - _quote_refresh_started_at < QUOTE_REFRESH_INTERVAL_SECONDS:
        return
    if not _quote_refresh_lock.acquire(blocking=False):
        return
    _quote_refresh_started_at = now
    thread = threading.Thread(target=refresh_quotes_in_background, args=(secids,), daemon=True)
    thread.start()


def schedule_nav_refresh(update_backtests: bool = True) -> None:
    global _nav_refresh_started_at
    now = time.monotonic()
    if now - _nav_refresh_started_at < NAV_REFRESH_INTERVAL_SECONDS:
        return
    if not _nav_refresh_lock.acquire(blocking=False):
        return
    _nav_refresh_started_at = now
    thread = threading.Thread(target=refresh_navs_in_background, args=(update_backtests,), daemon=True)
    thread.start()


def refresh_navs_in_background(update_backtests: bool = True) -> None:
    try:
        with connect() as con:
            refresh_navs(con, update_backtests=update_backtests)
    finally:
        _nav_refresh_lock.release()


def refresh_quotes_in_background(secids: list[str]) -> None:
    try:
        with connect() as con:
            refresh_quotes(con, secids)
            set_meta(con, "last_realtime_quotes_refresh_at", utc_now())
    finally:
        _quote_refresh_lock.release()


def schedule_purchase_limit_refresh() -> None:
    global _purchase_limit_refresh_started_at
    now = time.monotonic()
    if now - _purchase_limit_refresh_started_at < PURCHASE_LIMIT_REFRESH_INTERVAL_SECONDS:
        return
    if not _purchase_limit_refresh_lock.acquire(blocking=False):
        return
    _purchase_limit_refresh_started_at = now
    thread = threading.Thread(target=refresh_purchase_limits_in_background, daemon=True)
    thread.start()


def refresh_purchase_limits_in_background() -> None:
    try:
        with connect() as con:
            try:
                refresh_purchase_limits(con)
            except (RequestException, ValueError):
                pass
    finally:
        _purchase_limit_refresh_lock.release()


if __name__ == "__main__":
    main()
