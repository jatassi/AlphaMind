"""Tests for short_interest tool — ALP-259.

Uses an in-memory SQLite database for full integration coverage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.tools._envelope import ToolQuality
from alphamind.analysis.tools.short_interest import (
    ShortInterestInput,
    ShortInterestOutput,
    short_interest_factory,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    BorrowCostDaily,
    ShortInterestSnapshot,
    ShortVolumeDaily,
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


def _add_ticker(
    session: Session,
    ticker: str,
    *,
    shares_outstanding: int | None = 1_000_000,
    avg_daily_volume_shares: int | None = 100_000,
) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Corp",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            shares_outstanding=shares_outstanding,
            avg_daily_volume_shares=avg_daily_volume_shares,
            is_active=1,
            added_date="2026-01-01",
            last_updated="2026-01-01T00:00:00Z",
        )
    )
    session.flush()


def _add_short_interest(
    session: Session,
    ticker: str,
    *,
    settlement_date: str,
    current_short_shares: int,
    days_to_cover: float | None = 2.0,
) -> None:
    session.add(
        ShortInterestSnapshot(
            settlement_date=settlement_date,
            ticker=ticker,
            current_short_shares=current_short_shares,
            days_to_cover=days_to_cover,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


def _add_borrow_cost_daily(
    session: Session,
    ticker: str,
    *,
    observation_date: str,
    fee_pct: float,
) -> None:
    session.add(
        BorrowCostDaily(
            observation_date=observation_date,
            ticker=ticker,
            fee_pct=fee_pct,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


def _add_short_volume(
    session: Session,
    ticker: str,
    *,
    trade_date: str,
    short_volume: int,
    total_volume: int,
    market: str = "FINRA",
) -> None:
    session.add(
        ShortVolumeDaily(
            trade_date=trade_date,
            ticker=ticker,
            market=market,
            short_volume=short_volume,
            short_exempt_volume=0,
            total_volume=total_volume,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


def test_short_interest_happy_path_returns_complete(session: Session) -> None:
    """Full data set returns COMPLETE quality."""
    _add_ticker(session, "GME", shares_outstanding=1_000_000, avg_daily_volume_shares=200_000)
    _add_short_interest(
        session,
        "GME",
        settlement_date=(_NOW - timedelta(days=2)).strftime("%Y-%m-%d"),
        current_short_shares=300_000,
        days_to_cover=1.5,
    )
    _add_borrow_cost_daily(
        session,
        "GME",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=25.0,
    )
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_short_volume(
            session,
            "GME",
            trade_date=date,
            short_volume=100_000,
            total_volume=200_000,
        )
    session.commit()

    fn = short_interest_factory(session)
    result: ShortInterestOutput = fn(ShortInterestInput(tickers=("GME",)))

    assert result.quality == ToolQuality.COMPLETE
    assert len(result.per_ticker) == 1
    row = result.per_ticker[0]
    assert row.ticker == "GME"
    assert row.short_interest_pct is not None
    assert row.squeeze_score is not None
    assert isinstance(result.data_freshness, datetime)


def test_short_interest_pct_computed_from_shares(session: Session) -> None:
    """short_interest_pct = current_short_shares / shares_outstanding."""
    _add_ticker(session, "AMC", shares_outstanding=1_000_000, avg_daily_volume_shares=200_000)
    _add_short_interest(
        session,
        "AMC",
        settlement_date=(_NOW - timedelta(days=2)).strftime("%Y-%m-%d"),
        current_short_shares=200_000,
        days_to_cover=1.0,
    )
    _add_borrow_cost_daily(
        session,
        "AMC",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=5.0,
    )
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=("AMC",)))

    # 200_000 / 1_000_000 = 0.20 → expressed as percent = 20.0
    assert result.per_ticker[0].short_interest_pct == pytest.approx(20.0)


def test_short_interest_squeeze_score_bounded(session: Session) -> None:
    """squeeze_score is in [0.0, 1.0]."""
    _add_ticker(session, "MEME", shares_outstanding=500_000, avg_daily_volume_shares=50_000)
    _add_short_interest(
        session,
        "MEME",
        settlement_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        current_short_shares=400_000,
        days_to_cover=8.0,
    )
    _add_borrow_cost_daily(
        session,
        "MEME",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=40.0,
    )
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=("MEME",)))

    score = result.per_ticker[0].squeeze_score
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_short_interest_short_volume_ratio_5d_average(session: Session) -> None:
    """short_volume_ratio_5d is the 5-day mean of short_volume / total_volume."""
    _add_ticker(session, "NVDA")
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_short_volume(
            session,
            "NVDA",
            trade_date=date,
            short_volume=50_000,
            total_volume=100_000,
        )
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=("NVDA",)))

    # 50k/100k = 0.5 each day
    assert result.per_ticker[0].short_volume_ratio_5d == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Tests: missing data / partial quality
# ---------------------------------------------------------------------------


def test_short_interest_ticker_not_in_universe_unavailable(session: Session) -> None:
    """Ticker not in AssetUniverse returns per-ticker UNAVAILABLE row."""
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=("UNKNWN",)))

    assert result.per_ticker[0].quality == ToolQuality.UNAVAILABLE


def test_short_interest_no_data_returns_partial(session: Session) -> None:
    """Ticker in universe but no data rows returns PARTIAL quality."""
    _add_ticker(session, "MSFT")
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=("MSFT",)))

    assert result.per_ticker[0].quality == ToolQuality.PARTIAL
    assert result.per_ticker[0].short_interest_pct is None


def test_short_interest_no_shares_outstanding_partial(session: Session) -> None:
    """When shares_outstanding is None, short_interest_pct is None → PARTIAL."""
    _add_ticker(session, "AAPL", shares_outstanding=None)
    _add_short_interest(
        session,
        "AAPL",
        settlement_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        current_short_shares=100_000,
    )
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=("AAPL",)))

    assert result.per_ticker[0].short_interest_pct is None
    assert result.per_ticker[0].quality == ToolQuality.PARTIAL


# ---------------------------------------------------------------------------
# Tests: invalid input
# ---------------------------------------------------------------------------


def test_short_interest_empty_tickers_returns_unavailable(session: Session) -> None:
    """Empty tickers tuple returns envelope-level UNAVAILABLE without raising."""
    session.commit()

    fn = short_interest_factory(session)
    result = fn(ShortInterestInput(tickers=()))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.per_ticker == ()
    assert isinstance(result.data_freshness, datetime)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_short_interest_deterministic_output(session: Session) -> None:
    """Two calls with same data return identical payloads."""
    _add_ticker(session, "TSLA", shares_outstanding=800_000, avg_daily_volume_shares=100_000)
    _add_short_interest(
        session,
        "TSLA",
        settlement_date=(_NOW - timedelta(days=2)).strftime("%Y-%m-%d"),
        current_short_shares=100_000,
        days_to_cover=1.0,
    )
    _add_borrow_cost_daily(
        session,
        "TSLA",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=3.0,
    )
    session.commit()

    fn = short_interest_factory(session)
    r1 = fn(ShortInterestInput(tickers=("TSLA",)))
    r2 = fn(ShortInterestInput(tickers=("TSLA",)))

    assert r1.per_ticker == r2.per_ticker
    assert r1.quality == r2.quality
