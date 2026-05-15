"""Pure-compute integration test for the q1 assemble path.

Story ALP-467: ``assemble_q1_blocks_from_inputs`` takes a pre-loaded
:class:`Q1Inputs` and returns the same blocks as ``assemble_q1_blocks``
would for the equivalent session reads — but without touching the
database. The shell (loaders) is exercised separately; this test pins
the seam between shell and core.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.config.models.distillation import (
    AnomalyDetection,
    DistillationConfig,
    LeadLag,
    LeadLagPair,
    NarrativeLag,
    PersistenceWindows,
    PredictionMarket,
    RegimeClassification,
    RegimeTransition,
)
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.q1._loaders import load_q1_inputs
from alphamind.distillation.q1.assemble import (
    assemble_q1_blocks,
    assemble_q1_blocks_from_inputs,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    OhlcvBars,
    SectorClassification,
)
from alphamind.persistence.session import make_engine, make_session_factory


def _build_test_config() -> DistillationDomainConfig:
    """Minimal DistillationDomainConfig for q1 pure-path tests.

    Builds the Pydantic ``DistillationConfig`` (the boundary type) and
    projects it onto the frozen-dataclass mirror that distillation
    consumers take.
    """
    return DistillationConfig(
        anomaly_detection=AnomalyDetection(
            volume_anomaly_sigma=2.5,
            price_move_atr_multiple=1.5,
            options_low_oi_volume_multiple=5.0,
            block_trade_min_shares=10_000,
            block_trade_min_notional_usd=1_000_000,
            dark_pool_one_sided_window_minutes=60,
            earnings_revision_cluster_count=3,
            earnings_revision_cluster_days=5,
            macro_surprise_percentile=90,
            funding_stress_component_alert_count=2,
            funding_stress_component_percentile=80,
            market_liquidity_alert_percentile=10,
            news_price_divergence_window_hours=12,
            news_price_divergence_min_articles=5,
        ),
        regime_classification=RegimeClassification(
            regime_low_vol_vix_max=15.0,
            regime_normal_vix_min=15.0,
            regime_normal_vix_max=20.0,
            regime_elevated_vix_min=20.0,
            regime_elevated_vix_max=28.0,
            regime_crisis_vix_min=28.0,
            regime_term_structure_backwardation_threshold=0.0,
            regime_vvix_high_percentile=80,
            regime_vvix_low_percentile=20,
        ),
        regime_transition=RegimeTransition(
            regime_transition_confirmed_invocations=3,
            regime_transition_indicator_agreement_min=3,
            regime_skip_emergency_trigger=True,
        ),
        lead_lag=LeadLag(
            pairs=(
                LeadLagPair(key="credit_to_equity", lead="HYG", lag="SPY"),
                LeadLagPair(key="semis_to_tech", lead="SOXX", lag="QQQ"),
                LeadLagPair(key="financials_to_market", lead="XLF", lag="SPY"),
                LeadLagPair(key="commodity_to_energy_equity", lead="USO", lag="XLE"),
            ),
            lead_lag_funding_to_credit_max_days=4,
            lead_lag_credit_to_equity_max_days=4,
            lead_lag_semis_to_tech_max_days=3,
            lead_lag_financials_to_market_max_days=4,
            lead_lag_commodity_to_energy_equity_max_days=4,
            lead_lag_overdue_lead_sigma=2.0,
        ),
        narrative_lag=NarrativeLag(
            narrative_lag_correlation_shift_sigma=2.0,
            correlation_breakdown_sigma=2.0,
            narrative_lag_media_silence_hours=24,
        ),
        persistence_windows=PersistenceWindows(
            volume_baseline_days=20,
            atr_baseline_days=14,
            spread_baseline_days=20,
            correlation_short_days=20,
            correlation_long_days=60,
            sentiment_baseline_days=30,
            sentiment_min_observations=5,
            gap_fill_baseline_days=60,
            gap_fill_min_events=3,
            extended_hours_confirmation_days=30,
            extended_hours_min_events=3,
            prediction_market_history_days=30,
            funding_stress_baseline_days=60,
            market_liquidity_baseline_days=60,
        ),
        prediction_market=PredictionMarket(
            prediction_market_delta_pp_threshold=10.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={},
        ),
    ).to_domain()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _add_ticker(session: Session, ticker: str, sector: str, etf: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=sector,
            domain_researcher="tech_semis",
            sector_etf=etf,
            classification_source="manual",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_bars(
    session: Session,
    *,
    ticker: str,
    days: int,
) -> None:
    base = 100.0
    for d in range(1, days + 1):
        ts = f"2026-04-{d:02d}T00:00:00Z"
        close = base + d * 0.5
        session.add(
            OhlcvBars(
                ticker=ticker,
                timeframe="1d",
                period_start=ts,
                period_end=ts,
                session="regular",
                adj_open=close,
                adj_high=close + 1.0,
                adj_low=close - 1.0,
                adj_close=close,
                adj_volume=1_000_000,
                unadj_open=close,
                unadj_high=close + 1.0,
                unadj_low=close - 1.0,
                unadj_close=close,
                unadj_volume=1_000_000,
                source="polygon",
                ingested_at=ts,
            )
        )


def test_pure_path_matches_session_path_on_empty_universe(session: Session) -> None:
    """An empty universe collapses to the same empty list from both entry points."""
    session.commit()
    config = _build_test_config()
    as_of = datetime(2026, 4, 25, tzinfo=UTC)

    legacy = assemble_q1_blocks(session, config=config, as_of=as_of)

    repo = SqlDistillationRepository(session)
    inputs = load_q1_inputs(repo, config=config, as_of=as_of, ticker_scope=None)
    pure = assemble_q1_blocks_from_inputs(inputs, config=config)

    assert legacy == []
    assert pure == []


def test_pure_path_matches_session_path_on_populated_universe(
    session: Session,
) -> None:
    """The pure path returns the exact same blocks as the legacy session path."""
    _add_ticker(session, "AAPL", sector="tech", etf="XLK")
    _add_bars(session, ticker=Symbol("AAPL"), days=30)
    session.commit()
    config = _build_test_config()
    as_of = datetime(2026, 4, 25, tzinfo=UTC)

    legacy = assemble_q1_blocks(session, config=config, as_of=as_of)

    repo = SqlDistillationRepository(session)
    inputs = load_q1_inputs(repo, config=config, as_of=as_of, ticker_scope=None)
    pure = assemble_q1_blocks_from_inputs(inputs, config=config)

    # Same blocks — by block_id ordering and payload contents.
    assert len(pure) == len(legacy)
    for pb, lb in zip(
        sorted(pure, key=lambda b: b.block_id),
        sorted(legacy, key=lambda b: b.block_id),
        strict=True,
    ):
        assert pb.block_id == lb.block_id
        assert pb.audience == lb.audience
        assert pb.payload == lb.payload
        assert pb.calibration_state == lb.calibration_state
