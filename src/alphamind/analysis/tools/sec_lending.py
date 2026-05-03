"""sec_lending on-demand tool — ALP-259.

Queries BorrowCostIntraday (falling back to BorrowCostDaily), computes a
5-day linear-fit cost trend, and joins ShortInterestSnapshot for
days_to_cover. Validates every input ticker against AssetUniverse.

Missing/invalid per-ticker inputs degrade to per-row PARTIAL or UNAVAILABLE
rather than raising — aggregate quality reflects the worst-case across rows.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, parse_iso
from alphamind.persistence.models import (
    AssetUniverse,
    BorrowCostDaily,
    BorrowCostIntraday,
    ShortInterestSnapshot,
)

__all__ = [
    "SecLendingInput",
    "SecLendingOutput",
    "SecLendingTickerData",
    "sec_lending_factory",
]

# ---------------------------------------------------------------------------
# Named constants — no magic numbers
# ---------------------------------------------------------------------------

_SEC_LENDING_TREND_BP_PER_DAY = 0.5  # slope threshold for rising/falling classification


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SecLendingInput(BaseModel, frozen=True):
    """Input for the sec_lending tool.

    At least one ticker required; empty tuple returns UNAVAILABLE.
    """

    tickers: tuple[str, ...]


class SecLendingTickerData(BaseModel, frozen=True):
    ticker: str
    borrow_rate_pct: float | None
    available_shares: int | None
    utilization_pct: float | None
    days_to_cover: float | None
    cost_trend: str  # "rising" | "falling" | "stable"
    data_freshness: datetime
    quality: ToolQuality


class SecLendingOutput(ToolEnvelope, frozen=True):
    per_ticker: tuple[SecLendingTickerData, ...]


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Compute OLS slope of ys on xs. Returns 0.0 if fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xx = sum(x * x for x in xs)
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=True))
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0.0:
        return 0.0
    return (n * sum_xy - sum_x * sum_y) / denom


def _cost_trend(slope_pct_per_day: float) -> str:
    if slope_pct_per_day > _SEC_LENDING_TREND_BP_PER_DAY / 100.0:
        return "rising"
    if slope_pct_per_day < -_SEC_LENDING_TREND_BP_PER_DAY / 100.0:
        return "falling"
    return "stable"


def _ticker_row(session: Session, ticker: str) -> AssetUniverse | None:
    return session.execute(
        select(AssetUniverse).where(AssetUniverse.ticker == ticker).limit(1)
    ).scalar()


def _fetch_intraday_rate(session: Session, ticker: str) -> tuple[float, datetime] | None:
    """Return (fee_pct, snapshot_datetime) for the most recent intraday row, or None."""
    row = session.execute(
        select(BorrowCostIntraday.fee_pct, BorrowCostIntraday.snapshot_at)
        .where(BorrowCostIntraday.ticker == ticker)
        .order_by(BorrowCostIntraday.snapshot_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return row.fee_pct, parse_iso(row.snapshot_at)


def _fetch_daily_rows(
    session: Session, ticker: str, limit: int
) -> list[tuple[str, float | None, int | None]]:
    """Return up to `limit` most-recent BorrowCostDaily rows as (date, fee_pct, avail)."""
    rows = session.execute(
        select(
            BorrowCostDaily.observation_date,
            BorrowCostDaily.fee_pct,
            BorrowCostDaily.available_shares,
        )
        .where(BorrowCostDaily.ticker == ticker)
        .order_by(BorrowCostDaily.observation_date.desc())
        .limit(limit)
    ).all()
    return [(r.observation_date, r.fee_pct, r.available_shares) for r in rows]


def _fetch_short_interest(session: Session, ticker: str) -> ShortInterestSnapshot | None:
    return session.execute(
        select(ShortInterestSnapshot)
        .where(ShortInterestSnapshot.ticker == ticker)
        .order_by(ShortInterestSnapshot.settlement_date.desc())
        .limit(1)
    ).scalar()


def _build_unavailable_row(ticker: str, now: datetime) -> SecLendingTickerData:
    return SecLendingTickerData(
        ticker=ticker,
        borrow_rate_pct=None,
        available_shares=None,
        utilization_pct=None,
        days_to_cover=None,
        cost_trend="stable",
        data_freshness=now,
        quality=ToolQuality.UNAVAILABLE,
    )


def _build_ticker_row(
    session: Session,
    ticker: str,
    universe_row: AssetUniverse,
    now: datetime,
) -> SecLendingTickerData:
    # Primary: intraday rate; fallback: most recent daily rate
    intraday = _fetch_intraday_rate(session, ticker)
    daily_rows = _fetch_daily_rows(session, ticker, limit=5)

    borrow_rate_pct: float | None
    available_shares: int | None
    rate_freshness: datetime

    if intraday is not None:
        borrow_rate_pct, rate_freshness = intraday
        available_shares = daily_rows[0][2] if daily_rows else None
    elif daily_rows:
        borrow_rate_pct = daily_rows[0][1]
        available_shares = daily_rows[0][2]
        rate_freshness = parse_iso(daily_rows[0][0] + "T00:00:00Z")
    else:
        return SecLendingTickerData(
            ticker=ticker,
            borrow_rate_pct=None,
            available_shares=None,
            utilization_pct=None,
            days_to_cover=None,
            cost_trend="stable",
            data_freshness=now,
            quality=ToolQuality.PARTIAL,
        )

    # Cost trend from last 5 daily rows (oldest→newest)
    trend = "stable"
    if len(daily_rows) >= 2:
        ordered = list(reversed(daily_rows))  # oldest first
        ys = [r[1] for r in ordered if r[1] is not None]
        xs = list(range(len(ys)))
        slope = _linear_slope(xs, ys)
        trend = _cost_trend(slope)

    # days_to_cover from ShortInterestSnapshot + AssetUniverse
    si = _fetch_short_interest(session, ticker)
    days_to_cover: float | None = None
    quality = ToolQuality.COMPLETE

    if si is not None:
        avg_vol = universe_row.avg_daily_volume_shares
        if avg_vol and avg_vol > 0:
            days_to_cover = si.current_short_shares / avg_vol
        else:
            quality = ToolQuality.PARTIAL
    else:
        quality = ToolQuality.PARTIAL

    return SecLendingTickerData(
        ticker=ticker,
        borrow_rate_pct=borrow_rate_pct,
        available_shares=available_shares,
        utilization_pct=None,  # utilization not in current schema tables
        days_to_cover=days_to_cover,
        cost_trend=trend,
        data_freshness=rate_freshness,
        quality=quality,
    )


def _query_sec_lending(session: Session, inp: SecLendingInput) -> SecLendingOutput:
    now = datetime.now(UTC)

    if not inp.tickers:
        return SecLendingOutput(per_ticker=(), data_freshness=now, quality=ToolQuality.UNAVAILABLE)

    rows: list[SecLendingTickerData] = []
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

    return SecLendingOutput(
        per_ticker=tuple(rows),
        data_freshness=agg_freshness,
        quality=agg_quality,
    )


def sec_lending_factory(session: Session) -> Callable[[SecLendingInput], SecLendingOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry."""

    def _call(inp: SecLendingInput) -> SecLendingOutput:
        return _query_sec_lending(session, inp)

    return _call
