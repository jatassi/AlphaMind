"""Tests for sec_lending tool — ALP-259.

Uses an in-memory SQLite database for full integration coverage.
Each test covers one observable behavior via the public factory interface.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.tools._envelope import ToolQuality
from alphamind.analysis.tools.sec_lending import (
    SecLendingInput,
    SecLendingOutput,
    sec_lending_factory,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    BorrowCostDaily,
    BorrowCostIntraday,
    ShortInterestSnapshot,
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


def _add_borrow_cost_daily(
    session: Session,
    ticker: str,
    *,
    observation_date: str,
    fee_pct: float,
    available_shares: int = 500_000,
) -> None:
    session.add(
        BorrowCostDaily(
            observation_date=observation_date,
            ticker=ticker,
            fee_pct=fee_pct,
            available_shares=available_shares,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


def _add_borrow_cost_intraday(
    session: Session,
    ticker: str,
    *,
    snapshot_at: str,
    fee_pct: float,
    available_shares: int = 400_000,
) -> None:
    session.add(
        BorrowCostIntraday(
            snapshot_at=snapshot_at,
            ticker=ticker,
            fee_pct=fee_pct,
            available_shares=available_shares,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


def _add_short_interest(
    session: Session,
    ticker: str,
    *,
    settlement_date: str,
    current_short_shares: int,
    days_to_cover: float | None = None,
    avg_daily_volume_shares: int | None = None,
) -> None:
    session.add(
        ShortInterestSnapshot(
            settlement_date=settlement_date,
            ticker=ticker,
            current_short_shares=current_short_shares,
            days_to_cover=days_to_cover,
            avg_daily_volume_shares=avg_daily_volume_shares,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


def test_sec_lending_happy_path_complete_quality(session: Session) -> None:
    """Happy path: populated data returns COMPLETE quality envelope."""
    _add_ticker(session, "NVDA")
    # 5 days of borrow cost for trend
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_borrow_cost_daily(session, "NVDA", observation_date=date, fee_pct=1.0 + i * 0.1)
    _add_short_interest(
        session,
        "NVDA",
        settlement_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        current_short_shares=50_000,
        days_to_cover=0.5,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result: SecLendingOutput = fn(SecLendingInput(tickers=("NVDA",)))

    assert result.quality == ToolQuality.COMPLETE
    assert len(result.per_ticker) == 1
    row = result.per_ticker[0]
    assert row.ticker == "NVDA"
    assert row.borrow_rate_pct is not None
    assert isinstance(result.data_freshness, datetime)


def test_sec_lending_rising_trend_detected(session: Session) -> None:
    """Rising borrow cost over 5 days is classified as 'rising'."""
    _add_ticker(session, "GME")
    # Strongly increasing rates: +1 bp/day
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_borrow_cost_daily(session, "GME", observation_date=date, fee_pct=1.0 + i * 0.01)
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("GME",)))

    assert result.per_ticker[0].cost_trend == "rising"


def test_sec_lending_falling_trend_detected(session: Session) -> None:
    """Falling borrow cost over 5 days is classified as 'falling'."""
    _add_ticker(session, "AMC")
    # Strongly decreasing rates
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_borrow_cost_daily(session, "AMC", observation_date=date, fee_pct=5.0 - i * 0.01)
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("AMC",)))

    assert result.per_ticker[0].cost_trend == "falling"


def test_sec_lending_intraday_used_when_available(session: Session) -> None:
    """When intraday data exists, it is preferred over daily for borrow_rate_pct."""
    _add_ticker(session, "AAPL")
    _add_borrow_cost_daily(
        session,
        "AAPL",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=2.0,
    )
    _add_borrow_cost_intraday(
        session,
        "AAPL",
        snapshot_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        fee_pct=3.5,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("AAPL",)))

    # Should use the intraday rate of 3.5
    assert result.per_ticker[0].borrow_rate_pct == pytest.approx(3.5)


def test_sec_lending_days_to_cover_populated(session: Session) -> None:
    """days_to_cover is computed when short interest is available."""
    _add_ticker(session, "TSLA", shares_outstanding=1_000_000, avg_daily_volume_shares=100_000)
    _add_borrow_cost_daily(
        session,
        "TSLA",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=1.5,
    )
    _add_short_interest(
        session,
        "TSLA",
        settlement_date=(_NOW - timedelta(days=2)).strftime("%Y-%m-%d"),
        current_short_shares=200_000,
        days_to_cover=2.0,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("TSLA",)))

    assert result.per_ticker[0].days_to_cover is not None


def test_sec_lending_envelope_data_freshness_is_min_per_ticker(session: Session) -> None:
    """Aggregate data_freshness is the minimum across per-ticker freshness."""
    _add_ticker(session, "NVDA")
    _add_ticker(session, "AAPL")
    # NVDA has recent data, AAPL has older data
    _add_borrow_cost_daily(
        session,
        "NVDA",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=1.0,
    )
    _add_borrow_cost_daily(
        session,
        "AAPL",
        observation_date=(_NOW - timedelta(days=5)).strftime("%Y-%m-%d"),
        fee_pct=2.0,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("NVDA", "AAPL")))

    assert isinstance(result.data_freshness, datetime)
    # Freshness should be the min of all per-ticker freshness dates
    freshness_values = [r.data_freshness for r in result.per_ticker]
    assert result.data_freshness == min(freshness_values)


# ---------------------------------------------------------------------------
# Tests: missing data / partial quality
# ---------------------------------------------------------------------------


def test_sec_lending_ticker_not_in_universe_per_row_unavailable(session: Session) -> None:
    """Ticker not in AssetUniverse returns a per-ticker UNAVAILABLE row."""
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("UNKNWN",)))

    assert len(result.per_ticker) == 1
    assert result.per_ticker[0].quality == ToolQuality.UNAVAILABLE


def test_sec_lending_no_borrow_data_returns_partial(session: Session) -> None:
    """Ticker in universe but no BorrowCostDaily rows returns PARTIAL per-ticker quality."""
    _add_ticker(session, "MSFT")
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("MSFT",)))

    assert result.per_ticker[0].quality == ToolQuality.PARTIAL
    assert result.per_ticker[0].borrow_rate_pct is None


def test_sec_lending_no_short_interest_days_to_cover_none(session: Session) -> None:
    """Without short interest data, days_to_cover is None and quality is PARTIAL."""
    _add_ticker(session, "META")
    _add_borrow_cost_daily(
        session,
        "META",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=0.5,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("META",)))

    assert result.per_ticker[0].days_to_cover is None
    assert result.per_ticker[0].quality == ToolQuality.PARTIAL


def test_sec_lending_mixed_universe_valid_and_unknown(session: Session) -> None:
    """Valid ticker proceeds; unknown ticker gets UNAVAILABLE row."""
    _add_ticker(session, "NVDA")
    _add_borrow_cost_daily(
        session,
        "NVDA",
        observation_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        fee_pct=1.0,
    )
    _add_short_interest(
        session,
        "NVDA",
        settlement_date=(_NOW - timedelta(days=2)).strftime("%Y-%m-%d"),
        current_short_shares=50_000,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=("NVDA", "UNKNWN")))

    tickers_out = {r.ticker for r in result.per_ticker}
    assert "NVDA" in tickers_out
    assert "UNKNWN" in tickers_out
    nvda_row = next(r for r in result.per_ticker if r.ticker == "NVDA")
    unknwn_row = next(r for r in result.per_ticker if r.ticker == "UNKNWN")
    assert nvda_row.quality != ToolQuality.UNAVAILABLE
    assert unknwn_row.quality == ToolQuality.UNAVAILABLE


# ---------------------------------------------------------------------------
# Tests: invalid input
# ---------------------------------------------------------------------------


def test_sec_lending_empty_tickers_returns_unavailable(session: Session) -> None:
    """Empty tickers tuple returns UNAVAILABLE without raising."""
    session.commit()

    fn = sec_lending_factory(session)
    result = fn(SecLendingInput(tickers=()))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.per_ticker == ()
    assert isinstance(result.data_freshness, datetime)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_sec_lending_deterministic_output(session: Session) -> None:
    """Two calls against the same data return identical payloads."""
    _add_ticker(session, "NVDA")
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_borrow_cost_daily(session, "NVDA", observation_date=date, fee_pct=1.0 + i * 0.1)
    _add_short_interest(
        session,
        "NVDA",
        settlement_date=(_NOW - timedelta(days=1)).strftime("%Y-%m-%d"),
        current_short_shares=50_000,
        days_to_cover=0.5,
    )
    session.commit()

    fn = sec_lending_factory(session)
    result1 = fn(SecLendingInput(tickers=("NVDA",)))
    result2 = fn(SecLendingInput(tickers=("NVDA",)))

    assert result1.per_ticker == result2.per_ticker
    assert result1.quality == result2.quality
