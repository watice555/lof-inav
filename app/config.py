from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = os.environ.get("LOF_INAV_DATA_DIR", str(ROOT / "data"))
DB_PATH = os.environ.get("LOF_INAV_DB_PATH", str(Path(DATA_DIR) / "lof_inav.sqlite3"))
FUND_RULES_PATH = Path(
    os.environ.get("LOF_INAV_FUND_RULES_PATH", str(ROOT / "config" / "fund_rules.json"))
)


@dataclass(frozen=True)
class FundConfig:
    code: str
    exchange_market: int
    fund_type: str = "其他"
    proxy_secids: tuple[str, ...] = ()
    proxy_weight: float | None = None
    proxy_basis: str = "non_cash_gap"
    note: str = ""
    manual_holdings: tuple[dict[str, Any], ...] = ()
    manual_holdings_mode: str = "overlay"


def load_fund_rules(path: Path = FUND_RULES_PATH) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def load_funds(path: Path = FUND_RULES_PATH) -> dict[str, FundConfig]:
    rules = load_fund_rules(path)
    funds: dict[str, FundConfig] = {}
    for code, item in rules.get("funds", {}).items():
        funds[code] = FundConfig(
            code=code,
            exchange_market=int(item["exchange_market"]),
            fund_type=item.get("type") or item.get("fund_type", "其他"),
            proxy_secids=tuple(item.get("proxy_secids") or ()),
            proxy_weight=item.get("proxy_weight"),
            proxy_basis=item.get("proxy_basis", "non_cash_gap"),
            note=item.get("note", ""),
            manual_holdings=tuple(item.get("manual_holdings") or ()),
            manual_holdings_mode=item.get("manual_holdings_mode", "overlay"),
        )
    return funds


FUNDS: dict[str, FundConfig] = load_funds()


EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://fund.eastmoney.com/",
}


FX_MIDPOINT_SECIDS = {
    100: "120.USDCNYC",
    101: "120.USDCNYC",
    102: "120.USDCNYC",
    105: "120.USDCNYC",
    106: "120.USDCNYC",
    107: "120.USDCNYC",
    112: "120.USDCNYC",
    116: "120.HKDCNYC",
    124: "120.HKDCNYC",
    122: "120.USDCNYC",
}


FX_SECID_OVERRIDES = {
    "100.HSI": "120.HKDCNYC",
    "100.HSCEI": "120.HKDCNYC",
}


US_EQUITY_CLOSE_MARKS = {
    "102.CL00Y": "CL=F",
    "112.B00Y": "BZ=F",
    "122.XAU": "IAU",
    "107.SLV": "SLV",
}


YAHOO_PRICE_SYMBOLS = {
    "100.HSI": "^HSI",
    "100.HSCEI": "^HSCE",
    "100.NDX100": "^NDX",
    "100.SOX": "^SOX",
    "100.SPX": "^GSPC",
    "124.HSTECH": "3033.HK",
    "0.159995": "159995.SZ",
    "102.CL00Y": "CL=F",
    "105.ASML": "ASML",
    "105.GOOGL": "GOOGL",
    "105.SNDK": "SNDK",
    "106.TSM": "TSM",
    "112.B00Y": "BZ=F",
    "120.HKDCNYC": "HKDCNY=X",
    "120.USDCNYC": "USDCNY=X",
    "122.XAU": "GC=F",
    "105.AAPL": "AAPL",
    "105.AMAT": "AMAT",
    "105.AMD": "AMD",
    "105.ATHM": "ATHM",
    "105.AVGO": "AVGO",
    "105.BZ": "BZ",
    "105.CSCO": "CSCO",
    "105.JOYY": "JOYY",
    "105.LRCX": "LRCX",
    "105.MSFT": "MSFT",
    "105.MU": "MU",
    "105.NVDA": "NVDA",
    "105.PDD": "PDD",
    "105.PLTR": "PLTR",
    "105.QFIN": "QFIN",
    "105.TAL": "TAL",
    "105.TME": "TME",
    "105.VIPS": "VIPS",
    "105.WB": "WB",
    "105.YMM": "YMM",
    "107.AIQ": "AIQ",
    "107.ARKG": "ARKG",
    "107.ARKK": "ARKK",
    "107.ARKQ": "ARKQ",
    "107.AGG": "AGG",
    "107.EPI": "EPI",
    "107.BNDX": "BNDX",
    "107.BOTZ": "BOTZ",
    "107.CPER": "CPER",
    "107.DBA": "DBA",
    "107.DBC": "DBC",
    "107.EWH": "EWH",
    "107.FINX": "FINX",
    "107.GLIN": "GLIN",
    "107.INCO": "INCO",
    "107.INDA": "INDA",
    "107.INDY": "INDY",
    "107.IYE": "IYE",
    "107.IXC": "IXC",
    "107.KWEB": "KWEB",
    "107.MCHI": "MCHI",
    "107.NFTY": "NFTY",
    "107.PIN": "PIN",
    "107.QQQ": "QQQ",
    "107.RSPH": "RSPH",
    "107.SLV": "SLV",
    "107.SMH": "SMH",
    "107.SOXX": "SOXX",
    "107.SMIN": "SMIN",
    "107.VDE": "VDE",
    "107.VNQ": "VNQ",
    "107.XBI": "XBI",
    "107.XLE": "XLE",
    "107.XLK": "XLK",
    "107.XLY": "XLY",
    "107.XOP": "XOP",
    "107.CNYB": "CNYB",
}


SINA_PRICE_SYMBOLS = {
    "100.HSI": "rt_hkHSI",
    "100.HSCEI": "rt_hkHSCEI",
    "102.CL00Y": "hf_CL",
    "105.ASML": "gb_asml",
    "105.GOOGL": "gb_googl",
    "105.SNDK": "gb_sndk",
    "106.TSM": "gb_tsm",
    "112.B00Y": "hf_OIL",
    "120.HKDCNYC": "fx_shkdcny",
    "120.USDCNYC": "fx_susdcny",
    "122.XAU": "hf_XAU",
    "124.HSTECH": "rt_hkHSTECH",
}
