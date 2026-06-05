"""Scheduler-layer borrow-accrual accrual, relocated from the always-on monitor (ALP-855 / W4a).

Daily SHORT-equity borrow cost is **accounting**, which ADR-0004 evicts from the
always-on continuous monitor into scheduled / pipeline-cadence work, and ADR-0005
makes the pipeline the single writer of the projection (positions / cash). This
module is that relocation: :func:`run_borrow_accrual` runs inside the
orchestrator's Phase-1 write transaction — reusing the invocation's open session
and ``invocation_id`` — alongside fill integration and the account-activities
poll. The pure :func:`compute_tick` kernel is unchanged; the once-per-trading-day
timer machinery (``run_borrow_accrual_loop`` / ``_next_tick_utc`` / the per-tick
``InvocationRow`` mint) is gone — the pipeline already owns the invocation row and
fires on its own cadence.

Idempotency. The pipeline runs ~3× per trading day, but borrow accrual is a daily
quantity. :func:`run_borrow_accrual` is therefore guarded: it books the accrual at
most once per US/Eastern trading day, keyed on the ``BORROW_COST_ACCRUED``
activity-log rows already booked for that ``accrual_date``. A second same-day
invocation is a no-op; the first invocation on a new trading day books the day's
accrual.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind._kernel.ids import Symbol, make_symbol
from alphamind.scheduler.borrow_accrual_kernel import (
    AccrualTickResult,
    compute_tick,
)
from alphamind.persistence.models import OhlcvBars
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    PositionRecord,
    is_open_short_equity,
)
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_to_row,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

log = logging.getLogger(__name__)

# The per-accrual wall clock is US/Eastern — the accrual covers one trading day,
# and the idempotency key is that day's ET date (mirrors the kernel's
# ``BorrowCostAccruedDetail.accrual_date``).
_US_EASTERN = ZoneInfo("US/Eastern")

# Same daily-timeframe key the rest of the system reads from ``ohlcv_bars``.
_OHLCV_DAILY_TIMEFRAME = "1d"

# Staleness ceiling for the per-ticker closing print (mirrors the prior monitor
# task / ``scheduler.phase1_inputs._MAX_REFERENCE_BAR_AGE_SECONDS``): 7 calendar
# days clears the longest US market-holiday weekend yet still drops a ticker whose
# OHLCV feed has genuinely stalled, surfacing the kernel's "missing closing print"
# ValueError instead of a stale-but-present accrual.
_MAX_EOD_BAR_AGE_SECONDS = 7 * 24 * 60 * 60


BorrowCostResolver = Callable[[str], float | None]
"""The latest-fee-per-ticker lookup the pipeline already builds.

``scheduler/orchestrator.py`` builds this once per invocation via
``risk_guardrails.borrow_cost.build_borrow_cost_resolver`` and reuses it across
the decision pipeline; the accrual consumes the same mapping (no per-tick
resolver factory / off-loop thread bridge — that was monitor-task machinery).
"""


async def run_borrow_accrual(
    handle: InvocationHandle,
    *,
    borrow_cost_resolver: BorrowCostResolver,
    now: datetime,
) -> AccrualTickResult:
    """Book the daily SHORT-equity borrow accrual inside the Phase-1 write unit.

    Reuses *handle*'s open session and ``invocation_id`` — the pipeline already
    owns the ``invocations`` row, so this does **not** mint one. The once-per-
    trading-day guard (:func:`_already_accrued_today`) makes a second same-day
    invocation a no-op; otherwise it reads OPEN SHORT EQUITY positions + latest
    closes + live fees, calls the pure :func:`compute_tick` kernel, UPDATEs each
    in-scope position's row, and INSERTs one ``BORROW_COST_ACCRUED`` activity-log
    row per position — all inside *handle*'s transaction (single writer =
    pipeline). The caller commits.

    A miss on any closing print or any fee rate raises :class:`ValueError` from
    the kernel; it propagates to abort the surrounding write unit (a real
    inconsistency, not a transient).
    """
    session = handle.session
    accrual_date_et = now.astimezone(_US_EASTERN).date().isoformat()
    if await _already_accrued_today(session, accrual_date_iso=accrual_date_et):
        log.info("borrow_accrual already booked for %s; skipping", accrual_date_et)
        return AccrualTickResult(updated_positions=(), activity_log_entries=(), total_accrued_usd=0.0)

    positions = await _read_all_positions(session)
    in_scope = tuple(p for p in positions if is_open_short_equity(p))
    close_prices = await _read_latest_closes(
        session,
        tickers=tuple(_ticker_of(p) for p in in_scope),
        as_of=now,
    )
    fee_rates: dict[Symbol, float | None] = {
        _ticker_of(p): borrow_cost_resolver(str(_ticker_of(p))) for p in in_scope
    }

    result = compute_tick(
        positions=positions,
        close_prices=close_prices,
        fee_rates=fee_rates,
        now=now,
        invocation_id=handle.invocation_id,
    )
    for updated in result.updated_positions:
        await _persist_updated_position(session, updated)
    for entry in result.activity_log_entries:
        session.add(activity_log_entry_to_row(entry))

    log.info(
        "borrow_accrual booked for %s: positions=%d total_accrued_usd=%.4f",
        accrual_date_et,
        len(result.updated_positions),
        result.total_accrued_usd,
    )
    return result


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _ticker_of(position: PositionRecord) -> Symbol:
    assert isinstance(position.details, EquityPositionDetails)  # narrowed by predicate
    return position.details.ticker


async def _already_accrued_today(session: AsyncSession, *, accrual_date_iso: str) -> bool:
    """Whether a ``BORROW_COST_ACCRUED`` row already covers *accrual_date_iso*.

    The activity-log ``detail_json`` carries the kernel's ``accrual_date`` (ISO
    ``YYYY-MM-DD``). A ``LIKE`` match on the serialized detail is the cheapest
    once-per-trading-day key that needs no schema change — the accrual is a
    discrete EOD quantity keyed off the trading day, so one booked row for the
    day means the day is done regardless of which invocation booked it.
    """
    pattern = f'%"accrual_date":"{accrual_date_iso}"%'
    stmt = (
        select(func.count())
        .select_from(ActivityLogRow)
        .where(
            ActivityLogRow.event_type == EventType.BORROW_COST_ACCRUED.value,
            ActivityLogRow.detail_json.like(pattern),
        )
    )
    count = (await session.execute(stmt)).scalar_one()
    return count > 0


async def _read_all_positions(session: AsyncSession) -> tuple[PositionRecord, ...]:
    """Read every position from the ``positions`` table.

    The kernel filters in-scope (OPEN SHORT EQUITY); the read is unfiltered so
    the typed predicate stays the single source of truth. The row count is
    bounded by the open book — tens, not thousands.
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

    Returns a dict only for tickers whose latest bar exists *and* is fresher than
    ``_MAX_EOD_BAR_AGE_SECONDS`` relative to ``as_of``; the kernel raises if any
    in-scope ticker is missing. ``unadj_close`` is the actual traded price.
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

    Fetches the row by id and rewrites ``details_json`` from the new typed
    record. Identity columns are unchanged by the kernel for an OPEN SHORT EQUITY
    tick; the codec recomputes them all so a future kernel change still
    round-trips cleanly.
    """
    row = (
        await session.execute(
            select(PositionRow).where(PositionRow.position_id == updated.position_id)
        )
    ).scalar_one()
    new_row = position_record_to_row(updated)
    row.details_json = new_row.details_json
    row.realized_pnl_to_date_usd = new_row.realized_pnl_to_date_usd


__all__ = ["BorrowCostResolver", "run_borrow_accrual"]
