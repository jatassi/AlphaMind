"""Trading calendar and clock cache over Alpaca's /v2/calendar and /v2/clock (ALP-386).

Ships:
* ``TradingCalendarCache`` — refreshable in-memory cache over CalendarDay records
  from Alpaca's ``/v2/calendar``, with session-aware utilities the OMS,
  scheduler, settlement calculator, and continuous monitor consume.
* ``MarketClockSnapshot`` — thin dataclass wrapping ``MarketClock`` with a
  freshness timestamp (no caching; pass-through to ``/v2/clock``).
* ``get_market_clock`` — fetch current market clock state without caching.

Per ``docs/design/05-execution-layer/venue-configuration.md`` § Market hours:

* Regular session only - 9:30-16:00 ET.  Pre-market and after-hours are not
  "open" for the purposes of ``is_market_open``.
* Alpaca's ``/v2/calendar`` is authoritative; holidays and weekend exclusions
  come from the calendar API response, not from a local holiday library.
* If Alpaca is unreachable AND the cache has no data for the requested date,
  raise ``RuntimeError("calendar unavailable")`` rather than silently falling
  back to yaml session_hours.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Final
from zoneinfo import ZoneInfo

from alphamind.execution.broker_adapter import AccountStateQueries, CalendarDay, MarketClock

_ET: Final[ZoneInfo] = ZoneInfo("America/New_York")

_DEFAULT_REFRESH_TTL: Final[dt.timedelta] = dt.timedelta(hours=24)
_DEFAULT_FETCH_WINDOW_DAYS: Final[int] = 90
# Maximum calendar days to scan forward/backward when searching for the next session.
# Covers the longest known US market holiday stretch (≤ 5 days) with ample margin.
_MAX_SESSION_SEARCH_DAYS: Final[int] = 14


# ---------------------------------------------------------------------------
# MarketClockSnapshot — thin pass-through; no caching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketClockSnapshot:
    """Wraps AccountStateQueries.get_clock() with a freshness timestamp."""

    timestamp: dt.datetime
    is_open: bool
    next_open: dt.datetime
    next_close: dt.datetime
    fetched_at: dt.datetime  # when this snapshot was retrieved


def get_market_clock(queries: AccountStateQueries) -> MarketClockSnapshot:
    """Fetch the current market clock state. No caching."""
    clock: MarketClock = queries.get_clock()
    return MarketClockSnapshot(
        timestamp=clock.timestamp,
        is_open=clock.is_open,
        next_open=clock.next_open,
        next_close=clock.next_close,
        fetched_at=dt.datetime.now(tz=dt.UTC),
    )


# ---------------------------------------------------------------------------
# TradingCalendarCache
# ---------------------------------------------------------------------------


def _as_et(ts: dt.datetime) -> dt.datetime:
    """Convert *ts* to America/New_York, handling both aware and naive input."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=_ET)
    return ts.astimezone(_ET)


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
        refresh_ttl: dt.timedelta = _DEFAULT_REFRESH_TTL,
    ) -> None:
        self._queries = queries
        self._fetch_window_days = fetch_window_days
        self._refresh_ttl = refresh_ttl
        # _days maps date → CalendarDay for all fetched trading days.
        self._days: dict[dt.date, CalendarDay] = {}
        # Bounds of currently cached calendar data.
        self._window_start: dt.date | None = None
        self._window_end: dt.date | None = None
        # refresh_after: next lookup after this timestamp triggers re-fetch.
        self._refresh_after: dt.datetime = dt.datetime.now(tz=dt.UTC) + refresh_ttl

    # ------------------------------------------------------------------
    # Internal fetch helpers
    # ------------------------------------------------------------------

    def _fetch_window(self, start: dt.date, end: dt.date) -> None:
        """Fetch [start, end] from Alpaca and merge into _days."""
        days = self._queries.get_calendar(start=start, end=end)
        for day in days:
            self._days[day.date] = day
        # Expand tracked window bounds.
        if self._window_start is None or start < self._window_start:
            self._window_start = start
        if self._window_end is None or end > self._window_end:
            self._window_end = end
        self._refresh_after = dt.datetime.now(tz=dt.UTC) + self._refresh_ttl

    def _ensure_date_cached(self, d: dt.date) -> None:
        """Ensure *d* is within the cached window, fetching or extending as needed."""
        now = dt.datetime.now(tz=dt.UTC)
        ttl_expired = now >= self._refresh_after

        if ttl_expired:
            # Re-fetch the full window centered on d.
            half = self._fetch_window_days // 2
            start = d - dt.timedelta(days=half)
            end = d + dt.timedelta(days=half)
            self._fetch_window(start, end)
            return

        if self._window_start is None or self._window_end is None:
            # First fetch: center window on d.
            half = self._fetch_window_days // 2
            start = d - dt.timedelta(days=half)
            end = d + dt.timedelta(days=half)
            self._fetch_window(start, end)
            return

        if d < self._window_start:
            # Extend window backward to cover d.
            fetch_end = self._window_start - dt.timedelta(days=1)
            fetch_start = min(d, d - dt.timedelta(days=self._fetch_window_days))
            self._fetch_window(fetch_start, fetch_end)
        elif d > self._window_end:
            # Extend window forward to cover d.
            fetch_start = self._window_end + dt.timedelta(days=1)
            fetch_end = max(d + dt.timedelta(days=self._fetch_window_days), d)
            self._fetch_window(fetch_start, fetch_end)
        # else: d is within window — no fetch needed

    def _ensure_range_cached(self, start: dt.date, end: dt.date) -> None:
        """Ensure all dates in [start, end] are within the cached window."""
        # Check if the range is covered; if not, fetch accordingly.
        if (
            self._window_start is None
            or self._window_end is None
            or start < self._window_start
            or end > self._window_end
        ):
            self._ensure_date_cached(start)
            if end > (self._window_end or start):
                self._ensure_date_cached(end)

    # ------------------------------------------------------------------
    # Public API — session queries
    # ------------------------------------------------------------------

    def is_market_open(self, at: dt.datetime) -> bool:
        """True if ``at`` falls within a regular trading session.

        Pre-market and after-hours are NOT considered open here per
        venue-configuration.md (regular session 9:30-16:00 ET only).
        """
        at_et = _as_et(at)
        d = at_et.date()
        self._ensure_date_cached(d)
        day = self._days.get(d)
        if day is None:
            return False
        # Compare in ET to avoid cross-midnight artifacts.
        open_et = _as_et(day.session_open)
        close_et = _as_et(day.session_close)
        return open_et <= at_et < close_et

    def next_session_open(self, after: dt.datetime) -> dt.datetime:
        """Return the next regular-session open at or after ``after``.

        Semantics:
        * If ``after`` is before today's open → today's open.
        * If ``after`` is at-or-past today's open (i.e., mid-session or later)
          → next business day's open.
        """
        after_et = _as_et(after)
        d = after_et.date()
        # Fetch enough data to find the next session.
        self._ensure_date_cached(d)

        day = self._days.get(d)
        if day is not None:
            open_et = _as_et(day.session_open)
            if after_et < open_et:
                # Before today's open → return today's open.
                return open_et

        # at-or-past open (mid-session or after), or no session today (holiday/weekend):
        # find next business day.
        return self._next_open_after_date(d)

    def _next_session_day_after(self, d: dt.date) -> CalendarDay:
        """Return the CalendarDay for the first business day strictly after *d*."""
        candidate = d + dt.timedelta(days=1)
        for _ in range(_MAX_SESSION_SEARCH_DAYS):
            self._ensure_date_cached(candidate)
            day = self._days.get(candidate)
            if day is not None:
                return day
            candidate += dt.timedelta(days=1)
        msg = f"calendar unavailable: could not find next session after {d}"
        raise RuntimeError(msg)

    def _next_open_after_date(self, d: dt.date) -> dt.datetime:
        """Return the open time of the first business day strictly after *d*."""
        return _as_et(self._next_session_day_after(d).session_open)

    def next_session_close(self, after: dt.datetime) -> dt.datetime:
        """Return the regular-session close at or after ``after``.

        If ``after`` is mid-session, returns the current session's close.
        If ``after`` is past today's close, returns next business day's close.
        """
        after_et = _as_et(after)
        d = after_et.date()
        self._ensure_date_cached(d)

        day = self._days.get(d)
        if day is not None:
            close_et = _as_et(day.session_close)
            if after_et < close_et:
                return close_et

        # Past today's close or no session today: find next business day.
        return _as_et(self._next_session_day_after(d).session_close)

    def business_day_offset(self, start: dt.date, days: int) -> dt.date:
        """Return the date ``days`` business days from ``start``.

        Business days are calendar days where Alpaca's /v2/calendar
        returns a session entry. T+1 settlement uses
        ``business_day_offset(trade_date, 1)``.
        """
        if days == 0:
            # If start is a business day, return it; otherwise find next.
            self._ensure_date_cached(start)
            if start in self._days:
                return start
            return self._next_business_day_from(start)

        current = start
        remaining = abs(days)
        direction = 1 if days > 0 else -1

        for _ in range(remaining * _MAX_SESSION_SEARCH_DAYS):
            current += dt.timedelta(days=direction)
            self._ensure_date_cached(current)
            if current in self._days:
                remaining -= 1
                if remaining == 0:
                    return current

        msg = f"calendar unavailable: could not offset {days} business days from {start}"
        raise RuntimeError(msg)

    def _next_business_day_from(self, d: dt.date) -> dt.date:
        """Return the first business day on or after *d*."""
        candidate = d
        for _ in range(_MAX_SESSION_SEARCH_DAYS):
            self._ensure_date_cached(candidate)
            if candidate in self._days:
                return candidate
            candidate += dt.timedelta(days=1)
        msg = f"calendar unavailable: could not find business day from {d}"
        raise RuntimeError(msg)

    def iter_business_days(self, start: dt.date, end: dt.date) -> tuple[dt.date, ...]:
        """Return business days in [start, end] inclusive, ascending.

        ``self._days`` is a dict keyed by date; iterating it preserves
        insertion order, which is fetch order. After interleaved backward +
        forward window extensions the cache's insertion order can diverge
        from chronological order, so we sort explicitly to keep the
        contract.
        """
        self._ensure_range_cached(start, end)
        return tuple(sorted(d for d in self._days if start <= d <= end))

    def refresh(self) -> None:
        """Force-refetch the calendar window and reset refresh_after."""
        if self._window_start is None or self._window_end is None:
            return
        self._fetch_window(self._window_start, self._window_end)
