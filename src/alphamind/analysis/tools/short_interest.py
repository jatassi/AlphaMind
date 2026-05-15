"""short_interest on-demand tool — ALP-259.

Queries ShortInterestSnapshot, ShortVolumeDaily, and BorrowCostDaily to
produce a squeeze composite score and per-ticker short interest data.
Validates every input ticker against AssetUniverse.

Missing/invalid per-ticker inputs degrade to per-row PARTIAL or UNAVAILABLE
rather than raising — aggregate quality reflects the worst-case across rows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.clock import Clock, RealClock
from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, parse_iso
from alphamind.persistence.models import (
    AssetUniverse,
    BorrowCostDaily,
    ShortInterestSnapshot,
    ShortVolumeDaily,
)

__all__ = [
    "ShortInterestInput",
    "ShortInterestOutput",
    "ShortInterestTickerData",
    "short_interest_factory",
]

# ---------------------------------------------------------------------------
# Named constants — squeeze composite weights
# ---------------------------------------------------------------------------

_SQUEEZE_SI_WEIGHT = 0.4
_SQUEEZE_DTC_WEIGHT = 0.4
_SQUEEZE_CTB_WEIGHT = 0.2

_SQUEEZE_SI_SCALE = 100.0  # denominator for short_interest_pct (expressed as %)
_SQUEEZE_DTC_SCALE = 10.0  # denominator for days_to_cover
_SQUEEZE_CTB_SCALE = 50.0  # denominator for cost_to_borrow_pct

_SHORT_VOLUME_LOOKBACK = 5  # trading days for short_volume_ratio_5d


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ShortInterestInput(BaseModel, frozen=True):
    """Input for the short_interest tool.

    At least one ticker required; empty tuple returns UNAVAILABLE.
    """

    tickers: tuple[str, ...]


class ShortInterestTickerData(BaseModel, frozen=True):
    ticker: str
    short_interest_pct: float | None
    squeeze_score: float | None
    days_to_cover: float | None
    cost_to_borrow_pct: float | None
    short_volume_ratio_5d: float | None
    data_freshness: datetime
    quality: ToolQuality


class ShortInterestOutput(ToolEnvelope, frozen=True):
    per_ticker: tuple[ShortInterestTickerData, ...]


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _ticker_row(session: Session, ticker: str) -> AssetUniverse | None:
    return session.execute(
        select(AssetUniverse).where(AssetUniverse.ticker == ticker).limit(1)
    ).scalar()


def _fetch_short_interest_snapshot(session: Session, ticker: str) -> ShortInterestSnapshot | None:
    return session.execute(
        select(ShortInterestSnapshot)
        .where(ShortInterestSnapshot.ticker == ticker)
        .order_by(ShortInterestSnapshot.settlement_date.desc())
        .limit(1)
    ).scalar()


def _fetch_borrow_cost(session: Session, ticker: str) -> tuple[float, datetime] | None:
    """Return (fee_pct, date_as_datetime) for the most recent BorrowCostDaily row."""
    row = session.execute(
        select(BorrowCostDaily.fee_pct, BorrowCostDaily.observation_date)
        .where(BorrowCostDaily.ticker == ticker)
        .order_by(BorrowCostDaily.observation_date.desc())
        .limit(1)
    ).first()
    if row is None or row.fee_pct is None:
        return None
    return row.fee_pct, parse_iso(row.observation_date + "T00:00:00Z")


def _fetch_short_volume_ratio_5d(session: Session, ticker: str) -> float | None:
    """Return 5-day average of short_volume / total_volume, or None."""
    rows = session.execute(
        select(ShortVolumeDaily.short_volume, ShortVolumeDaily.total_volume)
        .where(ShortVolumeDaily.ticker == ticker)
        .order_by(ShortVolumeDaily.trade_date.desc())
        .limit(_SHORT_VOLUME_LOOKBACK)
    ).all()
    if not rows:
        return None
    ratios = [r.short_volume / r.total_volume for r in rows if r.total_volume > 0]
    return sum(ratios) / len(ratios) if ratios else None


def _compute_squeeze_score(
    short_interest_pct: float | None,
    days_to_cover: float | None,
    cost_to_borrow_pct: float | None,
) -> float | None:
    """Deterministic composite in [0.0, 1.0].

    Formula: min(1.0, 0.4 * SI/100 + 0.4 * DTC/10 + 0.2 * CTB/50).
    Returns None when all inputs are None.
    """
    si = (short_interest_pct or 0.0) / _SQUEEZE_SI_SCALE
    dtc = (days_to_cover or 0.0) / _SQUEEZE_DTC_SCALE
    ctb = (cost_to_borrow_pct or 0.0) / _SQUEEZE_CTB_SCALE
    if short_interest_pct is None and days_to_cover is None and cost_to_borrow_pct is None:
        return None
    return min(1.0, _SQUEEZE_SI_WEIGHT * si + _SQUEEZE_DTC_WEIGHT * dtc + _SQUEEZE_CTB_WEIGHT * ctb)


def _build_unavailable_row(ticker: str, now: datetime) -> ShortInterestTickerData:
    return ShortInterestTickerData(
        ticker=ticker,
        short_interest_pct=None,
        squeeze_score=None,
        days_to_cover=None,
        cost_to_borrow_pct=None,
        short_volume_ratio_5d=None,
        data_freshness=now,
        quality=ToolQuality.UNAVAILABLE,
    )


def _build_ticker_row(
    session: Session,
    ticker: str,
    universe_row: AssetUniverse,
    now: datetime,
) -> ShortInterestTickerData:
    si_snap = _fetch_short_interest_snapshot(session, ticker)
    borrow = _fetch_borrow_cost(session, ticker)
    vol_ratio = _fetch_short_volume_ratio_5d(session, ticker)

    short_interest_pct: float | None = None
    days_to_cover: float | None = None
    freshness = now

    if si_snap is not None:
        shares_out = universe_row.shares_outstanding
        if shares_out and shares_out > 0:
            short_interest_pct = si_snap.current_short_shares / shares_out * 100.0
        days_to_cover = si_snap.days_to_cover
        freshness = parse_iso(si_snap.settlement_date + "T00:00:00Z")

    cost_to_borrow_pct: float | None = None
    if borrow is not None:
        cost_to_borrow_pct, borrow_freshness = borrow
        if freshness == now or borrow_freshness < freshness:
            freshness = borrow_freshness

    squeeze_score = _compute_squeeze_score(short_interest_pct, days_to_cover, cost_to_borrow_pct)

    has_any = si_snap is not None or borrow is not None or vol_ratio is not None
    quality = ToolQuality.COMPLETE if has_any else ToolQuality.PARTIAL
    if short_interest_pct is None and has_any:
        quality = ToolQuality.PARTIAL

    return ShortInterestTickerData(
        ticker=ticker,
        short_interest_pct=short_interest_pct,
        squeeze_score=squeeze_score,
        days_to_cover=days_to_cover,
        cost_to_borrow_pct=cost_to_borrow_pct,
        short_volume_ratio_5d=vol_ratio,
        data_freshness=freshness,
        quality=quality,
    )


def _query_short_interest(
    session: Session, inp: ShortInterestInput, clock: Clock
) -> ShortInterestOutput:
    now = clock.now()

    if not inp.tickers:
        return ShortInterestOutput(
            per_ticker=(), data_freshness=now, quality=ToolQuality.UNAVAILABLE
        )

    rows: list[ShortInterestTickerData] = []
    for raw_ticker in inp.tickers:
        ticker = raw_ticker.upper()
        universe_row = _ticker_row(session, ticker)
        if universe_row is None:
            rows.append(_build_unavailable_row(ticker, now))
            continue
        rows.append(_build_ticker_row(session, ticker, universe_row, now))

    populated = [r for r in rows if r.quality != ToolQuality.UNAVAILABLE]
    agg_freshness = min(r.data_freshness for r in populated) if populated else now
    all_complete = all(r.quality == ToolQuality.COMPLETE for r in rows)
    agg_quality = ToolQuality.COMPLETE if all_complete else ToolQuality.PARTIAL
    if not populated:
        agg_quality = ToolQuality.UNAVAILABLE

    return ShortInterestOutput(
        per_ticker=tuple(rows),
        data_freshness=agg_freshness,
        quality=agg_quality,
    )


def short_interest_factory(
    session: Session,
    *,
    clock: Clock | None = None,
) -> Callable[[ShortInterestInput], ShortInterestOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry.

    ``clock`` defaults to :class:`RealClock`; tests pass a fake to control
    the timestamp deterministically (ALP-474).
    """
    resolved_clock: Clock = clock if clock is not None else RealClock()

    def _call(inp: ShortInterestInput) -> ShortInterestOutput:
        return _query_short_interest(session, inp, resolved_clock)

    return _call
