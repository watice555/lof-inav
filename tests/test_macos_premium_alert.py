from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

if sys.platform != "darwin":
    raise unittest.SkipTest("macOS LaunchAgent tests require macOS")

from scripts.install_macos_premium_alert import (
    LAUNCH_AGENT_LABEL,
    launch_agent_path,
    launch_agent_payload,
)
from scripts.run_premium_alert_macos import read_pid_base_url


class MacosPremiumAlertTests(unittest.TestCase):
    def test_launch_agent_runs_each_weekday_at_1430_without_credentials(self) -> None:
        root = Path("/opt/LOF_iNAV")

        payload = launch_agent_payload(root)

        self.assertEqual(payload["Label"], LAUNCH_AGENT_LABEL)
        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/opt/LOF_iNAV/.venv/bin/python",
                "/opt/LOF_iNAV/scripts/run_premium_alert_macos.py",
            ],
        )
        self.assertEqual(
            payload["StartCalendarInterval"],
            [
                {"Weekday": weekday, "Hour": 14, "Minute": 30}
                for weekday in range(2, 7)
            ],
        )
        self.assertNotIn("EnvironmentVariables", payload)
        serialized = json.dumps(payload)
        self.assertNotIn("LOF_iNAV_TG_TOKEN", serialized)
        self.assertNotIn("LOF_iNAV_TG_CHAT_ID", serialized)

    def test_launch_agent_path_is_scoped_to_the_user_library(self) -> None:
        self.assertEqual(
            launch_agent_path(Path("/Users/example")),
            Path("/Users/example/Library/LaunchAgents")
            / f"{LAUNCH_AGENT_LABEL}.plist",
        )

    def test_pid_base_url_requires_the_expected_server_process(self) -> None:
        with TemporaryDirectory() as directory:
            pid_path = Path(directory) / "lof_inav.pid"
            pid_path.write_text(
                json.dumps({"pid": 1234, "port": 8123}),
                encoding="utf-8",
            )

            self.assertEqual(
                read_pid_base_url(pid_path, expected_pid=1234),
                "http://127.0.0.1:8123",
            )
            self.assertIsNone(read_pid_base_url(pid_path, expected_pid=4321))

    def test_pid_base_url_rejects_invalid_or_missing_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            pid_path = Path(directory) / "lof_inav.pid"
            self.assertIsNone(read_pid_base_url(pid_path))
            pid_path.write_text('{"pid": 1, "port": 8001}', encoding="utf-8")
            self.assertIsNone(read_pid_base_url(pid_path))


if __name__ == "__main__":
    unittest.main()
