"""Supervisor wiring for the borrow-accrual task (ALP-719).

:func:`register_borrow_accrual_task` is the composition root the
monitor ``__main__`` calls during startup. It binds the production
substrate — sync session factory, async session factory, calendar cache,
process_lifetime_id — into the closure shape :func:`run_borrow_accrual_loop`
expects, then registers the resulting coroutine factory on the
supervisor under the name ``borrow_accrual``.

Pattern mirrors :func:`register_greeks_refresh_task` and
:func:`make_emergency_callback` — each piece of injected production
substrate is a small constructor that is independently unit-testable.

Two protocols the wiring depends on:

* The :class:`alphamind.execution.venue_configuration.calendar_cache.TradingCalendarCache`
  surface (``is_market_open(ts) -> bool``).
* :func:`build_borrow_cost_resolver` over a sync ``Session`` (returns
  ``Callable[[str], float | None]``).
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.borrow_accrual.task import (
    BorrowCostResolverFactory,
    TradingCalendar,
    run_borrow_accrual_loop,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.risk_guardrails.borrow_cost import build_borrow_cost_resolver

_US_EASTERN = ZoneInfo("US/Eastern")
# 12:00 ET is safely inside any regular market session; the calendar
# cache's ``is_market_open`` answers "is this a trading day" for a
# noon-of-day probe (avoids pre-open / after-hours windows).
_NOON = time(12, 0)


class CalendarCacheProtocol(Protocol):
    """Narrow probe surface this wiring needs from the production cache.

    The full :class:`alphamind.execution.venue_configuration.calendar_cache.TradingCalendarCache`
    carries more behavior; the borrow-accrual scheduler only needs to
    answer "is the supplied datetime inside a regular session" so a noon-ET
    probe can answer "is this a trading day".
    """

    def is_market_open(self, ts: datetime) -> bool: ...


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def register_borrow_accrual_task(
    supervisor: MonitorSupervisor,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    sync_session_factory: sessionmaker[Session],
    calendar_cache: CalendarCacheProtocol,
    process_lifetime_id: str,
) -> None:
    """Register the ``borrow_accrual`` task on *supervisor*.

    The closure binds:

    * a fresh resolver per tick via :func:`_resolver_factory` (so each
      tick observes the latest ``borrow_cost_daily`` state);
    * a :class:`TradingCalendar` adapter that delegates to
      ``calendar_cache.is_market_open`` (12:00 ET probe per day);
    * the supplied *process_lifetime_id* (the monitor process's
      ``process_lifetimes`` row foreign-keyed by every
      ``InvocationRow`` the tick inserts).
    """
    resolver_factory = _resolver_factory(sync_session_factory)
    calendar = _calendar_adapter(calendar_cache)

    async def _coro(monitor_session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        await run_borrow_accrual_loop(
            monitor_session,
            config,
            session_factory=session_factory,
            borrow_cost_resolver_factory=resolver_factory,
            process_lifetime_id=process_lifetime_id,
            calendar=calendar,
        )

    supervisor.register_task(name="borrow_accrual", coro_fn=_coro)


# ---------------------------------------------------------------------------
# Module-private composition helpers — exposed for unit testing
# ---------------------------------------------------------------------------


def _resolver_factory(
    sync_session_factory: sessionmaker[Session],
) -> BorrowCostResolverFactory:
    """Return a factory that builds a fresh borrow-cost resolver per call.

    :func:`build_borrow_cost_resolver` is a sync function that takes a
    sync ``Session`` and reads every ticker's latest non-NULL ``fee_pct``
    once. Calling the factory per tick rebuilds the dict, so a fee change
    in ``borrow_cost_daily`` between two ticks reaches the next tick.
    """

    def _factory() -> object:  # actually Callable[[str], float | None]
        with sync_session_factory() as sess:
            return build_borrow_cost_resolver(sess)

    return _factory  # type: ignore[return-value]


def _calendar_adapter(calendar_cache: CalendarCacheProtocol) -> TradingCalendar:
    """Adapt ``TradingCalendarCache`` to the narrow ``TradingCalendar`` Protocol.

    The probe uses 12:00 US/Eastern: well inside any regular session and
    well outside the pre-open / after-hours overhang where
    ``is_market_open`` could disagree with "is this a trading day".
    """

    class _Adapter:
        def is_trading_day(self, day: date) -> bool:
            noon_et = datetime.combine(day, _NOON, tzinfo=_US_EASTERN)
            return calendar_cache.is_market_open(noon_et)

    return _Adapter()


__all__ = [
    "CalendarCacheProtocol",
    "register_borrow_accrual_task",
]
