"""KRX trading-day guard.

The bot must fail closed when its local calendar has not been reviewed for a
year.  Update ``KRX_CLOSED_DATES`` before each new calendar year after KRX
publishes its official trading calendar.
"""

import datetime


# 2026 KRX weekday closures (public/substitute holidays, election day,
# Labour Day and year-end closing day). Weekends are handled separately.
KRX_CLOSED_DATES = {
    datetime.date(2026, 1, 1),
    datetime.date(2026, 2, 16),
    datetime.date(2026, 2, 17),
    datetime.date(2026, 2, 18),
    datetime.date(2026, 3, 2),
    datetime.date(2026, 5, 1),
    datetime.date(2026, 5, 5),
    datetime.date(2026, 5, 25),
    datetime.date(2026, 6, 3),
    datetime.date(2026, 8, 17),
    datetime.date(2026, 9, 24),
    datetime.date(2026, 9, 25),
    datetime.date(2026, 9, 28),
    datetime.date(2026, 10, 5),
    datetime.date(2026, 10, 9),
    datetime.date(2026, 12, 25),
    datetime.date(2026, 12, 31),
}

SUPPORTED_YEARS = {2026}


def is_krx_trading_day(value=None):
    """Return False for weekends, KRX closures, or unreviewed years."""
    value = value or datetime.date.today()
    if isinstance(value, datetime.datetime):
        value = value.date()
    if value.year not in SUPPORTED_YEARS:
        return False
    return value.weekday() < 5 and value not in KRX_CLOSED_DATES
