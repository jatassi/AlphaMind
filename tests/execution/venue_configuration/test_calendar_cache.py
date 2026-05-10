"""Tests for venue_configuration.calendar_cache — ALP-386.

Uses a fake AccountStateQueries instrumented with a call counter so tests
assert caching behavior without network calls.  All tests use public
interfaces only.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from alphamind.execution.broker_adapter import (
    AccountStateQueries,
    CalendarDay,
    MarketClock,
)

_ET = ZoneInfo("America/New_York")
_UTC = dt.UTC


# ---------------------------------------------------------------------------
# Helpers — fake AccountStateQueries
# ---------------------------------------------------------------------------


def _make_calendar_day(
    d: dt.date,
    open_hour: int = 9,
    open_min: int = 30,
    close_hour: int = 16,
    close_min: int = 0,
) -> CalendarDay:
    """Build a CalendarDay with ET-aware open/close times."""
    session_open = dt.datetime(d.year, d.month, d.day, open_hour, open_min, tzinfo=_ET)
    session_close = dt.datetime(d.year, d.month, d.day, close_hour, close_min, tzinfo=_ET)
    return CalendarDay(
        date=d,
        open_time=session_open,
        close_time=session_close,
        session_open=session_open,
        session_close=session_close,
    )


def _business_days_in_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """Generate Mon-Fri dates in [start, end] — used by fake calendar."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:  # 0=Mon … 4=Fri
            days.append(current)
        current += dt.timedelta(days=1)
    return days


class FakeAccountStateQueries(AccountStateQueries):
    """Minimal fake for AccountStateQueries; tracks call counts.

    Subclasses the real type so structural type-checks pass; deliberately
    skips ``super().__init__`` (no TradingClient needed for these tests).
    """

    def __init__(
        self,
        *,
        holiday_dates: set[dt.date] | None = None,
        fail_calendar: bool = False,
    ) -> None:
        # Deliberately skip super().__init__ — no TradingClient needed.
        self.calendar_call_count = 0
        self.clock_call_count = 0
        self._holidays: set[dt.date] = holiday_dates or set()
        self._fail_calendar = fail_calendar

    def get_calendar(
        self,
        *,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> tuple[CalendarDay, ...]:
        if self._fail_calendar:
            msg = "calendar unavailable"
            raise RuntimeError(msg)
        self.calendar_call_count += 1
        # Generate Mon-Fri days in the requested range, minus holidays.
        range_start = start or dt.date(2026, 1, 1)
        range_end = end or dt.date(2026, 12, 31)
        return tuple(
            _make_calendar_day(d)
            for d in _business_days_in_range(range_start, range_end)
            if d not in self._holidays
        )

    def get_clock(self) -> MarketClock:
        self.clock_call_count += 1
        now = dt.datetime.now(tz=_UTC)
        return MarketClock(
            timestamp=now,
            is_open=True,
            next_open=now + dt.timedelta(hours=16),
            next_close=now + dt.timedelta(hours=8),
        )


# ---------------------------------------------------------------------------
# 1. Import surface
# ---------------------------------------------------------------------------


def test_importable_from_package() -> None:
    """TradingCalendarCache, MarketClockSnapshot, get_market_clock importable."""
    import alphamind.execution.venue_configuration as pkg

    assert hasattr(pkg, "TradingCalendarCache")
    assert hasattr(pkg, "MarketClockSnapshot")
    assert hasattr(pkg, "get_market_clock")


# ---------------------------------------------------------------------------
# 2. Initialization
# ---------------------------------------------------------------------------


def test_default_init() -> None:
    """TradingCalendarCache initializes with defaults without error."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    assert cache is not None


def test_default_fetch_window_days() -> None:
    """Default fetch window is 90 days."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    assert cache._fetch_window_days == 90


def test_default_refresh_ttl() -> None:
    """Default TTL is 24 hours."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    assert cache._refresh_ttl == dt.timedelta(hours=24)


# ---------------------------------------------------------------------------
# 3. is_market_open
# ---------------------------------------------------------------------------


def test_is_market_open_during_session() -> None:
    """True for a datetime within regular 9:30-16:00 ET session."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # 2026-05-07 is a Thursday — a regular business day
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)  # 11:00 ET
    assert cache.is_market_open(at) is True


def test_is_market_open_pre_market() -> None:
    """False for a datetime before 9:30 ET (pre-market)."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 7, 8, 0, tzinfo=_ET)  # 08:00 ET
    assert cache.is_market_open(at) is False


def test_is_market_open_after_hours() -> None:
    """False for a datetime after 16:00 ET (after-hours)."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 7, 17, 0, tzinfo=_ET)  # 17:00 ET
    assert cache.is_market_open(at) is False


def test_is_market_open_on_weekend() -> None:
    """False for a Saturday."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 9, 11, 0, tzinfo=_ET)  # Saturday 2026-05-09
    assert cache.is_market_open(at) is False


def test_is_market_open_on_sunday() -> None:
    """False for a Sunday."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 10, 11, 0, tzinfo=_ET)  # Sunday 2026-05-10
    assert cache.is_market_open(at) is False


def test_is_market_open_on_holiday() -> None:
    """False for a day excluded from Alpaca's calendar (holiday)."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    holiday = dt.date(2026, 12, 25)
    queries = FakeAccountStateQueries(holiday_dates={holiday})
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 12, 25, 11, 0, tzinfo=_ET)
    assert cache.is_market_open(at) is False


# ---------------------------------------------------------------------------
# 4. next_session_open
# ---------------------------------------------------------------------------


def test_next_session_open_mid_session_returns_next_day() -> None:
    """next_session_open mid-session returns NEXT business day's open."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Thursday 2026-05-07 at 11:00 ET — mid session
    after = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    nxt = cache.next_session_open(after)
    # Next session open is Friday 2026-05-08 at 9:30 ET
    expected = dt.datetime(2026, 5, 8, 9, 30, tzinfo=_ET)
    assert nxt == expected


def test_next_session_open_before_open_returns_today() -> None:
    """next_session_open before today's open returns today's open."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Thursday 2026-05-07 at 8:00 ET — before open
    after = dt.datetime(2026, 5, 7, 8, 0, tzinfo=_ET)
    nxt = cache.next_session_open(after)
    expected = dt.datetime(2026, 5, 7, 9, 30, tzinfo=_ET)
    assert nxt == expected


def test_next_session_open_after_close_returns_next_business_day() -> None:
    """next_session_open after close returns next business day's open."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Thursday 2026-05-07 at 17:00 ET — after close
    after = dt.datetime(2026, 5, 7, 17, 0, tzinfo=_ET)
    nxt = cache.next_session_open(after)
    expected = dt.datetime(2026, 5, 8, 9, 30, tzinfo=_ET)
    assert nxt == expected


def test_next_session_open_friday_after_close_returns_monday() -> None:
    """next_session_open after Friday close skips weekend → Monday open."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Friday 2026-05-08 at 17:00 ET — after close
    after = dt.datetime(2026, 5, 8, 17, 0, tzinfo=_ET)
    nxt = cache.next_session_open(after)
    # Monday 2026-05-11
    expected = dt.datetime(2026, 5, 11, 9, 30, tzinfo=_ET)
    assert nxt == expected


def test_next_session_open_friday_before_close_returns_next_monday() -> None:
    """next_session_open mid-Friday session returns Monday (next session)."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Friday 2026-05-08 at 11:00 ET — mid session
    after = dt.datetime(2026, 5, 8, 11, 0, tzinfo=_ET)
    nxt = cache.next_session_open(after)
    expected = dt.datetime(2026, 5, 11, 9, 30, tzinfo=_ET)
    assert nxt == expected


# ---------------------------------------------------------------------------
# 5. next_session_close
# ---------------------------------------------------------------------------


def test_next_session_close_mid_session_returns_today_close() -> None:
    """next_session_close mid-session returns today's close."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Thursday 2026-05-07 at 11:00 ET — mid session
    after = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    close = cache.next_session_close(after)
    expected = dt.datetime(2026, 5, 7, 16, 0, tzinfo=_ET)
    assert close == expected


def test_next_session_close_after_close_returns_next_day() -> None:
    """next_session_close after close returns next business day's close."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    after = dt.datetime(2026, 5, 7, 17, 0, tzinfo=_ET)
    close = cache.next_session_close(after)
    expected = dt.datetime(2026, 5, 8, 16, 0, tzinfo=_ET)
    assert close == expected


# ---------------------------------------------------------------------------
# 6. business_day_offset
# ---------------------------------------------------------------------------


def test_business_day_offset_saturday_plus_1() -> None:
    """Sat 2026-05-09 + 1 business day → Mon 2026-05-11."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    result = cache.business_day_offset(dt.date(2026, 5, 9), 1)
    # Sat→Mon skipping Sun
    assert result == dt.date(2026, 5, 11)


def test_business_day_offset_skips_holiday() -> None:
    """business_day_offset skips Christmas (holiday) when computing T+2."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    # Christmas 2026 is on a Friday
    holiday = dt.date(2026, 12, 25)
    queries = FakeAccountStateQueries(holiday_dates={holiday})
    cache = TradingCalendarCache(queries)
    # Dec 24=Thu, Dec 25=Fri (holiday, skipped), Dec 26=Sat, Dec 28=Mon (T+1), Dec 29=Tue (T+2)
    result = cache.business_day_offset(dt.date(2026, 12, 24), 2)
    assert result == dt.date(2026, 12, 29)


def test_business_day_offset_zero() -> None:
    """Offset of 0 returns start date (if it's a business day)."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    result = cache.business_day_offset(dt.date(2026, 5, 7), 0)
    assert result == dt.date(2026, 5, 7)


# ---------------------------------------------------------------------------
# 7. iter_business_days
# ---------------------------------------------------------------------------


def test_iter_business_days_week() -> None:
    """iter_business_days over Tue-Sat 2026-05-05 to Sat 2026-05-09 → 5 dates."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # May 4=Mon … May 8=Fri are 5 business days; May 9=Sat is excluded.
    result = cache.iter_business_days(dt.date(2026, 5, 4), dt.date(2026, 5, 9))
    assert len(result) == 5
    assert dt.date(2026, 5, 9) not in result  # Saturday excluded
    assert dt.date(2026, 5, 4) in result  # Monday included


def test_iter_business_days_excludes_holidays() -> None:
    """iter_business_days excludes holidays from Alpaca's calendar."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    holiday = dt.date(2026, 5, 7)  # Thursday
    queries = FakeAccountStateQueries(holiday_dates={holiday})
    cache = TradingCalendarCache(queries)
    result = cache.iter_business_days(dt.date(2026, 5, 4), dt.date(2026, 5, 8))
    # Mon-Fri = 5 days, minus holiday Thu = 4 days
    assert len(result) == 4
    assert dt.date(2026, 5, 7) not in result


def test_iter_business_days_returns_tuple() -> None:
    """iter_business_days returns a tuple of dates."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    result = cache.iter_business_days(dt.date(2026, 5, 7), dt.date(2026, 5, 7))
    assert isinstance(result, tuple)
    assert result == (dt.date(2026, 5, 7),)


# ---------------------------------------------------------------------------
# 8. Cache hit — no re-fetch on second call within window
# ---------------------------------------------------------------------------


def test_cache_hit_no_refetch() -> None:
    """Second call within cached window does NOT re-fetch (call count = 1)."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    # Prime the cache
    cache.is_market_open(at)
    count_after_first = queries.calendar_call_count
    # Second call for the same date — should not fetch again
    cache.is_market_open(at)
    assert queries.calendar_call_count == count_after_first


def test_cache_hit_different_date_in_window() -> None:
    """Call for a date within the already-fetched 90-day window does not re-fetch."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at_first = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    # Prime the cache
    cache.is_market_open(at_first)
    count_after_first = queries.calendar_call_count
    # A date within the 90-day window (e.g., next day)
    at_second = dt.datetime(2026, 5, 8, 11, 0, tzinfo=_ET)
    cache.is_market_open(at_second)
    assert queries.calendar_call_count == count_after_first


# ---------------------------------------------------------------------------
# 9. Cache miss — window extension
# ---------------------------------------------------------------------------


def test_cache_miss_window_extension() -> None:
    """Lookup outside cached window triggers a fetch that extends the window."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries, fetch_window_days=10)
    # Prime the cache with a narrow 10-day window centered on 2026-05-07
    at_first = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    cache.is_market_open(at_first)
    count_after_first = queries.calendar_call_count
    # Request a date far outside: 2026-09-01 — must trigger a new fetch
    at_far = dt.datetime(2026, 9, 1, 11, 0, tzinfo=_ET)
    cache.is_market_open(at_far)
    assert queries.calendar_call_count > count_after_first


# ---------------------------------------------------------------------------
# 10. refresh()
# ---------------------------------------------------------------------------


def test_refresh_forces_refetch() -> None:
    """refresh() calls get_calendar again and resets refresh_after."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Prime with one call
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    cache.is_market_open(at)
    count_before_refresh = queries.calendar_call_count
    # Force-refresh
    cache.refresh()
    assert queries.calendar_call_count > count_before_refresh


def test_refresh_resets_refresh_after() -> None:
    """After refresh(), refresh_after is pushed into the future."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    cache.is_market_open(at)
    old_refresh_after = cache._refresh_after
    cache.refresh()
    assert cache._refresh_after >= old_refresh_after


# ---------------------------------------------------------------------------
# 11. TTL-driven refresh
# ---------------------------------------------------------------------------


def test_ttl_expired_triggers_refetch() -> None:
    """After TTL elapses, next lookup triggers a re-fetch."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    # Set a very short TTL so we can expire it immediately
    cache = TradingCalendarCache(queries, refresh_ttl=dt.timedelta(seconds=-1))
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    # Prime (already expired TTL, so this is a fetch)
    cache.is_market_open(at)
    count_after_first = queries.calendar_call_count
    # Second call — TTL already expired at construction, so this should fetch again
    cache.is_market_open(at)
    assert queries.calendar_call_count > count_after_first


# ---------------------------------------------------------------------------
# 12. get_market_clock — no caching
# ---------------------------------------------------------------------------


def test_get_market_clock_calls_get_clock_each_time() -> None:
    """get_market_clock calls queries.get_clock() every invocation."""
    from alphamind.execution.venue_configuration import get_market_clock

    queries = FakeAccountStateQueries()
    get_market_clock(queries)
    assert queries.clock_call_count == 1
    get_market_clock(queries)
    assert queries.clock_call_count == 2


def test_get_market_clock_returns_snapshot() -> None:
    """get_market_clock returns a MarketClockSnapshot."""
    from alphamind.execution.venue_configuration import MarketClockSnapshot, get_market_clock

    queries = FakeAccountStateQueries()
    snapshot = get_market_clock(queries)
    assert isinstance(snapshot, MarketClockSnapshot)


def test_market_clock_snapshot_has_fetched_at() -> None:
    """MarketClockSnapshot carries a fetched_at timestamp."""
    from alphamind.execution.venue_configuration import get_market_clock

    queries = FakeAccountStateQueries()
    snapshot = get_market_clock(queries)
    assert snapshot.fetched_at is not None
    assert isinstance(snapshot.fetched_at, dt.datetime)


# ---------------------------------------------------------------------------
# 13. Error propagation — RuntimeError when API unreachable and no cache
# ---------------------------------------------------------------------------


def test_runtime_error_when_api_unreachable_and_no_cache() -> None:
    """RuntimeError raised when Alpaca is unreachable and no cached data."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries(fail_calendar=True)
    cache = TradingCalendarCache(queries)
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        cache.is_market_open(at)


def test_no_error_when_api_unreachable_but_cache_populated() -> None:
    """No error when API unreachable if date is within the cached window."""
    from alphamind.execution.venue_configuration import TradingCalendarCache

    queries = FakeAccountStateQueries()
    cache = TradingCalendarCache(queries)
    # Prime the cache successfully
    at = dt.datetime(2026, 5, 7, 11, 0, tzinfo=_ET)
    cache.is_market_open(at)
    # Now simulate the API going down (but cache covers this date)
    queries._fail_calendar = True
    # Should NOT raise — cache covers 2026-05-07
    result = cache.is_market_open(at)
    assert isinstance(result, bool)
