"""Tests for prediction_markets tool — ALP-247."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.analysis.tools import TOOLS, ToolQuality
from alphamind.analysis.tools.prediction_markets import (
    PredictionMarketsInput,
    PredictionMarketsOutput,
)
from alphamind.persistence.models import (
    Base,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
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
_TS1 = (_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
_TS2 = (_NOW - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_contract(
    session: Session,
    *,
    contract_id: str,
    description: str,
    platform: str = "Polymarket",
    category: str = "macro",
    prob_latest: float = 0.65,
    prob_prior: float | None = 0.60,
    volume: float = 50_000.0,
    resolution_outcome: str | None = None,
    resolution_date: str | None = None,
) -> None:
    session.add(
        PredictionMarketContracts(
            contract_id=contract_id,
            platform=platform,
            description=description,
            category=category,
            resolution_date=resolution_date,
            resolution_outcome=resolution_outcome,
            created_at="2026-01-01T00:00:00Z",
            last_seen_at=_TS1,
        )
    )
    session.flush()
    session.add(
        PredictionMarketSnapshots(
            contract_id=contract_id,
            snapshot_ts=_TS1,
            yes_probability=prob_latest,
            volume_24h_usd=volume,
            ingested_at=_TS1,
        )
    )
    if prob_prior is not None:
        session.add(
            PredictionMarketSnapshots(
                contract_id=contract_id,
                snapshot_ts=_TS2,
                yes_probability=prob_prior,
                volume_24h_usd=volume,
                ingested_at=_TS2,
            )
        )


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


def test_prediction_markets_query_returns_matching_contract(session: Session) -> None:
    _add_contract(session, contract_id="c1", description="FOMC rate cut by March 2026")
    _add_contract(session, contract_id="c2", description="Bitcoin above 100k by EOY")
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(query="FOMC"))

    assert len(result.contracts) == 1
    assert "FOMC" in result.contracts[0].description


def test_prediction_markets_query_is_case_insensitive(session: Session) -> None:
    _add_contract(session, contract_id="c1", description="FOMC rate cut in June")
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(query="fomc"))

    assert len(result.contracts) == 1


def test_prediction_markets_output_carries_envelope_fields(session: Session) -> None:
    _add_contract(session, contract_id="c1", description="FOMC June 2026")
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(query="FOMC"))

    assert isinstance(result.data_freshness, datetime)
    assert isinstance(result.quality, ToolQuality)


def test_prediction_markets_24h_delta_computed(session: Session) -> None:
    _add_contract(
        session,
        contract_id="c1",
        description="FOMC hike",
        prob_latest=0.70,
        prob_prior=0.50,
    )
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(query="FOMC"))

    contract = result.contracts[0]
    assert contract.current_probability == pytest.approx(0.70)
    assert contract.prob_24h_ago == pytest.approx(0.50)
    assert contract.prob_delta_24h_pp == pytest.approx(0.20)


def test_prediction_markets_category_filter(session: Session) -> None:
    _add_contract(session, contract_id="c1", description="Fed rate cut", category="macro")
    _add_contract(session, contract_id="c2", description="SPX at 6000", category="equity")
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(categories=("macro",)))

    assert len(result.contracts) == 1
    assert result.contracts[0].category == "macro"


def test_prediction_markets_quality_complete_with_results(session: Session) -> None:
    _add_contract(session, contract_id="c1", description="FOMC hike")
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(query="FOMC"))

    assert result.quality == ToolQuality.COMPLETE


# ---------------------------------------------------------------------------
# Invalid inputs / missing data
# ---------------------------------------------------------------------------


def test_prediction_markets_empty_query_and_categories_returns_unavailable(
    session: Session,
) -> None:
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput())

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.contracts == ()
    assert isinstance(result.data_freshness, datetime)


def test_prediction_markets_no_matching_contracts_returns_unavailable(
    session: Session,
) -> None:
    _add_contract(session, contract_id="c1", description="Bitcoin at 100k")
    session.commit()

    fn = TOOLS["prediction_markets"].callable_factory(session)
    result: PredictionMarketsOutput = fn(PredictionMarketsInput(query="FOMC"))

    assert result.quality == ToolQuality.UNAVAILABLE
    assert result.contracts == ()
