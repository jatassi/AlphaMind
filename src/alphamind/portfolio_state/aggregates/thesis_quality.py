"""Thesis quality aggregate records (raw state category 6)."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel

__all__ = [
    "AlphaBetaDecomposition",
    "AttributionDimension",
    "ConvictionCalibrationEntry",
    "ConvictionSizingDeviation",
    "InvalidationTimingClass",
    "InvalidationTimingStat",
    "PerformanceAttributionEntry",
    "RegimeLabel",
    "ResolutionWindowCounts",
    "SignalHitRate",
    "SignalToThesisConversion",
    "ThesisDurationStat",
    "ThesisQualityAggregate",
    "ThesisType",
    "TrailingWindow",
]

_NonNegInt = Annotated[int, Field(ge=0)]
_FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _check_finite_or_none(v: float | None, label: str) -> float | None:
    if v is not None and not math.isfinite(v):
        msg = f"{label} must be finite when not None"
        raise ValueError(msg)
    return v


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TrailingWindow(StrEnum):
    """Aggregate trailing windows per portfolio-state.md § 6a."""

    FIVE_DAYS = "FIVE_DAYS"
    TWENTY_DAYS = "TWENTY_DAYS"
    INCEPTION = "INCEPTION"


class ThesisType(StrEnum):
    """Thesis natures driving P/L attribution per portfolio-state.md § 6c."""

    EVENT_DRIVEN = "EVENT_DRIVEN"
    MEAN_REVERSION = "MEAN_REVERSION"
    MOMENTUM = "MOMENTUM"
    CROSS_ASSET_DIVERGENCE = "CROSS_ASSET_DIVERGENCE"


class InvalidationTimingClass(StrEnum):
    """Invalidation timing classification per portfolio-state.md § 6a."""

    EARLY = "EARLY"
    ON_TIME = "ON_TIME"
    LATE = "LATE"


class AttributionDimension(StrEnum):
    """Dimensions used in PerformanceAttributionEntry."""

    SECTOR = "SECTOR"
    THESIS_TYPE = "THESIS_TYPE"
    REGIME = "REGIME"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class ResolutionWindowCounts(BaseModel):
    """Trailing resolution counts per thesis-model.md resolution categories for one window."""

    model_config = ConfigDict(frozen=True)

    window: TrailingWindow
    total_resolutions: _NonNegInt
    validated: _NonNegInt
    profitable_but_wrong: _NonNegInt
    invalidated_stopped_correctly: _NonNegInt
    invalidated_wrong_on_exit: _NonNegInt
    cancelled_never_entered: _NonNegInt

    @model_validator(mode="after")
    def _validate_conservation(self) -> ResolutionWindowCounts:
        total = (
            self.validated
            + self.profitable_but_wrong
            + self.invalidated_stopped_correctly
            + self.invalidated_wrong_on_exit
            + self.cancelled_never_entered
        )
        if total != self.total_resolutions:
            msg = (
                f"resolution counts sum ({total}) must equal total_resolutions "
                f"({self.total_resolutions})"
            )
            raise ValueError(msg)
        return self

    @property
    def validation_rate(self) -> float | None:
        """Return validated / total_resolutions, or None when total_resolutions is zero."""
        if self.total_resolutions == 0:
            return None
        return self.validated / self.total_resolutions


class ThesisDurationStat(BaseModel):
    """Thesis duration accuracy for one window per portfolio-state.md § 6a."""

    model_config = ConfigDict(frozen=True)

    window: TrailingWindow
    mean_actual_to_expected_ratio: float | None
    median_actual_to_expected_ratio: float | None
    count: _NonNegInt

    @field_validator("mean_actual_to_expected_ratio", "median_actual_to_expected_ratio")
    @classmethod
    def _require_finite_or_none(cls, v: float | None) -> float | None:
        return _check_finite_or_none(v, "ratio")


class InvalidationTimingStat(BaseModel):
    """Invalidation timing statistics for one window per portfolio-state.md § 6a."""

    model_config = ConfigDict(frozen=True)

    window: TrailingWindow
    class_distribution: dict[InvalidationTimingClass, int]
    mean_position_age_at_invalidation_hours: float | None

    @model_validator(mode="after")
    def _validate_class_distribution(self) -> InvalidationTimingStat:
        missing = set(InvalidationTimingClass) - set(self.class_distribution)
        if missing:
            msg = f"class_distribution missing keys: {missing}"
            raise ValueError(msg)
        negatives = {k: v for k, v in self.class_distribution.items() if v < 0}
        if negatives:
            msg = f"class_distribution values must be non-negative; got {negatives}"
            raise ValueError(msg)
        return self

    @field_validator("mean_position_age_at_invalidation_hours")
    @classmethod
    def _require_finite_non_negative_or_none(cls, v: float | None) -> float | None:
        v = _check_finite_or_none(v, "mean_position_age_at_invalidation_hours")
        if v is not None and v < 0:
            msg = "mean_position_age_at_invalidation_hours must be non-negative when not None"
            raise ValueError(msg)
        return v


class SignalHitRate(BaseModel):
    """Trailing hit rate for one (signal_type, window) pair per portfolio-state.md § 6b."""

    model_config = ConfigDict(frozen=True)

    signal_type: str
    window: TrailingWindow
    cited_count: _NonNegInt
    validated_count: _NonNegInt

    @property
    def hit_rate(self) -> float | None:
        """Return validated_count / cited_count, or None when cited_count is zero."""
        if self.cited_count == 0:
            return None
        return self.validated_count / self.cited_count


class SignalToThesisConversion(BaseModel):
    """Signal-to-thesis conversion rate per portfolio-state.md § 6b.

    One entry per (signal_type, window) pair.
    """

    model_config = ConfigDict(frozen=True)

    signal_type: str
    window: TrailingWindow
    signal_observed_count: _NonNegInt
    pm_approved_count: _NonNegInt

    @property
    def conversion_rate(self) -> float | None:
        """Return pm_approved_count / signal_observed_count, or None when denominator is zero."""
        if self.signal_observed_count == 0:
            return None
        return self.pm_approved_count / self.signal_observed_count


class ConvictionCalibrationEntry(BaseModel):
    """One (conviction_level, window) slot per portfolio-state.md § 6b."""

    model_config = ConfigDict(frozen=True)

    conviction_level: int
    window: TrailingWindow
    count: _NonNegInt
    validation_rate: float | None
    mean_realized_pnl_pct: float | None

    @field_validator("conviction_level")
    @classmethod
    def _require_valid_conviction_level(cls, v: int) -> int:
        if v not in {1, 2, 3, 4, 5}:
            msg = f"conviction_level must be in {{1, 2, 3, 4, 5}}; got {v}"
            raise ValueError(msg)
        return v

    @field_validator("validation_rate", "mean_realized_pnl_pct")
    @classmethod
    def _require_finite_or_none(cls, v: float | None) -> float | None:
        return _check_finite_or_none(v, "field")


class ConvictionSizingDeviation(BaseModel):
    """Conviction-sizing deviation tracking per portfolio-state.md § 6b."""

    model_config = ConfigDict(frozen=True)

    window: TrailingWindow
    total_proposals: _NonNegInt
    pm_sized_above_advisory_count: _NonNegInt
    pm_sized_below_advisory_count: _NonNegInt
    pm_sized_within_advisory_count: _NonNegInt
    outcome_correlation_above: float | None
    outcome_correlation_below: float | None

    @model_validator(mode="after")
    def _validate_conservation(self) -> ConvictionSizingDeviation:
        total = (
            self.pm_sized_above_advisory_count
            + self.pm_sized_below_advisory_count
            + self.pm_sized_within_advisory_count
        )
        if total != self.total_proposals:
            msg = f"sizing counts sum ({total}) must equal total_proposals ({self.total_proposals})"
            raise ValueError(msg)
        return self

    @field_validator("outcome_correlation_above", "outcome_correlation_below")
    @classmethod
    def _require_finite_or_none(cls, v: float | None) -> float | None:
        return _check_finite_or_none(v, "outcome_correlation")

    @property
    def deviation_rate(self) -> float | None:
        """Return (above + below) / total_proposals, or None when total_proposals is zero."""
        if self.total_proposals == 0:
            return None
        above = self.pm_sized_above_advisory_count
        below = self.pm_sized_below_advisory_count
        return (above + below) / self.total_proposals


class PerformanceAttributionEntry(BaseModel):
    """One (dimension, key, window) slice per portfolio-state.md § 6c."""

    model_config = ConfigDict(frozen=True)

    dimension: AttributionDimension
    key: str
    window: TrailingWindow
    cumulative_realized_pnl_usd: _FiniteFloat
    realized_pnl_pct_of_window_capital: float | None
    count: _NonNegInt

    @field_validator("realized_pnl_pct_of_window_capital")
    @classmethod
    def _require_finite_or_none(cls, v: float | None) -> float | None:
        return _check_finite_or_none(v, "realized_pnl_pct_of_window_capital")


class AlphaBetaDecomposition(BaseModel):
    """Alpha vs. beta decomposition per portfolio-state.md § 6c."""

    model_config = ConfigDict(frozen=True)

    window: TrailingWindow
    total_realized_pnl_usd: _FiniteFloat
    market_component_usd: _FiniteFloat
    sector_component_usd: _FiniteFloat
    alpha_component_usd: _FiniteFloat

    @property
    def attribution_ratio(self) -> float | None:
        """Return alpha_component_usd / total_realized_pnl_usd, or None when total is zero."""
        if self.total_realized_pnl_usd == 0:
            return None
        return self.alpha_component_usd / self.total_realized_pnl_usd


class ThesisQualityAggregate(BaseModel):
    """Outer record consumed via raw state category 6."""

    model_config = ConfigDict(frozen=True)

    as_of_timestamp: datetime
    resolution_counts_by_window: tuple[ResolutionWindowCounts, ...]
    duration_stats_by_window: tuple[ThesisDurationStat, ...]
    invalidation_timing_stats_by_window: tuple[InvalidationTimingStat, ...]
    signal_hit_rates: tuple[SignalHitRate, ...]
    signal_to_thesis_conversions: tuple[SignalToThesisConversion, ...]
    conviction_calibration: tuple[ConvictionCalibrationEntry, ...]
    conviction_sizing_deviation_by_window: tuple[ConvictionSizingDeviation, ...]
    performance_attribution: tuple[PerformanceAttributionEntry, ...]
    alpha_beta_decomposition_by_window: tuple[AlphaBetaDecomposition, ...]

    @field_validator("as_of_timestamp")
    @classmethod
    def _require_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "as_of_timestamp must be timezone-aware UTC"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _validate_window_uniqueness(self) -> ThesisQualityAggregate:
        window_tuples = [
            ("resolution_counts_by_window", [e.window for e in self.resolution_counts_by_window]),
            ("duration_stats_by_window", [e.window for e in self.duration_stats_by_window]),
            (
                "invalidation_timing_stats_by_window",
                [e.window for e in self.invalidation_timing_stats_by_window],
            ),
            (
                "conviction_sizing_deviation_by_window",
                [e.window for e in self.conviction_sizing_deviation_by_window],
            ),
            (
                "alpha_beta_decomposition_by_window",
                [e.window for e in self.alpha_beta_decomposition_by_window],
            ),
        ]
        for field_name, windows in window_tuples:
            if len(windows) != len(set(windows)):
                msg = f"{field_name} must not contain duplicate TrailingWindow values"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_pair_uniqueness(self) -> ThesisQualityAggregate:
        shr_pairs = [(e.signal_type, e.window) for e in self.signal_hit_rates]
        if len(shr_pairs) != len(set(shr_pairs)):
            msg = "signal_hit_rates must not contain duplicate (signal_type, window) pairs"
            raise ValueError(msg)

        stc_pairs = [(e.signal_type, e.window) for e in self.signal_to_thesis_conversions]
        if len(stc_pairs) != len(set(stc_pairs)):
            msg = (
                "signal_to_thesis_conversions must not contain duplicate "
                "(signal_type, window) pairs"
            )
            raise ValueError(msg)

        cc_pairs = [(e.conviction_level, e.window) for e in self.conviction_calibration]
        if len(cc_pairs) != len(set(cc_pairs)):
            msg = (
                "conviction_calibration must not contain duplicate (conviction_level, window) pairs"
            )
            raise ValueError(msg)

        pa_triples = [(e.dimension, e.key, e.window) for e in self.performance_attribution]
        if len(pa_triples) != len(set(pa_triples)):
            msg = (
                "performance_attribution must not contain duplicate "
                "(dimension, key, window) triples"
            )
            raise ValueError(msg)

        return self

    def counts_for(self, window: TrailingWindow) -> ResolutionWindowCounts | None:
        """Return the ResolutionWindowCounts for window, or None if not present."""
        for entry in self.resolution_counts_by_window:
            if entry.window == window:
                return entry
        return None

    def signal_hit_rate(self, signal_type: str, window: TrailingWindow) -> SignalHitRate | None:
        """Return the SignalHitRate for (signal_type, window), or None if not present."""
        for entry in self.signal_hit_rates:
            if entry.signal_type == signal_type and entry.window == window:
                return entry
        return None

    def conviction_entries_for(
        self, window: TrailingWindow
    ) -> tuple[ConvictionCalibrationEntry, ...]:
        """Return all ConvictionCalibrationEntry instances for window; empty tuple if none."""
        return tuple(e for e in self.conviction_calibration if e.window == window)
