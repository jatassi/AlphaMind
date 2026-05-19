# 03a — Trading calendar / clock cache

## Goal

Ship a refreshable in-memory cache over Alpaca's `/v2/calendar` and `/v2/clock` endpoints, with session-aware utilities the OMS, scheduler, settlement calculator, and continuous monitor consume: `is_market_open(at: datetime)`, `next_session_open(after: datetime)`, `next_session_close(after: datetime)`, `business_day_offset(start: date, days: int)`, and `iter_business_days(start: date, end: date)`. The cache lazy-fetches a window from Alpaca on first miss and refreshes per a configurable TTL; calendar holidays and weekends are derived from `/v2/calendar`'s response (NOT from a local holiday library).

## Reading

* `docs/design/05-execution-layer/venue-configuration.md` § Market hours and session boundaries — session schedule from Alpaca's `/v2/calendar` and `/v2/clock`; calendar caching policy.
* `docs/design/05-execution-layer/broker-adapter.md` § Account state queries — `GET /v2/calendar` and `GET /v2/clock` consumers.
* `src/alphamind/execution/broker_adapter/queries.py` (story 02a) — `AccountStateQueries.get_calendar(start, end)` returning `tuple[CalendarDay, ...]` and `AccountStateQueries.get_clock()` returning `MarketClock`.
* `config/venue.yaml` — `session_hours.regular / .pre_market / .after_hours` already loaded as `VenueConfig.session_hours` (used as a sanity-check fallback when the calendar API is briefly unavailable, but Alpaca's calendar is authoritative).
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (J) — venue-config verification in isolation.

## Depends on

* <issue id="4029f936-92d1-4f06-a99a-6f7fd8b2841d">ALP-379</issue> (02a — Account state GET wrappers) — provides `get_calendar` and `get_clock`.

## Scope

Source under `src/alphamind/execution/venue_configuration/calendar_cache.py` (new file). Tests at `tests/execution/venue_configuration/test_calendar_cache.py`.

### 1. `TradingCalendarCache`

```python
from datetime import date, datetime, timedelta
from typing import Final

from alphamind.execution.broker_adapter import AccountStateQueries, CalendarDay, MarketClock


_DEFAULT_REFRESH_TTL: Final[timedelta] = timedelta(hours=24)
_DEFAULT_FETCH_WINDOW_DAYS: Final[int] = 90


class TradingCalendarCache:
    """In-memory cache over Alpaca's /v2/calendar with refreshable window.

    On first call, fetches a window of CalendarDay records from Alpaca
    centered on the requested date. Subsequent calls within the cached
    window return from memory; calls outside the window trigger a window
    extension. The cache also tracks a refresh_after timestamp; once
    elapsed, the next call re-fetches even if the date is in-window
    (handles Alpaca calendar-amendment scenarios).
    """

    def __init__(
        self,
        queries: AccountStateQueries,
        *,
        fetch_window_days: int = _DEFAULT_FETCH_WINDOW_DAYS,
        refresh_ttl: timedelta = _DEFAULT_REFRESH_TTL,
    ) -> None:
        ...

    def is_market_open(self, at: datetime) -> bool:
        """True if `at` falls within a regular trading session.

        Pre-market and after-hours are NOT considered open here per
        venue-configuration.md (regular session 9:30-16:00 ET only).
        """
        ...

    def next_session_open(self, after: datetime) -> datetime:
        """Return the next regular-session open at or after `after`.

        If `after` is mid-session, returns the NEXT session's open
        (not the current session's).
        """
        ...

    def next_session_close(self, after: datetime) -> datetime:
        """Return the regular-session close at or after `after`.

        If `after` is mid-session, returns the current session's close.
        """
        ...

    def business_day_offset(self, start: date, days: int) -> date:
        """Return the date `days` business days from `start`.

        Business days are calendar days where Alpaca's /v2/calendar
        returns a session entry. T+1 settlement uses `business_day_offset(trade_date, 1)`.
        """
        ...

    def iter_business_days(self, start: date, end: date) -> tuple[date, ...]:
        """Return business days in [start, end] inclusive."""
        ...

    def refresh(self) -> None:
        """Force-refetch the calendar window and reset refresh_after."""
        ...
```

### 2. `MarketClockSnapshot` cache helper

`/v2/clock` is volatile — the `is_open` value changes minute-to-minute around session boundaries. Don't cache this in the calendar cache; expose a thin pass-through helper that fetches each call:

```python
@dataclass(frozen=True)
class MarketClockSnapshot:
    """Wraps AccountStateQueries.get_clock() with a freshness timestamp."""

    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime
    fetched_at: datetime  # when this snapshot was retrieved


def get_market_clock(queries: AccountStateQueries) -> MarketClockSnapshot:
    """Fetch the current market clock state. No caching."""
    ...
```

### 3. Window-fetch strategy

* On first lookup for date D: fetch `[D - fetch_window_days // 2, D + fetch_window_days // 2]` from Alpaca.
* On lookup for date D outside the cached window: extend the cache to cover D (fetch `[D, max(D + fetch_window_days, current_window_end)]` if D is past current window; mirror for past).
* On lookup older than the cache's refresh_after timestamp: re-fetch the full window.

### 4. Session-time computation

The `CalendarDay` records from `/v2/calendar` carry `session_open` and `session_close` as tz-aware datetimes (per story 02a). Use them directly — do not derive from `config/venue.yaml`'s static hours. The yaml session_hours is for sanity-check fallback only when Alpaca is briefly unreachable; if the cache has no data for a date and Alpaca's API is unreachable, raise `RuntimeError("calendar unavailable")` rather than fall back silently.

### 5. Public surface

In `src/alphamind/execution/venue_configuration/__init__.py`:

```python
from alphamind.execution.venue_configuration.calendar_cache import (
    MarketClockSnapshot,
    TradingCalendarCache,
    get_market_clock,
)
```

### Out of scope

* Settlement-date computation — story 04a (depends on this cache).
* Account-derived venue state surfacer — story 03b (uses `MarketClockSnapshot` indirectly but doesn't consume the calendar).
* Disk persistence of the cache — in-memory only; the adapter is restarted per process per design (`venue-configuration.md § Configuration immutability`).
* Holiday-amendment notifications — Alpaca's calendar already reflects them; the cache's `refresh_ttl` ensures eventual freshness.

## Acceptance criteria

- [ ] `src/alphamind/execution/venue_configuration/calendar_cache.py` exists; `TradingCalendarCache`, `MarketClockSnapshot`, `get_market_clock` are importable from `alphamind.execution.venue_configuration`.
- [ ] `TradingCalendarCache(queries)` initializes with default 90-day fetch window and 24h refresh TTL.
- [ ] `is_market_open(<datetime within regular session>)` returns `True`; `is_market_open(<datetime in pre-market>)` returns `False`; `is_market_open(<datetime on weekend>)` returns `False`.
- [ ] `next_session_open(<datetime mid-session>)` returns the NEXT day's open, not today's.
- [ ] `next_session_close(<datetime mid-session>)` returns today's close.
- [ ] `next_session_open(<datetime on Friday after close>)` returns Monday's open (skipping weekend); `next_session_open(<datetime on Friday before close>)` returns Friday's close-day's open is the SAME day mid-session... actually the "next at or after" semantics — clarify: returns the next session open AT or AFTER the timestamp; if `after` is before today's open, returns today's open; if `after` is at-or-past today's open, returns tomorrow's (or next business day's) open.
- [ ] `business_day_offset(date(2026, 5, 9), 1)` returns the next business day after 2026-05-09 (Saturday → 2026-05-11 Monday).
- [ ] `business_day_offset(date(2026, 12, 24), 2)` correctly skips Christmas if it's a holiday in Alpaca's calendar.
- [ ] `iter_business_days(date(2026, 5, 5), date(2026, 5, 9))` returns 5 dates excluding Saturday and Sunday.
- [ ] First call for a date fetches a 90-day window centered on that date; subsequent calls within the window do NOT re-fetch (assertable via mock-call counter).
- [ ] Calls outside the cached window trigger a window extension that includes the requested date.
- [ ] `refresh()` re-fetches and resets `refresh_after`.
- [ ] After `refresh_ttl` elapsed, the next lookup triggers a re-fetch.
- [ ] `get_market_clock(queries)` calls `queries.get_clock()` each invocation (no caching) and returns a fresh `MarketClockSnapshot`.
- [ ] When Alpaca's calendar API is unreachable AND the cache has no data for the requested date, the cache raises `RuntimeError("calendar unavailable")`.
- [ ] Tests use a fake `AccountStateQueries` instrumented with a call counter; cover: cache hit, cache miss + window extension, ttl-driven refresh, weekend / holiday business-day skip, session-aware open/close lookups, error propagation when API is unreachable.
- [ ] `uv run pytest tests/execution/venue_configuration/test_calendar_cache.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/venue_configuration/test_calendar_cache.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check via verify_broker_adapter.py phase that fetches the calendar over a 30-day window and asserts business-day count matches expectations.
* Lint clean per CLAUDE.md.
