"""Thesis quality aggregate records (raw state category 6)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = [
    "InvalidationTimingClass",
    "InvalidationTimingStat",
    "ResolutionWindowCounts",
    "ThesisDurationStat",
    "ThesisQualityAggregate",
    "TrailingWindow",
]


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


class InvalidationTimingClass(StrEnum):
    """Invalidation timing classification per portfolio-state.md § 6a."""

    EARLY = "EARLY"
    ON_TIME = "ON_TIME"
    LATE = "LATE"


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
class ThesisQualityAggregate:
    """Outer record consumed via raw state category 6."""

    as_of_timestamp: datetime
    resolution_counts_by_window: tuple[ResolutionWindowCounts, ...]
    duration_stats_by_window: tuple[ThesisDurationStat, ...]
    invalidation_timing_stats_by_window: tuple[InvalidationTimingStat, ...]

    def __post_init__(self) -> None:
        if self.as_of_timestamp.tzinfo is None or self.as_of_timestamp.utcoffset() is None:
            msg = "as_of_timestamp must be timezone-aware UTC"
            raise ValueError(msg)
        self._validate_window_uniqueness()

    def _validate_window_uniqueness(self) -> None:
        window_tuples = [
            ("resolution_counts_by_window", [e.window for e in self.resolution_counts_by_window]),
            ("duration_stats_by_window", [e.window for e in self.duration_stats_by_window]),
            (
                "invalidation_timing_stats_by_window",
                [e.window for e in self.invalidation_timing_stats_by_window],
            ),
        ]
        for field_name, windows in window_tuples:
            if len(windows) != len(set(windows)):
                msg = f"{field_name} must not contain duplicate TrailingWindow values"
                raise ValueError(msg)

    def counts_for(self, window: TrailingWindow) -> ResolutionWindowCounts | None:
        """Return the ResolutionWindowCounts for window, or None if not present."""
        for entry in self.resolution_counts_by_window:
            if entry.window == window:
                return entry
        return None
