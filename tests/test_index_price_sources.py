from __future__ import annotations

import io
import unittest
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.config import FUNDS, YAHOO_PRICE_SYMBOLS
from app.sources import (
    CSINDEX_INDEX_SECIDS,
    csindex_daily_prices,
    csindex_realtime_quote,
    fetch_daily_prices,
    hang_seng_index_daily_prices,
    hang_seng_index_realtime_quote,
    special_realtime_quote,
)


def make_csindex_workbook(rows: list[tuple[str, str, str]]) -> bytes:
    sheet_rows = [
        '<row r="1"><c r="A1" t="inlineStr"><is><t>Date</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>Index Code</t></is></c>'
        '<c r="J1" t="inlineStr"><is><t>Close</t></is></c>'
        '<c r="L1" t="inlineStr"><is><t>Change(%)</t></is></c></row>'
    ]
    for index, (day, close, pct) in enumerate(rows, start=2):
        sheet_rows.append(
            f'<row r="{index}">'
            f'<c r="A{index}" t="inlineStr"><is><t>{day}</t></is></c>'
            f'<c r="B{index}" t="inlineStr"><is><t>930720</t></is></c>'
            f'<c r="J{index}" t="inlineStr"><is><t>{close}</t></is></c>'
            f'<c r="L{index}" t="inlineStr"><is><t>{pct}</t></is></c>'
            "</row>"
        )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as workbook:
        workbook.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class IndexPriceSourceTests(unittest.TestCase):
    def test_csindex_official_intraday_quote_is_normalized(self) -> None:
        document = {
            "data": {
                "intraDayHeader": {
                    "indexCode": "000808",
                    "tradeDate": "2026-07-10",
                    "tradeTime": "16:29:54",
                    "current": 7969.16,
                    "closePre": 7802.2,
                    "changePct": 2.14,
                },
                "intraDayPerfList": [
                    {"indexCode": "000808", "indexName": "医药生物"}
                ],
            }
        }
        with (
            patch("app.sources.CSINDEX_REQUEST_INTERVAL_SECONDS", 0),
            patch(
                "app.sources._get",
                return_value=SimpleNamespace(json=lambda: document),
            ) as get,
        ):
            quote = csindex_realtime_quote("1.000808")

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote["price"], 7969.16)
        self.assertEqual(quote["previous_close"], 7802.2)
        self.assertEqual(quote["pct"], 2.14)
        self.assertEqual(quote["name"], "医药生物")
        self.assertEqual(quote["quote_time"], "2026-07-10T08:29:54+00:00")
        self.assertEqual(get.call_args.kwargs["params"], {"indexCode": "000808"})

    def test_all_csi_proxy_indices_use_the_official_realtime_path(self) -> None:
        expected = {
            "secid": "1.000961",
            "symbol": "000961",
            "market": 1,
            "name": "中证上游资源产业指数",
            "price": 4832.14,
            "pct": -0.57,
            "previous_close": 4860.13,
            "quote_time": "2026-07-10T08:29:54+00:00",
        }
        self.assertIn("1.000961", CSINDEX_INDEX_SECIDS)
        self.assertIn("1.000998", CSINDEX_INDEX_SECIDS)
        with (
            patch("app.sources.csindex_realtime_quote", return_value=expected) as official,
            patch("app.sources.eastmoney_index_quote") as fallback,
        ):
            quote = special_realtime_quote("1.000961")

        self.assertEqual(quote, expected)
        official.assert_called_once_with("1.000961")
        fallback.assert_not_called()

    def test_special_realtime_fallback_records_official_source_failure(self) -> None:
        diagnostics = []
        expected = {
            "secid": "1.000808",
            "price": 7969.16,
        }
        with (
            patch(
                "app.sources.csindex_realtime_quote",
                side_effect=ValueError("injected malformed response"),
            ),
            patch("app.sources.eastmoney_index_quote", return_value=expected),
        ):
            quote = special_realtime_quote("1.000808", diagnostics)

        self.assertEqual(quote, expected)
        self.assertEqual(diagnostics[0]["source"], "csindex")
        self.assertEqual(diagnostics[0]["secids"], ["1.000808"])
        self.assertIn("ValueError: injected malformed response", diagnostics[0]["error"])

    def test_csindex_export_is_padded_clamped_and_filtered(self) -> None:
        workbook = make_csindex_workbook(
            [
                ("20260617", "1900", "0.1"),
                ("20260701", "2090.53", "3.06"),
                ("20260710", "2145.31", "1.2"),
            ]
        )

        with (
            patch("app.sources.app_today", return_value=date(2026, 7, 10)),
            patch("app.sources.CSINDEX_REQUEST_INTERVAL_SECONDS", 0),
            patch(
                "app.sources._post",
                return_value=SimpleNamespace(content=workbook),
            ) as post,
        ):
            rows = csindex_daily_prices(
                "2.930720",
                begin="20260701",
                end="20500101",
            )

        self.assertEqual(
            rows,
            [
                {
                    "date": "2026-07-01",
                    "close": 2090.53,
                    "pct": 3.06,
                    "source": "csindex",
                    "adjustment": "raw",
                },
                {
                    "date": "2026-07-10",
                    "close": 2145.31,
                    "pct": 1.2,
                    "source": "csindex",
                    "adjustment": "raw",
                },
            ],
        )
        self.assertEqual(
            post.call_args.kwargs["json"],
            [
                {
                    "startDate": "20260617",
                    "endDate": "20260710",
                    "indexCode": "930720",
                }
            ],
        )

    def test_csindex_is_preferred_for_supported_indices(self) -> None:
        expected = [
            {
                "date": "2026-07-10",
                "close": 2145.31,
                "pct": 1.2,
                "source": "csindex",
                "adjustment": "raw",
            }
        ]

        with (
            patch("app.sources.csindex_daily_prices", return_value=expected) as csindex,
            patch("app.sources.eastmoney_index_daily_prices") as eastmoney,
        ):
            rows = fetch_daily_prices(
                "2.930720",
                begin="20260710",
                end="20260710",
            )

        self.assertEqual(rows, expected)
        csindex.assert_called_once_with("2.930720", "20260710", "20260710")
        eastmoney.assert_not_called()

    def test_hang_seng_rebased_chart_is_scaled_and_uses_hong_kong_dates(self) -> None:
        points = [
            [
                int(datetime(2026, 6, 29, 16, tzinfo=timezone.utc).timestamp() * 1000),
                100.0,
            ],
            [
                int(datetime(2026, 6, 30, 16, tzinfo=timezone.utc).timestamp() * 1000),
                105.0,
            ],
            [
                int(datetime(2026, 7, 1, 16, tzinfo=timezone.utc).timestamp() * 1000),
                110.0,
            ],
        ]
        document = {
            "indexSeriesList": [
                {
                    "indexList": [
                        {
                            "indexCode": "OTHER",
                            "subIndexList": [
                                {
                                    "indexCode": "00016.00",
                                    "indexName": "Hang Seng Composite SmallCap Index",
                                    "previousClose": "220",
                                    "lastUpdate": "2026-07-02 00:00:00",
                                    "indexLevels-5y": points,
                                    "subIndexList": [],
                                }
                            ],
                        }
                    ]
                }
            ]
        }

        with (
            patch("app.sources.app_today", return_value=date(2026, 7, 3)),
            patch(
                "app.sources._get",
                return_value=SimpleNamespace(json=lambda: document),
            ),
        ):
            rows = hang_seng_index_daily_prices(
                "124.HSSI",
                begin="20260701",
                end="20260702",
            )

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["date"], "2026-07-01")
        self.assertAlmostEqual(rows[0]["close"], 210.0)
        self.assertAlmostEqual(rows[0]["pct"], 5.0)
        self.assertEqual(rows[1]["date"], "2026-07-02")
        self.assertAlmostEqual(rows[1]["close"], 220.0)
        self.assertAlmostEqual(rows[1]["pct"], (110 / 105 - 1) * 100)
        self.assertEqual(rows[1]["source"], "hang_seng_indexes_chart")
        self.assertEqual(rows[1]["adjustment"], "rebased_scaled")

    def test_hang_seng_performance_is_an_exact_realtime_quote(self) -> None:
        document = {
            "indexSeriesList": [
                {
                    "indexList": [
                        {
                            "indexCode": "02083.00",
                            "indexName": "Hang Seng TECH Index",
                            "indexValue": "4721.66",
                            "previousClose": "4731.56",
                            "lastUpdate": "2026-07-10 16:09:04",
                            "subIndexList": [],
                        }
                    ]
                }
            ]
        }

        with patch(
            "app.sources._get",
            return_value=SimpleNamespace(json=lambda: document),
        ):
            quote = hang_seng_index_realtime_quote("124.HSTECH")

        self.assertIsNotNone(quote)
        assert quote is not None
        self.assertEqual(quote["price"], 4721.66)
        self.assertEqual(quote["previous_close"], 4731.56)
        self.assertAlmostEqual(
            quote["pct"],
            (4721.66 / 4731.56 - 1) * 100,
        )
        self.assertEqual(quote["quote_time"], "2026-07-10T08:09:04+00:00")

    def test_hang_seng_index_does_not_use_an_etf_as_yahoo_history(self) -> None:
        expected = [
            {
                "date": "2026-07-10",
                "close": 4721.66,
                "pct": -0.21,
                "source": "hang_seng_indexes_chart",
                "adjustment": "rebased_scaled",
            }
        ]

        with (
            patch(
                "app.sources.hang_seng_index_daily_prices",
                return_value=expected,
            ) as hang_seng,
            patch("app.sources.eastmoney_index_daily_prices") as eastmoney,
        ):
            rows = fetch_daily_prices(
                "124.HSTECH",
                begin="20260710",
                end="20260710",
            )

        self.assertEqual(rows, expected)
        hang_seng.assert_called_once_with("124.HSTECH", "20260710", "20260710")
        eastmoney.assert_not_called()
        self.assertNotIn("124.HSTECH", YAHOO_PRICE_SYMBOLS)


class FundEtfBasketTests(unittest.TestCase):
    def test_160216_q2_uses_disclosed_top_ten_basket(self) -> None:
        q2 = next(
            period
            for period in FUNDS["160216"].manual_holdings
            if period["report_date"] == "2026-06-30"
        )
        expected = {
            "IAU": 0.1611,
            "GLD": 0.107,
            "DBB": 0.1023,
            "UGL": 0.1002,
            "DBA": 0.054,
            "XOP": 0.0502,
            "ZSL": 0.041,
            "TMF": 0.0322,
            "SCO": 0.0235,
            "OIH": 0.0213,
        }

        self.assertEqual(
            {holding["symbol"]: holding["weight"] for holding in q2["holdings"]},
            expected,
        )
        self.assertAlmostEqual(sum(expected.values()), 0.6928)

    def test_160216_q2_etfs_have_yahoo_fallback_symbols(self) -> None:
        symbols = {
            "IAU",
            "GLD",
            "DBB",
            "UGL",
            "DBA",
            "XOP",
            "ZSL",
            "TMF",
            "SCO",
            "OIH",
        }

        self.assertEqual(
            {
                f"107.{symbol}": YAHOO_PRICE_SYMBOLS.get(f"107.{symbol}")
                for symbol in symbols
            },
            {f"107.{symbol}": symbol for symbol in symbols},
        )


if __name__ == "__main__":
    unittest.main()
