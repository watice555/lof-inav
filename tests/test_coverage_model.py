from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from app.config import FundConfig
from app.db import SCHEMA
from app.server import collect_data_alerts
from app.sources import utc_now
from app.valuation import calculate_backtest_row, estimate_intraday, realtime_weighted_return


def make_connection() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def add_holding(con: sqlite3.Connection, code: str, secid: str, weight: float) -> None:
    con.execute(
        """
        insert into holdings
        (fund_code, report_date, publish_date, secid, symbol, name, weight, source)
        values (?, '2026-06-30', '2026-07-01', ?, ?, ?, ?, 'test')
        """,
        (code, secid, secid.split(".", 1)[1], secid, weight),
    )


def add_quote(con: sqlite3.Connection, secid: str, session_date: str = "2026-07-10") -> None:
    market, symbol = secid.split(".", 1)
    now = utc_now()
    con.execute(
        """
        insert into quotes
        (secid, symbol, market, name, price, pct, previous_close, quote_time,
         session_date, last_attempt_at, last_success_at, fetch_status, updated_at)
        values (?, ?, ?, ?, 101, 1, 100, ?, ?, ?, ?, 'ok', ?)
        """,
        (
            secid,
            symbol,
            int(market),
            symbol,
            f"{session_date}T07:00:00+00:00",
            session_date,
            now,
            now,
            now,
        ),
    )


class CoverageModelTests(unittest.TestCase):
    def test_missing_fx_removes_holding_from_priced_weight(self) -> None:
        con = make_connection()
        add_holding(con, "TEST", "105.NVDA", 0.1)
        add_quote(con, "105.NVDA")
        holdings = con.execute("select * from holdings").fetchall()

        weighted, modeled, priced, missing, warnings = realtime_weighted_return(
            con, holdings, base_date=None
        )

        self.assertEqual(weighted, 0.0)
        self.assertAlmostEqual(modeled, 0.1)
        self.assertEqual(priced, 0.0)
        self.assertEqual(missing, [])
        fx_warning = next(item for item in warnings if item["kind"] == "fx")
        self.assertEqual(fx_warning["type"], "fx_quote_missing")

    def test_low_modeled_weight_with_complete_prices_is_soft_context_only(self) -> None:
        con = make_connection()
        con.execute(
            "insert into funds(code, name, exchange_market, fund_type, note, updated_at) values ('TEST', 'Test', 0, '其他', '', 'now')"
        )
        con.execute(
            "insert into navs(fund_code, date, nav, distribution) values ('TEST', '2026-07-10', 1, 0)"
        )
        add_holding(con, "TEST", "0.ASSET", 0.1)
        add_quote(con, "0.ASSET")

        with patch.dict("app.valuation.FUNDS", {"TEST": FundConfig("TEST", 0)}):
            result = estimate_intraday(con, "TEST")
        result["status"] = "ok"

        self.assertAlmostEqual(result["modeled_weight"], 0.1)
        self.assertAlmostEqual(result["priced_weight"], 0.1)
        self.assertAlmostEqual(result["priced_ratio"], 1.0)
        self.assertAlmostEqual(result["unmodeled_weight"], 0.9)
        self.assertEqual(
            collect_data_alerts(con, [result], include_backtests=False),
            [],
        )

    def test_backtest_quality_uses_priced_ratio_not_absolute_stock_weight(self) -> None:
        con = make_connection()
        add_holding(con, "TEST", "0.ASSET", 0.1)
        con.executemany(
            "insert into navs(fund_code, date, nav, distribution) values ('TEST', ?, ?, 0)",
            [("2026-07-09", 1.0), ("2026-07-10", 1.01)],
        )
        navs = con.execute("select * from navs order by date").fetchall()

        row = calculate_backtest_row(
            con,
            "TEST",
            navs[0],
            navs[1],
            {"0.ASSET": [("2026-07-09", 100), ("2026-07-10", 101)]},
            25,
        )

        self.assertAlmostEqual(row["modeled_weight"], 0.1)
        self.assertAlmostEqual(row["covered_weight"], 0.1)
        self.assertAlmostEqual(row["priced_ratio"], 1.0)
        self.assertEqual(row["data_quality"], "ok")


if __name__ == "__main__":
    unittest.main()
