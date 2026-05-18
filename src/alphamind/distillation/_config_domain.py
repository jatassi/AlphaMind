"""Frozen-dataclass mirror of ``config.models.DistillationConfig`` (ALP-471).

The Pydantic model at the YAML-load boundary stays in
:mod:`alphamind.config.models.distillation`; once the YAML has been validated
and parsed, the loader invokes :meth:`DistillationConfig.to_domain` to project
the Pydantic shape onto the frozen-dataclass shape declared here. Internal
distillation code consumes this dataclass form; the Pydantic surface is the
YAML-load boundary only.

Field names mirror the Pydantic surface one-for-one — the projection is
mechanical, the dataclasses carry the same primitive scalars / tuples /
mappings the Pydantic classes carry, and the validators do not re-run on the
dataclass side (they have already passed at the YAML boundary). The
dataclasses are frozen, slotted, hashable, and mypy-strict.

Pilot for audit finding L7 — Pydantic config leak into compute code. Other
Pydantic config types (``GuardrailsConfig``, ``ContinuousMonitorConfig``,
etc.) are out of scope for the pilot; the same boundary→domain pattern can
be applied to each as a follow-up.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class AnomalyDetectionDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.AnomalyDetection``.

    Every field matches the Pydantic surface one-for-one; the validators that
    enforce the range constraints (``ge=0``, ``ge=1``, ``ge=0, le=100``,
    etc.) run at YAML parse time on the Pydantic side and do not re-execute
    here.
    """

    volume_anomaly_sigma: float
    price_move_atr_multiple: float
    options_low_oi_volume_multiple: float
    block_trade_min_shares: int
    block_trade_min_notional_usd: int
    dark_pool_one_sided_window_minutes: int
    earnings_revision_cluster_count: int
    earnings_revision_cluster_days: int
    macro_surprise_percentile: int
    funding_stress_component_alert_count: int
    funding_stress_component_percentile: int
    market_liquidity_alert_percentile: int
    news_price_divergence_window_hours: int
    news_price_divergence_min_articles: int


@dataclass(frozen=True, slots=True)
class RegimeClassificationDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.RegimeClassification``.

    The Pydantic cross-field invariants (adjacent regime boundaries, strict
    monotonicity of the VIX ceilings, VVIX low < VVIX high) run at YAML
    parse time and do not re-execute on the dataclass side.
    """

    regime_low_vol_vix_max: float
    regime_normal_vix_min: float
    regime_normal_vix_max: float
    regime_elevated_vix_min: float
    regime_elevated_vix_max: float
    regime_crisis_vix_min: float
    regime_term_structure_backwardation_threshold: float
    regime_vvix_high_percentile: int
    regime_vvix_low_percentile: int


@dataclass(frozen=True, slots=True)
class RegimeTransitionDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.RegimeTransition``."""

    regime_transition_confirmed_invocations: int
    regime_transition_indicator_agreement_min: int
    regime_skip_emergency_trigger: bool


@dataclass(frozen=True, slots=True)
class LeadLagPairDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.LeadLagPair``.

    Ticker / pair-key regex validators run at YAML parse time on the Pydantic
    side; the dataclass carries the validated strings as-is.
    """

    key: str
    lead: str
    lag: str


@dataclass(frozen=True, slots=True)
class LeadLagDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.LeadLag``.

    The Pydantic ``pair_keys_unique`` validator runs at YAML parse time on
    the Pydantic side; uniqueness across ``pairs`` is therefore guaranteed
    structurally by the time the dataclass is constructed.
    """

    pairs: tuple[LeadLagPairDomainConfig, ...]
    lead_lag_funding_to_credit_max_days: int
    lead_lag_credit_to_equity_max_days: int
    lead_lag_semis_to_tech_max_days: int
    lead_lag_financials_to_market_max_days: int
    lead_lag_commodity_to_energy_equity_max_days: int
    lead_lag_overdue_lead_sigma: float


@dataclass(frozen=True, slots=True)
class NarrativeLagDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.NarrativeLag``."""

    narrative_lag_correlation_shift_sigma: float
    correlation_breakdown_sigma: float
    narrative_lag_media_silence_hours: int


@dataclass(frozen=True, slots=True)
class PersistenceWindowsDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.PersistenceWindows``.

    The Pydantic ``min_observations_within_baseline`` validator runs at YAML
    parse time on the Pydantic side; the ``*_min_observations`` <=
    ``*_baseline_days`` invariants therefore hold structurally by the time
    the dataclass is constructed.
    """

    volume_baseline_days: int
    atr_baseline_days: int
    spread_baseline_days: int
    correlation_short_days: int
    correlation_long_days: int
    sentiment_baseline_days: int
    sentiment_min_observations: int
    gap_fill_baseline_days: int
    gap_fill_min_events: int
    extended_hours_confirmation_days: int
    extended_hours_min_events: int
    prediction_market_history_days: int
    funding_stress_baseline_days: int
    market_liquidity_baseline_days: int


@dataclass(frozen=True, slots=True)
class TrackedCategoryOverrideDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.TrackedCategoryOverride``."""

    min_volume_24h_usd: int | None = None


@dataclass(frozen=True, slots=True)
class PredictionMarketDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.PredictionMarket``.

    The Pydantic ``categories_in_canonical_taxonomy`` validator runs at YAML
    parse time on the Pydantic side; ``tracked_categories`` keys are
    therefore guaranteed canonical-taxonomy members by the time the
    dataclass is constructed.

    ``tracked_categories`` is stored as a read-only :class:`MappingProxyType`
    view over a defensively-copied dict so the frozen dataclass cannot be
    silently mutated through the mapping.
    """

    prediction_market_delta_pp_threshold: float
    prediction_market_low_liquidity_volume_min_usd: int
    tracked_default_min_volume_24h_usd: int
    tracked_categories: Mapping[str, TrackedCategoryOverrideDomainConfig]


@dataclass(frozen=True, slots=True)
class SeverityCapsDomainConfig:
    """Domain-typed mirror of ``config.models.distillation.SeverityCaps``.

    ``exempt_flag_names`` lists anomaly-flag names whose severity is
    preserved regardless of the surrounding block's calibration state.
    The cap rule itself lives in
    :mod:`alphamind.distillation._severity_cap`.
    """

    exempt_flag_names: frozenset[str]


@dataclass(frozen=True, slots=True)
class DistillationDomainConfig:
    """Frozen-dataclass mirror of ``config.models.DistillationConfig``.

    Internal distillation code consumes this; the Pydantic surface is the
    YAML-load boundary only. Built via
    :meth:`alphamind.config.models.distillation.DistillationConfig.to_domain`.
    """

    anomaly_detection: AnomalyDetectionDomainConfig
    regime_classification: RegimeClassificationDomainConfig
    regime_transition: RegimeTransitionDomainConfig
    lead_lag: LeadLagDomainConfig
    narrative_lag: NarrativeLagDomainConfig
    persistence_windows: PersistenceWindowsDomainConfig
    prediction_market: PredictionMarketDomainConfig
    severity_caps: SeverityCapsDomainConfig


def _freeze_tracked_categories(
    items: Mapping[str, TrackedCategoryOverrideDomainConfig],
) -> Mapping[str, TrackedCategoryOverrideDomainConfig]:
    """Return a read-only view over a copy of ``items``."""
    return MappingProxyType(dict(items))


__all__ = [
    "AnomalyDetectionDomainConfig",
    "DistillationDomainConfig",
    "LeadLagDomainConfig",
    "LeadLagPairDomainConfig",
    "NarrativeLagDomainConfig",
    "PersistenceWindowsDomainConfig",
    "PredictionMarketDomainConfig",
    "RegimeClassificationDomainConfig",
    "RegimeTransitionDomainConfig",
    "SeverityCapsDomainConfig",
    "TrackedCategoryOverrideDomainConfig",
]
