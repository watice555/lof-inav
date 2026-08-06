from __future__ import annotations

import unittest
from datetime import date

from app.market_calendar import (
    calendar_year_is_known,
    is_trading_session,
    previous_trading_session,
)


class MarketCalendarTests(unittest.TestCase):
    def test_official_cn_calendars_cover_cached_history(self) -> None:
        self.assertFalse(is_trading_session("CN", date(2024, 2, 9)))
        self.assertFalse(is_trading_session("CN", date(2025, 2, 3)))
        self.assertEqual(
            previous_trading_session("CN", date(2025, 2, 5)),
            date(2025, 1, 27),
        )
        self.assertTrue(calendar_year_is_known("CN", 2024))
        self.assertTrue(calendar_year_is_known("CN", 2026))
        self.assertFalse(calendar_year_is_known("CN", 2027))

    def test_official_hk_calendars_include_2024_through_2027(self) -> None:
        self.assertFalse(is_trading_session("HK", date(2024, 9, 18)))
        self.assertFalse(is_trading_session("HK", date(2025, 10, 29)))
        self.assertFalse(is_trading_session("HK", date(2027, 2, 8)))
        self.assertTrue(calendar_year_is_known("HK", 2027))

    def test_unknown_cn_year_remains_conservative(self) -> None:
        # Do not invent a future closure: treating an unknown weekday as open
        # prevents stale prices from being silently accepted as holiday data.
        self.assertTrue(is_trading_session("CN", date(2027, 2, 8)))

    def test_2026_cn_spring_festival_includes_february_23(self) -> None:
        self.assertFalse(is_trading_session("CN", date(2026, 2, 23)))
        self.assertEqual(
            previous_trading_session("CN", date(2026, 2, 24)),
            date(2026, 2, 13),
        )
        self.assertTrue(is_trading_session("CN", date(2026, 2, 24)))

    def test_2026_hk_easter_and_observed_dates_match_exchange_schedule(self) -> None:
        self.assertFalse(is_trading_session("HK", date(2026, 4, 7)))
        self.assertEqual(
            previous_trading_session("HK", date(2026, 4, 8)),
            date(2026, 4, 2),
        )
        self.assertTrue(is_trading_session("HK", date(2026, 9, 28)))
        self.assertTrue(is_trading_session("HK", date(2026, 12, 28)))


if __name__ == "__main__":
    unittest.main()
