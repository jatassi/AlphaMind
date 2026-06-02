"""Shared test fixtures for the external distillation test suite.

Hoisted duplication (ALP-792):
- engine/session in-memory fixtures (was ~15 files)
- _build_distillation_config + the ~80-line DistillationConfig(...) literal (was 4 files)

The engine uses StaticPool + check_same_thread=False so that tests for
orchestrator and q3 (which exercise asyncio.to_thread paths) share one
in-memory DB across threads. This variant is safe for single-threaded tests too.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Pull in the state-persistence tables so ``Base.metadata.create_all`` materializes
# the ``invocations`` and ``process_lifetimes`` rows the briefs FK targets.
import alphamind.state.tables  # noqa: F401
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
from alphamind.persistence.models import Base

# ---------------------------------------------------------------------------
# In-memory SQLite scaffolding — shared across asyncio.to_thread workers
# ---------------------------------------------------------------------------
#
# The orchestrator (and q3 under it) uses ``asyncio.to_thread`` to drive
# synchronous DB calls off the event loop. SQLite ``:memory:`` databases are
# per-connection by default, so a multi-thread test must pin ``StaticPool``
# plus ``check_same_thread=False`` to share one underlying connection across
# every worker. Production runs against a file-backed SQLite where each
# thread opens its own connection — the production session factory does
# not need this scaffolding.


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite (StaticPool for cross-thread sharing)."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Yield a session bound to the per-test engine fixture."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Distillation config builder — produces a complete config from defaults
# ---------------------------------------------------------------------------


def _build_distillation_config(
    *,
    volume_anomaly_sigma: float = 2.0,
    price_move_atr_multiple: float = 2.5,
    volume_baseline_days: int = 20,
    atr_baseline_days: int = 14,
) -> DistillationDomainConfig:
    """Return a fully-populated :class:`DistillationDomainConfig` for tests.

    Builds the Pydantic ``DistillationConfig`` (the boundary type) and
    projects it onto the frozen-dataclass mirror that distillation
    consumers take.
    """
    return DistillationConfig(
        anomaly_detection=AnomalyDetection(
            volume_anomaly_sigma=volume_anomaly_sigma,
            price_move_atr_multiple=price_move_atr_multiple,
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
            correlation_min_overlap_fraction=0.9,
            correlation_noise_floor=0.05,
            correlation_breakdown_fdr_q=0.05,
            correlation_locus_pair_count_threshold=3,
            narrative_lag_media_silence_hours=24,
        ),
        persistence_windows=PersistenceWindows(
            volume_baseline_days=volume_baseline_days,
            atr_baseline_days=atr_baseline_days,
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
