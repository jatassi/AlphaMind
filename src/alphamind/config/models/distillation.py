"""Pydantic models for distillation.yaml — distillation-layer thresholds (story 02).

The seven top-level groups mirror the structure in
``docs/design/configuration-management.md`` § distillation.yaml. Range
constraints, type checks, and cross-field invariants implement
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Static configuration thresholds and § Validation invariants. The schema
is the load-time gate — failure aborts the invocation per
``configuration-management.md`` § Validation.

ALP-471 added the ``to_domain()`` methods that project each Pydantic class
onto the matching frozen-dataclass mirror in
:mod:`alphamind.distillation._config_domain`. The Pydantic surface stays at
the YAML-load boundary; downstream distillation code consumes the dataclass
form. The projection is mechanical — every field maps one-for-one.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    _freeze_tracked_categories,
)


class AnomalyDetection(BaseModel):
    model_config = ConfigDict(frozen=True)

    volume_anomaly_sigma: float = Field(ge=0)
    price_move_atr_multiple: float = Field(ge=0)
    options_low_oi_volume_multiple: float = Field(ge=0)
    block_trade_min_shares: int = Field(ge=1)
    block_trade_min_notional_usd: int = Field(ge=1)
    dark_pool_one_sided_window_minutes: int = Field(ge=1)
    earnings_revision_cluster_count: int = Field(ge=1)
    earnings_revision_cluster_days: int = Field(ge=1)
    macro_surprise_percentile: int = Field(ge=0, le=100)
    funding_stress_component_alert_count: int = Field(ge=1, le=4)
    funding_stress_component_percentile: int = Field(ge=50, le=100)
    market_liquidity_alert_percentile: int = Field(ge=0, le=50)
    news_price_divergence_window_hours: int = Field(ge=1)
    news_price_divergence_min_articles: int = Field(ge=1)

    def to_domain(self) -> AnomalyDetectionDomainConfig:
        return AnomalyDetectionDomainConfig(
            volume_anomaly_sigma=self.volume_anomaly_sigma,
            price_move_atr_multiple=self.price_move_atr_multiple,
            options_low_oi_volume_multiple=self.options_low_oi_volume_multiple,
            block_trade_min_shares=self.block_trade_min_shares,
            block_trade_min_notional_usd=self.block_trade_min_notional_usd,
            dark_pool_one_sided_window_minutes=self.dark_pool_one_sided_window_minutes,
            earnings_revision_cluster_count=self.earnings_revision_cluster_count,
            earnings_revision_cluster_days=self.earnings_revision_cluster_days,
            macro_surprise_percentile=self.macro_surprise_percentile,
            funding_stress_component_alert_count=self.funding_stress_component_alert_count,
            funding_stress_component_percentile=self.funding_stress_component_percentile,
            market_liquidity_alert_percentile=self.market_liquidity_alert_percentile,
            news_price_divergence_window_hours=self.news_price_divergence_window_hours,
            news_price_divergence_min_articles=self.news_price_divergence_min_articles,
        )


class RegimeClassification(BaseModel):
    model_config = ConfigDict(frozen=True)

    regime_low_vol_vix_max: float = Field(ge=0)
    regime_normal_vix_min: float = Field(ge=0)
    regime_normal_vix_max: float = Field(ge=0)
    regime_elevated_vix_min: float = Field(ge=0)
    regime_elevated_vix_max: float = Field(ge=0)
    regime_crisis_vix_min: float = Field(ge=0)
    regime_term_structure_backwardation_threshold: float
    regime_vvix_high_percentile: int = Field(ge=0, le=100)
    regime_vvix_low_percentile: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def adjacent_regime_boundaries_match(self) -> "RegimeClassification":
        if self.regime_low_vol_vix_max != self.regime_normal_vix_min:
            raise ValueError(
                "regime_low_vol_vix_max must equal regime_normal_vix_min "
                f"(got {self.regime_low_vol_vix_max} vs {self.regime_normal_vix_min})"
            )
        if self.regime_normal_vix_max != self.regime_elevated_vix_min:
            raise ValueError(
                "regime_normal_vix_max must equal regime_elevated_vix_min "
                f"(got {self.regime_normal_vix_max} vs {self.regime_elevated_vix_min})"
            )
        if self.regime_elevated_vix_max != self.regime_crisis_vix_min:
            raise ValueError(
                "regime_elevated_vix_max must equal regime_crisis_vix_min "
                f"(got {self.regime_elevated_vix_max} vs {self.regime_crisis_vix_min})"
            )
        return self

    @model_validator(mode="after")
    def regime_ceilings_strictly_monotonic(self) -> "RegimeClassification":
        if not (
            self.regime_low_vol_vix_max < self.regime_normal_vix_max < self.regime_elevated_vix_max
        ):
            raise ValueError(
                "regime VIX ceilings must satisfy "
                "regime_low_vol_vix_max < regime_normal_vix_max < regime_elevated_vix_max "
                f"(got {self.regime_low_vol_vix_max} / {self.regime_normal_vix_max} / "
                f"{self.regime_elevated_vix_max})"
            )
        return self

    @model_validator(mode="after")
    def vvix_low_below_high(self) -> "RegimeClassification":
        if self.regime_vvix_low_percentile >= self.regime_vvix_high_percentile:
            raise ValueError(
                "regime_vvix_low_percentile must be strictly less than "
                f"regime_vvix_high_percentile (got {self.regime_vvix_low_percentile} "
                f"vs {self.regime_vvix_high_percentile})"
            )
        return self

    def to_domain(self) -> RegimeClassificationDomainConfig:
        return RegimeClassificationDomainConfig(
            regime_low_vol_vix_max=self.regime_low_vol_vix_max,
            regime_normal_vix_min=self.regime_normal_vix_min,
            regime_normal_vix_max=self.regime_normal_vix_max,
            regime_elevated_vix_min=self.regime_elevated_vix_min,
            regime_elevated_vix_max=self.regime_elevated_vix_max,
            regime_crisis_vix_min=self.regime_crisis_vix_min,
            regime_term_structure_backwardation_threshold=(
                self.regime_term_structure_backwardation_threshold
            ),
            regime_vvix_high_percentile=self.regime_vvix_high_percentile,
            regime_vvix_low_percentile=self.regime_vvix_low_percentile,
        )


class RegimeTransition(BaseModel):
    model_config = ConfigDict(frozen=True)

    regime_transition_confirmed_invocations: int = Field(ge=1)
    regime_transition_indicator_agreement_min: int = Field(ge=1)
    regime_skip_emergency_trigger: bool

    def to_domain(self) -> RegimeTransitionDomainConfig:
        return RegimeTransitionDomainConfig(
            regime_transition_confirmed_invocations=self.regime_transition_confirmed_invocations,
            regime_transition_indicator_agreement_min=(
                self.regime_transition_indicator_agreement_min
            ),
            regime_skip_emergency_trigger=self.regime_skip_emergency_trigger,
        )


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")
_PAIR_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class LeadLagPair(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    lead: str
    lag: str

    @field_validator("key")
    @classmethod
    def key_well_formed(cls, v: str) -> str:
        if not _PAIR_KEY_RE.match(v):
            raise ValueError(f"pair key {v!r} must match {_PAIR_KEY_RE.pattern}")
        return v

    @field_validator("lead", "lag")
    @classmethod
    def ticker_well_formed(cls, v: str) -> str:
        if not _TICKER_RE.match(v):
            raise ValueError(f"pair ticker {v!r} must match {_TICKER_RE.pattern}")
        return v

    def to_domain(self) -> LeadLagPairDomainConfig:
        return LeadLagPairDomainConfig(key=self.key, lead=self.lead, lag=self.lag)


class LeadLag(BaseModel):
    model_config = ConfigDict(frozen=True)

    pairs: tuple[LeadLagPair, ...] = Field(min_length=1)
    lead_lag_funding_to_credit_max_days: int = Field(ge=1)
    lead_lag_credit_to_equity_max_days: int = Field(ge=1)
    lead_lag_semis_to_tech_max_days: int = Field(ge=1)
    lead_lag_financials_to_market_max_days: int = Field(ge=1)
    lead_lag_commodity_to_energy_equity_max_days: int = Field(ge=1)
    lead_lag_overdue_lead_sigma: float = Field(ge=0)

    @field_validator("pairs")
    @classmethod
    def pair_keys_unique(cls, v: tuple[LeadLagPair, ...]) -> tuple[LeadLagPair, ...]:
        keys = [p.key for p in v]
        if len(set(keys)) != len(keys):
            raise ValueError(f"pair keys must be unique; got {keys}")
        return v

    def to_domain(self) -> LeadLagDomainConfig:
        return LeadLagDomainConfig(
            pairs=tuple(p.to_domain() for p in self.pairs),
            lead_lag_funding_to_credit_max_days=self.lead_lag_funding_to_credit_max_days,
            lead_lag_credit_to_equity_max_days=self.lead_lag_credit_to_equity_max_days,
            lead_lag_semis_to_tech_max_days=self.lead_lag_semis_to_tech_max_days,
            lead_lag_financials_to_market_max_days=self.lead_lag_financials_to_market_max_days,
            lead_lag_commodity_to_energy_equity_max_days=(
                self.lead_lag_commodity_to_energy_equity_max_days
            ),
            lead_lag_overdue_lead_sigma=self.lead_lag_overdue_lead_sigma,
        )


class NarrativeLag(BaseModel):
    model_config = ConfigDict(frozen=True)

    narrative_lag_correlation_shift_sigma: float = Field(ge=0)
    correlation_breakdown_sigma: float = Field(ge=0)
    # ALP-541: data-alignment guards on the correlation breakdown sigma-test.
    correlation_min_overlap_fraction: float = Field(ge=0, le=1)
    correlation_noise_floor: float = Field(ge=0, le=1)
    # ALP-542: Benjamini-Hochberg FDR target for the multi-pair sigma-test.
    correlation_breakdown_fdr_q: float = Field(gt=0, le=1)
    narrative_lag_media_silence_hours: int = Field(ge=1)

    def to_domain(self) -> NarrativeLagDomainConfig:
        return NarrativeLagDomainConfig(
            narrative_lag_correlation_shift_sigma=self.narrative_lag_correlation_shift_sigma,
            correlation_breakdown_sigma=self.correlation_breakdown_sigma,
            correlation_min_overlap_fraction=self.correlation_min_overlap_fraction,
            correlation_noise_floor=self.correlation_noise_floor,
            correlation_breakdown_fdr_q=self.correlation_breakdown_fdr_q,
            narrative_lag_media_silence_hours=self.narrative_lag_media_silence_hours,
        )


class PersistenceWindows(BaseModel):
    model_config = ConfigDict(frozen=True)

    volume_baseline_days: int = Field(ge=1)
    atr_baseline_days: int = Field(ge=1)
    spread_baseline_days: int = Field(ge=1)
    correlation_short_days: int = Field(ge=1)
    correlation_long_days: int = Field(ge=1)
    sentiment_baseline_days: int = Field(ge=1)
    sentiment_min_observations: int = Field(ge=1)
    gap_fill_baseline_days: int = Field(ge=1)
    gap_fill_min_events: int = Field(ge=1)
    extended_hours_confirmation_days: int = Field(ge=1)
    extended_hours_min_events: int = Field(ge=1)
    prediction_market_history_days: int = Field(ge=1)
    funding_stress_baseline_days: int = Field(ge=1)
    market_liquidity_baseline_days: int = Field(ge=1)

    @model_validator(mode="after")
    def min_observations_within_baseline(self) -> "PersistenceWindows":
        pairs: tuple[tuple[str, str], ...] = (
            ("sentiment_min_observations", "sentiment_baseline_days"),
            ("gap_fill_min_events", "gap_fill_baseline_days"),
            ("extended_hours_min_events", "extended_hours_confirmation_days"),
        )
        for minimum_field, baseline_field in pairs:
            minimum = getattr(self, minimum_field)
            baseline = getattr(self, baseline_field)
            if minimum > baseline:
                raise ValueError(
                    f"{minimum_field} ({minimum}) must not exceed {baseline_field} ({baseline})"
                )
        return self

    def to_domain(self) -> PersistenceWindowsDomainConfig:
        return PersistenceWindowsDomainConfig(
            volume_baseline_days=self.volume_baseline_days,
            atr_baseline_days=self.atr_baseline_days,
            spread_baseline_days=self.spread_baseline_days,
            correlation_short_days=self.correlation_short_days,
            correlation_long_days=self.correlation_long_days,
            sentiment_baseline_days=self.sentiment_baseline_days,
            sentiment_min_observations=self.sentiment_min_observations,
            gap_fill_baseline_days=self.gap_fill_baseline_days,
            gap_fill_min_events=self.gap_fill_min_events,
            extended_hours_confirmation_days=self.extended_hours_confirmation_days,
            extended_hours_min_events=self.extended_hours_min_events,
            prediction_market_history_days=self.prediction_market_history_days,
            funding_stress_baseline_days=self.funding_stress_baseline_days,
            market_liquidity_baseline_days=self.market_liquidity_baseline_days,
        )


class TrackedCategoryOverride(BaseModel):
    """Optional per-category overrides for ``tracked_categories``.

    ``min_volume_24h_usd`` overrides
    :attr:`PredictionMarket.tracked_default_min_volume_24h_usd` when set.
    Empty dict ``{}`` accepts the default floor.
    """

    model_config = ConfigDict(frozen=True)

    min_volume_24h_usd: int | None = Field(default=None, ge=0)

    def to_domain(self) -> TrackedCategoryOverrideDomainConfig:
        return TrackedCategoryOverrideDomainConfig(min_volume_24h_usd=self.min_volume_24h_usd)


class PredictionMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    prediction_market_delta_pp_threshold: float = Field(gt=0, le=100)
    prediction_market_low_liquidity_volume_min_usd: int = Field(ge=1)
    tracked_default_min_volume_24h_usd: int = Field(ge=0)
    tracked_categories: dict[str, TrackedCategoryOverride]

    @field_validator("tracked_categories")
    @classmethod
    def categories_in_canonical_taxonomy(
        cls, v: dict[str, TrackedCategoryOverride]
    ) -> dict[str, TrackedCategoryOverride]:
        from alphamind.data_sources.prediction_market.categories import CANONICAL_CATEGORIES

        invalid = sorted(name for name in v if name not in CANONICAL_CATEGORIES)
        if invalid:
            raise ValueError(
                "tracked_categories keys not in canonical taxonomy: "
                f"{invalid}; valid keys are {list(CANONICAL_CATEGORIES)}"
            )
        return v

    def to_domain(self) -> PredictionMarketDomainConfig:
        return PredictionMarketDomainConfig(
            prediction_market_delta_pp_threshold=self.prediction_market_delta_pp_threshold,
            prediction_market_low_liquidity_volume_min_usd=(
                self.prediction_market_low_liquidity_volume_min_usd
            ),
            tracked_default_min_volume_24h_usd=self.tracked_default_min_volume_24h_usd,
            tracked_categories=_freeze_tracked_categories(
                {key: value.to_domain() for key, value in self.tracked_categories.items()}
            ),
        )


class SeverityCaps(BaseModel):
    """Calibration-state severity-cap exemption surface.

    The cap rule itself lives in :mod:`alphamind.distillation._severity_cap`
    (calibrated → no cap, accumulating → investigate_if_persists,
    unavailable → note_for_context). ``exempt_flag_names`` lists anomaly
    flags whose producer-side severity is preserved regardless of the
    surrounding block's calibration state — used when the underlying
    signal is structural (single macro surprise, single ETF imbalance)
    and does not gain accuracy with more history.
    """

    model_config = ConfigDict(frozen=True)

    exempt_flag_names: tuple[str, ...] = Field(default=())

    def to_domain(self) -> SeverityCapsDomainConfig:
        return SeverityCapsDomainConfig(
            exempt_flag_names=frozenset(self.exempt_flag_names),
        )


class DistillationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    anomaly_detection: AnomalyDetection
    regime_classification: RegimeClassification
    regime_transition: RegimeTransition
    lead_lag: LeadLag
    narrative_lag: NarrativeLag
    persistence_windows: PersistenceWindows
    prediction_market: PredictionMarket
    severity_caps: SeverityCaps = Field(default_factory=SeverityCaps)

    def to_domain(self) -> DistillationDomainConfig:
        """Project the Pydantic config onto its frozen-dataclass mirror.

        Called once at the YAML-load boundary; downstream distillation code
        consumes the dataclass form, never the Pydantic surface. ALP-471 pilot
        for audit finding L7 — Pydantic config leak into compute code.
        """
        return DistillationDomainConfig(
            anomaly_detection=self.anomaly_detection.to_domain(),
            regime_classification=self.regime_classification.to_domain(),
            regime_transition=self.regime_transition.to_domain(),
            lead_lag=self.lead_lag.to_domain(),
            narrative_lag=self.narrative_lag.to_domain(),
            persistence_windows=self.persistence_windows.to_domain(),
            prediction_market=self.prediction_market.to_domain(),
            severity_caps=self.severity_caps.to_domain(),
        )
