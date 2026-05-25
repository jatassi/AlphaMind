"""earnings_commentary on-demand tool — ALP-247.

Implements Tier 1 earnings commentary: EPS/revenue results, price reaction,
and post-earnings estimate-revision activity.

Tier 2 fields (transcript_available, transcript_analysis) are stub-shaped:
  ``transcript_available = False``, ``transcript_analysis = None`` always.

``quality`` logic:
  - UNAVAILABLE: ticker not in asset_universe, or no earnings event found
  - STALE: most recent earnings event is >90 days old
  - PARTIAL_NO_TRANSCRIPT: ``include_transcript_analysis=True`` (Tier 2 not yet built)
  - COMPLETE: Tier 1 fields all populate and caller did not request transcript
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind._kernel.clock import Clock, RealClock
from alphamind.analysis.tools._envelope import ToolEnvelope, ToolQuality, parse_iso
from alphamind.persistence.models import (
    AssetUniverse,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    EventCalendar,
    OhlcvBars,
)

__all__ = [
    "EarningsCommentaryInput",
    "EarningsCommentaryOutput",
    "EarningsResult",
    "NotCollected",
    "PostEarningsActivity",
    "PriceReaction",
    "RatingChange",
    "earnings_commentary_factory",
]

_STALE_THRESHOLD_DAYS = 90
_RATING_CHANGES_DEFERRED_REASON = "analyst-rating ingestion not yet shipped"


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class EarningsCommentaryInput(BaseModel, frozen=True):
    ticker: str
    include_transcript_analysis: bool = True


class EarningsResult(BaseModel, frozen=True):
    eps_actual: float
    eps_consensus: float
    eps_surprise_pct: float
    revenue_actual: float
    revenue_consensus: float
    revenue_surprise_pct: float


class PriceReaction(BaseModel, frozen=True):
    close_to_close_pct: float
    immediate_move_pct: float
    vs_implied_move: float | None  # None: options-implied data not in scope


class RatingChange(BaseModel, frozen=True):
    analyst_firm: str
    prior_rating: str
    new_rating: str
    prior_target: float | None
    new_target: float | None


class NotCollected(BaseModel, frozen=True):
    """LLM-facing sentinel that distinguishes "data not yet collected" from
    a factual empty value (ALP-654). When a field's source ingestion has
    not shipped yet, the producer emits this instead of a default-shaped
    empty so the agent can tell "we don't have this data" from "this data
    exists and is empty".
    """

    status: Literal["not_collected"] = "not_collected"
    reason: str


class PostEarningsActivity(BaseModel, frozen=True):
    estimate_revisions_since: int
    revision_direction: Literal["up", "down", "mixed", "none"]
    rating_changes_since: tuple[RatingChange, ...] | NotCollected


class EarningsCommentaryOutput(ToolEnvelope, frozen=True):
    ticker: str
    earnings_date: str
    result: EarningsResult
    price_reaction: PriceReaction
    post_earnings_activity: PostEarningsActivity
    transcript_available: bool  # always False in this story's scope
    transcript_analysis: None  # always None in this story's scope


# ---------------------------------------------------------------------------
# Implementation helpers
# ---------------------------------------------------------------------------


def _zero_result() -> EarningsResult:
    return EarningsResult(
        eps_actual=0.0,
        eps_consensus=0.0,
        eps_surprise_pct=0.0,
        revenue_actual=0.0,
        revenue_consensus=0.0,
        revenue_surprise_pct=0.0,
    )


def _zero_price_reaction() -> PriceReaction:
    return PriceReaction(close_to_close_pct=0.0, immediate_move_pct=0.0, vs_implied_move=None)


def _zero_post_earnings() -> PostEarningsActivity:
    return PostEarningsActivity(
        estimate_revisions_since=0,
        revision_direction="none",
        rating_changes_since=NotCollected(reason=_RATING_CHANGES_DEFERRED_REASON),
    )


def _unavailable(ticker: str, now: datetime) -> EarningsCommentaryOutput:
    """Return a typed UNAVAILABLE envelope with zeroed Tier 1 fields."""
    return EarningsCommentaryOutput(
        ticker=ticker,
        earnings_date="",
        data_freshness=now,
        result=_zero_result(),
        price_reaction=_zero_price_reaction(),
        post_earnings_activity=_zero_post_earnings(),
        transcript_available=False,
        transcript_analysis=None,
        quality=ToolQuality.UNAVAILABLE,
    )


def _surprise_pct(actual: float, consensus: float) -> float:
    if consensus == 0.0:
        return 0.0
    return (actual - consensus) / abs(consensus) * 100.0


def _ticker_in_universe(session: Session, ticker: str) -> bool:
    return (
        session.execute(
            select(AssetUniverse.ticker).where(AssetUniverse.ticker == ticker).limit(1)
        ).scalar()
        is not None
    )


def _fetch_most_recent_earnings(session: Session, ticker: str) -> EarningsEventDetails | None:
    rows = (
        session.execute(
            select(EarningsEventDetails)
            .where(
                EarningsEventDetails.ticker == ticker,
                EarningsEventDetails.reported_at.isnot(None),
            )
            .order_by(EarningsEventDetails.reported_at.desc())
            .limit(1)
        )
        .scalars()
        .all()
    )
    return rows[0] if rows else None


def _build_result(details: EarningsEventDetails) -> EarningsResult:
    eps_actual = details.eps_actual or 0.0
    eps_consensus = details.eps_consensus or 0.0
    rev_actual = details.revenue_actual_usd or 0.0
    rev_consensus = details.revenue_consensus_usd or 0.0
    return EarningsResult(
        eps_actual=eps_actual,
        eps_consensus=eps_consensus,
        eps_surprise_pct=_surprise_pct(eps_actual, eps_consensus),
        revenue_actual=rev_actual,
        revenue_consensus=rev_consensus,
        revenue_surprise_pct=_surprise_pct(rev_actual, rev_consensus),
    )


def _build_price_reaction(session: Session, ticker: str, reported_at: datetime) -> PriceReaction:
    """Compute close-to-close and immediate-move from ohlcv_bars."""
    reported_date = reported_at.date()

    daily_rows = session.execute(
        select(OhlcvBars.period_start, OhlcvBars.adj_close)
        .where(OhlcvBars.ticker == ticker, OhlcvBars.timeframe == "1d")
        .order_by(OhlcvBars.period_start)
    ).all()

    prior_close: float | None = None
    post_close: float | None = None
    for row in daily_rows:
        bar_date = parse_iso(row.period_start).date()
        if bar_date < reported_date:
            prior_close = row.adj_close
        elif post_close is None:
            post_close = row.adj_close

    close_to_close = 0.0
    if prior_close and post_close and prior_close != 0.0:
        close_to_close = (post_close - prior_close) / prior_close * 100.0

    immediate_move = 0.0
    reported_iso = reported_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    for tf in ("30m", "1m"):
        intra_rows = session.execute(
            select(OhlcvBars.adj_open, OhlcvBars.adj_close)
            .where(
                OhlcvBars.ticker == ticker,
                OhlcvBars.timeframe == tf,
                OhlcvBars.period_start >= reported_iso,
            )
            .order_by(OhlcvBars.period_start)
            .limit(1)
        ).all()
        if intra_rows:
            bar = intra_rows[0]
            if bar.adj_open and bar.adj_open != 0.0:
                immediate_move = (bar.adj_close - bar.adj_open) / bar.adj_open * 100.0
            break

    return PriceReaction(
        close_to_close_pct=close_to_close,
        immediate_move_pct=immediate_move,
        vs_implied_move=None,
    )


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


def _build_post_earnings_activity(
    session: Session,
    ticker: str,
    details: EarningsEventDetails,
) -> PostEarningsActivity:
    if details.reported_at is None:
        return _zero_post_earnings()

    reported_at_iso = parse_iso(details.reported_at).strftime("%Y-%m-%dT%H:%M:%SZ")
    revision_rows = session.execute(
        select(
            EarningsEstimateRevisions.consensus_value,
            EarningsEstimateRevisions.prior_consensus_value,
        ).where(
            EarningsEstimateRevisions.ticker == ticker,
            EarningsEstimateRevisions.revised_at >= reported_at_iso,
        )
    ).all()

    pairs = [(row.consensus_value, row.prior_consensus_value) for row in revision_rows]
    # ``rating_changes_since`` is union[tuple[RatingChange, ...], NotCollected] —
    # the deferred sentinel keeps the LLM-facing JSON honest about absent
    # data until analyst-rating ingestion lands.
    return PostEarningsActivity(
        estimate_revisions_since=len(revision_rows),
        revision_direction=_revision_direction(pairs),
        rating_changes_since=NotCollected(reason=_RATING_CHANGES_DEFERRED_REASON),
    )


def _get_earnings_commentary(
    session: Session, inp: EarningsCommentaryInput, clock: Clock
) -> EarningsCommentaryOutput:
    now = clock.now()
    ticker = inp.ticker.upper()

    if not _ticker_in_universe(session, ticker):
        return _unavailable(ticker, now)

    details = _fetch_most_recent_earnings(session, ticker)
    if details is None:
        return _unavailable(ticker, now)

    reported_at = parse_iso(details.reported_at)  # type: ignore[arg-type]

    age = now - reported_at
    if age > timedelta(days=_STALE_THRESHOLD_DAYS):
        quality: ToolQuality = ToolQuality.STALE
    elif inp.include_transcript_analysis:
        quality = ToolQuality.PARTIAL_NO_TRANSCRIPT
    else:
        quality = ToolQuality.COMPLETE

    freshness_row = session.execute(
        select(EventCalendar.ingested_at).where(EventCalendar.event_id == details.event_id).limit(1)
    ).scalar()

    return EarningsCommentaryOutput(
        ticker=ticker,
        earnings_date=reported_at.strftime("%Y-%m-%d"),
        data_freshness=parse_iso(freshness_row) if freshness_row else now,
        result=_build_result(details),
        price_reaction=_build_price_reaction(session, ticker, reported_at),
        post_earnings_activity=_build_post_earnings_activity(session, ticker, details),
        transcript_available=False,
        transcript_analysis=None,
        quality=quality,
    )


def earnings_commentary_factory(
    session: Session,
    *,
    clock: Clock | None = None,
) -> Callable[[EarningsCommentaryInput], EarningsCommentaryOutput]:
    """Return a callable suitable for the Claude Agent SDK tool registry.

    ``clock`` defaults to :class:`RealClock`; tests pass a fake to control
    the timestamp deterministically (ALP-474).
    """
    resolved_clock: Clock = clock if clock is not None else RealClock()

    def _call(inp: EarningsCommentaryInput) -> EarningsCommentaryOutput:
        return _get_earnings_commentary(session, inp, resolved_clock)

    return _call
