from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache

from .config import US_EQUITY_CLOSE_MARKS


HK_CALENDAR_SECIDS = {
    "2.930914",
}

US_INDEX_SECIDS = {
    "100.NDX100",
    "100.SOX",
    "100.SPX",
}

HK_INDEX_SECIDS = {
    "100.HSI",
    "100.HSCEI",
}

HK_HOLIDAYS = {
    "2026-01-01",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-04-03",
    "2026-04-06",
    "2026-05-01",
    "2026-05-25",
    "2026-06-19",
    "2026-07-01",
    "2026-09-28",
    "2026-10-01",
    "2026-10-19",
    "2026-12-25",
    "2026-12-28",
}

CN_HOLIDAYS = {
    "2026-01-01",
    "2026-02-16",
    "2026-02-17",
    "2026-02-18",
    "2026-02-19",
    "2026-02-20",
    "2026-04-06",
    "2026-05-01",
    "2026-05-04",
    "2026-05-05",
    "2026-06-19",
    "2026-09-25",
    "2026-10-01",
    "2026-10-02",
    "2026-10-05",
    "2026-10-06",
    "2026-10-07",
}


def expected_market_closure_gap(secid: str, target_date: str, price_date: str) -> str | None:
    target_day = _parse_date(target_date)
    price_day = _parse_date(price_date)
    if target_day is None or price_day is None or price_day >= target_day:
        return None

    market = historical_price_market(secid)
    if market is None or is_trading_session(market, target_day):
        return None

    return market if previous_trading_session(market, target_day) == price_day else None


def historical_price_market(secid: str) -> str | None:
    if secid in US_EQUITY_CLOSE_MARKS:
        return "US"
    if secid in US_INDEX_SECIDS:
        return "US"
    if secid in HK_INDEX_SECIDS or secid in HK_CALENDAR_SECIDS:
        return "HK"

    market, _symbol = secid.split(".", 1)
    if market in {"105", "106", "107"}:
        return "US"
    if market in {"116", "124"}:
        return "HK"
    if market in {"0", "1", "2"}:
        return "CN"
    return None


def previous_trading_session(market: str, day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_trading_session(market, cursor):
        cursor -= timedelta(days=1)
    return cursor


def is_trading_session(market: str, day: date) -> bool:
    if day.weekday() >= 5:
        return False
    if market == "US":
        return day not in _us_holidays(day.year)
    if market == "HK":
        return day.isoformat() not in HK_HOLIDAYS
    if market == "CN":
        return day.isoformat() not in CN_HOLIDAYS
    return True


def _parse_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


@lru_cache(maxsize=None)
def _us_holidays(year: int) -> frozenset[date]:
    holidays = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_date(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
    }
    for holiday_year in (year - 1, year, year + 1):
        for month, day in ((1, 1), (6, 19), (7, 4), (12, 25)):
            observed = _observed_fixed_holiday(holiday_year, month, day)
            if observed.year == year:
                holidays.add(observed)
    if year == 2025:
        holidays.add(date(2025, 1, 9))
    return frozenset(holidays)


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    day = date(year, month, 1)
    while day.weekday() != weekday:
        day += timedelta(days=1)
    return day + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    while day.weekday() != weekday:
        day -= timedelta(days=1)
    return day


def _easter_date(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)
