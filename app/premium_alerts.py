from __future__ import annotations

import ipaddress
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


CHINA_TZ = timezone(timedelta(hours=8))
DEFAULT_REFRESH_TIMEOUT_SECONDS = 30 * 60
DEFAULT_THRESHOLD = 0.015
DEFAULT_MIN_PRICED_RATIO = 0.60
SUPPORTED_BOT_TYPES = {"dingtalk", "feishu", "generic", "slack", "telegram", "wecom"}


class AlertJobError(RuntimeError):
    """The alert job could not produce a trustworthy report."""


class BotDeliveryError(AlertJobError):
    """The report could not be delivered to the configured bot."""


@dataclass(frozen=True)
class RefreshOutcome:
    status: dict[str, Any]
    progress: dict[str, dict[str, Any]]


class LocalApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        request_timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = validate_local_base_url(base_url)
        self.request_timeout = request_timeout
        self.session = session or requests.Session()
        self.csrf_token: str | None = None

    def get_funds(self, *, allow_background_refresh: bool = True) -> dict[str, Any]:
        path = "/api/funds" if allow_background_refresh else "/api/funds?refresh=0"
        payload = self._request_json("GET", path)
        if not isinstance(payload.get("funds"), list):
            raise AlertJobError("/api/funds returned an invalid funds payload")
        token = payload.get("csrf_token")
        if isinstance(token, str) and token:
            self.csrf_token = token
        return payload

    def get_refresh_status(self) -> dict[str, Any]:
        return self._request_json("GET", "/api/refresh-status")

    def force_current_refresh(
        self,
        *,
        timeout_seconds: float = DEFAULT_REFRESH_TIMEOUT_SECONDS,
        poll_seconds: float = 2.0,
    ) -> RefreshOutcome:
        baseline = self.get_refresh_status()
        token = baseline.get("csrf_token")
        if isinstance(token, str) and token:
            self.csrf_token = token
        if not self.csrf_token:
            self.get_funds()
            baseline = self.get_refresh_status()
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        status = baseline
        states = {}
        pending = {"navs", "quotes"}

        while pending:
            self._check_deadline(
                deadline, started_at, timeout_seconds, baseline, status, pending, states
            )
            response = self._request_json(
                "POST",
                "/api/refresh/current",
                json_payload={"kinds": sorted(pending)},
                csrf=True,
            )
            states = response.get("states")
            if not isinstance(states, dict):
                raise AlertJobError("refresh endpoint returned an invalid state payload")
            for kind in tuple(pending):
                state = states.get(kind)
                if state == "started" or (kind == "navs" and state == "running"):
                    pending.remove(kind)
            if pending:
                status = self.get_refresh_status()
                time.sleep(poll_seconds)

        status = self.get_refresh_status()
        while status.get("navs_refreshing") or status.get("quotes_refreshing"):
            self._check_deadline(
                deadline, started_at, timeout_seconds, baseline, status, pending, states
            )
            time.sleep(poll_seconds)
            status = self.get_refresh_status()

        progress = status.get("refresh_progresses") or {}
        if not isinstance(progress, dict):
            progress = {}
        failures = [
            kind
            for kind in ("navs", "quotes")
            if isinstance(progress.get(kind), dict)
            and progress[kind].get("status") == "error"
        ]
        if failures:
            raise AlertJobError(
                f"强制刷新失败：{', '.join(failures)}\n"
                + self._refresh_diagnostics(baseline, status, pending, states)
            )
        self._validate_refresh_completed(baseline, status, progress)
        return RefreshOutcome(status=status, progress=progress)

    def _validate_refresh_completed(
        self,
        baseline: dict[str, Any],
        status: dict[str, Any],
        progress: dict[str, dict[str, Any]],
    ) -> None:
        checks = {
            "navs": "last_navs_refresh_at",
            "quotes": "last_realtime_quotes_completed_at",
        }
        incomplete = []
        for kind, field in checks.items():
            value = status.get(field)
            changed = value and value != baseline.get(field)
            completed_progress = (progress.get(kind) or {}).get("status") in {
                "done",
                "partial",
            }
            if not changed and not completed_progress:
                incomplete.append(kind)
        if incomplete:
            raise AlertJobError(
                f"refresh completion could not be verified: {', '.join(incomplete)}"
            )

    def _check_deadline(
        self,
        deadline: float,
        started_at: float,
        timeout_seconds: float,
        baseline: dict[str, Any],
        status: dict[str, Any],
        pending: set[str],
        states: dict[str, Any],
    ) -> None:
        now = time.monotonic()
        if now >= deadline:
            raise AlertJobError(
                f"强制刷新超时：已等待 {now - started_at:.0f} 秒，"
                f"上限 {timeout_seconds:g} 秒。\n"
                + self._refresh_diagnostics(baseline, status, pending, states)
            )

    def _refresh_diagnostics(
        self,
        baseline: dict[str, Any],
        status: dict[str, Any],
        pending: set[str],
        states: dict[str, Any],
    ) -> str:
        # Only expose structured progress, never raw provider errors or messages.
        phases = {
            "navs": "拉取基金净值",
            "current_base_prices": "准备估值基准价",
            "daily_prices": "补齐日线基准价",
            "mark_prices": "补齐参考价格",
            "quotes": "拉取实时行情",
        }
        progress_states = {
            "done": "已完成", "partial": "部分完成", "error": "失败",
        }
        progresses = status.get("refresh_progresses")
        progresses = progresses if isinstance(progresses, dict) else {}
        lines = []
        for kind, name, timestamp in (
            ("navs", "净值", "last_navs_refresh_at"),
            ("quotes", "行情", "last_realtime_quotes_completed_at"),
        ):
            progress = progresses.get(kind)
            progress = progress if isinstance(progress, dict) else {}
            if kind in pending:
                state = {
                    "blocked": "等待启动（其他刷新任务占用）",
                    "running": "等待已有任务结束后启动本轮刷新",
                }.get(states.get(kind), "等待启动")
            elif status.get(f"{kind}_refreshing"):
                state = "刷新中"
            elif progress.get("status") in progress_states:
                state = progress_states[progress["status"]]
            elif status.get(timestamp) and status[timestamp] != baseline.get(timestamp):
                state = "已完成"
            else:
                state = "完成状态尚未确认"
            details = [f"{name}：{state}"]
            if progress.get("phase") in phases:
                details.append(f"最近阶段：{phases[progress['phase']]}")
            completed, total = progress.get("completed"), progress.get("total")
            if (
                isinstance(completed, (int, float))
                and isinstance(total, (int, float))
                and math.isfinite(completed) and math.isfinite(total)
                and completed >= 0 and total > 0
            ):
                details.append(f"最近进度：{completed:g}/{total:g}")
            lines.append("；".join(details))
        return "\n".join(lines)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
        csrf: bool = False,
    ) -> dict[str, Any]:
        headers = {}
        if csrf:
            if not self.csrf_token:
                raise AlertJobError("local API CSRF token is unavailable")
            headers["X-LOF-CSRF"] = self.csrf_token
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=json_payload,
                headers=headers,
                timeout=self.request_timeout,
            )
        except requests.RequestException as exc:
            raise AlertJobError(
                f"local API request failed at {path}: {type(exc).__name__}"
            ) from exc
        if not 200 <= response.status_code < 300:
            raise AlertJobError(
                f"local API request failed at {path}: HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError as exc:
            raise AlertJobError(f"local API returned invalid JSON at {path}") from exc
        if not isinstance(payload, dict):
            raise AlertJobError(f"local API returned a non-object payload at {path}")
        return payload


def validate_local_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlparse(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AlertJobError("base URL contains an invalid port") from exc
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise AlertJobError("base URL must be a local HTTP origin")
    if port is not None and not 1 <= port <= 65535:
        raise AlertJobError("base URL contains an invalid port")
    hostname = parsed.hostname.lower()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise AlertJobError("base URL must use a loopback address")
        except ValueError as exc:
            raise AlertJobError("base URL must use localhost or a loopback address") from exc
    return normalized


def discover_base_url(explicit: str | None, pid_path: Path) -> str:
    if explicit:
        return validate_local_base_url(explicit.strip())
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        port = int(payload["port"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AlertJobError(
            "cannot discover the local server; start serve.py or set LOF_INAV_BASE_URL"
        ) from exc
    if not 1 <= port <= 65535:
        raise AlertJobError("local server PID file contains an invalid port")
    return f"http://127.0.0.1:{port}"


def select_threshold_funds(
    payload: dict[str, Any],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    excluded_codes: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    excluded_codes = excluded_codes or set()
    premiums = []
    discounts = []
    for fund in payload.get("funds") or []:
        if (
            not isinstance(fund, dict)
            or fund.get("code") in excluded_codes
            or purchase_is_paused(fund)
        ):
            continue
        premium = fund.get("smart_premium")
        signal = fund.get("smart_premium_status")
        if (
            fund.get("status") != "ok"
            or isinstance(premium, bool)
            or not isinstance(premium, (int, float))
            or not math.isfinite(premium)
        ):
            continue
        if signal == "sell_premium" and premium > threshold:
            premiums.append(fund)
        elif signal == "buy_discount" and premium < -threshold:
            discounts.append(fund)
    premiums.sort(key=lambda fund: (-fund["smart_premium"], str(fund.get("code") or "")))
    discounts.sort(key=lambda fund: (fund["smart_premium"], str(fund.get("code") or "")))
    return premiums, discounts


def is_a_share_fund(fund: dict[str, Any]) -> bool:
    fund_type = fund.get("type")
    return isinstance(fund_type, str) and fund_type.startswith("A股")


def purchase_is_paused(fund: dict[str, Any]) -> bool:
    purchase_limit = fund.get("purchase_limit")
    return (
        isinstance(purchase_limit, dict)
        and purchase_limit.get("display") == "暂停"
    )


def nav_failed_codes(refresh_status: dict[str, Any]) -> set[str]:
    return {
        str(item["code"])
        for item in refresh_status.get("last_navs_refresh_errors") or []
        if isinstance(item, dict) and item.get("code")
    }


def build_report(
    payload: dict[str, Any],
    refresh: RefreshOutcome,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_priced_ratio: float = DEFAULT_MIN_PRICED_RATIO,
    generated_at: datetime | None = None,
) -> str:
    generated_at = generated_at or datetime.now(CHINA_TZ)
    excluded_codes = nav_failed_codes(refresh.status)
    premiums, discounts = select_threshold_funds(
        payload,
        threshold=threshold,
        excluded_codes=excluded_codes,
    )
    quote_time = display_timestamp(refresh.status.get("last_realtime_quotes_completed_at"))
    nav_time = display_timestamp(refresh.status.get("last_navs_refresh_at"))
    non_a_premiums = [fund for fund in premiums if not is_a_share_fund(fund)]
    a_premiums = [fund for fund in premiums if is_a_share_fund(fund)]
    non_a_discounts = [fund for fund in discounts if not is_a_share_fund(fund)]
    a_discounts = [fund for fund in discounts if is_a_share_fund(fund)]
    non_a_count = len(non_a_premiums) + len(non_a_discounts)
    a_count = len(a_premiums) + len(a_discounts)
    warnings = report_warnings(payload, refresh, premiums + discounts, min_priced_ratio)
    lines = [
        f"LOF 智能折溢价报告 {generated_at:%Y-%m-%d %H:%M}",
        f"阈值：智能折溢价绝对值 > {threshold:.2%}（卖出看买一，买入看卖一）",
        "筛选：已屏蔽暂停申购基金",
        f"刷新：行情 {quote_time}；净值 {nav_time}",
        f"命中：非A股 {non_a_count} 只（重点）；A股 {a_count} 只",
    ]
    if warnings:
        lines.append(
            f"数据质量提示：{len(warnings)} 项，详见末尾；"
            "长消息截断时请查看本机完整报告"
        )
    lines.extend(["", "以上为项目指示性估值，不是官方净值或交易建议。", ""])
    append_market_group(
        lines,
        "非A股（重点）",
        non_a_premiums,
        non_a_discounts,
        threshold=threshold,
        min_priced_ratio=min_priced_ratio,
    )
    lines.append("")
    append_market_group(
        lines,
        "A股",
        a_premiums,
        a_discounts,
        threshold=threshold,
        min_priced_ratio=min_priced_ratio,
    )

    if warnings:
        lines.extend(["", "数据质量提示："])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def append_market_group(
    lines: list[str],
    title: str,
    premiums: list[dict[str, Any]],
    discounts: list[dict[str, Any]],
    *,
    threshold: float,
    min_priced_ratio: float,
) -> None:
    count = len(premiums) + len(discounts)
    lines.append(f"【{title}｜{count} 只】")
    if count == 0:
        lines.append("- 未发现超过阈值的基金")
        return
    append_fund_section(
        lines, f"卖出溢价 > {threshold:.2%}", premiums, min_priced_ratio,
        book_side="bid_price", price_label="买一",
    )
    lines.append("")
    append_fund_section(
        lines, f"买入折价 < -{threshold:.2%}", discounts, min_priced_ratio,
        book_side="ask_price", price_label="卖一",
    )


def append_fund_section(
    lines: list[str],
    title: str,
    funds: list[dict[str, Any]],
    min_priced_ratio: float,
    *,
    book_side: str,
    price_label: str,
) -> None:
    lines.append(title)
    if not funds:
        lines.append("- 未发现")
        return
    for fund in funds:
        details = [
            f"{price_label} {format_number((fund.get('order_book') or {}).get(book_side))}",
            f"估值 {format_number(fund.get('estimated_nav'))}",
        ]
        priced_ratio = fund.get("priced_ratio")
        if isinstance(priced_ratio, (int, float)) and math.isfinite(priced_ratio):
            quality = "，低覆盖" if priced_ratio < min_priced_ratio else ""
            details.append(f"取价 {priced_ratio:.1%}{quality}")
        lines.append(
            f"- {fund.get('code', '-')} {fund.get('name', '')}："
            f"{fund['smart_premium']:+.2%}（{' / '.join(details)}）"
        )


def report_warnings(
    payload: dict[str, Any],
    refresh: RefreshOutcome,
    matched: list[dict[str, Any]],
    min_priced_ratio: float,
) -> list[str]:
    warnings = []
    failed_codes = sorted(nav_failed_codes(refresh.status))
    if failed_codes:
        preview = "、".join(failed_codes[:10])
        suffix = f"等 {len(failed_codes)} 只" if len(failed_codes) > 10 else ""
        warnings.append(f"本轮净值刷新失败，已排除：{preview}{suffix}")
    missing_secids = refresh.status.get("last_realtime_quotes_missing_secids") or []
    if missing_secids:
        warnings.append(f"本轮有 {len(missing_secids)} 个行情标的未成功刷新")
    low_coverage = [
        str(fund.get("code"))
        for fund in matched
        if isinstance(fund.get("priced_ratio"), (int, float))
        and math.isfinite(fund["priced_ratio"])
        and fund["priced_ratio"] < min_priced_ratio
    ]
    if low_coverage:
        warnings.append(f"阈值命中基金中低取价覆盖：{'、'.join(low_coverage)}")
    realtime_alert_codes = sorted(
        {
            str(alert.get("code"))
            for alert in payload.get("data_alerts") or []
            if isinstance(alert, dict)
            and alert.get("code")
            and alert.get("type") in {"realtime_fallback", "realtime_missing_quotes"}
        }
    )
    if realtime_alert_codes:
        warnings.append(f"实时估值存在降级或缺口：{'、'.join(realtime_alert_codes[:20])}")
    return warnings


def display_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "未知"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def format_number(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:.4f}"
    return "-"


def bot_payload(bot_type: str, message: str, *, chat_id: str | None = None) -> dict[str, Any]:
    normalized = bot_type.lower()
    if normalized not in SUPPORTED_BOT_TYPES:
        raise AlertJobError(f"unsupported bot type: {bot_type}")
    if normalized == "feishu":
        return {"msg_type": "text", "content": {"text": message}}
    if normalized in {"wecom", "dingtalk"}:
        return {"msgtype": "text", "text": {"content": message}}
    if normalized == "telegram":
        if not chat_id:
            raise AlertJobError("Telegram requires LOF_INAV_BOT_CHAT_ID")
        return {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
    return {"text": message}


def send_bot_message(
    webhook_url: str,
    bot_type: str,
    message: str,
    *,
    chat_id: str | None = None,
    timeout: float = 15.0,
    session: requests.Session | None = None,
) -> None:
    normalized = bot_type.lower()
    limit = 3800 if normalized == "telegram" else 12000
    payload = bot_payload(normalized, truncate_message(message, limit), chat_id=chat_id)
    client = session or requests.Session()
    try:
        response = client.post(webhook_url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise BotDeliveryError(f"{normalized} bot request failed: {type(exc).__name__}") from exc
    if not 200 <= response.status_code < 300:
        raise BotDeliveryError(f"{normalized} bot returned HTTP {response.status_code}")
    validate_bot_response(normalized, response)


def validate_bot_response(bot_type: str, response: requests.Response) -> None:
    if bot_type in {"generic", "slack"}:
        return
    try:
        payload = response.json()
    except requests.exceptions.JSONDecodeError as exc:
        raise BotDeliveryError(f"{bot_type} bot returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise BotDeliveryError(f"{bot_type} bot returned an invalid response")
    if bot_type == "telegram":
        success = payload.get("ok") is True
    elif bot_type == "feishu":
        success = payload.get("code", payload.get("StatusCode")) == 0
    else:
        success = payload.get("errcode") == 0
    if not success:
        raise BotDeliveryError(f"{bot_type} bot rejected the message")


def truncate_message(message: str, limit: int) -> str:
    if len(message) <= limit:
        return message
    suffix = "\n…报告已截断，请在本机查看完整结果。"
    available = max(0, limit - len(suffix))
    shortened = message[:available].rsplit("\n", 1)[0]
    return f"{shortened}{suffix}"
