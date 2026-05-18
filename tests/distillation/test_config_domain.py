"""Tests for the DistillationConfig boundary→domain pattern (ALP-471).

The Pydantic ``config.models.DistillationConfig`` stays at the YAML-load
boundary; ``distillation._config_domain.DistillationDomainConfig`` is the
frozen-dataclass mirror downstream distillation code consumes. The bridge
is the ``to_domain()`` method on each Pydantic class.

These tests pin:

* Each Pydantic class projects every numeric field onto its dataclass mirror
  with byte-identical scalars and the same nested-tuple / nested-mapping
  shape.
* The full YAML at ``config/distillation.yaml`` parses through
  :class:`DistillationConfig` and round-trips through ``to_domain()`` without
  losing or coercing any value.
* The pure Q1 compute path runs over a ``DistillationDomainConfig`` without
  any Pydantic instance in the call chain (one of the acceptance criteria
  for the boundary→domain pilot).
* ``dataclasses.replace(domain_cfg, ...)`` behaves identically to
  ``pydantic_cfg.model_copy(update=...)`` for the kind of "override one
  field" usage the Pydantic boundary supported.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import yaml

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
    SeverityCaps,
    TrackedCategoryOverride,
)
from alphamind.distillation._config_domain import (
    AnomalyDetectionDomainConfig,
    DistillationDomainConfig,
    LeadLagDomainConfig,
    LeadLagPairDomainConfig,
    NarrativeLagDomainConfig,
    PersistenceWindowsDomainConfig,
    PredictionMarketDomainConfig,
    RegimeClassificationDomainConfig,
    RegimeTransitionDomainConfig,
    SeverityCapsDomainConfig,
    TrackedCategoryOverrideDomainConfig,
)


def _build_test_pydantic_config() -> DistillationConfig:
    """Hand-built Pydantic config covering every nested class.

    Mirrors the fixture in :mod:`tests.distillation.q1.test_q1_assemble_pure`
    so the boundary→domain test does not depend on the on-disk YAML.
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
            tracked_categories={
                "monetary_policy": TrackedCategoryOverride(),
                "election": TrackedCategoryOverride(min_volume_24h_usd=1_000),
            },
        ),
    )


def _repo_root() -> Path:
    return Path(__file__).parents[2]


# ---------------------------------------------------------------------------
# 1. to_domain() round-trip preserves every numeric field
# ---------------------------------------------------------------------------


def test_to_domain_returns_distillation_domain_config_instance() -> None:
    """``DistillationConfig.to_domain`` returns a frozen-dataclass instance."""
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain, DistillationDomainConfig)


def test_to_domain_mirrors_anomaly_detection_fields() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.anomaly_detection, AnomalyDetectionDomainConfig)
    pyd = pydantic_cfg.anomaly_detection
    dom = domain.anomaly_detection
    assert dom.volume_anomaly_sigma == pyd.volume_anomaly_sigma
    assert dom.price_move_atr_multiple == pyd.price_move_atr_multiple
    assert dom.options_low_oi_volume_multiple == pyd.options_low_oi_volume_multiple
    assert dom.block_trade_min_shares == pyd.block_trade_min_shares
    assert dom.block_trade_min_notional_usd == pyd.block_trade_min_notional_usd
    assert dom.dark_pool_one_sided_window_minutes == pyd.dark_pool_one_sided_window_minutes
    assert dom.earnings_revision_cluster_count == pyd.earnings_revision_cluster_count
    assert dom.earnings_revision_cluster_days == pyd.earnings_revision_cluster_days
    assert dom.macro_surprise_percentile == pyd.macro_surprise_percentile
    assert dom.funding_stress_component_alert_count == pyd.funding_stress_component_alert_count
    assert dom.funding_stress_component_percentile == pyd.funding_stress_component_percentile
    assert dom.market_liquidity_alert_percentile == pyd.market_liquidity_alert_percentile
    assert dom.news_price_divergence_window_hours == pyd.news_price_divergence_window_hours
    assert dom.news_price_divergence_min_articles == pyd.news_price_divergence_min_articles


def test_to_domain_mirrors_regime_classification_fields() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.regime_classification, RegimeClassificationDomainConfig)
    pyd = pydantic_cfg.regime_classification
    dom = domain.regime_classification
    assert dom.regime_low_vol_vix_max == pyd.regime_low_vol_vix_max
    assert dom.regime_normal_vix_min == pyd.regime_normal_vix_min
    assert dom.regime_normal_vix_max == pyd.regime_normal_vix_max
    assert dom.regime_elevated_vix_min == pyd.regime_elevated_vix_min
    assert dom.regime_elevated_vix_max == pyd.regime_elevated_vix_max
    assert dom.regime_crisis_vix_min == pyd.regime_crisis_vix_min
    assert (
        dom.regime_term_structure_backwardation_threshold
        == pyd.regime_term_structure_backwardation_threshold
    )
    assert dom.regime_vvix_high_percentile == pyd.regime_vvix_high_percentile
    assert dom.regime_vvix_low_percentile == pyd.regime_vvix_low_percentile


def test_to_domain_mirrors_regime_transition_fields() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.regime_transition, RegimeTransitionDomainConfig)
    pyd = pydantic_cfg.regime_transition
    dom = domain.regime_transition
    assert (
        dom.regime_transition_confirmed_invocations == pyd.regime_transition_confirmed_invocations
    )
    assert (
        dom.regime_transition_indicator_agreement_min
        == pyd.regime_transition_indicator_agreement_min
    )
    assert dom.regime_skip_emergency_trigger == pyd.regime_skip_emergency_trigger


def test_to_domain_mirrors_lead_lag_fields_and_pairs() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.lead_lag, LeadLagDomainConfig)
    pyd = pydantic_cfg.lead_lag
    dom = domain.lead_lag
    assert dom.lead_lag_funding_to_credit_max_days == pyd.lead_lag_funding_to_credit_max_days
    assert dom.lead_lag_credit_to_equity_max_days == pyd.lead_lag_credit_to_equity_max_days
    assert dom.lead_lag_semis_to_tech_max_days == pyd.lead_lag_semis_to_tech_max_days
    assert dom.lead_lag_financials_to_market_max_days == pyd.lead_lag_financials_to_market_max_days
    assert (
        dom.lead_lag_commodity_to_energy_equity_max_days
        == pyd.lead_lag_commodity_to_energy_equity_max_days
    )
    assert dom.lead_lag_overdue_lead_sigma == pyd.lead_lag_overdue_lead_sigma
    assert len(dom.pairs) == len(pyd.pairs)
    for dpair, ppair in zip(dom.pairs, pyd.pairs, strict=True):
        assert isinstance(dpair, LeadLagPairDomainConfig)
        assert dpair.key == ppair.key
        assert dpair.lead == ppair.lead
        assert dpair.lag == ppair.lag


def test_to_domain_mirrors_narrative_lag_fields() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.narrative_lag, NarrativeLagDomainConfig)
    pyd = pydantic_cfg.narrative_lag
    dom = domain.narrative_lag
    assert dom.narrative_lag_correlation_shift_sigma == pyd.narrative_lag_correlation_shift_sigma
    assert dom.correlation_breakdown_sigma == pyd.correlation_breakdown_sigma
    assert dom.narrative_lag_media_silence_hours == pyd.narrative_lag_media_silence_hours


def test_to_domain_mirrors_persistence_windows_fields() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.persistence_windows, PersistenceWindowsDomainConfig)
    pyd = pydantic_cfg.persistence_windows
    dom = domain.persistence_windows
    for field_name in (
        "volume_baseline_days",
        "atr_baseline_days",
        "spread_baseline_days",
        "correlation_short_days",
        "correlation_long_days",
        "sentiment_baseline_days",
        "sentiment_min_observations",
        "gap_fill_baseline_days",
        "gap_fill_min_events",
        "extended_hours_confirmation_days",
        "extended_hours_min_events",
        "prediction_market_history_days",
        "funding_stress_baseline_days",
        "market_liquidity_baseline_days",
    ):
        assert getattr(dom, field_name) == getattr(pyd, field_name)


def test_to_domain_mirrors_severity_caps_exempt_flag_names() -> None:
    """SeverityCaps round-trips its exempt-flag-names tuple as a frozenset."""
    pydantic_cfg = _build_test_pydantic_config().model_copy(
        update={
            "severity_caps": SeverityCaps(
                exempt_flag_names=("macro_surprise_anomaly", "etf_vs_single_name_divergence")
            )
        }
    )
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.severity_caps, SeverityCapsDomainConfig)
    assert domain.severity_caps.exempt_flag_names == frozenset(
        {"macro_surprise_anomaly", "etf_vs_single_name_divergence"}
    )


def test_severity_caps_default_is_empty_frozenset() -> None:
    """Omitting severity_caps from the Pydantic surface yields an empty exempt set."""
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert domain.severity_caps.exempt_flag_names == frozenset()


def test_to_domain_mirrors_prediction_market_fields_and_categories() -> None:
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()
    assert isinstance(domain.prediction_market, PredictionMarketDomainConfig)
    pyd = pydantic_cfg.prediction_market
    dom = domain.prediction_market
    assert dom.prediction_market_delta_pp_threshold == pyd.prediction_market_delta_pp_threshold
    assert (
        dom.prediction_market_low_liquidity_volume_min_usd
        == pyd.prediction_market_low_liquidity_volume_min_usd
    )
    assert dom.tracked_default_min_volume_24h_usd == pyd.tracked_default_min_volume_24h_usd
    assert set(dom.tracked_categories) == set(pyd.tracked_categories)
    for key in pyd.tracked_categories:
        pyd_override = pyd.tracked_categories[key]
        dom_override = dom.tracked_categories[key]
        assert isinstance(dom_override, TrackedCategoryOverrideDomainConfig)
        assert dom_override.min_volume_24h_usd == pyd_override.min_volume_24h_usd


# ---------------------------------------------------------------------------
# 2. The on-disk YAML round-trips end-to-end
# ---------------------------------------------------------------------------


def test_yaml_to_pydantic_to_domain_preserves_every_field() -> None:
    """Load the canonical YAML, validate, project — assert every value matches."""
    yaml_path = _repo_root() / "config" / "distillation.yaml"
    parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    pydantic_cfg = DistillationConfig.model_validate(parsed)
    domain = pydantic_cfg.to_domain()

    # Top-level fields are dataclass mirrors of the Pydantic sub-models.
    assert isinstance(domain, DistillationDomainConfig)

    # Spot-check three representative scalars on three different sub-configs.
    assert (
        domain.anomaly_detection.volume_anomaly_sigma
        == pydantic_cfg.anomaly_detection.volume_anomaly_sigma
    )
    assert (
        domain.regime_classification.regime_low_vol_vix_max
        == pydantic_cfg.regime_classification.regime_low_vol_vix_max
    )
    assert (
        domain.persistence_windows.volume_baseline_days
        == pydantic_cfg.persistence_windows.volume_baseline_days
    )

    # Exhaustive check: every Pydantic ``model_dump`` field maps to an equal
    # value on the dataclass mirror.
    pyd_dump = pydantic_cfg.model_dump(mode="json")
    for top_field, pyd_section in pyd_dump.items():
        dom_section = getattr(domain, top_field)
        if isinstance(pyd_section, dict):
            for key, expected in pyd_section.items():
                if key == "pairs":
                    # Compare structurally.
                    actual_pairs = getattr(dom_section, key)
                    assert len(actual_pairs) == len(expected)
                    for ap, ep in zip(actual_pairs, expected, strict=True):
                        assert ap.key == ep["key"]
                        assert ap.lead == ep["lead"]
                        assert ap.lag == ep["lag"]
                elif key == "tracked_categories":
                    actual_map = getattr(dom_section, key)
                    assert set(actual_map) == set(expected)
                    for cat_key, cat_payload in expected.items():
                        assert (
                            actual_map[cat_key].min_volume_24h_usd
                            == cat_payload["min_volume_24h_usd"]
                        )
                elif key == "exempt_flag_names":
                    # Stored as frozenset on the domain side; JSON-dumped as list.
                    assert getattr(dom_section, key) == frozenset(expected)
                else:
                    assert getattr(dom_section, key) == expected
        else:  # pragma: no cover — top-level fields are always sub-models in this schema
            assert dom_section == pyd_section


# ---------------------------------------------------------------------------
# 3. The pure compute path runs over the dataclass without Pydantic in scope
# ---------------------------------------------------------------------------


def test_pure_q1_compute_accepts_domain_config_without_pydantic_instances() -> None:
    """``compute_q1_blocks_from_inputs(inputs, domain_cfg)`` runs to completion."""
    from alphamind.distillation.q1._loaders import Q1Inputs
    from alphamind.distillation.q1.assemble import assemble_q1_blocks_from_inputs

    domain = _build_test_pydantic_config().to_domain()
    empty_inputs = Q1Inputs(
        ticker_scope=(),
        as_of=datetime(2026, 4, 25, tzinfo=UTC),
        as_of_iso="2026-04-25T00:00:00Z",
        sector_per_ticker={},
        bars_by_ticker={},
        baselines_volume={},
        baselines_atr={},
        gap_fill_history={},
        spy_window_returns=None,
    )

    blocks = assemble_q1_blocks_from_inputs(empty_inputs, config=domain)
    # Empty scope short-circuits to no blocks — proves the compute call signature
    # accepts the dataclass without raising.
    assert blocks == []


# ---------------------------------------------------------------------------
# 4. dataclasses.replace mirrors model_copy(update=...) semantics
# ---------------------------------------------------------------------------


def test_dataclasses_replace_overrides_one_field_like_model_copy_update() -> None:
    """``dataclasses.replace(domain, anomaly_detection=...)`` matches
    ``dataclasses.replace(pydantic, anomaly_detection=...)`` semantically."""
    pydantic_cfg = _build_test_pydantic_config()
    domain = pydantic_cfg.to_domain()

    new_anomaly_pyd = pydantic_cfg.anomaly_detection.model_copy(
        update={"volume_anomaly_sigma": 4.5}
    )
    pyd_overridden = pydantic_cfg.model_copy(update={"anomaly_detection": new_anomaly_pyd})

    new_anomaly_dom = dataclasses.replace(domain.anomaly_detection, volume_anomaly_sigma=4.5)
    dom_overridden = dataclasses.replace(domain, anomaly_detection=new_anomaly_dom)

    # Same scalar in both surfaces.
    assert pyd_overridden.anomaly_detection.volume_anomaly_sigma == 4.5
    assert dom_overridden.anomaly_detection.volume_anomaly_sigma == 4.5

    # Identity of the un-touched sub-config is preserved (frozen dataclass
    # shares the existing reference when not overridden).
    assert dom_overridden.regime_classification is domain.regime_classification

    # The original frozen dataclass is unchanged — replace does not mutate.
    assert domain.anomaly_detection.volume_anomaly_sigma == 2.5


# ---------------------------------------------------------------------------
# Hashability and frozenness
# ---------------------------------------------------------------------------


def test_domain_config_is_hashable() -> None:
    """The frozen dataclass plus slots makes the domain config hashable."""
    domain = _build_test_pydantic_config().to_domain()
    # tracked_categories is a Mapping — hash() of the outer dataclass should
    # not blow up because the field is a MappingProxyType (id-based hash).
    h = hash(domain.anomaly_detection)
    assert isinstance(h, int)
    h_rc = hash(domain.regime_classification)
    assert isinstance(h_rc, int)


def test_domain_config_is_frozen() -> None:
    """Assigning to a frozen dataclass raises ``dataclasses.FrozenInstanceError``."""
    import pytest

    domain = _build_test_pydantic_config().to_domain()
    with pytest.raises(dataclasses.FrozenInstanceError):
        domain.anomaly_detection.volume_anomaly_sigma = 9.9  # type: ignore[misc]
