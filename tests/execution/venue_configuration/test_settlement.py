"""Tests for venue_configuration.settlement — ALP-391.

Uses a fake TradingCalendarCache constructed via FakeAccountStateQueries
(same pattern as test_calendar_cache.py) with controlled holidays.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from alphamind.execution.broker_adapter import AccountStateQueries, CalendarDay, MarketClock
from alphamind.execution.venue_configuration.calendar_cache import TradingCalendarCache
from alphamind.execution.venue_configuration.settlement import (
    compute_settlement_date,
    is_settled,
)

_ET = ZoneInfo("America/New_York")


# ---------------------------------------------------------------------------
# Helpers — fake calendar infrastructure (mirrors test_calendar_cache.py)
# ---------------------------------------------------------------------------


def _business_days_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """Mon-Fri dates in [start, end]."""
    days: list[dt.date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:  # 0=Mon … 4=Fri
            days.append(current)
        current += dt.timedelta(days=1)
    return days


def _make_calendar_day(d: dt.date) -> CalendarDay:
    session_open = dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=_ET)
    session_close = dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=_ET)
    return CalendarDay(
        date=d,
        open_time=session_open,
        close_time=session_close,
        session_open=session_open,
        session_close=session_close,
    )


class FakeAccountStateQueries(AccountStateQueries):
    """Minimal fake: returns Mon-Fri calendar days minus explicit holidays."""

    def __init__(self, *, holiday_dates: set[dt.date] | None = None) -> None:
        # Skip super().__init__ — no TradingClient needed.
        self._holidays: set[dt.date] = holiday_dates or set()

    def get_calendar(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> tuple[CalendarDay, ...]:
        range_start = start or dt.date(2026, 1, 1)
        range_end = end or dt.date(2026, 12, 31)
        return tuple(
            _make_calendar_day(d)
            for d in _business_days_in_range(range_start, range_end)
            if d not in self._holidays
        )

    def get_clock(self) -> MarketClock:  # pragma: no cover
        now = dt.datetime.now(tz=dt.UTC)
        return MarketClock(
            timestamp=now,
            is_open=True,
            next_open=now + dt.timedelta(hours=16),
            next_close=now + dt.timedelta(hours=8),
        )


def _make_cache(holiday_dates: set[dt.date] | None = None) -> TradingCalendarCache:
    return TradingCalendarCache(FakeAccountStateQueries(holiday_dates=holiday_dates))


# ---------------------------------------------------------------------------
# 1. Standard T+1: Monday → Tuesday
# ---------------------------------------------------------------------------


def test_equity_standard_t1_mon_to_tue() -> None:
    """Equity trade on Mon 2026-05-11 settles Tue 2026-05-12 (T+1)."""
    cache = _make_cache()
    result = compute_settlement_date(dt.date(2026, 5, 11), "equity", calendar=cache)
    assert result == dt.date(2026, 5, 12)


# ---------------------------------------------------------------------------
# 2. Weekend skip: Friday → Monday
# ---------------------------------------------------------------------------


def test_equity_friday_to_monday() -> None:
    """Equity trade on Fri 2026-05-08 settles Mon 2026-05-11 (T+1, skipping weekend)."""
    cache = _make_cache()
    result = compute_settlement_date(dt.date(2026, 5, 8), "equity", calendar=cache)
    assert result == dt.date(2026, 5, 11)


# ---------------------------------------------------------------------------
# 3. Market holiday skip
# ---------------------------------------------------------------------------


def test_equity_skips_market_holiday() -> None:
    """Trade on day before a holiday: T+1 skips the holiday.

    Thu 2026-05-14 + holiday on Fri 2026-05-15 → settles Mon 2026-05-18.
    """
    holiday = dt.date(2026, 5, 15)  # Fri — treat as market holiday
    cache = _make_cache(holiday_dates={holiday})
    result = compute_settlement_date(dt.date(2026, 5, 14), "equity", calendar=cache)
    # T+1 from Thu should be Fri, but Fri is a holiday → Mon
    assert result == dt.date(2026, 5, 18)


# ---------------------------------------------------------------------------
# 4. Weekend trade_date roll-forward
# ---------------------------------------------------------------------------


def test_equity_weekend_trade_date_rolls_forward() -> None:
    """Sat 2026-05-09 rolls trade_date to Mon 2026-05-11, then T+1 = Tue 2026-05-12."""
    cache = _make_cache()
    result = compute_settlement_date(dt.date(2026, 5, 9), "equity", calendar=cache)
    # Sat → roll to Mon 5/11, then T+1 = Tue 5/12
    assert result == dt.date(2026, 5, 12)


# ---------------------------------------------------------------------------
# 5. Options settle on same schedule as equity (T+1)
# ---------------------------------------------------------------------------


def test_option_same_as_equity() -> None:
    """Option trade settles T+1 — identical to equity."""
    cache = _make_cache()
    trade_date = dt.date(2026, 5, 11)
    assert compute_settlement_date(trade_date, "option", calendar=cache) == compute_settlement_date(
        trade_date, "equity", calendar=cache
    )


# ---------------------------------------------------------------------------
# 6 & 7. Crypto T+0
# ---------------------------------------------------------------------------


def test_crypto_t0_on_business_day() -> None:
    """Crypto trade on a business day settles same day (T+0)."""
    cache = _make_cache()
    trade_date = dt.date(2026, 5, 11)  # Monday
    result = compute_settlement_date(trade_date, "crypto", calendar=cache)
    assert result == trade_date


def test_crypto_non_business_day_rolls_forward() -> None:
    """Crypto trade on Saturday rolls to next business day (Monday)."""
    cache = _make_cache()
    result = compute_settlement_date(dt.date(2026, 5, 9), "crypto", calendar=cache)
    assert result == dt.date(2026, 5, 11)


# ---------------------------------------------------------------------------
# 8 & 9. is_settled boundary
# ---------------------------------------------------------------------------


def test_is_settled_true_on_settlement_date() -> None:
    """is_settled returns True when as_of == settlement_date.

    Fri 2026-05-08 equity settles Mon 2026-05-11; as_of=2026-05-11 → True.
    """
    cache = _make_cache()
    assert is_settled(
        dt.date(2026, 5, 8),
        "equity",
        as_of=dt.date(2026, 5, 11),
        calendar=cache,
    )


def test_is_settled_true_after_settlement_date() -> None:
    """is_settled returns True when as_of is after settlement_date."""
    cache = _make_cache()
    assert is_settled(
        dt.date(2026, 5, 8),
        "equity",
        as_of=dt.date(2026, 5, 12),
        calendar=cache,
    )


def test_is_settled_false_before_settlement_date() -> None:
    """is_settled returns False when as_of < settlement_date.

    Fri 2026-05-08 equity settles Mon 2026-05-11; as_of=2026-05-10 (Sun) → False.
    """
    cache = _make_cache()
    assert not is_settled(
        dt.date(2026, 5, 8),
        "equity",
        as_of=dt.date(2026, 5, 10),
        calendar=cache,
    )


# ---------------------------------------------------------------------------
# 10. Package-level import surface
# ---------------------------------------------------------------------------


def test_importable_from_package() -> None:
    """compute_settlement_date and is_settled importable from top-level package."""
    import alphamind.execution.venue_configuration as pkg

    assert hasattr(pkg, "compute_settlement_date")
    assert hasattr(pkg, "is_settled")
