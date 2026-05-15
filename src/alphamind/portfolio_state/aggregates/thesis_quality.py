"""Thesis quality aggregate records (raw state category 6)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from alphamind._kernel.regime import RegimeLabel

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


def _check_finite(value: float, field_name: str) -> None:
    if not math.isfinite(value):
        msg = f"{field_name} must be finite; got {value}"
        raise ValueError(msg)


def _check_finite_or_none(v: float | None, label: str) -> None:
    if v is not None and not math.isfinite(v):
        msg = f"{label} must be finite when not None"
        raise ValueError(msg)


def _check_non_negative_int(value: int, field_name: str) -> None:
    if value < 0:
        msg = f"{field_name} must be >= 0; got {value}"
        raise ValueError(msg)


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


@dataclass(frozen=True, slots=True)
class ResolutionWindowCounts:
    """Trailing resolution counts per thesis-model.md resolution categories for one window."""

    window: TrailingWindow
    total_resolutions: int
    validated: int
    profitable_but_wrong: int
    invalidated_stopped_correctly: int
    invalidated_wrong_on_exit: int
    cancelled_never_entered: int

    def __post_init__(self) -> None:
        for name in (
            "total_resolutions",
            "validated",
            "profitable_but_wrong",
            "invalidated_stopped_correctly",
            "invalidated_wrong_on_exit",
            "cancelled_never_entered",
        ):
            _check_non_negative_int(getattr(self, name), name)
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

    @property
    def validation_rate(self) -> float | None:
        """Return validated / total_resolutions, or None when total_resolutions is zero."""
        if self.total_resolutions == 0:
            return None
        return self.validated / self.total_resolutions


@dataclass(frozen=True, slots=True)
class ThesisDurationStat:
    """Thesis duration accuracy for one window per portfolio-state.md § 6a."""

    window: TrailingWindow
    mean_actual_to_expected_ratio: float | None
    median_actual_to_expected_ratio: float | None
    count: int

    def __post_init__(self) -> None:
        _check_non_negative_int(self.count, "count")
        _check_finite_or_none(self.mean_actual_to_expected_ratio, "ratio")
        _check_finite_or_none(self.median_actual_to_expected_ratio, "ratio")


@dataclass(frozen=True, slots=True)
class InvalidationTimingStat:
    """Invalidation timing statistics for one window per portfolio-state.md § 6a."""

    window: TrailingWindow
    class_distribution: dict[InvalidationTimingClass, int]
    mean_position_age_at_invalidation_hours: float | None

    def __post_init__(self) -> None:
        missing = set(InvalidationTimingClass) - set(self.class_distribution)
        if missing:
            msg = f"class_distribution missing keys: {missing}"
            raise ValueError(msg)
        negatives = {k: v for k, v in self.class_distribution.items() if v < 0}
        if negatives:
            msg = f"class_distribution values must be non-negative; got {negatives}"
            raise ValueError(msg)
        _check_finite_or_none(
            self.mean_position_age_at_invalidation_hours,
            "mean_position_age_at_invalidation_hours",
        )
        if (
            self.mean_position_age_at_invalidation_hours is not None
            and self.mean_position_age_at_invalidation_hours < 0
        ):
            msg = "mean_position_age_at_invalidation_hours must be non-negative when not None"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class SignalHitRate:
    """Trailing hit rate for one (signal_type, window) pair per portfolio-state.md § 6b."""

    signal_type: str
    window: TrailingWindow
    cited_count: int
    validated_count: int

    def __post_init__(self) -> None:
        _check_non_negative_int(self.cited_count, "cited_count")
        _check_non_negative_int(self.validated_count, "validated_count")

    @property
    def hit_rate(self) -> float | None:
        """Return validated_count / cited_count, or None when cited_count is zero."""
        if self.cited_count == 0:
            return None
        return self.validated_count / self.cited_count


@dataclass(frozen=True, slots=True)
class SignalToThesisConversion:
    """Signal-to-thesis conversion rate per portfolio-state.md § 6b.

    One entry per (signal_type, window) pair.
    """

    signal_type: str
    window: TrailingWindow
    signal_observed_count: int
    pm_approved_count: int

    def __post_init__(self) -> None:
        _check_non_negative_int(self.signal_observed_count, "signal_observed_count")
        _check_non_negative_int(self.pm_approved_count, "pm_approved_count")

    @property
    def conversion_rate(self) -> float | None:
        """Return pm_approved_count / signal_observed_count, or None when denominator is zero."""
        if self.signal_observed_count == 0:
            return None
        return self.pm_approved_count / self.signal_observed_count


@dataclass(frozen=True, slots=True)
class ConvictionCalibrationEntry:
    """One (conviction_level, window) slot per portfolio-state.md § 6b."""

    conviction_level: int
    window: TrailingWindow
    count: int
    validation_rate: float | None
    mean_realized_pnl_pct: float | None

    def __post_init__(self) -> None:
        if self.conviction_level not in {1, 2, 3, 4, 5}:
            msg = f"conviction_level must be in {{1, 2, 3, 4, 5}}; got {self.conviction_level}"
            raise ValueError(msg)
        _check_non_negative_int(self.count, "count")
        _check_finite_or_none(self.validation_rate, "validation_rate")
        _check_finite_or_none(self.mean_realized_pnl_pct, "mean_realized_pnl_pct")


@dataclass(frozen=True, slots=True)
class ConvictionSizingDeviation:
    """Conviction-sizing deviation tracking per portfolio-state.md § 6b."""

    window: TrailingWindow
    total_proposals: int
    pm_sized_above_advisory_count: int
    pm_sized_below_advisory_count: int
    pm_sized_within_advisory_count: int
    outcome_correlation_above: float | None
    outcome_correlation_below: float | None

    def __post_init__(self) -> None:
        for name in (
            "total_proposals",
            "pm_sized_above_advisory_count",
            "pm_sized_below_advisory_count",
            "pm_sized_within_advisory_count",
        ):
            _check_non_negative_int(getattr(self, name), name)
        total = (
            self.pm_sized_above_advisory_count
            + self.pm_sized_below_advisory_count
            + self.pm_sized_within_advisory_count
        )
        if total != self.total_proposals:
            msg = f"sizing counts sum ({total}) must equal total_proposals ({self.total_proposals})"
            raise ValueError(msg)
        _check_finite_or_none(self.outcome_correlation_above, "outcome_correlation_above")
        _check_finite_or_none(self.outcome_correlation_below, "outcome_correlation_below")

    @property
    def deviation_rate(self) -> float | None:
        """Return (above + below) / total_proposals, or None when total_proposals is zero."""
        if self.total_proposals == 0:
            return None
        above = self.pm_sized_above_advisory_count
        below = self.pm_sized_below_advisory_count
        return (above + below) / self.total_proposals


@dataclass(frozen=True, slots=True)
class PerformanceAttributionEntry:
    """One (dimension, key, window) slice per portfolio-state.md § 6c."""

    dimension: AttributionDimension
    key: str
    window: TrailingWindow
    cumulative_realized_pnl_usd: float
    realized_pnl_pct_of_window_capital: float | None
    count: int

    def __post_init__(self) -> None:
        _check_finite(self.cumulative_realized_pnl_usd, "cumulative_realized_pnl_usd")
        _check_non_negative_int(self.count, "count")
        _check_finite_or_none(
            self.realized_pnl_pct_of_window_capital, "realized_pnl_pct_of_window_capital"
        )


@dataclass(frozen=True, slots=True)
class AlphaBetaDecomposition:
    """Alpha vs. beta decomposition per portfolio-state.md § 6c."""

    window: TrailingWindow
    total_realized_pnl_usd: float
    market_component_usd: float
    sector_component_usd: float
    alpha_component_usd: float

    def __post_init__(self) -> None:
        for name in (
            "total_realized_pnl_usd",
            "market_component_usd",
            "sector_component_usd",
            "alpha_component_usd",
        ):
            _check_finite(getattr(self, name), name)

    @property
    def attribution_ratio(self) -> float | None:
        """Return alpha_component_usd / total_realized_pnl_usd, or None when total is zero."""
        if self.total_realized_pnl_usd == 0:
            return None
        return self.alpha_component_usd / self.total_realized_pnl_usd


@dataclass(frozen=True, slots=True)
class ThesisQualityAggregate:
    """Outer record consumed via raw state category 6."""

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

    def __post_init__(self) -> None:
        if self.as_of_timestamp.tzinfo is None or self.as_of_timestamp.utcoffset() is None:
            msg = "as_of_timestamp must be timezone-aware UTC"
            raise ValueError(msg)
        self._validate_window_uniqueness()
        self._validate_pair_uniqueness()

    def _validate_window_uniqueness(self) -> None:
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

    def _validate_pair_uniqueness(self) -> None:
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
