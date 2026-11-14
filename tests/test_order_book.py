from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.build import refresh_quotes
from app.db import SCHEMA, get_meta, init_db, schema_contract_issues, set_meta
from app.server import (
    Handler, compact_fund_payload, quote_cache_missing_secids,
    quote_refresh_due_secids, quote_status_payload,
)
from app.sources import (
    _sina_cn_quote_from_parts, fetch_realtime_quotes, normalize_realtime_quote,
    sina_cn_realtime_quotes, utc_now,
)
from app.valuation import estimate_intraday, realtime_asset_return_with_warning, trade_quote_status


def sina_parts() -> list[str]:
    """Synthetic response using the user's November 12 top-of-book example."""
    parts = ["0"] * 33
    parts[0] = "测试LOF"
    parts[2] = "1.2"
    parts[10:12] = ["4500", "1.199"]
    parts[20:22] = ["7300", "1.209"]
    parts[30:32] = ["2026-11-12", "15:00:00"]
    return parts


def snapshot(**changes) -> dict:
    return {**_sina_cn_quote_from_parts("0.160526", sina_parts()), **changes}


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.execute("insert into funds(code, name, exchange_market, updated_at) values ('160526', '测试LOF', 0, 'now')")
    con.execute("insert into navs(fund_code, date, nav) values ('160526', '2026-11-12', 1.2)")
    return con


def refresh(con, rows, secids=None):
    with patch("app.build.fetch_realtime_quotes", return_value=rows):
        return refresh_quotes(con, secids or ["0.160526"], attempts=1)


class OrderBookTests(unittest.TestCase):
    def test_sina_retains_share_units_and_zero_trade_snapshot(self) -> None:
        body = 'var hq_str_sz160526="' + ",".join(sina_parts()) + '";'
        with patch("app.sources._get", return_value=SimpleNamespace(text=body)):
            rows = sina_cn_realtime_quotes(["0.160526"])
        quote = normalize_realtime_quote(rows[0])
        self.assertEqual(quote["trade_status"], "no_trade")
        self.assertIsNone(quote["price"])
        self.assertIsNone(quote["pct"])
        self.assertEqual(quote["volume"], 0)
        self.assertEqual((quote["bid_price"], quote["bid_volume"]), (1.199, 4500))
        self.assertEqual((quote["ask_price"], quote["ask_volume"]), (1.209, 7300))
        self.assertEqual(quote["quote_time"], "2026-11-12T07:00:00+00:00")
        self.assertEqual(quote["session_date"], "2026-11-12")
        self.assertEqual(normalize_realtime_quote(quote), quote)

    def test_zero_price_alone_is_not_evidence_of_no_trades(self) -> None:
        for volume in (None, -1, float("nan"), float("inf"), 100):
            with self.subTest(volume=volume):
                quote = normalize_realtime_quote(snapshot(volume=volume))
                self.assertEqual(quote["trade_status"], "book_only")
                self.assertIsNone(quote["price"])
        self.assertEqual(normalize_realtime_quote(snapshot(price=1.2))["trade_status"], "no_trade")
        self.assertEqual(normalize_realtime_quote(snapshot(price=1.205, volume=200))["trade_status"], "traded")

    def test_malformed_symbol_does_not_discard_other_book_quotes(self) -> None:
        malformed = sina_parts()
        malformed[30] = "invalid"
        body = (
            'var hq_str_sz161131="' + ",".join(malformed) + '";'
            'var hq_str_sz160526="' + ",".join(sina_parts()) + '";'
        )
        with patch("app.sources._get", return_value=SimpleNamespace(text=body)):
            rows = sina_cn_realtime_quotes(["0.161131", "0.160526"])
        self.assertEqual([row["secid"] for row in rows], ["0.160526"])

    def test_book_requires_timestamp_and_positive_price_quantity_pairs(self) -> None:
        for changes in (
            {"quote_time": None}, {"quote_time": "invalid"},
            {"bid_volume": 0, "ask_volume": 0},
            {"bid_price": -1, "ask_price": float("nan")},
            {"bid_volume": float("inf"), "ask_volume": 0.5},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(normalize_realtime_quote(snapshot(**changes)))
        quote = normalize_realtime_quote(snapshot(ask_volume=0))
        self.assertEqual(quote["bid_volume"], 4500)
        self.assertIsNone(quote["ask_price"])
        self.assertIsNone(quote["ask_volume"])

    def test_source_fallback_recovers_invalid_eastmoney_and_enriches_funds(self) -> None:
        for price in (None, 0, 1.2):
            with (
                self.subTest(price=price),
                patch("app.sources.eastmoney_realtime_batch", return_value=[snapshot(price=price, volume=None, bid_volume=None, ask_volume=None)]),
                patch("app.sources.sina_cn_realtime_quotes", return_value=[snapshot()]) as sina,
                patch("app.sources.fallback_realtime_quote") as fallback,
            ):
                rows = fetch_realtime_quotes(["0.160526"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["trade_status"], "no_trade")
                sina.assert_called_once_with(["0.160526"])
                fallback.assert_not_called()

    def test_older_or_failed_book_source_does_not_destroy_newer_trade(self) -> None:
        for response in ([snapshot()], []):
            with (
                self.subTest(response=response),
                patch("app.sources.eastmoney_realtime_batch", return_value=[snapshot(price=1.21, volume=None, quote_time="2026-11-13T07:00:00+00:00", bid_volume=None, ask_volume=None)]),
                patch("app.sources.sina_cn_realtime_quotes", return_value=response),
            ):
                rows = fetch_realtime_quotes(["0.160526"])
                self.assertEqual(rows[0]["price"], 1.21)
                self.assertEqual(rows[0]["trade_status"], "traded")

    def test_same_session_closing_book_survives_vendor_timestamp_difference(self) -> None:
        for price, trade_price, expected in ((1.205, 1.205, "traded"), (0, 1.2, "no_trade"), (1.195, 1.205, None)):
            trade = snapshot(price=trade_price, volume=None, bid_volume=None, ask_volume=None)
            trade["quote_time"] = "2026-11-12T07:34:00+00:00"
            book = snapshot(price=price, volume=100 if price else 0)
            with (
                self.subTest(price=price, trade_price=trade_price),
                patch("app.sources.eastmoney_realtime_batch", return_value=[trade]),
                patch("app.sources.sina_cn_realtime_quotes", return_value=[book]),
            ):
                quote, = fetch_realtime_quotes(["0.160526"])
            if expected is None:
                self.assertIsNone(quote["bid_price"])
                self.assertEqual(quote["quote_time"], trade["quote_time"])
            else:
                self.assertEqual(quote["trade_status"], expected)
                self.assertEqual(quote["bid_price"], book["bid_price"])
                self.assertEqual(quote["quote_time"], book["quote_time"])

    def test_no_trade_replaces_old_price_without_using_book_in_valuation(self) -> None:
        con = make_connection()
        refresh(con, [snapshot(price=1.21, volume=100)])
        result = refresh(con, [snapshot()])
        self.assertEqual(result, {"requested": 1, "saved": 1, "missing": []})
        item = compact_fund_payload(estimate_intraday(con, "160526"))
        self.assertIsNone(item["trade_price"])
        self.assertIsNone(item["premium"])
        self.assertAlmostEqual(item["buy_premium"], 1.209 / 1.2 - 1)
        self.assertAlmostEqual(item["sell_premium"], 1.199 / 1.2 - 1)
        self.assertEqual(item["estimated_nav"], 1.2)
        self.assertEqual(item["quote_status"], "no_trade")
        self.assertEqual(item["order_book"]["bid_volume"], 4500)
        self.assertEqual(quote_cache_missing_secids(con, ["0.160526"]), [])
        self.assertEqual(quote_refresh_due_secids(con, ["0.160526"], now_at=utc_now()), [])
        later = (datetime.now(timezone.utc) + timedelta(seconds=61)).isoformat()
        self.assertEqual(quote_refresh_due_secids(con, ["0.160526"], now_at=later), ["0.160526"])
        for base in (None, "2026-11-12", "2026-11-11"):
            value, warning = realtime_asset_return_with_warning(con, "0.160526", base)
            self.assertIsNone(value)
            self.assertEqual(warning["type"], "quote_trade_unavailable")

    def test_failed_or_stale_snapshots_hide_book_and_recovery_clears_old_sides(self) -> None:
        con = make_connection()
        refresh(con, [snapshot()])
        row = con.execute("select * from quotes").fetchone()
        future = datetime.now(timezone.utc) + timedelta(minutes=16)
        self.assertEqual(trade_quote_status(row, "0.160526", now=future), "stale")
        with patch("app.valuation.REALTIME_QUOTE_MAX_AGE_SECONDS", -1):
            item = estimate_intraday(con, "160526")
            self.assertEqual(item["quote_status"], "stale")
            self.assertIsNone(item["order_book"])
            self.assertIsNone(item["buy_premium"])
            self.assertIsNone(item["sell_premium"])
            self.assertIsNone(item["smart_premium"])
            self.assertEqual(item["smart_premium_status"], "stale")
        refresh(con, [])
        item = estimate_intraday(con, "160526")
        self.assertEqual(item["quote_status"], "fetch_failed")
        self.assertIsNone(item["smart_premium"])
        self.assertEqual(item["smart_premium_status"], "fetch_failed")
        self.assertIsNone(item["order_book"])
        self.assertIsNone(item["buy_premium"])
        self.assertIsNone(item["sell_premium"])
        self.assertIsNone(item["premium"])
        refresh(con, [snapshot(price=1.21, volume=100, ask_volume=0)])
        item = estimate_intraday(con, "160526")
        self.assertEqual(item["quote_status"], "traded")
        self.assertEqual(item["trade_price"], 1.21)
        self.assertAlmostEqual(item["premium"], 1.21 / 1.2 - 1)
        self.assertIsNone(item["order_book"]["ask_price"])
        refresh(con, [snapshot(price=1.21, volume=100, bid_volume=0, ask_volume=0)])
        self.assertIsNone(estimate_intraday(con, "160526")["order_book"])

    def test_book_premiums_follow_execution_side_not_the_sign(self) -> None:
        con = make_connection()
        con.execute("update navs set nav = 1 where fund_code = '160526'")
        for bid, ask in ((0.99, 1.01), (1.01, 1.02), (0.98, 0.99), (1.0, 1.0)):
            for price, volume in ((0, 0), (0.995, 100), (0, None)):
                with self.subTest(bid=bid, ask=ask, price=price, volume=volume):
                    refresh(con, [snapshot(price=price, volume=volume, bid_price=bid, ask_price=ask)])
                    item = compact_fund_payload(estimate_intraday(con, "160526"))
                    self.assertAlmostEqual(item["buy_premium"], ask - 1)
                    self.assertAlmostEqual(item["sell_premium"], bid - 1)
                    if volume == 100:
                        self.assertAlmostEqual(item["premium"], -0.005)
                    else:
                        self.assertIsNone(item["premium"])

    def test_book_premiums_require_the_corresponding_valid_side(self) -> None:
        con = make_connection()
        for bid_volume, ask_volume in ((4500, 0), (0, 7300), (0, 0)):
            with self.subTest(bid_volume=bid_volume, ask_volume=ask_volume):
                refresh(con, [snapshot(price=1.2, volume=100, bid_volume=bid_volume, ask_volume=ask_volume)])
                item = estimate_intraday(con, "160526")
                self.assertEqual(item["premium"], 0)
                if ask_volume:
                    self.assertAlmostEqual(item["buy_premium"], 1.209 / 1.2 - 1)
                else:
                    self.assertIsNone(item["buy_premium"])
                if bid_volume:
                    self.assertAlmostEqual(item["sell_premium"], 1.199 / 1.2 - 1)
                else:
                    self.assertIsNone(item["sell_premium"])

    def test_book_premiums_use_current_positive_estimate(self) -> None:
        con = make_connection()
        refresh(con, [snapshot()])
        for change in (0.1, -1, -2, float("nan"), float("inf")):
            with self.subTest(change=change), patch(
                "app.valuation.realtime_weighted_return",
                return_value=(change, 1, 1, [], []),
            ):
                item = estimate_intraday(con, "160526")
                if change == 0.1:
                    self.assertAlmostEqual(item["buy_premium"], 1.209 / 1.32 - 1)
                    self.assertAlmostEqual(item["sell_premium"], 1.199 / 1.32 - 1)
                else:
                    self.assertIsNone(item["buy_premium"])
                    self.assertIsNone(item["sell_premium"])
        con.execute("delete from navs")
        item = estimate_intraday(con, "160526")
        self.assertIsNotNone(item["order_book"])
        self.assertIsNone(item["buy_premium"])
        self.assertIsNone(item["sell_premium"])

    def test_smart_premium_selects_only_buy_discounts_or_sell_premiums(self) -> None:
        con = make_connection()
        con.execute("update navs set nav = 1 where fund_code = '160526'")
        for bid, ask, expected, status in (
            (1.01, 1.02, 0.01, "sell_premium"),
            (0.98, 0.99, -0.01, "buy_discount"),
            (0.99, 1.01, 0, "none"),
            (1, 1, 0, "none"),
            (0.99, 1, 0, "none"),
            (1, 1.01, 0, "none"),
        ):
            for price, volume in ((0, 0), (1.15, 100)):
                with self.subTest(bid=bid, ask=ask, price=price):
                    refresh(con, [snapshot(price=price, volume=volume, bid_price=bid, ask_price=ask)])
                    item = compact_fund_payload(estimate_intraday(con, "160526"))
                    self.assertEqual(item["smart_premium_status"], status)
                    self.assertAlmostEqual(item["smart_premium"], expected)
                    if volume:
                        self.assertAlmostEqual(item["premium"], 0.15)
                    else:
                        self.assertIsNone(item["premium"])

    def test_smart_premium_handles_one_sided_and_crossed_books(self) -> None:
        con = make_connection()
        con.execute("update navs set nav = 1 where fund_code = '160526'")
        for bid, ask, expected, status in (
            (1.01, None, 0.01, "sell_premium"),
            (None, 0.99, -0.01, "buy_discount"),
            (0.99, None, None, "book_incomplete"),
            (None, 1.01, None, "book_incomplete"),
            (1.02, 0.98, None, "book_invalid"),
            (None, None, None, "book_missing"),
        ):
            with self.subTest(bid=bid, ask=ask):
                refresh(con, [snapshot(price=1, volume=100, bid_price=bid, ask_price=ask)])
                item = estimate_intraday(con, "160526")
                self.assertEqual(item["smart_premium_status"], status)
                if expected is None:
                    self.assertIsNone(item["smart_premium"])
                else:
                    self.assertAlmostEqual(item["smart_premium"], expected)

    def test_smart_premium_keeps_unavailable_inputs_distinct_from_no_opportunity(self) -> None:
        con = make_connection()
        item = estimate_intraday(con, "160526")
        self.assertIsNone(item["smart_premium"])
        self.assertEqual(item["smart_premium_status"], "missing")
        refresh(con, [snapshot()])
        con.execute("delete from navs")
        item = estimate_intraday(con, "160526")
        self.assertIsNone(item["smart_premium"])
        self.assertEqual(item["smart_premium_status"], "estimate_missing")

    def test_partial_retries_preserve_unrelated_failure_and_status_api_agrees(self) -> None:
        con = make_connection()
        secids = ["0.160526", "0.161131", "0.161222"]
        refresh(con, [], secids)
        refresh(con, [snapshot()])
        expected_missing = secids[1:]
        self.assertEqual(get_meta(con, "last_realtime_quotes_fetch_missing_secids"), expected_missing)
        self.assertIsNone(get_meta(con, "last_realtime_quotes_refresh_success_at"))
        expected = quote_status_payload(con, secids)
        self.assertEqual(expected["last_realtime_quotes_missing_secids"], expected_missing)
        self.assertEqual(expected["last_realtime_quotes_no_trade_secids"], ["0.160526"])
        active_only = quote_status_payload(con, ["0.160526", "0.161834"])
        self.assertEqual(active_only["last_realtime_quotes_missing_secids"], ["0.161834"])

        @contextmanager
        def connection():
            yield con

        handler = object.__new__(Handler)
        handler.json = Mock()
        with patch("app.server.connect", connection), patch("app.server.collect_realtime_secids", return_value=secids):
            handler.handle_refresh_status()
        payload = handler.json.call_args.args[0]
        for key, value in expected.items():
            self.assertEqual(payload[key], value)
        refresh(con, [snapshot(secid="0.161131", symbol="161131")], ["0.161131"])
        self.assertEqual(get_meta(con, "last_realtime_quotes_fetch_missing_secids"), ["0.161222"])
        refresh(con, [snapshot(secid="0.161222", symbol="161222")], ["0.161222"])
        self.assertEqual(get_meta(con, "last_realtime_quotes_fetch_missing_secids"), [])
        self.assertIsNotNone(get_meta(con, "last_realtime_quotes_refresh_success_at"))

    def test_additive_migration_preserves_ready_database_and_backtests(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            con = sqlite3.connect(path)
            con.executescript(SCHEMA)
            for column in ("volume", "bid_price", "bid_volume", "ask_price", "ask_volume", "trade_status"):
                con.execute(f"alter table quotes drop column {column}")
            con.execute("insert into quotes(secid,symbol,market,name,price,updated_at) values ('0.160526','160526',0,'Test',1.2,'now')")
            con.execute("insert into backtests(fund_code,date,previous_date,previous_nav,actual_nav,estimated_nav,error_pct,covered_weight) values ('160526','2026-11-12','2026-11-11',1,1,1,0,1)")
            for key, value in (("config_hash", "expected"), ("build_state", "complete"), ("backtest_cache_version", 7), ("historical_price_cache_version", 3)):
                set_meta(con, key, value)
            con.commit()
            con.close()
            with patch("app.db.DB_PATH", path), patch("app.db.DATA_DIR", directory), patch("app.db.current_config_hash", return_value="expected"):
                init_db()
                init_db()
            con = sqlite3.connect(path)
            con.row_factory = sqlite3.Row
            self.assertEqual(schema_contract_issues(con), [])
            self.assertEqual(get_meta(con, "build_state"), "complete")
            self.assertIsNone(get_meta(con, "schema_repair_issues"))
            self.assertEqual(con.execute("select count(*) from backtests").fetchone()[0], 1)
            self.assertEqual(con.execute("select price from quotes").fetchone()[0], 1.2)
            con.close()


if __name__ == "__main__":
    unittest.main()
