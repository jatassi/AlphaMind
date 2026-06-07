"""Supervisor-side wiring + production writer for the greeks-refresh task (story 03a).

Two pieces:

* :class:`SqlGreeksWriter` — concrete :class:`GreeksWriter` that opens a
  fresh ``AsyncSession`` per call and **upserts** the position's row in the
  single-writer (monitor-owned) ``position_greeks`` side table (ADR-0005 /
  story 04b). The greeks are the lone computed decoration; the refresh writes
  its *own* table keyed by ``position_id`` and **never** RMW's the
  pipeline-owned ``positions`` row — the second-writer pattern that generated
  ALP-824. One row per position, one transaction per call.

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
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId
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
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.records.positions import OptionGreeks
from alphamind.state.records_position_greeks import PositionGreeksRecord
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.position_greeks import PositionGreeksRow
from alphamind.state.tables.position_greeks_codec import (
    record_to_row as greeks_record_to_row,
)

log = logging.getLogger(__name__)

# Mirrors ``alphamind.scheduler.fill_collection_inputs._DEFAULT_RISK_FREE_RATE`` — the
# conservative mid-cycle scalar used when ``macro_observations`` has no
# DTB3 row (bootstrap path).
_DEFAULT_RISK_FREE_RATE = 0.045
_DTB3_SERIES_ID = "DTB3"


def _utcnow() -> datetime:
    """Refresh-instant fallback when the greeks carry no ``as_of_timestamp``.

    The failure path preserves a prior ``as_of_timestamp`` (possibly ``None``
    on a never-refreshed position); the side table's ``updated_at`` is
    non-nullable, so an unstamped greeks value falls back to the wall clock.
    """
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Concrete writer
# ---------------------------------------------------------------------------


class SqlGreeksWriter:
    """Concrete :class:`GreeksWriter` that upserts the ``position_greeks`` side table.

    Single-writer (monitor-owned), keyed by ``position_id`` (ADR-0005). Each
    call opens a fresh session, upserts the one greeks row for the position,
    and commits — **never** an RMW/UPDATE on the pipeline-owned ``positions``
    row. One row per call, one transaction per call — matches the fill-stream
    consumer's single-row-write contract.

    The side table carries one greeks row per position. For a single-leg
    options position that is its ``OptionGreeks``; for a multi-leg strategy
    that is the *aggregated* ``strategy_greeks`` (the per-leg breakdown is an
    in-memory recompute detail, not a persisted decoration — downstream risk
    reads the position-level aggregate). ``iv`` carries ``OptionGreeks.iv_used``
    and ``updated_at`` carries ``OptionGreeks.as_of_timestamp`` (the monitor's
    refresh instant), falling back to ``now`` when the greeks carry no stamp.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def update_options_greeks(self, *, position_id: str, greeks: OptionGreeks) -> None:
        await self._upsert(position_id=position_id, greeks=greeks)

    async def update_strategy_greeks(
        self,
        *,
        position_id: str,
        per_leg: dict[str, OptionGreeks],
        aggregated: OptionGreeks,
    ) -> None:
        # The side table holds one row per position: the strategy-level
        # aggregate. ``per_leg`` is an in-memory recompute detail with no
        # persisted home in the single-row-per-position side table.
        del per_leg
        await self._upsert(position_id=position_id, greeks=aggregated)

    async def _upsert(self, *, position_id: str, greeks: OptionGreeks) -> None:
        """Append-only-idempotent upsert: one ``INSERT ... ON CONFLICT DO UPDATE``.

        A single write statement with **no preceding read** in the same
        transaction — the write lock is taken directly, so ``PRAGMA busy_timeout``
        governs cross-writer contention rather than a deferred read snapshot whose
        later write-upgrade a concurrent committer could race into
        ``SQLITE_BUSY_SNAPSHOT`` (ADR-0005). Idempotent on ``position_id`` (PK),
        so re-running a cycle overwrites the row in place.
        """
        record = PositionGreeksRecord(
            position_id=PositionId(position_id),
            delta=greeks.delta,
            gamma=greeks.gamma,
            theta=greeks.theta,
            vega=greeks.vega,
            iv=greeks.iv_used,
            updated_at=greeks.as_of_timestamp or _utcnow(),
        )
        row = greeks_record_to_row(record)
        values = {
            "position_id": row.position_id,
            "delta": row.delta,
            "gamma": row.gamma,
            "theta": row.theta,
            "vega": row.vega,
            "iv": row.iv,
            "updated_at": row.updated_at,
        }
        stmt = sqlite_insert(PositionGreeksRow).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[PositionGreeksRow.position_id],
            set_={k: v for k, v in values.items() if k != "position_id"},
        )
        async with self._session_factory() as sess:
            try:
                await sess.execute(stmt)
                await sess.commit()
            except IntegrityError as exc:
                # The DEFERRED ``position_id`` FK to ``positions`` fires at COMMIT
                # when the position does not exist; translate to the writer's
                # typed "no such position" contract.
                await sess.rollback()
                msg = f"no such position: {position_id!r}"
                raise LookupError(msg) from exc


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
    from alphamind.state.invocation_context.activity_log import (
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
    market_open: MarketOpenPredicate,
) -> None:
    """Register the ``greeks_refresh`` task on *supervisor*.

    Constructs the production wiring for every injected callable the
    run-forever entry point consumes. The cache is shared with the
    underlying-stream task (story 02b) so spots flow lock-free; the
    session_factory is shared with the fill-stream consumer (story 02c)
    and the breach loop / cascade dispatcher. ``market_open`` is the
    monitor-process-wide :class:`TradingCalendarCache`'s ``is_market_open``
    so the refresh task and the breach loop pause on the same calendar
    (per ALP-453's market-hours consolidation).
    """
    writer = SqlGreeksWriter(session_factory)
    iv_fetch = make_iv_fetcher(session_factory)
    risk_free_rate_provider = make_risk_free_rate_provider(session_factory)
    invocation_id_provider = make_invocation_id_provider(session_factory)
    activity_log = make_activity_log_emitter(session_factory)

    async def _coro(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        cadence = float(config.greeks_refresh_inspection_cadence_seconds)
        await run_greeks_refresh(
            session,
            config,
            loop=lambda: supervisor.supervised_loop("greeks_refresh", cadence),
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
    "make_risk_free_rate_provider",
    "register_greeks_refresh_task",
]
