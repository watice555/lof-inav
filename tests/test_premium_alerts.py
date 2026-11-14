from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.premium_alerts import (
    AlertJobError,
    CHINA_TZ,
    LocalApiClient,
    RefreshOutcome,
    bot_payload,
    build_report,
    discover_base_url,
    is_a_share_fund,
    select_threshold_funds,
    truncate_message,
    validate_local_base_url,
)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


def fund(code: str, signal_value: float | None, **overrides):
    item = {
        "code": code,
        "name": f"基金{code}",
        "status": "ok",
        "smart_premium": signal_value,
        "smart_premium_status": (
            "book_missing" if signal_value is None
            else "sell_premium" if signal_value > 0
            else "buy_discount" if signal_value < 0
            else "none"
        ),
        "estimated_nav": 1.00,
        "priced_ratio": 0.9,
    }
    item.update(overrides)
    return item


class PremiumAlertTests(unittest.TestCase):
    def test_threshold_selection_splits_and_sorts_both_directions(self) -> None:
        payload = {
            "funds": [
                fund("A", 0.016),
                fund("B", -0.030),
                fund("C", 0.025),
                fund("D", -0.015),
                fund("E", 0.015),
                fund("F", None),
                fund("G", 0.10, status="error"),
                fund("H", -0.020),
                fund("I", 0.080, purchase_limit={"display": "暂停"}),
                fund("J", -0.040, purchase_limit={"display": "限购 100 元"}),
                fund("K", 0.030, purchase_limit=None),
            ]
        }

        premiums, discounts = select_threshold_funds(
            payload,
            threshold=0.015,
            excluded_codes={"H"},
        )

        self.assertEqual([item["code"] for item in premiums], ["K", "C", "A"])
        self.assertEqual([item["code"] for item in discounts], ["J", "B"])

    def test_threshold_selection_requires_a_matching_smart_signal(self) -> None:
        payload = {
            "funds": [
                fund("SELL", 0.03, premium=-0.05),
                fund("BUY", -0.03, premium=None, quote_status="no_trade"),
                {"code": "LEGACY", "status": "ok", "premium": 0.05},
                fund("STALE", None, smart_premium_status="stale", premium=0.05),
                fund("NONE", 0.0, premium=-0.05),
                fund("CROSSED", None, smart_premium_status="book_invalid", premium=-0.05),
                fund("WRONG_SIDE", 0.04, smart_premium_status="buy_discount"),
                fund("NO_SIGNAL", -0.04, smart_premium_status=None),
                fund("NON_FINITE", float("inf")),
                fund("NON_NUMERIC", 0.04, smart_premium="0.04"),
                fund("BOOL", True),
            ]
        }

        premiums, discounts = select_threshold_funds(payload)

        self.assertEqual([item["code"] for item in premiums], ["SELL"])
        self.assertEqual([item["code"] for item in discounts], ["BUY"])

    def test_report_displays_the_order_book_price_for_each_direction(self) -> None:
        payload = {
            "funds": [
                fund("SELL", 0.03, premium=-0.03, trade_price=0.97,
                     order_book={"bid_price": 1.03, "ask_price": 1.04}),
                fund("BUY", -0.02, trade_price=None,
                     order_book={"bid_price": 0.97, "ask_price": 0.98}),
            ],
        }

        report = build_report(payload, RefreshOutcome(status={}, progress={}))

        self.assertIn("LOF 智能折溢价报告", report)
        self.assertIn("卖出溢价 > 1.50%\n- SELL 基金SELL：+3.00%（买一 1.0300 / 估值", report)
        self.assertIn("买入折价 < -1.50%\n- BUY 基金BUY：-2.00%（卖一 0.9800 / 估值", report)
        self.assertNotIn("场内", report)

    def test_report_excludes_failed_navs_and_surfaces_quality_warnings(self) -> None:
        payload = {
            "funds": [
                fund("A", 0.020, priced_ratio=0.5),
                fund("B", -0.025),
                fund("C", 0.030, purchase_limit={"display": "暂停"}),
            ],
            "data_alerts": [
                {"code": "A", "type": "realtime_fallback"},
            ],
        }
        refresh = RefreshOutcome(
            status={
                "last_realtime_quotes_completed_at": "2026-08-25T07:00:00+00:00",
                "last_realtime_quotes_missing_secids": ["0.MISSING"],
                "last_navs_refresh_at": "2026-08-25T06:59:00+00:00",
                "last_navs_refresh_errors": [{"code": "B", "error": "injected"}],
            },
            progress={
                "quotes": {"status": "partial"},
                "navs": {"status": "partial"},
            },
        )

        report = build_report(
            payload,
            refresh,
            generated_at=datetime(2026, 8, 25, 15, 5, tzinfo=CHINA_TZ),
        )

        self.assertIn("A：+2.00%", report)
        self.assertNotIn("B：-2.50%", report)
        self.assertNotIn("C：+3.00%", report)
        self.assertIn("筛选：已屏蔽暂停申购基金", report)
        self.assertIn("本轮净值刷新失败，已排除：B", report)
        self.assertIn("本轮有 1 个行情标的未成功刷新", report)
        self.assertIn("低取价覆盖：A", report)
        self.assertIn("不是官方净值或交易建议", report)
        self.assertIn("【非A股（重点）｜1 只】", report)
        self.assertIn("【A股｜0 只】", report)
        self.assertLess(
            report.index("【非A股（重点）｜1 只】"),
            report.index("【A股｜0 只】"),
        )
        self.assertLess(
            report.index("不是官方净值或交易建议"),
            report.index("【非A股（重点）｜1 只】"),
        )
        self.assertIn("数据质量提示：4 项，详见末尾", report)

    def test_report_splits_funds_into_non_a_and_a_share_groups(self) -> None:
        payload = {
            "funds": [
                fund("N1", 0.032, type="QDII-美股"),
                fund("N2", -0.021, type="商品-原油"),
                fund("A1", 0.018, type="A股-指数"),
                fund("A2", 0.030, type="A股-主动"),
                fund("M1", -0.040),
            ]
        }
        refresh = RefreshOutcome(status={}, progress={})

        report = build_report(
            payload,
            refresh,
            generated_at=datetime(2026, 10, 15, 14, 30, tzinfo=CHINA_TZ),
        )

        self.assertIn("命中：非A股 3 只（重点）；A股 2 只", report)
        non_a_start = report.index("【非A股（重点）｜3 只】")
        a_start = report.index("【A股｜2 只】")
        self.assertLess(non_a_start, a_start)
        non_a_section = report[non_a_start:a_start]
        a_section = report[a_start:]
        for code in ("N1", "N2", "M1"):
            self.assertIn(code, non_a_section)
            self.assertNotIn(code, a_section)
        for code in ("A1", "A2"):
            self.assertIn(code, a_section)
            self.assertNotIn(code, non_a_section)
        # Group order keeps the premium/discount sorting rules.
        self.assertLess(non_a_section.index("N1"), non_a_section.index("N2"))
        self.assertLess(a_section.index("A2"), a_section.index("A1"))

    def test_report_empty_groups_show_zero_counts(self) -> None:
        payload = {"funds": [fund("A", 0.005, type="A股-指数")]}
        refresh = RefreshOutcome(status={}, progress={})

        report = build_report(
            payload,
            refresh,
            generated_at=datetime(2026, 10, 15, 14, 30, tzinfo=CHINA_TZ),
        )

        self.assertIn("命中：非A股 0 只（重点）；A股 0 只", report)
        self.assertIn("【非A股（重点）｜0 只】\n- 未发现超过阈值的基金", report)
        self.assertIn("【A股｜0 只】\n- 未发现超过阈值的基金", report)

    def test_is_a_share_fund_classification(self) -> None:
        self.assertTrue(is_a_share_fund({"type": "A股"}))
        self.assertTrue(is_a_share_fund({"type": "A股-指数"}))
        self.assertFalse(is_a_share_fund({"type": "QDII-港股"}))
        self.assertFalse(is_a_share_fund({"type": "商品-贵金属"}))
        self.assertFalse(is_a_share_fund({"type": "债券-美元债"}))
        self.assertFalse(is_a_share_fund({}))
        self.assertFalse(is_a_share_fund({"type": None}))
        self.assertFalse(is_a_share_fund({"type": ""}))
        self.assertFalse(is_a_share_fund({"type": 123}))

    def test_grouped_report_truncation_keeps_summary_header(self) -> None:
        funds = [
            fund(f"N{i:04d}", 0.02 + i * 0.0001, type="QDII-美股")
            for i in range(120)
        ]
        payload = {"funds": funds}
        refresh = RefreshOutcome(status={}, progress={})

        report = build_report(
            payload,
            refresh,
            generated_at=datetime(2026, 10, 15, 14, 30, tzinfo=CHINA_TZ),
        )
        truncated = truncate_message(report, 3800)

        self.assertLessEqual(len(truncated), 3800)
        self.assertIn("报告已截断", truncated)
        self.assertIn("命中：非A股 120 只（重点）；A股 0 只", truncated)
        self.assertIn("不是官方净值或交易建议", truncated)
        self.assertIn("【非A股（重点）｜120 只】", truncated)

    def test_default_timeout_reports_progress_after_thirty_minutes(self) -> None:
        session = FakeSession([
            FakeResponse({"csrf_token": "token", "last_realtime_quotes_completed_at": "old"}),
            FakeResponse({"states": {"navs": "started", "quotes": "started"}}),
            FakeResponse({
                "navs_refreshing": True,
                "last_realtime_quotes_completed_at": "new",
                "refresh_progresses": {"navs": {
                    "phase": "daily_prices", "completed": 2000, "total": 2256,
                    "message": "secret provider URL",
                }},
            }),
        ])
        with patch("app.premium_alerts.time.monotonic", side_effect=[0, 0, 1800]):
            with self.assertRaises(AlertJobError) as caught:
                LocalApiClient("http://localhost:8001", session=session).force_current_refresh()
        message = str(caught.exception)
        self.assertIn("已等待 1800 秒，上限 1800 秒", message)
        self.assertIn("净值：刷新中；最近阶段：补齐日线基准价；最近进度：2000/2256", message)
        self.assertIn("行情：已完成", message)
        self.assertNotIn("secret", message)

    def test_timeout_while_waiting_to_start_reports_blockers(self) -> None:
        session = FakeSession([
            FakeResponse({"csrf_token": "token"}),
            FakeResponse({"states": {"navs": "blocked", "quotes": "running"}}),
            FakeResponse({}),
        ])
        with patch("app.premium_alerts.time.monotonic", side_effect=[0, 0, 5]), patch(
            "app.premium_alerts.time.sleep"
        ):
            with self.assertRaises(AlertJobError) as caught:
                LocalApiClient("http://localhost:8001", session=session).force_current_refresh(
                    timeout_seconds=5,
                )
        self.assertIn("上限 5 秒", str(caught.exception))
        self.assertIn("净值：等待启动（其他刷新任务占用）", str(caught.exception))
        self.assertIn("行情：等待已有任务结束后启动本轮刷新", str(caught.exception))

    def test_failed_refresh_reports_structured_state_without_raw_error(self) -> None:
        session = FakeSession([
            FakeResponse({"csrf_token": "token"}),
            FakeResponse({"states": {"navs": "started", "quotes": "started"}}),
            FakeResponse({"refresh_progresses": {
                "navs": {"status": "error", "message": "private error"},
                "quotes": {"status": "partial"},
            }}),
        ])
        with self.assertRaises(AlertJobError) as caught:
            LocalApiClient("http://localhost:8001", session=session).force_current_refresh()
        self.assertIn("净值：失败", str(caught.exception))
        self.assertIn("行情：部分完成", str(caught.exception))
        self.assertNotIn("private", str(caught.exception))

    def test_report_cli_defaults_to_thirty_minutes_and_accepts_override(self) -> None:
        from scripts.report_premium_alerts import parse_args

        with patch("sys.argv", ["report_premium_alerts.py"]):
            self.assertEqual(parse_args().refresh_timeout, 1800)
        with patch("sys.argv", ["report_premium_alerts.py", "--refresh-timeout", "60"]):
            self.assertEqual(parse_args().refresh_timeout, 60)

    def test_force_refresh_uses_csrf_and_waits_for_verified_completion(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "csrf_token": "token",
                        "last_navs_refresh_at": "old-nav",
                        "last_realtime_quotes_completed_at": "old-quotes",
                    }
                ),
                FakeResponse(
                    {
                        "states": {"navs": "started", "quotes": "started"},
                    }
                ),
                FakeResponse(
                    {
                        "navs_refreshing": False,
                        "quotes_refreshing": False,
                        "last_navs_refresh_at": "new-nav",
                        "last_realtime_quotes_completed_at": "new-quotes",
                        "refresh_progresses": {
                            "navs": {"status": "done"},
                            "quotes": {"status": "done"},
                        },
                    }
                ),
            ]
        )
        client = LocalApiClient("http://127.0.0.1:8001", session=session)

        outcome = client.force_current_refresh(timeout_seconds=1, poll_seconds=0.001)

        self.assertEqual(outcome.status["last_navs_refresh_at"], "new-nav")
        method, url, kwargs = session.requests[1]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:8001/api/refresh/current"))
        self.assertEqual(kwargs["headers"], {"X-LOF-CSRF": "token"})
        self.assertEqual(kwargs["json"], {"kinds": ["navs", "quotes"]})

    def test_force_refresh_reuses_running_nav_but_starts_full_quote_refresh(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "csrf_token": "token",
                        "last_navs_refresh_at": "old-nav",
                        "last_realtime_quotes_completed_at": "old-quotes",
                    }
                ),
                FakeResponse(
                    {
                        "states": {"navs": "running", "quotes": "started"},
                    }
                ),
                FakeResponse(
                    {
                        "navs_refreshing": False,
                        "quotes_refreshing": False,
                        "last_navs_refresh_at": "new-nav",
                        "last_realtime_quotes_completed_at": "new-quotes",
                        "refresh_progresses": {
                            "navs": {"status": "done"},
                            "quotes": {"status": "partial"},
                        },
                    }
                ),
            ]
        )
        client = LocalApiClient("http://127.0.0.1:8001", session=session)

        outcome = client.force_current_refresh(timeout_seconds=1, poll_seconds=0.001)

        self.assertEqual(outcome.progress["quotes"]["status"], "partial")
        refresh_requests = [
            request for request in session.requests if request[1].endswith("/api/refresh/current")
        ]
        self.assertEqual(len(refresh_requests), 1)

    def test_force_refresh_falls_back_to_funds_for_an_older_server_csrf_token(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    {
                        "last_navs_refresh_at": "old-nav",
                        "last_realtime_quotes_completed_at": "old-quotes",
                    }
                ),
                FakeResponse({"csrf_token": "token", "funds": []}),
                FakeResponse(
                    {
                        "last_navs_refresh_at": "old-nav",
                        "last_realtime_quotes_completed_at": "old-quotes",
                    }
                ),
                FakeResponse({"states": {"navs": "started", "quotes": "started"}}),
                FakeResponse(
                    {
                        "navs_refreshing": False,
                        "quotes_refreshing": False,
                        "last_navs_refresh_at": "new-nav",
                        "last_realtime_quotes_completed_at": "new-quotes",
                        "refresh_progresses": {
                            "navs": {"status": "done"},
                            "quotes": {"status": "done"},
                        },
                    }
                ),
            ]
        )
        client = LocalApiClient("http://127.0.0.1:8001", session=session)

        client.force_current_refresh(timeout_seconds=1, poll_seconds=0.001)

        self.assertTrue(any(url.endswith("/api/funds") for _method, url, _kwargs in session.requests))

    def test_funds_snapshot_request_disables_background_refresh(self) -> None:
        session = FakeSession([FakeResponse({"csrf_token": "token", "funds": []})])
        client = LocalApiClient("http://127.0.0.1:8001", session=session)

        client.get_funds(allow_background_refresh=False)

        self.assertTrue(session.requests[0][1].endswith("/api/funds?refresh=0"))

    def test_local_base_url_and_pid_discovery_reject_remote_targets(self) -> None:
        self.assertEqual(
            validate_local_base_url("http://localhost:8001/"),
            "http://localhost:8001",
        )
        with self.assertRaises(AlertJobError):
            validate_local_base_url("https://127.0.0.1:8001")
        with self.assertRaises(AlertJobError):
            validate_local_base_url("http://example.com:8001")

        with TemporaryDirectory() as directory:
            pid_path = Path(directory) / "lof_inav.pid"
            pid_path.write_text(json.dumps({"port": 8123}), encoding="utf-8")
            self.assertEqual(discover_base_url(None, pid_path), "http://127.0.0.1:8123")

    def test_bot_payloads_do_not_share_provider_specific_shapes(self) -> None:
        self.assertEqual(bot_payload("slack", "hello"), {"text": "hello"})
        self.assertEqual(
            bot_payload("feishu", "hello"),
            {"msg_type": "text", "content": {"text": "hello"}},
        )
        self.assertEqual(
            bot_payload("wecom", "hello"),
            {"msgtype": "text", "text": {"content": "hello"}},
        )
        self.assertEqual(
            bot_payload("telegram", "hello", chat_id="123"),
            {"chat_id": "123", "text": "hello", "disable_web_page_preview": True},
        )
        with self.assertRaises(AlertJobError):
            bot_payload("telegram", "hello")

    def test_message_truncation_respects_provider_limit(self) -> None:
        message = "first line\n" + "x" * 100
        truncated = truncate_message(message, 40)
        self.assertLessEqual(len(truncated), 40)
        self.assertIn("报告已截断", truncated)


if __name__ == "__main__":
    unittest.main()
