"""Tests for macro_data tool — ALP-259.

Uses an in-memory SQLite database for full integration coverage.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.tools._envelope import ToolQuality
from alphamind.analysis.tools.macro_data import (
    MacroDataInput,
    MacroDataOutput,
    macro_data_factory,
)
from alphamind.persistence.models import (
    Base,
    MacroObservations,
    TreasuryAuctions,
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


def _add_macro_obs(
    session: Session,
    *,
    source: str,
    series_id: str,
    observation_date: str,
    value: float,
    revision_number: int = 0,
) -> None:
    session.add(
        MacroObservations(
            source=source,
            series_id=series_id,
            observation_date=observation_date,
            revision_number=revision_number,
            value=value,
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


def _add_treasury_auction(
    session: Session,
    *,
    auction_id: str,
    tenor: str,
    auction_date: str,
    auction_yield_bp: float,
) -> None:
    session.add(
        TreasuryAuctions(
            auction_id=auction_id,
            tenor=tenor,
            auction_date=auction_date,
            auction_yield_bp=auction_yield_bp,
            source="test",
            ingested_at=_NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ---------------------------------------------------------------------------
# Tests: happy path — MacroObservations
# ---------------------------------------------------------------------------


def test_macro_data_known_indicator_returns_complete(session: Session) -> None:
    """Known indicator with data returns COMPLETE quality envelope."""
    for i in range(10):
        date = (_NOW - timedelta(days=9 - i)).strftime("%Y-%m-%d")
        _add_macro_obs(
            session,
            source="fred",
            series_id="VIXCLS",
            observation_date=date,
            value=15.0 + i * 0.5,
        )
    session.commit()

    fn = macro_data_factory(session)
    result: MacroDataOutput = fn(MacroDataInput(indicator="vix"))

    assert result.quality == ToolQuality.COMPLETE
    assert len(result.series) > 0
    assert result.indicator == "vix"
    assert isinstance(result.data_freshness, datetime)


def test_macro_data_series_points_have_required_fields(session: Session) -> None:
    """Each MacroDataPoint carries date (ISO str) and value (float)."""
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_macro_obs(
            session,
            source="fred",
            series_id="VIXCLS",
            observation_date=date,
            value=20.0 + i,
        )
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="vix", lookback_days=10))

    for pt in result.series:
        assert isinstance(pt.date, str)
        assert isinstance(pt.value, float)


def test_macro_data_change_1d_computed(session: Session) -> None:
    """change_1d is delta vs. prior trading day observation."""
    # Two consecutive observations
    yesterday = (_NOW - timedelta(days=1)).strftime("%Y-%m-%d")
    today = _NOW.strftime("%Y-%m-%d")
    _add_macro_obs(
        session, source="fred", series_id="VIXCLS", observation_date=yesterday, value=20.0
    )
    _add_macro_obs(session, source="fred", series_id="VIXCLS", observation_date=today, value=22.0)
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="vix", lookback_days=5))

    # Most recent point should have change_1d = 22.0 - 20.0 = 2.0
    most_recent = max(result.series, key=lambda p: p.date)
    assert most_recent.change_1d == pytest.approx(2.0)


def test_macro_data_percentile_1y_in_range(session: Session) -> None:
    """percentile_1y is in [0.0, 1.0] when 252 days of data are available."""
    # Seed 252 observations
    for i in range(252):
        date = (_NOW - timedelta(days=251 - i)).strftime("%Y-%m-%d")
        _add_macro_obs(
            session,
            source="fred",
            series_id="VIXCLS",
            observation_date=date,
            value=float(10 + i % 30),
        )
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="vix", lookback_days=30))

    for pt in result.series:
        if pt.percentile_1y is not None:
            assert 0.0 <= pt.percentile_1y <= 1.0


# ---------------------------------------------------------------------------
# Tests: happy path — TreasuryAuctions alias
# ---------------------------------------------------------------------------


def test_macro_data_treasury_2y_alias_resolves(session: Session) -> None:
    """treasury_2y indicator resolves via TreasuryAuctions table."""
    for i in range(5):
        date = (_NOW - timedelta(days=4 - i)).strftime("%Y-%m-%d")
        _add_treasury_auction(
            session,
            auction_id=f"t2y-{i}",
            tenor="2-Year",
            auction_date=date,
            auction_yield_bp=450.0 + i * 5,
        )
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="treasury_2y", lookback_days=10))

    assert result.quality == ToolQuality.COMPLETE
    assert len(result.series) > 0


def test_macro_data_treasury_10y_alias_resolves(session: Session) -> None:
    """treasury_10y indicator resolves via TreasuryAuctions table."""
    for i in range(3):
        date = (_NOW - timedelta(days=2 - i)).strftime("%Y-%m-%d")
        _add_treasury_auction(
            session,
            auction_id=f"t10y-{i}",
            tenor="10-Year",
            auction_date=date,
            auction_yield_bp=420.0 + i * 3,
        )
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="treasury_10y", lookback_days=10))

    assert result.quality == ToolQuality.COMPLETE
    assert len(result.series) > 0


# ---------------------------------------------------------------------------
# Tests: missing data / UNAVAILABLE for unknown indicators
# ---------------------------------------------------------------------------


def test_macro_data_unknown_indicator_returns_unavailable(session: Session) -> None:
    """Unknown indicator returns UNAVAILABLE envelope with empty series."""
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="totally_unknown_xyz"))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.series == ()
    assert isinstance(result.data_freshness, datetime)


def test_macro_data_empty_indicator_returns_unavailable(session: Session) -> None:
    """Empty indicator string returns UNAVAILABLE without raising."""
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator=""))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.series == ()


def test_macro_data_known_indicator_no_rows_returns_unavailable(session: Session) -> None:
    """Known indicator with no database rows returns UNAVAILABLE."""
    session.commit()

    fn = macro_data_factory(session)
    result = fn(MacroDataInput(indicator="vix"))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.series == ()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_macro_data_deterministic_output(session: Session) -> None:
    """Two calls against the same fixture data return identical payloads."""
    for i in range(10):
        date = (_NOW - timedelta(days=9 - i)).strftime("%Y-%m-%d")
        _add_macro_obs(
            session,
            source="fred",
            series_id="VIXCLS",
            observation_date=date,
            value=15.0 + i,
        )
    session.commit()

    fn = macro_data_factory(session)
    r1 = fn(MacroDataInput(indicator="vix", lookback_days=30))
    r2 = fn(MacroDataInput(indicator="vix", lookback_days=30))

    assert r1.series == r2.series
    assert r1.quality == r2.quality
