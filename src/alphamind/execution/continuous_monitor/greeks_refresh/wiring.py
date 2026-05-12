"""Supervisor-side wiring + production writer for the greeks-refresh task (story 03a).

Two pieces:

* :class:`SqlGreeksWriter` — concrete :class:`GreeksWriter` that opens a
  fresh ``AsyncSession`` per call, loads the position row, projects a
  fresh :class:`PositionRecord` with the new greeks, and commits. Mirrors
  ``state_persistence.write_paths.phase1._persist_position_update``'s
  shape but does NOT require an open ``InvocationContext`` — the monitor
  runs across invocations.

* :func:`register_greeks_refresh_task` — entry-point wiring helper that
  the :file:`__main__.py` calls during daemon setup. Constructs the
  production IV-fetch callable (via the broker-adapter's
  ``submit_with_retry`` envelope), a default risk-free-rate provider
  (reads ``macro_observations`` for DTB3 with the same fallback the
  Phase 1 input bundle uses), and the production activity-log emitter.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.broker_adapter.retry import Submitted, submit_with_retry
from alphamind.execution.continuous_monitor.greeks_refresh.iv_provider import (
    IVQuote,
    fetch_iv_quotes_batch,
)
from alphamind.execution.continuous_monitor.greeks_refresh.task import (
    ActivityLogEmitter,
    InvocationIdProvider,
    IVFetcher,
    MarketOpenPredicate,
    RiskFreeRateProvider,
    run_greeks_refresh,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
)
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.positions import (
    OptionGreeks,
    OptionsPositionDetails,
    StrategyLeg,
    StrategyPositionDetails,
)

log = logging.getLogger(__name__)

# Mirrors ``alphamind.scheduler.phase1_inputs._DEFAULT_RISK_FREE_RATE`` — the
# conservative mid-cycle scalar used when ``macro_observations`` has no
# DTB3 row (bootstrap path).
_DEFAULT_RISK_FREE_RATE = 0.045
_DTB3_SERIES_ID = "DTB3"


# ---------------------------------------------------------------------------
# Concrete writer
# ---------------------------------------------------------------------------


class SqlGreeksWriter:
    """Concrete :class:`GreeksWriter` backed by ``AsyncSession`` writes.

    Each call opens a fresh session, loads the position row, projects the
    typed ``PositionRecord``, mutates only the greeks field on the
    ``details`` payload, and rewrites the row's ``details_json``. One row
    per call, one transaction per call — matches the fill-stream consumer's
    single-row-write contract.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def update_options_greeks(self, *, position_id: str, greeks: OptionGreeks) -> None:
        async with self._session_factory() as sess:
            row = await self._load_row(sess, position_id)
            record = position_row_to_record(row)
            details = record.details
            if not isinstance(details, OptionsPositionDetails):
                msg = (
                    f"position_id={position_id!r} is not an OptionsPositionDetails "
                    f"row; got {type(details).__name__}"
                )
                raise TypeError(msg)
            new_details = details.model_copy(update={"greeks": greeks})
            new_record = record.model_copy(update={"details": new_details})
            new_row = position_record_to_row(new_record)
            row.details_json = new_row.details_json
            await sess.commit()

    async def update_strategy_greeks(
        self,
        *,
        position_id: str,
        per_leg: dict[str, OptionGreeks],
        aggregated: OptionGreeks,
    ) -> None:
        async with self._session_factory() as sess:
            row = await self._load_row(sess, position_id)
            record = position_row_to_record(row)
            details = record.details
            if not isinstance(details, StrategyPositionDetails):
                msg = (
                    f"position_id={position_id!r} is not a StrategyPositionDetails "
                    f"row; got {type(details).__name__}"
                )
                raise TypeError(msg)
            new_legs: list[StrategyLeg] = []
            for leg in details.legs:
                leg_greeks = per_leg.get(leg.leg_id, leg.options.greeks)
                new_options = leg.options.model_copy(update={"greeks": leg_greeks})
                new_legs.append(leg.model_copy(update={"options": new_options}))
            new_details = details.model_copy(
                update={
                    "legs": tuple(new_legs),
                    "strategy_greeks": aggregated,
                }
            )
            new_record = record.model_copy(update={"details": new_details})
            new_row = position_record_to_row(new_record)
            row.details_json = new_row.details_json
            await sess.commit()

    async def _load_row(self, sess: AsyncSession, position_id: str) -> PositionRow:
        result = await sess.execute(
            select(PositionRow).where(PositionRow.position_id == position_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            msg = f"no such position: {position_id!r}"
            raise LookupError(msg)
        return row


# ---------------------------------------------------------------------------
# Production iv-fetch envelope (with retry)
# ---------------------------------------------------------------------------


def make_iv_fetcher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retry_window_seconds: float = 5.0,
) -> IVFetcher:
    """Return an :class:`IVFetcher` that wraps the SQL batch query with
    ``submit_with_retry``.

    The broker-adapter's retry helper handles transient SQLite locks /
    aiosqlite hiccups inside the cycle; the kernel observes one
    ``IVFetcher`` call as the exhausted-retry envelope and treats failures
    accordingly. The retry window defaults to 5 seconds — the kernel must
    finish within the inspection-cadence budget, and 5s leaves headroom
    for the per-position computation.
    """

    async def _fetch(occ_symbols: Iterable[str]) -> dict[str, IVQuote]:
        async def _one_attempt() -> dict[str, IVQuote]:
            async with session_factory() as sess:
                return await fetch_iv_quotes_batch(sess, occ_symbols=occ_symbols)

        outcome = await submit_with_retry(_one_attempt, window_seconds=retry_window_seconds)
        if isinstance(outcome, Submitted):
            return outcome.payload
        # GatewaySubmissionFailed — surface as an exception the kernel catches.
        msg = (
            f"iv_fetch exhausted retries: reason={outcome.reason!r}, "
            f"last_error={outcome.last_error_class!r}"
        )
        raise RuntimeError(msg)

    return _fetch


# ---------------------------------------------------------------------------
# Production risk-free rate + invocation-id + market-hours providers
# ---------------------------------------------------------------------------


def make_risk_free_rate_provider(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cache_ttl_seconds: float = 300.0,
) -> RiskFreeRateProvider:
    """Return an async callable that reads the latest DTB3 observation per cycle.

    The Phase 1 path reads the same series with the same fallback; the
    monitor mirrors the convention so paper-mode and live-mode greeks
    refresh use the same rate the validation greeks were computed with.

    The provider is **async** because every call site runs inside the
    supervisor's event loop — synchronous ``asyncio.run`` bridging would
    deadlock-fall-through to a hard-coded fallback on every call. A small
    in-memory TTL cache amortizes the SQL read across multiple cycles per
    inspection cadence; ``DTB3`` rates change once per business day, so a
    5-minute cache (default) trades zero observable staleness for the
    avoided DB hit budget.
    """
    from alphamind.persistence.models import MacroObservations

    cached: dict[str, float | None] = {"value": None}
    last_read_at: list[float] = [0.0]

    async def _read_db() -> float:
        async with session_factory() as sess:
            stmt = (
                select(MacroObservations.value)
                .where(
                    MacroObservations.series_id == _DTB3_SERIES_ID,
                    MacroObservations.value.is_not(None),
                )
                .order_by(MacroObservations.observation_date.desc())
                .limit(1)
            )
            result = await sess.execute(stmt)
            value = result.scalar_one_or_none()
            if value is None:
                return _DEFAULT_RISK_FREE_RATE
            return float(value) / 100.0

    async def _provider() -> float:
        import time

        now = time.monotonic()
        cached_value = cached["value"]
        if cached_value is not None and (now - last_read_at[0]) < cache_ttl_seconds:
            return cached_value
        value = await _read_db()
        cached["value"] = value
        last_read_at[0] = now
        return value

    return _provider


def make_invocation_id_provider(
    session_factory: async_sessionmaker[AsyncSession],
) -> InvocationIdProvider:
    """Return an async callable that resolves the latest invocation_id.

    The monitor writes activity-log entries while running across
    invocations; the ``activity_log.invocation_id`` FK to ``invocations``
    requires the row to reference a real invocation. The convention
    (matching the emergency-trigger pattern in
    ``risk_guardrails.breach_behavior.emergency_triggers``) is to use the
    most-recently-started invocation_id at emission time.

    The provider is **async** because every call site runs inside the
    supervisor's event loop — synchronous ``asyncio.run`` bridging would
    deadlock-fall-through to a hard-coded sentinel on every call, which
    would in turn fail the ``activity_log.invocation_id`` FK at COMMIT
    time. On a fresh DB with no invocations yet, the provider returns
    the fallback string ``"monitor-bootstrap"``; activity-log entries
    written with this sentinel will fail the FK check and surface during
    the e2e verification — that's the correct failure mode (no invocation
    has run yet).
    """

    async def _provider() -> str:
        async with session_factory() as sess:
            stmt = (
                select(InvocationRow.invocation_id).order_by(InvocationRow.start_at.desc()).limit(1)
            )
            result = await sess.execute(stmt)
            value = result.scalar_one_or_none()
            if value is None:
                return "monitor-bootstrap"
            return str(value)

    return _provider


def make_market_open_predicate() -> MarketOpenPredicate:
    """Return a callable that returns ``True`` during the US regular session.

    Per ``docs/design/05-execution-layer/architecture.md`` § 4d, the refresh
    task pauses outside market hours. Story 03a ships a lightweight ET-clock
    predicate (9:30-16:00 ET weekdays) rather than the full
    ``TradingCalendarCache`` because:

    * The cache requires an ``AccountStateQueries`` instance, which pulls
      Alpaca's ``/v2/calendar`` — coupling the refresh task to broker
      connectivity for what is fundamentally a clock decision.
    * On a US market holiday (e.g., NYSE closed), the predicate returns
      ``True`` but the refresh attempts fail safely: the
      ``options_contract_snapshots`` table has no new rows that day, so
      every position routes through the ``iv_fetch_no_row`` failure path,
      preserves prior greeks, and emits a routine activity-log entry.

    Future iteration can swap this for the cache-backed predicate when the
    monitor lifecycle owns its own ``AccountStateQueries`` reference;
    story 03a stays narrowly scoped.
    """
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    open_time = (9, 30)
    close_time = (16, 0)

    def _open(at: datetime) -> bool:
        if at.tzinfo is None:
            return False
        local = at.astimezone(et)
        if local.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        minutes = local.hour * 60 + local.minute
        open_min = open_time[0] * 60 + open_time[1]
        close_min = close_time[0] * 60 + close_time[1]
        return open_min <= minutes < close_min

    return _open


# ---------------------------------------------------------------------------
# Activity-log emitter — opens a fresh session per emit and commits
# ---------------------------------------------------------------------------


def make_activity_log_emitter(
    session_factory: async_sessionmaker[AsyncSession],
) -> ActivityLogEmitter:
    """Return a callable that writes one ``ActivityLogEntry`` per call.

    Re-uses the activity-log row codec via a thin per-emit transaction. The
    refresh task emits one entry per failing position; one transaction per
    emit avoids long-held locks during a chain of failures.
    """
    from alphamind.execution.state_persistence.invocation_context.activity_log import (
        activity_log_entry_to_row,
    )

    async def _emit(entry: ActivityLogEntry) -> None:
        async with session_factory() as sess:
            sess.add(activity_log_entry_to_row(entry))
            await sess.commit()

    return _emit


# ---------------------------------------------------------------------------
# Supervisor registration
# ---------------------------------------------------------------------------


def register_greeks_refresh_task(
    supervisor: MonitorSupervisor,
    *,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Register the ``greeks_refresh`` task on *supervisor*.

    Constructs the production wiring for every injected callable the
    run-forever entry point consumes. The cache is shared with the
    underlying-stream task (story 02b) so spots flow lock-free; the
    session_factory is shared with the fill-stream consumer (story 02c)
    and the future breach loop / cascade dispatcher.
    """
    writer = SqlGreeksWriter(session_factory)
    iv_fetch = make_iv_fetcher(session_factory)
    risk_free_rate_provider = make_risk_free_rate_provider(session_factory)
    invocation_id_provider = make_invocation_id_provider(session_factory)
    market_open = make_market_open_predicate()
    activity_log = make_activity_log_emitter(session_factory)

    async def _coro(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        await run_greeks_refresh(
            session,
            config,
            repository=repository,
            cache=cache,
            iv_fetch=iv_fetch,
            writer=writer,
            activity_log=activity_log,
            risk_free_rate_provider=risk_free_rate_provider,
            invocation_id_provider=invocation_id_provider,
            market_open=market_open,
        )

    supervisor.register_task(name="greeks_refresh", coro_fn=_coro)


__all__ = [
    "SqlGreeksWriter",
    "make_activity_log_emitter",
    "make_invocation_id_provider",
    "make_iv_fetcher",
    "make_market_open_predicate",
    "make_risk_free_rate_provider",
    "register_greeks_refresh_task",
]
