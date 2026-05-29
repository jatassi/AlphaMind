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
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Protocol
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
    EquityPositionDetails,
    PositionRecord,
    is_open_short_equity,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_to_row,
)
from alphamind.state.invocation_id import mint_invocation_id
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

# ALP-715 review F3 — staleness ceiling for the per-ticker closing print.
# Mirrors ``scheduler.phase1_inputs._MAX_REFERENCE_BAR_AGE_SECONDS`` (7 calendar
# days). The longest US market-holiday weekend is ~4 days; 7 days clears
# that yet still drops a ticker whose OHLCV feed has genuinely stalled
# (data-source outage, ticker delisted). A dropped ticker then becomes
# absent from the close-price map and the kernel's existing "missing
# closing print" ``ValueError`` fires — partial accrual would silently
# under-report P/L drag, so fail-fast is the correct posture.
_MAX_EOD_BAR_AGE_SECONDS: Final[int] = 7 * 24 * 60 * 60


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
    adapts ``TradingCalendarCache`` directly: ``is_trading_day`` answers
    "does *day* host a regular session" (early-close days qualify, just
    like the rest of the system treats them) and ``next_session_day_after``
    returns the date of the first session strictly after *day*. Both reads
    consult Alpaca's holiday calendar, so the scheduler does not need its
    own noon-ET market-hours probe loop.
    """

    def is_trading_day(self, day: date) -> bool: ...

    def next_session_day_after(self, day: date) -> date: ...


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
    # across ticks (per the design's *Trigger* section). The factory opens a
    # synchronous ``Session`` and runs the multi-table ``build_borrow_cost_resolver``
    # query; hand it to ``asyncio.to_thread`` so the event loop keeps
    # progressing (breach_loop / fill_stream_consumer / greeks_refresh) while
    # the borrow-cost query runs.
    resolver = await asyncio.to_thread(borrow_cost_resolver_factory)
    invocation_id = mint_invocation_id(now)

    async with session_factory() as session:
        positions = await _read_all_positions(session)
        in_scope = tuple(p for p in positions if is_open_short_equity(p))

        close_prices = await _read_latest_closes(
            session,
            tickers=tuple(_ticker_of(p) for p in in_scope),
            as_of=now,
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
    session: AsyncSession,
    *,
    tickers: tuple[Symbol, ...],
    as_of: datetime,
) -> dict[Symbol, float]:
    """Read the latest ``ohlcv_bars`` close (``timeframe='1d'``) per ticker.

    Returns a dict only for tickers whose latest bar exists *and* is fresher
    than ``_MAX_EOD_BAR_AGE_SECONDS`` relative to ``as_of``; the kernel
    raises if any in-scope ticker is missing. ``unadj_close`` is the
    actual traded price (matches the convention
    ``scheduler.phase1_inputs.read_universe_eod_close_map`` uses).

    Staleness bound (ALP-715 review F3): ``period_start`` is held to within
    seven calendar days of ``as_of`` so a ticker whose OHLCV feed has
    silently stopped (delisting, data-source outage) does not produce a
    stale-but-present accrual. Dropping the ticker here surfaces the
    kernel's existing "missing closing print" ``ValueError`` instead.
    The bound is applied via lexicographic ISO-8601 comparison on the
    ``period_start`` string, matching the pattern in
    ``scheduler.phase1_inputs.read_universe_eod_close_map``.
    """
    if not tickers:
        return {}
    ticker_strs = tuple(str(t) for t in tickers)
    cutoff_iso = (
        (as_of - timedelta(seconds=_MAX_EOD_BAR_AGE_SECONDS)).replace(microsecond=0).isoformat()
    )
    latest_subq = (
        select(
            OhlcvBars.ticker.label("ticker"),
            func.max(OhlcvBars.period_start).label("latest"),
        )
        .where(
            OhlcvBars.timeframe == _OHLCV_DAILY_TIMEFRAME,
            OhlcvBars.ticker.in_(ticker_strs),
            OhlcvBars.period_start >= cutoff_iso,
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
        .where(
            OhlcvBars.timeframe == _OHLCV_DAILY_TIMEFRAME,
            OhlcvBars.period_start >= cutoff_iso,
        )
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

    Computed in US/Eastern. If today is a trading day and today's
    ``tick_local_time`` is still in the future → fire today. Otherwise
    defer to :meth:`TradingCalendar.next_session_day_after` for the next
    session date. The cache handles weekends, Alpaca holiday entries, and
    rare multi-day closures uniformly — we do not run our own noon-ET
    market-hours probe loop here.
    """
    current_et = current.astimezone(_US_EASTERN)
    today_et = current_et.date()
    today_tick = datetime.combine(today_et, tick_local_time, tzinfo=_US_EASTERN)
    if today_tick > current_et and calendar.is_trading_day(today_et):
        return today_tick.astimezone(UTC)
    next_day = calendar.next_session_day_after(today_et)
    next_tick = datetime.combine(next_day, tick_local_time, tzinfo=_US_EASTERN)
    return next_tick.astimezone(UTC)


__all__ = [
    "BorrowCostResolverFactory",
    "TradingCalendar",
    "run_accrual_tick",
    "run_borrow_accrual_loop",
]


# Suppress unused-import warning for ``ActivityLogRow``; the codec import
# keeps the symbol live for type-checker visibility.
_ = ActivityLogRow
