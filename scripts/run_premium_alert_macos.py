from __future__ import annotations

import json
import os
import pwd
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


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
    CHINA_TZ,
    LocalApiClient,
    send_bot_message,
    validate_local_base_url,
)
from app.runtime import data_dir


SERVER_START_TIMEOUT_SECONDS = 60.0
SERVER_STOP_TIMEOUT_SECONDS = 15.0
KEYCHAIN_TOKEN_SERVICE = "LOF_iNAV_TG_TOKEN"
KEYCHAIN_CHAT_ID_SERVICE = "LOF_iNAV_TG_CHAT_ID"


@dataclass(frozen=True)
class TelegramSecrets:
    token: str
    chat_id: str


def log(message: str) -> None:
    print(f"{datetime.now(CHINA_TZ):%Y-%m-%d %H:%M:%S} {message}", flush=True)


def read_pid_base_url(
    pid_path: Path,
    *,
    expected_pid: int | None = None,
) -> str | None:
    try:
        payload = json.loads(pid_path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        port = int(payload["port"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if pid <= 1 or not 1 <= port <= 65_535:
        return None
    if expected_pid is not None and pid != expected_pid:
        return None
    try:
        return validate_local_base_url(f"http://127.0.0.1:{port}")
    except AlertJobError:
        return None


def local_api_is_ready(base_url: str) -> bool:
    try:
        LocalApiClient(base_url, request_timeout=3.0).get_refresh_status()
    except AlertJobError:
        return False
    return True


def find_existing_server(pid_path: Path) -> str | None:
    base_url = read_pid_base_url(pid_path)
    if base_url and local_api_is_ready(base_url):
        return base_url
    return None


def start_local_server() -> subprocess.Popen[Any]:
    environment = os.environ.copy()
    environment["LOF_INAV_NO_BROWSER"] = "1"
    environment.pop("LOF_INAV_OPEN_BROWSER", None)
    return subprocess.Popen(
        [sys.executable, str(ROOT / "serve.py")],
        cwd=ROOT,
        env=environment,
    )


def wait_for_started_server(
    process: subprocess.Popen[Any],
    pid_path: Path,
    *,
    timeout_seconds: float = SERVER_START_TIMEOUT_SECONDS,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise AlertJobError(
                f"temporary local server exited during startup with status {return_code}"
            )
        base_url = read_pid_base_url(pid_path, expected_pid=process.pid)
        if base_url and local_api_is_ready(base_url):
            return base_url
        time.sleep(0.5)
    raise AlertJobError("temporary local server did not become ready within 60 seconds")


def stop_process(
    process: subprocess.Popen[Any],
    *,
    graceful_signal: signal.Signals,
    timeout_seconds: float,
) -> None:
    if process.poll() is not None:
        return
    process.send_signal(graceful_signal)
    try:
        process.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def read_keychain_secret(service: str) -> str:
    account = pwd.getpwuid(os.getuid()).pw_name
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            account,
            "-s",
            service,
            "-w",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise AlertJobError(f"macOS Keychain item {service} is unavailable")
    return value


def read_telegram_secrets() -> TelegramSecrets:
    return TelegramSecrets(
        token=read_keychain_secret(KEYCHAIN_TOKEN_SERVICE),
        chat_id=read_keychain_secret(KEYCHAIN_CHAT_ID_SERVICE),
    )


def telegram_environment(
    secrets: TelegramSecrets,
    *,
    base_url: str,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LOF_INAV_BASE_URL": base_url,
            "LOF_INAV_BOT_TYPE": "telegram",
            "LOF_INAV_BOT_CHAT_ID": secrets.chat_id,
            "LOF_INAV_BOT_WEBHOOK_URL": (
                f"https://api.telegram.org/bot{secrets.token}/sendMessage"
            ),
        }
    )
    return environment


def send_preflight_failure(secrets: TelegramSecrets, message: str) -> None:
    notice = (
        f"LOF 智能折溢价任务失败 {datetime.now(CHINA_TZ):%Y-%m-%d %H:%M}\n"
        f"{message}\n本次未发送阈值判断结果。"
    )
    webhook_url = f"https://api.telegram.org/bot{secrets.token}/sendMessage"
    try:
        send_bot_message(
            webhook_url,
            "telegram",
            notice,
            chat_id=secrets.chat_id,
        )
    except AlertJobError:
        log("Telegram preflight failure notice could not be delivered")


def _raise_on_shutdown(_signum: int, _frame: Any) -> None:
    raise KeyboardInterrupt


def main() -> int:
    signal.signal(signal.SIGINT, _raise_on_shutdown)
    signal.signal(signal.SIGTERM, _raise_on_shutdown)
    now = datetime.now(CHINA_TZ)
    if not calendar_year_is_known("CN", now.year):
        log(f"failed: China trading calendar is not maintained for {now.year}")
        return 1
    if calendar_year_is_provisional("CN", now.year):
        log(
            f"warning: China trading calendar for {now.year} is provisional pending "
            "the annual SSE closure notice"
        )
    if not is_trading_session("CN", now.date()):
        log(f"skipped: {now.date().isoformat()} is not a China trading session")
        return 0

    log("launchd premium alert started")
    server_process: subprocess.Popen[Any] | None = None
    report_process: subprocess.Popen[Any] | None = None
    secrets: TelegramSecrets | None = None
    report_started = False
    try:
        secrets = read_telegram_secrets()
        pid_path = data_dir() / "lof_inav.pid"
        base_url = find_existing_server(pid_path)
        if base_url:
            log("using the pre-existing local LOF service")
        else:
            log("starting a temporary local LOF service")
            server_process = start_local_server()
            base_url = wait_for_started_server(server_process, pid_path)

        environment = telegram_environment(secrets, base_url=base_url)
        report_process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "report_premium_alerts.py")],
            cwd=ROOT,
            env=environment,
        )
        report_started = True
        return_code = report_process.wait()
        if return_code != 0:
            log(f"premium alert exited with status {return_code}")
            return return_code
        log("launchd premium alert completed successfully")
        return 0
    except AlertJobError as exc:
        message = str(exc)
        log(f"failed: {message}")
        if secrets is not None and not report_started:
            send_preflight_failure(secrets, message)
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        message = f"local runner error: {type(exc).__name__}"
        log(f"failed: {message}")
        if secrets is not None and not report_started:
            send_preflight_failure(secrets, message)
        return 1
    except KeyboardInterrupt:
        log("launchd premium alert interrupted")
        return 130
    finally:
        if report_process is not None and report_process.poll() is None:
            stop_process(
                report_process,
                graceful_signal=signal.SIGTERM,
                timeout_seconds=5,
            )
        if server_process is not None:
            stop_process(
                server_process,
                graceful_signal=signal.SIGINT,
                timeout_seconds=SERVER_STOP_TIMEOUT_SECONDS,
            )
            log("temporary local LOF service stopped")


if __name__ == "__main__":
    raise SystemExit(main())
