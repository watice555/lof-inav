from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.build import proxy_name
from app.config import EASTMONEY_HEADERS, load_funds
from app.sources import fetch_daily_prices, fetch_realtime_quotes


GENERIC_NAME_CHARS = set("中证国证申万指数行业主题代理等权全指")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit configured LOF proxy secids against quote names.")
    parser.add_argument("--codes", nargs="*", help="fund codes to audit; default audits all configured funds")
    parser.add_argument("--include-manual", action="store_true", help="also audit secids in manual_holdings")
    parser.add_argument("--cn-index-only", action="store_true", help="only audit mainland China index-style secids")
    parser.add_argument("--suggest-alt", action="store_true", help="query alternate 399xxx/000xxx prefix candidates")
    parser.add_argument("--check-daily", action="store_true", help="also check recent daily price availability")
    parser.add_argument("--daily-days", type=int, default=30, help="calendar days for daily price check")
    args = parser.parse_args()

    funds = load_funds()
    selected = set(args.codes or funds)
    targets = []
    for code, cfg in funds.items():
        if code not in selected:
            continue
        for secid in cfg.proxy_secids:
            if args.cn_index_only and not is_cn_index_proxy(secid):
                continue
            targets.append((code, secid, "proxy_secids"))
        if args.include_manual:
            for manual in cfg.manual_holdings:
                for holding in manual.get("holdings") or []:
                    secid = holding.get("secid")
                    if secid and (not args.cn_index_only or is_cn_index_proxy(secid)):
                        targets.append((code, secid, "manual_holdings"))

    unique_secids = sorted({secid for _, secid, _ in targets})
    quote_by_secid = fetch_audit_quotes(unique_secids)

    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=[
            "status",
            "fund_code",
            "source",
            "secid",
            "configured_name",
            "quote_name",
            "suggested_secid",
            "suggested_name",
            "daily_rows",
            "reason",
        ],
    )
    writer.writeheader()

    begin = (date.today() - timedelta(days=args.daily_days)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    alt_cache: dict[str, dict | None] = {}

    for code, secid, source in targets:
        configured_name = proxy_name(secid)
        quote = quote_by_secid.get(secid)
        quote_name = quote["name"] if quote else ""
        status = "OK"
        reason = ""
        suggested_secid = ""
        suggested_name = ""

        if not quote:
            status = "MISSING_QUOTE"
            reason = "no realtime quote returned"
        elif names_conflict(configured_name, quote_name):
            status = "NAME_MISMATCH"
            reason = "configured display name and quote name have no specific overlap"

        if status != "OK":
            alt = alternate_cn_index_secid(secid)
            if alt:
                if alt in quote_by_secid:
                    alt_cache[alt] = quote_by_secid[alt]
                elif args.suggest_alt and alt not in alt_cache:
                    alt_quote = fetch_realtime_quotes([alt])
                    alt_cache[alt] = alt_quote[0] if alt_quote else None
                alt_quote = alt_cache.get(alt)
                if alt_quote:
                    alt_name = alt_quote["name"]
                    if similarity(configured_name, alt_name) > similarity(configured_name, quote_name):
                        suggested_secid = alt
                        suggested_name = alt_name
                        reason = f"{reason}; alternate prefix is a closer quote-name match"

        daily_rows = ""
        if args.check_daily:
            rows = fetch_daily_prices(secid, begin=begin, end=end)
            daily_rows = str(len(rows))
            if not rows:
                status = "DAILY_MISSING" if status == "OK" else f"{status}+DAILY_MISSING"
                reason = f"{reason}; no recent daily prices".strip("; ")

        writer.writerow(
            {
                "status": status,
                "fund_code": code,
                "source": source,
                "secid": secid,
                "configured_name": configured_name,
                "quote_name": quote_name,
                "suggested_secid": suggested_secid,
                "suggested_name": suggested_name,
                "daily_rows": daily_rows,
                "reason": reason,
            }
        )


def alternate_cn_index_secid(secid: str) -> str | None:
    market, symbol = split_secid(secid)
    if market == "0" and re.fullmatch(r"399\d{3}", symbol):
        return f"1.000{symbol[-3:]}"
    if market == "1" and re.fullmatch(r"000\d{3}", symbol):
        return f"0.399{symbol[-3:]}"
    return None


def fetch_audit_quotes(secids: list[str]) -> dict[str, dict]:
    rows: list[dict] = []
    for batch in chunks(secids, 80):
        try:
            response = requests.get(
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                params={
                    "fltt": "2",
                    "invt": "2",
                    "fields": "f12,f13,f14",
                    "secids": ",".join(batch),
                },
                headers=EASTMONEY_HEADERS,
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue
        for item in (payload.get("data") or {}).get("diff") or []:
            rows.append(
                {
                    "secid": f"{item['f13']}.{item['f12']}",
                    "symbol": item["f12"],
                    "market": int(item["f13"]),
                    "name": item.get("f14") or item["f12"],
                }
            )
    quote_by_secid = {row["secid"]: row for row in rows}
    missing_cn = [secid for secid in secids if secid not in quote_by_secid and is_cn_sina_symbol(secid)]
    quote_by_secid.update(fetch_sina_quote_names(missing_cn))
    return quote_by_secid


def fetch_sina_quote_names(secids: list[str]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    symbol_to_secid = {sina_symbol(secid): secid for secid in secids}
    for batch in chunks(list(symbol_to_secid), 80):
        try:
            response = requests.get(
                "https://hq.sinajs.cn/list=" + ",".join(batch),
                headers={"Referer": "https://finance.sina.com.cn/", "User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            response.raise_for_status()
        except Exception:
            continue
        for symbol, raw in re.findall(r'var hq_str_([a-z]{2}\d{6})="([^"]*)";', response.text):
            secid = symbol_to_secid.get(symbol)
            if not secid or not raw:
                continue
            name = raw.split(",", 1)[0]
            if not name:
                continue
            market, code = split_secid(secid)
            result[secid] = {"secid": secid, "symbol": code, "market": int(market), "name": name}
    return result


def is_cn_sina_symbol(secid: str) -> bool:
    market, symbol = split_secid(secid)
    return market in {"0", "1"} and re.fullmatch(r"\d{6}", symbol) is not None


def sina_symbol(secid: str) -> str:
    market, symbol = split_secid(secid)
    prefix = "sz" if market == "0" else "sh"
    return f"{prefix}{symbol}"


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def is_cn_index_proxy(secid: str) -> bool:
    market, symbol = split_secid(secid)
    if market == "0" and re.fullmatch(r"399\d{3}", symbol):
        return True
    if market == "1" and re.fullmatch(r"000\d{3}", symbol):
        return True
    if market == "2" and re.fullmatch(r"93[01]\d{3}", symbol):
        return True
    return False


def names_conflict(configured_name: str, quote_name: str) -> bool:
    configured = significant_chars(configured_name)
    quote = significant_chars(quote_name)
    if not configured or not quote:
        return False
    return not configured.intersection(quote)


def similarity(left: str, right: str) -> int:
    return len(significant_chars(left).intersection(significant_chars(right)))


def significant_chars(value: str) -> set[str]:
    chars = set(chinese_chars(value))
    return {char for char in chars if char not in GENERIC_NAME_CHARS}


def chinese_chars(value: str) -> Iterable[str]:
    return re.findall(r"[\u4e00-\u9fff]", value)


def split_secid(secid: str) -> tuple[str, str]:
    parts = secid.split(".", 1)
    if len(parts) != 2:
        return "", secid
    return parts[0], parts[1]


if __name__ == "__main__":
    main()
