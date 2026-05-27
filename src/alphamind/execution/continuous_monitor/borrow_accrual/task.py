"""Asyncio shell for the borrow-accrual tick (ALP-719).

Wraps the pure :func:`compute_tick` kernel in the imperative shell:

* :func:`run_accrual_tick` — runs one tick atomically inside a single
  ``AsyncSession`` transaction. Reads positions, reads close prices,
  resolves fees, calls the kernel, persists the result, commits.
* :func:`run_borrow_accrual_loop` — long-running task the supervisor
  registers. Sleeps until the next trading-day's configured local time
  in US/Eastern, calls :func:`run_accrual_tick`, repeats.

All transactional work commits in ONE :class:`AsyncSession.commit` call
per tick: insert the per-tick ``InvocationRow`` (``trigger_type="scheduled"``,
``trigger_source="borrow_accrual"``), UPDATE every in-scope position's row,
INSERT every per-position ``BORROW_COST_ACCRUED`` activity-log row. The
``activity_log.invocation_id`` FK to ``invocations`` is satisfied because
SQLAlchemy's flush orders dependent INSERTs after their parents.

A miss on any closing print or any fee rate raises :class:`ValueError`;
the session context exits via the exception path and rolls back. The
exception propagates to the supervisor's TaskGroup; NSSM restarts the
process. Per the design's *Failure semantics* note, the missed tick's
accrual stays lost — fail-fast by intent.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import Symbol, make_symbol
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.borrow_accrual.recompute import (
    AccrualTickResult,
    compute_tick,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.persistence.models import OhlcvBars
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    InstrumentType,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_to_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

log = logging.getLogger(__name__)

# Per the design doc, the per-tick wall clock is US/Eastern.
_US_EASTERN = ZoneInfo("US/Eastern")

# Same daily-timeframe key the rest of the system reads from ``ohlcv_bars``
# (see ``scheduler/phase1_inputs.py``). The accrual tick wants the most
# recent EOD bar; intraday timeframes are not consulted.
_OHLCV_DAILY_TIMEFRAME = "1d"


BorrowCostResolverFactory = Callable[[], Callable[[str], float | None]]
"""Sync factory the shell calls once per tick to build a fresh resolver.

Production wiring closes over a sync ``Session`` factory and runs
``build_borrow_cost_resolver`` (see :mod:`alphamind.risk_guardrails.borrow_cost`).
Tests pass a closure returning a dict-backed lookup.
"""

NowProvider = Callable[[], datetime]
SleepCallable = Callable[[float], Awaitable[None]]
MarketOpenPredicate = Callable[[datetime], bool]


class TradingCalendar(Protocol):
    """Narrow surface the daily timer needs from the calendar cache.

    A *trading day* is a Mon-Fri non-holiday session day. The wiring layer
    adapts the monitor-wide ``TradingCalendarCache.is_market_open`` predicate
    into this protocol by closure-binding a representative session-noon
    datetime per probed date.
    """

    def is_trading_day(self, day: date) -> bool: ...


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def run_accrual_tick(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    borrow_cost_resolver_factory: BorrowCostResolverFactory,
    process_lifetime_id: str,
    now: datetime,
) -> AccrualTickResult:
    """Run one borrow-accrual tick atomically.

    All five steps from the design's *Per-tick action steps*:

    1. Open one ``AsyncSession``.
    2. Read OPEN SHORT EQUITY positions, latest close per ticker, fees.
    3. Compose the per-tick :class:`InvocationRow` and add it to the session.
    4. Compute the kernel result; abort the transaction on any failure.
    5. Persist updated positions + activity-log entries; commit once.

    Returns the :class:`AccrualTickResult` so the long-running loop can
    log a per-tick summary line.
    """
    # The resolver factory builds one fresh resolver per tick — captures
    # the current ``borrow_cost_daily`` state without caching stale rates
    # across ticks (per the design's *Trigger* section).
    resolver = borrow_cost_resolver_factory()
    invocation_id = _mint_borrow_accrual_invocation_id(now)

    async with session_factory() as session:
        positions = await _read_all_positions(session)
        in_scope = tuple(p for p in positions if _is_open_short_equity(p))

        close_prices = await _read_latest_closes(
            session, tickers=tuple(_ticker_of(p) for p in in_scope)
        )
        fee_rates: dict[Symbol, float | None] = {
            _ticker_of(p): resolver(str(_ticker_of(p))) for p in in_scope
        }

        # Insert the InvocationRow first so the activity-log FK to
        # ``invocations`` is satisfied at flush time. The row is part of
        # the same transaction — a kernel raise rolls it back together
        # with any partial position updates.
        session.add(
            _build_invocation_row(
                invocation_id=invocation_id,
                process_lifetime_id=process_lifetime_id,
                now=now,
            )
        )

        # The kernel raises ValueError on first missing close / fee; the
        # exception propagates out of the context manager and SQLAlchemy
        # rolls back the session.
        result = compute_tick(
            positions=positions,
            close_prices=close_prices,
            fee_rates=fee_rates,
            now=now,
            invocation_id=invocation_id,
        )

        for updated in result.updated_positions:
            await _persist_updated_position(session, updated)
        for entry in result.activity_log_entries:
            session.add(activity_log_entry_to_row(entry))

        await session.commit()

    log.info(
        "borrow_accrual tick committed: invocation_id=%s positions=%d total_accrued_usd=%.4f",
        invocation_id,
        len(result.updated_positions),
        result.total_accrued_usd,
    )
    return result


async def run_borrow_accrual_loop(
    session: MonitorSession,
    config: ContinuousMonitorConfig,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    borrow_cost_resolver_factory: BorrowCostResolverFactory,
    process_lifetime_id: str,
    calendar: TradingCalendar,
    now: NowProvider = lambda: datetime.now(UTC),
    sleep: SleepCallable = asyncio.sleep,
) -> None:
    """Long-running task: sleep to next trading-day tick, fire, repeat.

    The loop body wraps :func:`run_accrual_tick` in the canonical
    monitor-task fail-fast envelope:

    * ``asyncio.CancelledError`` propagates so the supervisor's TaskGroup
      can complete shutdown.
    * Any other exception — most often a resolver miss or missing close
      print — propagates to the supervisor; NSSM restarts the process.

    Per the design's *Failure semantics*: the missed tick's accrual stays
    lost; operators repair the data source and the next trading-day tick
    catches up.
    """
    del session  # session identity flows through process_lifetime_id closure
    tick_time = _parse_local_time(config.borrow_accrual_tick_local_time)
    while True:
        current = now()
        next_fire_utc = _next_tick_utc(
            current=current,
            tick_local_time=tick_time,
            calendar=calendar,
        )
        delay = (next_fire_utc - current).total_seconds()
        if delay > 0.0:
            await sleep(delay)
        # ``current`` after the sleep — preserve precise wall-clock semantics
        # for the activity-log entry's accrual_date.
        await run_accrual_tick(
            session_factory=session_factory,
            borrow_cost_resolver_factory=borrow_cost_resolver_factory,
            process_lifetime_id=process_lifetime_id,
            now=now(),
        )


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _is_open_short_equity(position: PositionRecord) -> bool:
    return (
        position.status == PositionStatus.OPEN
        and position.direction == Direction.SHORT
        and position.instrument_type == InstrumentType.EQUITY
    )


def _ticker_of(position: PositionRecord) -> Symbol:
    assert isinstance(position.details, EquityPositionDetails)  # narrowed by predicate
    return position.details.ticker


async def _read_all_positions(session: AsyncSession) -> tuple[PositionRecord, ...]:
    """Read every position from the ``positions`` table.

    The kernel filters in-scope (OPEN SHORT EQUITY); the shell-side read
    is unfiltered so the typed predicate stays a single source of truth.
    The row count under live operation is bounded by the portfolio's open
    book — typically tens, not thousands.
    """
    stmt = select(PositionRow).order_by(PositionRow.position_id.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return tuple(position_row_to_record(row) for row in rows)


async def _read_latest_closes(
    session: AsyncSession, *, tickers: tuple[Symbol, ...]
) -> dict[Symbol, float]:
    """Read the latest ``ohlcv_bars`` close (``timeframe='1d'``) per ticker.

    Returns a dict only for tickers whose latest bar exists; the kernel
    raises if any in-scope ticker is missing. ``unadj_close`` is the
    actual traded price (matches the convention
    ``scheduler.phase1_inputs.read_universe_eod_close_map`` uses).
    """
    if not tickers:
        return {}
    ticker_strs = tuple(str(t) for t in tickers)
    latest_subq = (
        select(
            OhlcvBars.ticker.label("ticker"),
            func.max(OhlcvBars.period_start).label("latest"),
        )
        .where(
            OhlcvBars.timeframe == _OHLCV_DAILY_TIMEFRAME,
            OhlcvBars.ticker.in_(ticker_strs),
        )
        .group_by(OhlcvBars.ticker)
        .subquery()
    )
    stmt = (
        select(OhlcvBars.ticker, OhlcvBars.unadj_close)
        .join(
            latest_subq,
            (OhlcvBars.ticker == latest_subq.c.ticker)
            & (OhlcvBars.period_start == latest_subq.c.latest),
        )
        .where(OhlcvBars.timeframe == _OHLCV_DAILY_TIMEFRAME)
    )
    rows = (await session.execute(stmt)).all()
    return {make_symbol(str(ticker)): float(close) for ticker, close in rows}


async def _persist_updated_position(session: AsyncSession, updated: PositionRecord) -> None:
    """UPDATE one position row via the codec round-trip.

    Mirrors ``SqlGreeksWriter._load_row`` shape — fetch the row by id,
    rewrite ``details_json`` from the new typed record. Identity columns
    (status, direction, instrument_type, history) are unchanged by the
    kernel for an OPEN SHORT EQUITY tick; the codec recomputes them all
    so a future kernel change still round-trips cleanly.
    """
    row = (
        await session.execute(
            select(PositionRow).where(PositionRow.position_id == updated.position_id)
        )
    ).scalar_one()
    new_row = position_record_to_row(updated)
    row.details_json = new_row.details_json
    # No other field changes for this tick, but defensive about future kernel
    # extensions writing to ``realized_pnl_to_date_usd`` etc.
    row.realized_pnl_to_date_usd = new_row.realized_pnl_to_date_usd


def _mint_borrow_accrual_invocation_id(now: datetime) -> str:
    """Mint the per-tick invocation id (``inv-YYYYMMDDTHHMMSSZ-<8hex>``).

    Same shape :func:`alphamind.scheduler.invocation._mint_invocation_id`
    uses for the pipeline; the borrow-accrual tick is just another
    invocation flavour as far as the table is concerned.
    """
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"inv-{stamp}-{secrets.token_hex(4)}"


def _build_invocation_row(
    *,
    invocation_id: str,
    process_lifetime_id: str,
    now: datetime,
) -> InvocationRow:
    """Compose the minimal :class:`InvocationRow` the borrow-accrual tick stamps.

    Only execution-scaffolding columns are populated; the snapshot /
    composition columns get inert sentinels (``""`` or ``"{}"``) because
    the tick does not run an agent pipeline and has no resolved-config
    snapshot to point at. Phase 1 / Phase 2 completion stay NULL — they
    have no analogue for this row's single-shot lifecycle.
    """
    iso = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id=process_lifetime_id,
        start_at=iso,
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="borrow_accrual",
        trigger_reason="continuous_monitor_borrow_accrual_tick",
        git_sha_at_invocation="",
        active_profile="",
        active_regime="",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="",
        resolved_config_snapshot_path="",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _parse_local_time(value: str) -> time:
    """Parse the ``HH:MM`` config string into a :class:`datetime.time`."""
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _next_tick_utc(
    *,
    current: datetime,
    tick_local_time: time,
    calendar: TradingCalendar,
) -> datetime:
    """Return the next-fire wall clock in UTC.

    Computed in US/Eastern: today's ``tick_local_time`` if it is still in
    the future AND today is a trading day; otherwise the next trading
    day's ``tick_local_time``. Up to 14 forward iterations cover any
    realistic weekend + holiday run.
    """
    current_et = current.astimezone(_US_EASTERN)
    candidate_day = current_et.date()
    candidate_local = datetime.combine(candidate_day, tick_local_time, tzinfo=_US_EASTERN)
    if candidate_local <= current_et or not calendar.is_trading_day(candidate_day):
        candidate_day += timedelta(days=1)
        candidate_local = datetime.combine(candidate_day, tick_local_time, tzinfo=_US_EASTERN)

    # Advance one day at a time until a trading day lands. Bounded loop —
    # the longest US market holiday gap is ~4 days (e.g., Thanksgiving),
    # so 14 iterations is comfortably above the worst case.
    for _ in range(14):
        if calendar.is_trading_day(candidate_local.date()):
            return candidate_local.astimezone(UTC)
        candidate_day += timedelta(days=1)
        candidate_local = datetime.combine(candidate_day, tick_local_time, tzinfo=_US_EASTERN)
    msg = (
        f"borrow-accrual: could not find a trading day within 14 days of "
        f"{current_et.date().isoformat()}; trading calendar may be broken"
    )
    raise RuntimeError(msg)


__all__ = [
    "BorrowCostResolverFactory",
    "TradingCalendar",
    "run_accrual_tick",
    "run_borrow_accrual_loop",
]


# Suppress unused-import warning for ``ActivityLogRow``; the codec import
# keeps the symbol live for type-checker visibility.
_ = ActivityLogRow
