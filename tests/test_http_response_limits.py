from __future__ import annotations

import gzip
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from urllib3.exceptions import ReadTimeoutError
from urllib3.response import HTTPResponse

from app.sources import (
    HttpResponseBodyTooLarge,
    HttpResponseTransferDeadlineExceeded,
    _get,
    _post,
)


class RecordingSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)


class ChunkedRawResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = iter(chunks)
        self.closed = False
        self.socket = RecordingSocket()
        self._connection = SimpleNamespace(sock=self.socket)
        self.read_sizes: list[int] = []
        self.decode_content: bool | None = None

    def read1(self, size: int, *, decode_content: bool) -> bytes:
        self.read_sizes.append(size)
        self.decode_content = decode_content
        return next(self.chunks, b"")

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        pass


def make_response(
    raw: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response.url = "https://provider.example/data"
    response.headers.update(headers or {})
    response.raw = raw
    return response


def make_gzip_response(decoded_body: bytes) -> requests.Response:
    encoded_body = gzip.compress(decoded_body)
    raw = HTTPResponse(
        body=io.BytesIO(encoded_body),
        headers={"Content-Encoding": "gzip"},
        preload_content=False,
    )
    return make_response(raw, headers={"Content-Encoding": "gzip"})


class HttpResponseLimitTests(unittest.TestCase):
    def test_get_and_post_reject_redirects_without_following_them(self) -> None:
        cases = [
            ("get", _get, 302),
            ("post", _post, 307),
        ]
        for request_name, helper, status in cases:
            with self.subTest(method=request_name):
                raw = ChunkedRawResponse([])
                response = make_response(
                    raw,
                    status=status,
                    headers={"Location": "http://127.0.0.1/admin"},
                )
                with (
                    patch(
                        f"app.sources.requests.{request_name}",
                        return_value=response,
                    ) as request,
                    self.assertRaises(requests.TooManyRedirects),
                ):
                    helper(
                        "https://provider.example/data",
                        attempts=1,
                        allow_redirects=True,
                        stream=False,
                    )

                self.assertFalse(request.call_args.kwargs["allow_redirects"])
                self.assertTrue(request.call_args.kwargs["stream"])
                self.assertTrue(raw.closed)

    def test_successful_response_keeps_buffered_response_consumers(self) -> None:
        body = json.dumps({"value": 7}).encode()
        raw = ChunkedRawResponse([body])
        with patch("app.sources.requests.get", return_value=make_response(raw)) as request:
            response = _get("https://provider.example/data", attempts=1)

        self.assertEqual(response.content, body)
        self.assertEqual(response.text, body.decode())
        self.assertEqual(response.json(), {"value": 7})
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertTrue(request.call_args.kwargs["stream"])
        self.assertFalse(raw.decode_content)

    def test_decoded_response_body_is_capped(self) -> None:
        response = make_gzip_response(b"x" * 17)
        with (
            patch("app.sources.HTTP_RESPONSE_MAX_BYTES", 16, create=True),
            patch("app.sources.requests.get", return_value=response),
            self.assertRaises(HttpResponseBodyTooLarge),
        ):
            _get("https://provider.example/data", attempts=1)

        self.assertTrue(response.raw.closed)

    def test_total_transfer_deadline_stops_continuously_delivered_body(self) -> None:
        raw = ChunkedRawResponse([b"a", b"b"])
        response = make_response(raw)
        with (
            patch("app.sources.requests.get", return_value=response),
            patch("app.sources.time.monotonic", side_effect=[0.0, 0.1, 1.1]),
            self.assertRaises(HttpResponseTransferDeadlineExceeded),
        ):
            _get("https://provider.example/data", timeout=1, attempts=1)

        self.assertTrue(raw.closed)
        self.assertEqual(len(raw.socket.timeouts), 1)
        self.assertAlmostEqual(raw.socket.timeouts[0], 0.9)

    def test_socket_timeout_at_transfer_deadline_uses_deadline_error(self) -> None:
        raw = ChunkedRawResponse([])
        response = make_response(raw)
        with (
            patch("app.sources.requests.get", return_value=response),
            patch.object(
                raw,
                "read1",
                side_effect=ReadTimeoutError(None, None, "read timed out"),
            ),
            patch("app.sources.time.monotonic", side_effect=[0.0, 0.1, 1.1]),
            self.assertRaises(HttpResponseTransferDeadlineExceeded),
        ):
            _get("https://provider.example/data", timeout=1, attempts=1)

        self.assertTrue(raw.closed)

    def test_caller_cannot_expand_transfer_deadline(self) -> None:
        raw = ChunkedRawResponse([b"ok"])
        with patch("app.sources.requests.get", return_value=make_response(raw)) as request:
            response = _get(
                "https://provider.example/data",
                timeout=60,
                attempts=1,
            )

        self.assertEqual(response.content, b"ok")
        self.assertEqual(request.call_args.kwargs["timeout"], 30.0)

    def test_body_limit_failure_preserves_retry_semantics(self) -> None:
        responses = [
            make_response(ChunkedRawResponse([b"12345"])),
            make_response(ChunkedRawResponse([b"ok"])),
        ]
        with (
            patch("app.sources.HTTP_RESPONSE_MAX_BYTES", 4, create=True),
            patch("app.sources.requests.get", side_effect=responses) as request,
            patch("app.sources._sleep_within_daily_price_deadline") as retry_sleep,
        ):
            response = _get("https://provider.example/data", attempts=2)

        self.assertEqual(response.content, b"ok")
        self.assertEqual(request.call_count, 2)
        self.assertTrue(responses[0].raw.closed)
        retry_sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
