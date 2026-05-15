"""Tests for earnings_calendar tool — ALP-259.

Uses an in-memory SQLite database for full integration coverage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.analysis.tools._envelope import ToolQuality
from alphamind.analysis.tools.earnings_calendar import (
    EarningsCalendarInput,
    EarningsCalendarOutput,
    earnings_calendar_factory,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    EarningsEstimateRevisions,
    EarningsEventDetails,
    EventCalendar,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
_FUTURE = _NOW + timedelta(days=30)
_PAST = _NOW - timedelta(days=10)


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
    scheduled_at: datetime,
    eps_consensus: float | None = 1.5,
    reported_at: datetime | None = None,
) -> None:
    ingested = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        EventCalendar(
            event_id=event_id,
            event_type="earnings",
            ticker=ticker,
            scheduled_at=scheduled_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            description=f"{ticker} earnings",
            status="confirmed" if reported_at is None else "completed",
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
            reported_at=reported_at.strftime("%Y-%m-%dT%H:%M:%SZ") if reported_at else None,
            source="test",
        )
    )


def _add_revision(
    session: Session,
    ticker: str,
    *,
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
# Tests: happy path
# ---------------------------------------------------------------------------


def test_earnings_calendar_happy_path_complete(session: Session) -> None:
    """Happy path: forward event + past event returns COMPLETE quality."""
    _add_ticker(session, "NVDA")
    _add_earnings_event(session, event_id="ev-future", ticker=Symbol("NVDA"), scheduled_at=_FUTURE)
    _add_earnings_event(
        session, event_id="ev-past", ticker=Symbol("NVDA"), scheduled_at=_PAST, reported_at=_PAST
    )
    session.commit()

    fn = earnings_calendar_factory(session)
    result: EarningsCalendarOutput = fn(EarningsCalendarInput(tickers=("NVDA",)))

    assert result.quality == ToolQuality.COMPLETE
    assert len(result.per_ticker) == 1
    row = result.per_ticker[0]
    assert row.ticker == "NVDA"
    assert row.next_report_date is not None
    assert row.last_report_date is not None
    assert isinstance(result.data_freshness, datetime)


def test_earnings_calendar_next_report_date_is_future(session: Session) -> None:
    """next_report_date is the ISO date of the future-scheduled event."""
    _add_ticker(session, "TSLA")
    _add_earnings_event(session, event_id="ev-next", ticker=Symbol("TSLA"), scheduled_at=_FUTURE)
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("TSLA",)))

    expected_date = _FUTURE.strftime("%Y-%m-%d")
    assert result.per_ticker[0].next_report_date == expected_date


def test_earnings_calendar_last_report_date_is_past(session: Session) -> None:
    """last_report_date is the ISO date of the most recent past reported event."""
    _add_ticker(session, "AAPL")
    _add_earnings_event(
        session, event_id="ev-past", ticker=Symbol("AAPL"), scheduled_at=_PAST, reported_at=_PAST
    )
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("AAPL",)))

    expected_date = _PAST.strftime("%Y-%m-%d")
    assert result.per_ticker[0].last_report_date == expected_date


def test_earnings_calendar_consensus_eps_populated(session: Session) -> None:
    """consensus_eps is the latest consensus from the forward event."""
    _add_ticker(session, "MSFT")
    _add_earnings_event(
        session, event_id="ev-msft", ticker=Symbol("MSFT"), scheduled_at=_FUTURE, eps_consensus=2.75
    )
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("MSFT",)))

    assert result.per_ticker[0].consensus_eps == pytest.approx(2.75)


def test_earnings_calendar_whisper_number_is_none(session: Session) -> None:
    """whisper_number is always None (no whisper data source in scope)."""
    _add_ticker(session, "META")
    _add_earnings_event(session, event_id="ev-meta", ticker=Symbol("META"), scheduled_at=_FUTURE)
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("META",)))

    assert result.per_ticker[0].whisper_number is None


def test_earnings_calendar_revision_trend_up(session: Session) -> None:
    """All positive revisions within lookback_days → revision_trend='up'."""
    _add_ticker(session, "NVDA")
    _add_earnings_event(session, event_id="ev-nvda", ticker=Symbol("NVDA"), scheduled_at=_FUTURE)
    recent = _NOW - timedelta(days=5)
    _add_revision(
        session,
        "NVDA",
        revised_at=recent,
        consensus_value=2.0,
        prior_consensus_value=1.8,
    )
    _add_revision(
        session,
        "NVDA",
        revised_at=recent + timedelta(days=1),
        consensus_value=2.1,
        prior_consensus_value=2.0,
    )
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("NVDA",), lookback_days=30))

    assert result.per_ticker[0].revision_trend == "up"


def test_earnings_calendar_revision_trend_down(session: Session) -> None:
    """All negative revisions within lookback_days → revision_trend='down'."""
    _add_ticker(session, "GME")
    _add_earnings_event(session, event_id="ev-gme", ticker=Symbol("GME"), scheduled_at=_FUTURE)
    recent = _NOW - timedelta(days=5)
    _add_revision(
        session,
        "GME",
        revised_at=recent,
        consensus_value=1.0,
        prior_consensus_value=1.5,
    )
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("GME",), lookback_days=30))

    assert result.per_ticker[0].revision_trend == "down"


def test_earnings_calendar_revision_trend_none_when_no_revisions(session: Session) -> None:
    """No revisions in lookback → revision_trend='none'."""
    _add_ticker(session, "AMZN")
    _add_earnings_event(session, event_id="ev-amzn", ticker=Symbol("AMZN"), scheduled_at=_FUTURE)
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("AMZN",), lookback_days=30))

    assert result.per_ticker[0].revision_trend == "none"


# ---------------------------------------------------------------------------
# Tests: missing data / partial quality
# ---------------------------------------------------------------------------


def test_earnings_calendar_ticker_not_in_universe_unavailable(session: Session) -> None:
    """Ticker not in AssetUniverse → per-ticker UNAVAILABLE."""
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("UNKNWN",)))

    assert result.per_ticker[0].quality == ToolQuality.UNAVAILABLE


def test_earnings_calendar_no_events_returns_partial(session: Session) -> None:
    """Ticker in universe but no events → PARTIAL with None dates."""
    _add_ticker(session, "ORCL")
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=("ORCL",)))

    assert result.per_ticker[0].quality == ToolQuality.PARTIAL
    assert result.per_ticker[0].next_report_date is None
    assert result.per_ticker[0].last_report_date is None


# ---------------------------------------------------------------------------
# Tests: invalid input
# ---------------------------------------------------------------------------


def test_earnings_calendar_empty_tickers_returns_unavailable(session: Session) -> None:
    """Empty tickers tuple returns UNAVAILABLE envelope without raising."""
    session.commit()

    fn = earnings_calendar_factory(session)
    result = fn(EarningsCalendarInput(tickers=()))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.per_ticker == ()
    assert isinstance(result.data_freshness, datetime)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_earnings_calendar_deterministic_output(session: Session) -> None:
    """Two calls against the same fixture data return identical payloads."""
    _add_ticker(session, "CRM")
    _add_earnings_event(session, event_id="ev-crm", ticker=Symbol("CRM"), scheduled_at=_FUTURE)
    session.commit()

    fn = earnings_calendar_factory(session)
    r1 = fn(EarningsCalendarInput(tickers=("CRM",)))
    r2 = fn(EarningsCalendarInput(tickers=("CRM",)))

    assert r1.per_ticker == r2.per_ticker
    assert r1.quality == r2.quality
