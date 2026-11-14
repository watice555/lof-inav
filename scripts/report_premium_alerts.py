from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.market_calendar import (
    calendar_year_is_known,
    calendar_year_is_provisional,
    is_trading_session,
)
from app.premium_alerts import (
    AlertJobError,
    BotDeliveryError,
    CHINA_TZ,
    DEFAULT_MIN_PRICED_RATIO,
    DEFAULT_REFRESH_TIMEOUT_SECONDS,
    DEFAULT_THRESHOLD,
    LocalApiClient,
    build_report,
    discover_base_url,
    send_bot_message,
)
from app.runtime import data_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Force-refresh local LOF iNAV data, report funds beyond a smart premium/discount "
            "threshold, and deliver the report to a bot webhook."
        )
    )
    parser.add_argument(
        "--threshold-percent",
        type=float,
        default=DEFAULT_THRESHOLD * 100,
        help="absolute smart premium/discount threshold in percent (default: 1.5)",
    )
    parser.add_argument(
        "--min-priced-ratio",
        type=float,
        default=DEFAULT_MIN_PRICED_RATIO,
        help="ratio below which a matched estimate is marked low coverage (default: 0.6)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LOF_INAV_BASE_URL"),
        help="local server origin; otherwise discover it from data/lof_inav.pid",
    )
    parser.add_argument(
        "--refresh-timeout",
        type=float,
        default=DEFAULT_REFRESH_TIMEOUT_SECONDS,
        help="maximum seconds to wait for forced refreshes (default: 1800)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=2.0,
        help="refresh status polling interval (default: 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report without sending it to a bot",
    )
    parser.add_argument(
        "--force-run",
        action="store_true",
        help="run even when today is not a known China trading session",
    )
    parser.add_argument(
        "--allow-unknown-calendar",
        action="store_true",
        help="use weekday fallback when the built-in China holiday year is not maintained",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not math.isfinite(args.threshold_percent) or args.threshold_percent <= 0:
        raise AlertJobError("threshold percent must be a positive number")
    if (
        not math.isfinite(args.min_priced_ratio)
        or args.min_priced_ratio < 0
        or args.min_priced_ratio > 1
    ):
        raise AlertJobError("minimum priced ratio must be between 0 and 1")
    if not math.isfinite(args.refresh_timeout) or args.refresh_timeout <= 0:
        raise AlertJobError("refresh timeout must be positive")
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        raise AlertJobError("poll interval must be positive")


def bot_config() -> tuple[str, str | None, str | None]:
    return (
        os.environ.get("LOF_INAV_BOT_TYPE", "generic").strip().lower(),
        (os.environ.get("LOF_INAV_BOT_WEBHOOK_URL") or "").strip() or None,
        (os.environ.get("LOF_INAV_BOT_CHAT_ID") or "").strip() or None,
    )


def send_failure_notice(message: str) -> None:
    bot_type, webhook_url, chat_id = bot_config()
    if not webhook_url:
        return
    notice = (
        f"LOF 智能折溢价任务失败 {datetime.now(CHINA_TZ):%Y-%m-%d %H:%M}\n"
        f"{message}\n本次未发送阈值判断结果。"
    )
    try:
        send_bot_message(webhook_url, bot_type, notice, chat_id=chat_id)
    except AlertJobError as exc:
        print(f"Bot failure notice could not be delivered: {exc}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        now = datetime.now(CHINA_TZ)
        if not args.force_run:
            if not calendar_year_is_known("CN", now.year) and not args.allow_unknown_calendar:
                raise AlertJobError(
                    f"China trading calendar is not maintained for {now.year}; "
                    "update app/market_calendar.py or pass --allow-unknown-calendar"
                )
            if calendar_year_is_provisional("CN", now.year):
                print(
                    f"China trading calendar for {now.year} is provisional pending the "
                    "annual SSE closure notice",
                    file=sys.stderr,
                )
            if not is_trading_session("CN", now.date()):
                print(f"{now.date().isoformat()} is not a China trading session; skipped")
                return 0

        bot_type, webhook_url, chat_id = bot_config()
        if not args.dry_run and not webhook_url:
            raise AlertJobError("LOF_INAV_BOT_WEBHOOK_URL is required unless --dry-run is used")

        base_url = discover_base_url(args.base_url, data_dir() / "lof_inav.pid")
        client = LocalApiClient(base_url)
        refresh = client.force_current_refresh(
            timeout_seconds=args.refresh_timeout,
            poll_seconds=args.poll_seconds,
        )
        payload = client.get_funds(allow_background_refresh=False)
        report = build_report(
            payload,
            refresh,
            threshold=args.threshold_percent / 100,
            min_priced_ratio=args.min_priced_ratio,
            generated_at=now,
        )
        print(report)
        if not args.dry_run:
            send_bot_message(webhook_url or "", bot_type, report, chat_id=chat_id)
        return 0
    except BotDeliveryError as exc:
        print(f"Premium alert delivery failed: {exc}", file=sys.stderr)
        return 1
    except AlertJobError as exc:
        print(f"Premium alert job failed: {exc}", file=sys.stderr)
        if not args.dry_run:
            send_failure_notice(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
