from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.config import FundConfig
from app.db import SCHEMA
from app.server import collect_realtime_secids
from app.sources import utc_now
from app.valuation import estimate_intraday, prefetch_intraday_inputs


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def count_selects(con: sqlite3.Connection, operation):
    statements: list[str] = []
    con.set_trace_callback(statements.append)
    try:
        result = operation()
    finally:
        con.set_trace_callback(None)
    count = sum(
        statement.lstrip().lower().startswith(("select", "with"))
        for statement in statements
    )
    return result, count


class IntradayPrefetchTests(unittest.TestCase):
    def test_batch_prefetch_preserves_results_and_bounds_query_count(self) -> None:
        con = make_connection()
        codes = ["F1", "F2", "F3"]
        configs = {code: FundConfig(code, 0) for code in codes}
        now = utc_now()
        for index, code in enumerate(codes, start=1):
            con.execute(
                """
                insert into funds
                (code, name, exchange_market, fund_type, note, updated_at)
                values (?, ?, 0, '其他', ?, ?)
                """,
                (code, f"Fund {index}", f"note {index}", now),
            )
            con.execute(
                """
                insert into navs(fund_code, date, nav, distribution)
                values (?, '2026-07-08', ?, 0)
                """,
                (code, float(index)),
            )
            con.execute(
                """
                insert into holdings
                (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
                values (?, '2026-06-30', '2026-07-01', '0.ASSET',
                        'ASSET', 'Asset', 0.25, 'test')
                """,
                (code,),
            )
            con.execute(
                """
                insert into fund_announcements
                (fund_code, title, publish_date, announcement_id, url, updated_at)
                values (?, ?, '2026-07-01', ?, ?, ?)
                """,
                (code, f"Report {index}", f"A{index}", f"https://example/{index}", now),
            )
            con.execute(
                """
                insert into fund_purchase_limits
                (fund_code, purchase_status, redeem_status, display, updated_at)
                values (?, 'open', 'open', '开放申购', ?)
                """,
                (code, now),
            )
            con.execute(
                """
                insert into quotes
                (secid, symbol, market, name, price, pct, previous_close,
                 quote_time, session_date, last_attempt_at, last_success_at,
                 fetch_status, updated_at)
                values (?, ?, 0, ?, ?, 1, ?, '2026-07-10T07:00:00+00:00',
                        '2026-07-10', ?, ?, 'ok', ?)
                """,
                (
                    f"0.{code}",
                    code,
                    code,
                    1.01 * index,
                    1.0 * index,
                    now,
                    now,
                    now,
                ),
            )
        con.execute(
            """
            insert into quotes
            (secid, symbol, market, name, price, pct, previous_close,
             quote_time, session_date, last_attempt_at, last_success_at,
             fetch_status, updated_at)
            values ('0.ASSET', 'ASSET', 0, 'Asset', 101, 1, 100,
                    '2026-07-10T07:00:00+00:00', '2026-07-10',
                    ?, ?, 'ok', ?)
            """,
            (now, now, now),
        )
        con.execute(
            """
            insert into daily_prices
            (secid, date, close, source, adjustment)
            values ('0.ASSET', '2026-07-08', 100, 'test', 'raw')
            """
        )

        with (
            patch.dict("app.valuation.FUNDS", configs, clear=True),
            patch.dict("app.server.FUNDS", configs, clear=True),
        ):
            baseline, baseline_selects = count_selects(
                con, lambda: [estimate_intraday(con, code) for code in codes]
            )

            def batch_operation():
                prefetched = prefetch_intraday_inputs(con, codes)
                secids = collect_realtime_secids(con, prefetched)
                return (
                    [
                        estimate_intraday(con, code, prefetch=prefetched)
                        for code in codes
                    ],
                    secids,
                )

            (batched, secids), batched_selects = count_selects(con, batch_operation)

        self.assertEqual(batched, baseline)
        self.assertIn("0.ASSET", secids)
        self.assertIn("0.F1", secids)
        self.assertGreaterEqual(baseline_selects, 27)
        self.assertLessEqual(batched_selects, 7)
        self.assertLess(batched_selects, baseline_selects // 3)


if __name__ == "__main__":
    unittest.main()
