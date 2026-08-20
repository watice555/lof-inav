from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from app import server


@contextmanager
def fake_connection():
    yield object()


class FakeBacktestConnection:
    def execute(self, _sql, _params=()):
        return [
            {"date": "2026-07-10", "previous_date": "2026-07-09"},
            {"date": "2026-07-09", "previous_date": "2026-07-08"},
        ]


@contextmanager
def fake_backtest_connection():
    yield FakeBacktestConnection()


class ServerStartupTests(unittest.TestCase):
    def test_handler_access_logging_does_not_require_stderr(self) -> None:
        handler = object.__new__(server.Handler)
        handler.client_address = ("127.0.0.1", 12345)

        with (
            patch("app.server.sys.stderr", None),
            patch("app.server.LOGGER.info") as logger_info,
        ):
            handler.log_message('"%s" %s %s', "GET / HTTP/1.1", "200", "123")

        logger_info.assert_called_once_with(
            "%s - %s",
            "127.0.0.1",
            '"GET / HTTP/1.1" 200 123',
        )

    def test_windowed_logging_skips_missing_stderr(self) -> None:
        file_handler = Mock()
        root_logger = Mock()
        with (
            patch.object(server.configure_logging, "_configured", False, create=True),
            patch("app.server.LOG_PATH") as log_path,
            patch("app.server.sys.stderr", None),
            patch("app.server.RotatingFileHandler", return_value=file_handler),
            patch("app.server.logging.StreamHandler") as stream_handler,
            patch("app.server.logging.getLogger", return_value=root_logger),
        ):
            log_path.parent.mkdir = Mock()
            server.configure_logging()

        stream_handler.assert_not_called()
        root_logger.addHandler.assert_called_once_with(file_handler)

    def test_browser_is_opened_with_the_port_that_was_actually_bound(self) -> None:
        class FakeHTTPServer:
            server_address = ("127.0.0.1", 8001)

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        fake_http_server = FakeHTTPServer()
        with (
            patch("app.server.HOST", "127.0.0.1"),
            patch("app.server.PORT", 8000),
            patch("app.server.URL", "http://127.0.0.1:8000"),
            patch("app.server.ALLOWED_LOCAL_HOSTS", set()),
            patch("app.server.ALLOWED_LOCAL_ORIGINS", set()),
            patch("app.server.configure_logging"),
            patch("app.server.init_db"),
            patch("app.server.require_database_ready"),
            patch("app.server.create_http_server", return_value=fake_http_server),
            patch("app.server.should_open_browser", return_value=True),
            patch("app.server.write_server_pid", return_value="token"),
            patch("app.server.remove_server_pid"),
            patch("app.server.threading.Thread") as thread,
        ):
            server.main()

            self.assertEqual(server.URL, "http://127.0.0.1:8001")
            self.assertIn("127.0.0.1:8001", server.ALLOWED_LOCAL_HOSTS)
            self.assertEqual(thread.call_args.kwargs["args"], (server.URL,))
            thread.return_value.start.assert_called_once_with()

    def test_default_port_falls_back_when_used_by_another_program(self) -> None:
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        occupied_port = occupied.getsockname()[1]
        http_server = None
        try:
            with (
                patch("app.server.HOST", "127.0.0.1"),
                patch("app.server.PORT", occupied_port),
                patch("app.server.PORT_IS_EXPLICIT", False),
            ):
                http_server = server.create_http_server()
            self.assertNotEqual(http_server.server_address[1], occupied_port)
        finally:
            if http_server is not None:
                http_server.server_close()
            occupied.close()

    def test_explicit_port_reports_a_conflict_instead_of_claiming_app_is_running(self) -> None:
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        occupied_port = occupied.getsockname()[1]
        try:
            with (
                patch("app.server.HOST", "127.0.0.1"),
                patch("app.server.PORT", occupied_port),
                patch("app.server.PORT_IS_EXPLICIT", True),
            ):
                with self.assertRaisesRegex(SystemExit, "used by another program"):
                    server.create_http_server()
        finally:
            occupied.close()

    def test_pid_file_is_only_removed_by_its_owner_token(self) -> None:
        with TemporaryDirectory() as directory:
            pid_path = Path(directory) / "lof_inav.pid"
            with patch("app.server.PID_PATH", pid_path):
                token = server.write_server_pid()
                self.assertTrue(pid_path.exists())
                server.remove_server_pid("different-token")
                self.assertTrue(pid_path.exists())
                server.remove_server_pid(token)
                self.assertFalse(pid_path.exists())

    def test_remote_bind_is_rejected(self) -> None:
        server.require_loopback_host("127.0.0.1")
        server.require_loopback_host("localhost")
        with self.assertRaisesRegex(SystemExit, "loopback"):
            server.require_loopback_host("0.0.0.0")

    def test_ready_database_is_accepted(self) -> None:
        with (
            patch("app.server.connect", side_effect=fake_connection),
            patch("app.server.database_readiness", return_value={"ready": True}),
        ):
            self.assertEqual(server.require_database_ready(), {"ready": True})

    def test_incompatible_database_stops_before_binding(self) -> None:
        status = {"ready": False, "build_state": "legacy"}
        with (
            patch("app.server.connect", side_effect=fake_connection),
            patch("app.server.database_readiness", return_value=status),
        ):
            with self.assertRaisesRegex(SystemExit, "build.py --current-only"):
                server.require_database_ready()

    def test_backtest_details_share_price_lookup_cache_across_rows(self) -> None:
        caches = []

        def diagnostics(
            _con,
            code,
            _previous_date,
            date,
            *,
            price_lookup_cache=None,
        ):
            self.assertIsNotNone(price_lookup_cache)
            caches.append(price_lookup_cache)
            price_lookup_cache[(code, date)] = None
            return {"date": date}

        payloads = []
        handler = object.__new__(server.Handler)
        handler.json = payloads.append
        with (
            patch("app.server.connect", side_effect=fake_backtest_connection),
            patch("app.server.get_meta", return_value=False),
            patch("app.server.backtest_price_diagnostics", side_effect=diagnostics),
        ):
            handler.handle_backtest("TEST")

        self.assertEqual(len(caches), 2)
        self.assertIs(caches[0], caches[1])
        self.assertEqual(len(caches[0]), 2)
        self.assertEqual(len(payloads[0]["rows"]), 2)


if __name__ == "__main__":
    unittest.main()
