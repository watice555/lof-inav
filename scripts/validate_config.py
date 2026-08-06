from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES_PATH = ROOT / "config" / "fund_rules.json"
ALLOWED_MANUAL_HOLDINGS_MODES = {
    "overlay",
    "replace",
    "proxy_only",
    "proxy_then_manual_replace",
}
ALLOWED_PROXY_BASIS = {"non_cash_gap", "stock_gap"}
SECID_PATTERN = re.compile(r"^[A-Za-z0-9]+\.[A-Za-z0-9._=-]+$")
REPORT_DATE_PATTERN = re.compile(r"^20\d{2}-(03-31|06-30|09-30|12-31)$")
CODE_PATTERN = re.compile(r"^\d{6}$")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate LOF iNAV fund rule config.")
    parser.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_RULES_PATH),
        help="path to fund_rules.json",
    )
    args = parser.parse_args()

    path = Path(args.path)
    errors = validate_rules_file(path)
    if errors:
        print(f"{path}: {len(errors)} config error(s) found")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"{path}: config ok")
    return 0


def validate_rules_file(path: Path) -> list[str]:
    try:
        with path.open("r", encoding="utf-8") as file:
            rules = json.load(file)
    except OSError as exc:
        return [f"cannot read file: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"invalid json: {exc}"]
    return validate_rules(rules)


def validate_rules(rules: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(rules, dict):
        return ["top-level value must be an object"]
    funds = rules.get("funds")
    if not isinstance(funds, dict) or not funds:
        return ["funds must be a non-empty object"]
    for code, item in funds.items():
        errors.extend(validate_fund(code, item))
    return errors


def validate_fund(code: str, item: Any) -> list[str]:
    prefix = f"funds.{code}"
    errors: list[str] = []
    if not CODE_PATTERN.fullmatch(str(code)):
        errors.append(f"{prefix}: fund code must be 6 digits")
    if not isinstance(item, dict):
        return [f"{prefix}: fund config must be an object"]

    exchange_market = item.get("exchange_market")
    if (
        not isinstance(exchange_market, int)
        or isinstance(exchange_market, bool)
        or exchange_market < 0
    ):
        errors.append(f"{prefix}.exchange_market: required integer")

    fund_type = item.get("type") or item.get("fund_type")
    if not isinstance(fund_type, str) or not fund_type.strip():
        errors.append(f"{prefix}.type: required non-empty string")

    mode = item.get("manual_holdings_mode", "overlay")
    if mode not in ALLOWED_MANUAL_HOLDINGS_MODES:
        errors.append(f"{prefix}.manual_holdings_mode: unsupported value {mode!r}")

    proxy_basis = item.get("proxy_basis", "non_cash_gap")
    if proxy_basis not in ALLOWED_PROXY_BASIS:
        errors.append(f"{prefix}.proxy_basis: unsupported value {proxy_basis!r}")

    proxy_weight = item.get("proxy_weight")
    if proxy_weight is not None and not is_number(proxy_weight):
        errors.append(f"{prefix}.proxy_weight: must be numeric when present")
    elif is_number(proxy_weight) and proxy_weight < 0:
        errors.append(f"{prefix}.proxy_weight: must not be negative")

    proxy_secids = item.get("proxy_secids", [])
    if proxy_secids is None:
        proxy_secids = []
    if not isinstance(proxy_secids, list):
        errors.append(f"{prefix}.proxy_secids: must be a list")
    else:
        for index, secid in enumerate(proxy_secids):
            if not is_secid(secid):
                errors.append(f"{prefix}.proxy_secids[{index}]: invalid secid {secid!r}")

    manual_holdings = item.get("manual_holdings", [])
    if manual_holdings is None:
        manual_holdings = []
    if not isinstance(manual_holdings, list):
        errors.append(f"{prefix}.manual_holdings: must be a list")
    else:
        report_dates = []
        for index, period in enumerate(manual_holdings):
            errors.extend(validate_manual_period(f"{prefix}.manual_holdings[{index}]", period))
            if isinstance(period, dict) and isinstance(period.get("report_date"), str):
                report_dates.append(period["report_date"])
        duplicates = sorted(
            report_date
            for report_date in set(report_dates)
            if report_dates.count(report_date) > 1
        )
        if duplicates:
            errors.append(
                f"{prefix}.manual_holdings: duplicate report dates {', '.join(duplicates)}"
            )
    if mode == "replace" and not manual_holdings:
        errors.append(f"{prefix}.manual_holdings: replace mode requires manual holdings")
    if mode == "proxy_only" and not proxy_secids:
        errors.append(f"{prefix}.proxy_secids: proxy_only mode requires at least one secid")
    if mode == "proxy_then_manual_replace":
        if not proxy_secids:
            errors.append(
                f"{prefix}.proxy_secids: proxy_then_manual_replace mode requires at least one secid"
            )
        if not manual_holdings:
            errors.append(
                f"{prefix}.manual_holdings: proxy_then_manual_replace mode requires manual holdings"
            )
    return errors


def validate_manual_period(prefix: str, period: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(period, dict):
        return [f"{prefix}: period must be an object"]
    report_date = period.get("report_date")
    if not isinstance(report_date, str) or not REPORT_DATE_PATTERN.fullmatch(report_date):
        errors.append(f"{prefix}.report_date: must be a quarter-end date")
    publish_date = period.get("publish_date")
    if publish_date is not None:
        if not isinstance(publish_date, str):
            errors.append(f"{prefix}.publish_date: must be an ISO date when present")
        else:
            try:
                publish_day = date.fromisoformat(publish_date)
            except ValueError:
                errors.append(f"{prefix}.publish_date: must be an ISO date when present")
            else:
                if isinstance(report_date, str) and REPORT_DATE_PATTERN.fullmatch(report_date):
                    if publish_day < date.fromisoformat(report_date):
                        errors.append(
                            f"{prefix}.publish_date: must not be earlier than report_date"
                        )
    holdings = period.get("holdings")
    if not isinstance(holdings, list) or not holdings:
        errors.append(f"{prefix}.holdings: must be a non-empty list")
        return errors
    total_weight = 0.0
    for index, holding in enumerate(holdings):
        errors.extend(validate_manual_holding(f"{prefix}.holdings[{index}]", holding))
        if isinstance(holding, dict) and is_number(holding.get("weight")):
            total_weight += float(holding["weight"])
    if total_weight > 1.5:
        errors.append(f"{prefix}.holdings: total weight {total_weight:.4f} looks too high")
    return errors


def validate_manual_holding(prefix: str, holding: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(holding, dict):
        return [f"{prefix}: holding must be an object"]
    if not is_secid(holding.get("secid")):
        errors.append(f"{prefix}.secid: invalid or missing secid")
    for key in ("name", "source"):
        value = holding.get(key)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.{key}: required non-empty string")
    weight = holding.get("weight")
    if not is_number(weight) or weight <= 0:
        errors.append(f"{prefix}.weight: required positive number")
    symbol = holding.get("symbol")
    if symbol is not None and not isinstance(symbol, str):
        errors.append(f"{prefix}.symbol: must be a string when present")
    return errors


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_secid(value: Any) -> bool:
    return isinstance(value, str) and bool(SECID_PATTERN.fullmatch(value))


if __name__ == "__main__":
    raise SystemExit(main())
