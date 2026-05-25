"""Tests for earnings_commentary tool — ALP-247.

Covers Tier 1 fields, quality enum logic, transcript stub, and
invalid-input (UNAVAILABLE) paths.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.analysis.tools import TOOLS, ToolQuality
from alphamind.analysis.tools.earnings_commentary import (
    EarningsCommentaryInput,
    EarningsCommentaryOutput,
    NotCollected,
    PostEarningsActivity,
    RatingChange,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    EventCalendar,
)
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


_NOW = datetime.now(UTC)
_RECENT_REPORT = _NOW - timedelta(days=15)
_STALE_REPORT = _NOW - timedelta(days=100)


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2026-01-01",
            last_updated="2026-01-01T00:00:00Z",
        )
    )
    session.flush()


def _add_earnings_event(
    session: Session,
    *,
    event_id: str,
    ticker: str,
    reported_at: datetime,
    eps_actual: float = 1.5,
    eps_consensus: float = 1.2,
    revenue_actual: float = 5_000_000_000.0,
    revenue_consensus: float = 4_800_000_000.0,
) -> None:
    ingested = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        EventCalendar(
            event_id=event_id,
            event_type="earnings",
            ticker=ticker,
            scheduled_at=reported_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=f"{ticker} earnings",
            status="completed",
            source="test",
            ingested_at=ingested,
            last_updated=ingested,
            sectors=None,
        )
    )
    session.flush()
    session.add(
        EarningsEventDetails(
            event_id=event_id,
            ticker=ticker,
            fiscal_period="Q1",
            fiscal_year=2026,
            eps_consensus=eps_consensus,
            eps_actual=eps_actual,
            revenue_consensus_usd=revenue_consensus,
            revenue_actual_usd=revenue_actual,
            reported_at=reported_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            source="test",
        )
    )


def _add_revision(
    session: Session,
    *,
    ticker: str,
    revised_at: datetime,
    consensus_value: float,
    prior_consensus_value: float,
    metric: str = "eps",
) -> None:
    session.add(
        EarningsEstimateRevisions(
            revised_at=revised_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ticker=ticker,
            fiscal_year=2026,
            fiscal_period="Q2",
            metric=metric,
            consensus_value=consensus_value,
            prior_consensus_value=prior_consensus_value,
            source="test",
            ingested_at=revised_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ---------------------------------------------------------------------------
# Happy path: COMPLETE
# ---------------------------------------------------------------------------


def test_earnings_commentary_complete_when_no_transcript_requested(session: Session) -> None:
    _add_ticker(session, "NVDA")
    _add_earnings_event(session, event_id="ev-1", ticker=Symbol("NVDA"), reported_at=_RECENT_REPORT)
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("NVDA"), include_transcript_analysis=False)
    )

    assert result.quality == ToolQuality.COMPLETE
    assert result.ticker == "NVDA"
    assert result.transcript_available is False
    assert result.transcript_analysis is None


def test_earnings_commentary_partial_no_transcript_when_transcript_requested(
    session: Session,
) -> None:
    _add_ticker(session, "NVDA")
    _add_earnings_event(session, event_id="ev-1", ticker=Symbol("NVDA"), reported_at=_RECENT_REPORT)
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("NVDA"), include_transcript_analysis=True)
    )

    assert result.quality == ToolQuality.PARTIAL_NO_TRANSCRIPT
    assert result.transcript_available is False
    assert result.transcript_analysis is None


def test_earnings_commentary_tier1_fields_populated(session: Session) -> None:
    _add_ticker(session, "NVDA")
    _add_earnings_event(
        session,
        event_id="ev-1",
        ticker=Symbol("NVDA"),
        reported_at=_RECENT_REPORT,
        eps_actual=2.0,
        eps_consensus=1.6,
        revenue_actual=10_000_000_000.0,
        revenue_consensus=9_500_000_000.0,
    )
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("NVDA"), include_transcript_analysis=False)
    )

    assert result.result.eps_actual == pytest.approx(2.0)
    assert result.result.eps_consensus == pytest.approx(1.6)
    # (2.0 - 1.6) / 1.6 * 100 = 25.0
    assert result.result.eps_surprise_pct == pytest.approx(25.0)
    assert result.result.revenue_actual == pytest.approx(10_000_000_000.0)


def test_earnings_commentary_carries_envelope_fields(session: Session) -> None:
    _add_ticker(session, "NVDA")
    _add_earnings_event(session, event_id="ev-1", ticker=Symbol("NVDA"), reported_at=_RECENT_REPORT)
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("NVDA"), include_transcript_analysis=False)
    )

    assert isinstance(result.data_freshness, datetime)
    assert isinstance(result.quality, ToolQuality)


def test_earnings_commentary_stale_quality_for_old_event(session: Session) -> None:
    _add_ticker(session, "AAPL")
    _add_earnings_event(
        session, event_id="ev-old", ticker=Symbol("AAPL"), reported_at=_STALE_REPORT
    )
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("AAPL"), include_transcript_analysis=False)
    )

    assert result.quality == ToolQuality.STALE


def test_earnings_commentary_post_earnings_revision_count(session: Session) -> None:
    _add_ticker(session, "TSLA")
    _add_earnings_event(session, event_id="ev-1", ticker=Symbol("TSLA"), reported_at=_RECENT_REPORT)
    # Two upward revisions after reporting
    after = _RECENT_REPORT + timedelta(days=3)
    _add_revision(
        session,
        ticker=Symbol("TSLA"),
        revised_at=after,
        consensus_value=2.1,
        prior_consensus_value=2.0,
        metric="eps",
    )
    _add_revision(
        session,
        ticker=Symbol("TSLA"),
        revised_at=after + timedelta(days=1),
        consensus_value=2.2,
        prior_consensus_value=2.1,
        metric="revenue",
    )
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("TSLA"), include_transcript_analysis=False)
    )

    assert result.post_earnings_activity.estimate_revisions_since == 2
    assert result.post_earnings_activity.revision_direction == "up"
    # Until analyst-rating ingestion ships, the deferred-state sentinel is
    # what the builders emit — the empty-tuple shape would falsely read as
    # "no rating changes occurred this quarter".
    assert isinstance(result.post_earnings_activity.rating_changes_since, NotCollected)


# ---------------------------------------------------------------------------
# Deferred-field rendering (ALP-654)
# ---------------------------------------------------------------------------


def test_rating_changes_since_renders_as_deferred_sentinel_in_json(session: Session) -> None:
    """An LLM reading the JSON dump sees an explicit not-collected sentinel."""
    _add_ticker(session, "NVDA")
    _add_earnings_event(session, event_id="ev-1", ticker=Symbol("NVDA"), reported_at=_RECENT_REPORT)
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("NVDA"), include_transcript_analysis=False)
    )

    rendered = result.model_dump(mode="json")["post_earnings_activity"]["rating_changes_since"]
    assert rendered == {
        "status": "not_collected",
        "reason": "analyst-rating ingestion not yet shipped",
    }


def test_rating_changes_since_populated_serializes_as_list() -> None:
    """A populated tuple of RatingChange entries still serializes as a JSON list."""
    activity = PostEarningsActivity(
        estimate_revisions_since=0,
        revision_direction="none",
        rating_changes_since=(
            RatingChange(
                analyst_firm="GS",
                prior_rating="hold",
                new_rating="buy",
                prior_target=120.0,
                new_target=140.0,
            ),
        ),
    )

    rendered = activity.model_dump(mode="json")["rating_changes_since"]
    assert isinstance(rendered, list)
    assert rendered[0]["analyst_firm"] == "GS"


def test_rating_changes_empty_collected_distinguishable_from_not_collected() -> None:
    """Once ingestion ships, an empty tuple must round-trip as []
    (factually 'no rating changes since reporting'), textually distinct
    from the not-collected sentinel object."""
    factual_empty = PostEarningsActivity(
        estimate_revisions_since=0,
        revision_direction="none",
        rating_changes_since=(),
    )
    deferred = PostEarningsActivity(
        estimate_revisions_since=0,
        revision_direction="none",
        rating_changes_since=NotCollected(reason="analyst-rating ingestion not yet shipped"),
    )

    empty_render = factual_empty.model_dump(mode="json")["rating_changes_since"]
    deferred_render = deferred.model_dump(mode="json")["rating_changes_since"]

    assert empty_render == []
    assert deferred_render == {
        "status": "not_collected",
        "reason": "analyst-rating ingestion not yet shipped",
    }


# ---------------------------------------------------------------------------
# Invalid inputs / missing data
# ---------------------------------------------------------------------------


def test_earnings_commentary_unknown_ticker_returns_unavailable(session: Session) -> None:
    """Ticker not in asset_universe → UNAVAILABLE (no raise)."""
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("UNKNWN"), include_transcript_analysis=False)
    )

    assert result.quality == ToolQuality.UNAVAILABLE
    assert isinstance(result.data_freshness, datetime)


def test_earnings_commentary_no_earnings_event_returns_unavailable(session: Session) -> None:
    """Ticker in universe but no earnings event → UNAVAILABLE."""
    _add_ticker(session, "MSFT")
    session.commit()

    fn = TOOLS["earnings_commentary"].callable_factory(session)
    result: EarningsCommentaryOutput = fn(
        EarningsCommentaryInput(ticker=Symbol("MSFT"), include_transcript_analysis=False)
    )

    assert result.quality == ToolQuality.UNAVAILABLE
    assert isinstance(result.data_freshness, datetime)
