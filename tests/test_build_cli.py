from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import build as build_cli


def result(*, import_failed=None, backtests_failed=None) -> dict:
    return {
        "imported": [],
        "import_failed": import_failed or [],
        "backtests_refreshed": [],
        "backtests_failed": backtests_failed or [],
    }


class BuildCliTests(unittest.TestCase):
    def run_cli(self, args: list[str], build_result: dict) -> int:
        with (
            patch.object(build_cli, "build_all", return_value=build_result),
            redirect_stdout(StringIO()),
        ):
            return build_cli.main(args)

    def test_current_only_import_failure_returns_nonzero(self) -> None:
        exit_code = self.run_cli(
            ["--current-only"],
            result(import_failed=[{"code": "A", "error": "timeout"}]),
        )
        self.assertEqual(exit_code, 1)

    def test_full_backtest_failure_returns_nonzero(self) -> None:
        exit_code = self.run_cli(
            [],
            result(backtests_failed=[{"code": "A", "error": "no rows"}]),
        )
        self.assertEqual(exit_code, 1)

    def test_success_returns_zero(self) -> None:
        self.assertEqual(self.run_cli([], result()), 0)


if __name__ == "__main__":
    unittest.main()
