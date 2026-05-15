"""
Tests for ``src/alphamind/distillation/contract_scope.py``.

Covers the acceptance criteria for ALP-274 § Acceptance:
empty ``tracked_categories``, single-category resolution, per-category
``min_volume_24h_usd`` override, missing-category in DB, ISO 8601
``as_of`` boundary comparison.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session, sessionmaker

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
    TrackedCategoryOverride,
)
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation.contract_scope import resolve_prediction_market_scope
from alphamind.persistence.models import (
    Base,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _build_config(
    *,
    tracked_categories: dict[str, TrackedCategoryOverride] | None = None,
    tracked_default_min_volume_24h_usd: int = 5_000,
) -> DistillationDomainConfig:
    return DistillationConfig(
        anomaly_detection=AnomalyDetection(
            volume_anomaly_sigma=2.0,
            price_move_atr_multiple=2.5,
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
            regime_transition_confirmed_invocations=2,
            regime_transition_indicator_agreement_min=2,
            regime_skip_emergency_trigger=True,
        ),
        lead_lag=LeadLag(
            pairs=(LeadLagPair(key="credit_to_equity", lead="HYG", lag="SPY"),),
            lead_lag_funding_to_credit_max_days=3,
            lead_lag_credit_to_equity_max_days=3,
            lead_lag_semis_to_tech_max_days=2,
            lead_lag_financials_to_market_max_days=1,
            lead_lag_commodity_to_energy_equity_max_days=1,
            lead_lag_overdue_lead_sigma=1.5,
        ),
        narrative_lag=NarrativeLag(
            narrative_lag_correlation_shift_sigma=1.5,
            correlation_breakdown_sigma=3.0,
            narrative_lag_media_silence_hours=12,
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
            prediction_market_delta_pp_threshold=5.0,
            prediction_market_low_liquidity_volume_min_usd=10_000,
            tracked_default_min_volume_24h_usd=tracked_default_min_volume_24h_usd,
            tracked_categories=tracked_categories or {},
        ),
    ).to_domain()


# ---------------------------------------------------------------------------
# DB fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_contract(
    session: Session,
    *,
    contract_id: str,
    category: str,
    resolution_date: str | None,
    snapshots: list[tuple[str, float | None]],
    platform: str = "polymarket",
) -> None:
    session.add(
        PredictionMarketContracts(
            contract_id=contract_id,
            platform=platform,
            description=f"desc {contract_id}",
            category=category,
            resolution_date=resolution_date,
            resolution_outcome=None,
            created_at="2026-01-01T00:00:00Z",
            last_seen_at="2026-04-01T00:00:00Z",
        )
    )
    for snapshot_ts, volume in snapshots:
        session.add(
            PredictionMarketSnapshots(
                contract_id=contract_id,
                snapshot_ts=snapshot_ts,
                yes_probability=0.5,
                volume_24h_usd=volume,
                liquidity_usd=20_000.0,
                bid=0.49,
                ask=0.51,
                ingested_at=snapshot_ts,
            )
        )


_AS_OF = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Acceptance-criteria cases
# ---------------------------------------------------------------------------


class TestResolveScope:
    def test_empty_tracked_categories_returns_empty_tuple(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        config = _build_config(tracked_categories={})

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="ct-1",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            session.commit()

            assert resolve_prediction_market_scope(session, config=config, as_of=_AS_OF) == ()

    def test_single_category_resolution(self, session_factory: sessionmaker[Session]) -> None:
        config = _build_config(tracked_categories={"monetary_policy": TrackedCategoryOverride()})

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="ct-fed",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            _seed_contract(
                session,
                contract_id="ct-elec",
                category="election",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            session.commit()

            scope = resolve_prediction_market_scope(session, config=config, as_of=_AS_OF)
            assert scope == ("ct-fed",)

    def test_per_category_override_supersedes_default(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        config = _build_config(
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={
                "opec": TrackedCategoryOverride(min_volume_24h_usd=1_000),
                "election": TrackedCategoryOverride(min_volume_24h_usd=25_000),
            },
        )

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="opec-low",
                category="opec",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 1_500.0)],
            )
            _seed_contract(
                session,
                contract_id="elec-mid",
                category="election",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 10_000.0)],
            )
            _seed_contract(
                session,
                contract_id="elec-hi",
                category="election",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 30_000.0)],
            )
            session.commit()

            scope = resolve_prediction_market_scope(session, config=config, as_of=_AS_OF)
            # opec floor=1000 → opec-low passes; election floor=25000 →
            # only elec-hi passes (elec-mid below the override).
            assert scope == ("elec-hi", "opec-low")

    def test_missing_category_in_db_yields_empty_tuple(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Tracking a category with no contracts in DB just returns empty."""
        config = _build_config(tracked_categories={"sanctions": TrackedCategoryOverride()})

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="ct-fed",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            session.commit()

            assert resolve_prediction_market_scope(session, config=config, as_of=_AS_OF) == ()

    def test_resolution_date_at_or_before_as_of_excluded(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Contracts whose resolution_date has passed are excluded."""
        config = _build_config(tracked_categories={"monetary_policy": TrackedCategoryOverride()})

        with session_factory() as session:
            # resolution_date strictly equal to as_of → excluded (not strictly >)
            _seed_contract(
                session,
                contract_id="boundary",
                category="monetary_policy",
                resolution_date="2026-05-01T12:00:00Z",
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            # resolution_date one second after as_of → included
            _seed_contract(
                session,
                contract_id="future",
                category="monetary_policy",
                resolution_date="2026-05-01T12:00:01Z",
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            # resolution_date in past → excluded
            _seed_contract(
                session,
                contract_id="past",
                category="monetary_policy",
                resolution_date="2026-04-15T00:00:00Z",
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            # NULL resolution_date → included (perpetual market)
            _seed_contract(
                session,
                contract_id="null-res",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            session.commit()

            scope = resolve_prediction_market_scope(session, config=config, as_of=_AS_OF)
            assert scope == ("future", "null-res")

    def test_contract_with_no_snapshot_dropped(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        config = _build_config(tracked_categories={"monetary_policy": TrackedCategoryOverride()})

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="ct-fed",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 100_000.0)],
            )
            _seed_contract(
                session,
                contract_id="ct-no-snap",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[],
            )
            session.commit()

            scope = resolve_prediction_market_scope(session, config=config, as_of=_AS_OF)
            assert scope == ("ct-fed",)

    def test_volume_at_floor_is_included(self, session_factory: sessionmaker[Session]) -> None:
        config = _build_config(
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={"monetary_policy": TrackedCategoryOverride()},
        )

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="exact",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 5_000.0)],
            )
            _seed_contract(
                session,
                contract_id="below",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[("2026-04-30T00:00:00Z", 4_999.0)],
            )
            session.commit()

            scope = resolve_prediction_market_scope(session, config=config, as_of=_AS_OF)
            assert scope == ("exact",)

    def test_latest_snapshot_volume_used_not_earlier(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """When a contract has multiple snapshots, the most recent volume
        applies — earlier above-floor volumes do not keep the contract in
        scope after liquidity collapses."""
        config = _build_config(
            tracked_default_min_volume_24h_usd=5_000,
            tracked_categories={"monetary_policy": TrackedCategoryOverride()},
        )

        with session_factory() as session:
            _seed_contract(
                session,
                contract_id="collapsed",
                category="monetary_policy",
                resolution_date=None,
                snapshots=[
                    ("2026-04-01T00:00:00Z", 100_000.0),
                    ("2026-04-30T00:00:00Z", 100.0),
                ],
            )
            session.commit()

            assert resolve_prediction_market_scope(session, config=config, as_of=_AS_OF) == ()


# ---------------------------------------------------------------------------
# Pydantic validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_invalid_category_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="not in canonical taxonomy"):
            _build_config(tracked_categories={"made_up_category": TrackedCategoryOverride()})
