from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LAUNCH_AGENT_LABEL = "local.lof-inav.premium-alert"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or remove the macOS 14:30 LOF smart premium-alert LaunchAgent."
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="unload and remove the LaunchAgent instead of installing it",
    )
    return parser.parse_args()


def launch_agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / (
        f"{LAUNCH_AGENT_LABEL}.plist"
    )


def launch_agent_payload(root: Path = ROOT) -> dict[str, Any]:
    weekdays = [
        {"Weekday": weekday, "Hour": 14, "Minute": 30}
        for weekday in range(2, 7)
    ]
    log_path = root / "data" / "premium_alert_launchd.log"
    error_log_path = root / "data" / "premium_alert_launchd.error.log"
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            str(root / ".venv" / "bin" / "python"),
            str(root / "scripts" / "run_premium_alert_macos.py"),
        ],
        "WorkingDirectory": str(root),
        "StartCalendarInterval": weekdays,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(error_log_path),
    }


def launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def unload(path: Path) -> None:
    launchctl("bootout", f"gui/{os.getuid()}", str(path), check=False)


def install(path: Path) -> None:
    python_path = ROOT / ".venv" / "bin" / "python"
    if not python_path.is_file():
        raise SystemExit(f"Project virtual environment is unavailable: {python_path}")
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    unload(path)
    temporary_path = path.with_suffix(".plist.tmp")
    with temporary_path.open("wb") as stream:
        plistlib.dump(launch_agent_payload(), stream, sort_keys=False)
    os.replace(temporary_path, path)
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(path), check=False)
    if result.returncode != 0:
        raise SystemExit("Could not load the premium-alert LaunchAgent")
    launchctl("enable", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}")
    print(f"Installed {LAUNCH_AGENT_LABEL} at {path}")
    print("Schedule: Monday-Friday at 14:30 local time")


def uninstall(path: Path) -> None:
    unload(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    print(f"Removed {LAUNCH_AGENT_LABEL}")


def main() -> int:
    args = parse_args()
    path = launch_agent_path()
    if args.uninstall:
        uninstall(path)
    else:
        install(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
