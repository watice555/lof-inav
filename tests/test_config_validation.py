from __future__ import annotations

import unittest

from scripts.validate_config import validate_rules


def valid_fund(**updates) -> dict:
    fund = {
        "exchange_market": 0,
        "type": "测试",
        "manual_holdings_mode": "replace",
        "manual_holdings": [
            {
                "report_date": "2026-03-31",
                "publish_date": "2026-04-22",
                "holdings": [
                    {
                        "secid": "0.000001",
                        "name": "测试资产",
                        "weight": 0.1,
                        "source": "test",
                    }
                ],
            }
        ],
    }
    fund.update(updates)
    return fund


class ConfigValidationTests(unittest.TestCase):
    def validate(self, fund: dict) -> list[str]:
        return validate_rules({"funds": {"000001": fund}})

    def test_valid_manual_config_passes(self) -> None:
        self.assertEqual(self.validate(valid_fund()), [])

    def test_boolean_market_is_not_accepted_as_integer(self) -> None:
        errors = self.validate(valid_fund(exchange_market=True))
        self.assertTrue(any("exchange_market" in error for error in errors))

    def test_publish_date_must_be_valid_and_after_report_date(self) -> None:
        invalid_date = valid_fund()
        invalid_date["manual_holdings"][0]["publish_date"] = "2026-02-30"
        early_date = valid_fund()
        early_date["manual_holdings"][0]["publish_date"] = "2026-03-01"

        self.assertTrue(any("ISO date" in error for error in self.validate(invalid_date)))
        self.assertTrue(any("earlier" in error for error in self.validate(early_date)))

    def test_modes_require_the_data_they_consume(self) -> None:
        empty_replace = valid_fund(manual_holdings=[])
        empty_proxy = valid_fund(
            manual_holdings_mode="proxy_only",
            manual_holdings=[],
            proxy_secids=[],
        )

        self.assertTrue(any("replace mode" in error for error in self.validate(empty_replace)))
        self.assertTrue(any("proxy_only mode" in error for error in self.validate(empty_proxy)))

    def test_duplicate_manual_report_dates_are_rejected(self) -> None:
        fund = valid_fund()
        fund["manual_holdings"].append(dict(fund["manual_holdings"][0]))

        self.assertTrue(any("duplicate report dates" in error for error in self.validate(fund)))


if __name__ == "__main__":
    unittest.main()
