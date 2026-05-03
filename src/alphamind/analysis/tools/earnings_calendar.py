"""earnings_calendar on-demand tool — ALP-259.

Queries EarningsEventDetails joined to EventCalendar for the next forward
and most recent past earnings event per ticker, together with
EarningsEstimateRevisions for the revision trend within a lookback window.
Validates every input ticker against AssetUniverse.

Missing/invalid per-ticker inputs degrade to per-row PARTIAL or UNAVAILABLE
rather than raising — aggregate quality reflects the worst-case across rows.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, format_iso, parse_iso
from alphamind.persistence.models import (
    AssetUniverse,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    EventCalendar,
)

__all__ = [
    "EarningsCalendarEntry",
    "EarningsCalendarInput",
    "EarningsCalendarOutput",
    "earnings_calendar_factory",
]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class EarningsCalendarInput(BaseModel, frozen=True):
    """Input for the earnings_calendar tool.

    At least one ticker required; empty tuple returns UNAVAILABLE.
    lookback_days >= 0 for the revision trend window.
    """

    tickers: tuple[str, ...]
    lookback_days: int = 30


class EarningsCalendarEntry(BaseModel, frozen=True):
    ticker: str
    next_report_date: str | None
    consensus_eps: float | None
    revision_trend: str  # "up" | "down" | "mixed" | "none"
    whisper_number: None  # always None — no whisper data source in scope
    last_report_date: str | None
    data_freshness: datetime
    quality: ToolQuality


class EarningsCalendarOutput(ToolEnvelope, frozen=True):
    per_ticker: tuple[EarningsCalendarEntry, ...]


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _ticker_in_universe(session: Session, ticker: str) -> bool:
    return (
        session.execute(
            select(AssetUniverse.ticker).where(AssetUniverse.ticker == ticker).limit(1)
        ).scalar()
        is not None
    )


def _fetch_next_event(
    session: Session, ticker: str, now: datetime
) -> tuple[str | None, float | None, datetime]:
    """Return (next_report_date_iso, consensus_eps, freshness)."""
    now_iso = format_iso(now)
    row = session.execute(
        select(
            EarningsEventDetails.event_id,
            EarningsEventDetails.eps_consensus,
            EventCalendar.scheduled_at,
            EventCalendar.ingested_at,
        )
        .join(EventCalendar, EventCalendar.event_id == EarningsEventDetails.event_id)
        .where(
            EarningsEventDetails.ticker == ticker,
            EventCalendar.scheduled_at >= now_iso,
        )
        .order_by(EventCalendar.scheduled_at)
        .limit(1)
    ).first()
    if row is None:
        return None, None, now
    scheduled = parse_iso(row.scheduled_at)
    freshness = parse_iso(row.ingested_at) if row.ingested_at else now
    return scheduled.strftime("%Y-%m-%d"), row.eps_consensus, freshness


def _fetch_last_event(session: Session, ticker: str, now: datetime) -> tuple[str | None, datetime]:
    """Return (last_report_date_iso, freshness)."""
    now_iso = format_iso(now)
    row = session.execute(
        select(
            EarningsEventDetails.reported_at,
            EventCalendar.ingested_at,
        )
        .join(EventCalendar, EventCalendar.event_id == EarningsEventDetails.event_id)
        .where(
            EarningsEventDetails.ticker == ticker,
            EarningsEventDetails.reported_at.isnot(None),
            EventCalendar.scheduled_at <= now_iso,
        )
        .order_by(EventCalendar.scheduled_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None, now
    reported = parse_iso(row.reported_at)
    freshness = parse_iso(row.ingested_at) if row.ingested_at else now
    return reported.strftime("%Y-%m-%d"), freshness


def _revision_direction(
    revisions: list[tuple[float | None, float | None]],
) -> Literal["up", "down", "mixed", "none"]:
    ups = sum(1 for c, p in revisions if c is not None and p is not None and c > p)
    downs = sum(1 for c, p in revisions if c is not None and p is not None and c < p)
    if ups == 0 and downs == 0:
        return "none"
    if ups > 0 and downs == 0:
        return "up"
    if downs > 0 and ups == 0:
        return "down"
    return "mixed"


def _fetch_revision_trend(session: Session, ticker: str, now: datetime, lookback_days: int) -> str:
    window_start_iso = format_iso(now - timedelta(days=lookback_days))
    rows = session.execute(
        select(
            EarningsEstimateRevisions.consensus_value,
            EarningsEstimateRevisions.prior_consensus_value,
        ).where(
            EarningsEstimateRevisions.ticker == ticker,
            EarningsEstimateRevisions.revised_at >= window_start_iso,
        )
    ).all()
    pairs = [(r.consensus_value, r.prior_consensus_value) for r in rows]
    return _revision_direction(pairs)


def _build_unavailable_entry(ticker: str, now: datetime) -> EarningsCalendarEntry:
    return EarningsCalendarEntry(
        ticker=ticker,
        next_report_date=None,
        consensus_eps=None,
        revision_trend="none",
        whisper_number=None,
        last_report_date=None,
        data_freshness=now,
        quality=ToolQuality.UNAVAILABLE,
    )


def _build_ticker_entry(
    session: Session,
    ticker: str,
    now: datetime,
    lookback_days: int,
) -> EarningsCalendarEntry:
    next_date, consensus_eps, next_freshness = _fetch_next_event(session, ticker, now)
    last_date, last_freshness = _fetch_last_event(session, ticker, now)
    revision_trend = _fetch_revision_trend(session, ticker, now, lookback_days)

    real_freshness = [f for f in (next_freshness, last_freshness) if f != now]
    freshness = min(real_freshness) if real_freshness else now

    has_any = next_date is not None or last_date is not None
    quality = ToolQuality.COMPLETE if has_any else ToolQuality.PARTIAL

    return EarningsCalendarEntry(
        ticker=ticker,
        next_report_date=next_date,
        consensus_eps=consensus_eps,
        revision_trend=revision_trend,
        whisper_number=None,
        last_report_date=last_date,
        data_freshness=freshness,
        quality=quality,
    )


def _query_earnings_calendar(
    session: Session, inp: EarningsCalendarInput
) -> EarningsCalendarOutput:
    now = datetime.now(UTC)

    if not inp.tickers:
        return EarningsCalendarOutput(
            per_ticker=(), data_freshness=now, quality=ToolQuality.UNAVAILABLE
        )

    entries: list[EarningsCalendarEntry] = []
    for raw_ticker in inp.tickers:
        ticker = raw_ticker.upper()
        if not _ticker_in_universe(session, ticker):
            entries.append(_build_unavailable_entry(ticker, now))
            continue
        entries.append(_build_ticker_entry(session, ticker, now, inp.lookback_days))

    populated = [e for e in entries if e.quality != ToolQuality.UNAVAILABLE]
    agg_freshness = min(e.data_freshness for e in populated) if populated else now
    all_complete = all(e.quality == ToolQuality.COMPLETE for e in entries)
    agg_quality = ToolQuality.COMPLETE if all_complete else ToolQuality.PARTIAL
    if not populated:
        agg_quality = ToolQuality.UNAVAILABLE

    return EarningsCalendarOutput(
        per_ticker=tuple(entries),
        data_freshness=agg_freshness,
        quality=agg_quality,
    )


def earnings_calendar_factory(
    session: Session,
) -> Callable[[EarningsCalendarInput], EarningsCalendarOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry."""

    def _call(inp: EarningsCalendarInput) -> EarningsCalendarOutput:
        return _query_earnings_calendar(session, inp)

    return _call
