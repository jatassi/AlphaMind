"""Tests for thesis quality aggregate records (story 03f)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.portfolio_state.aggregates.thesis_quality import (
    InvalidationTimingClass,
    InvalidationTimingStat,
    ResolutionWindowCounts,
    ThesisDurationStat,
    ThesisQualityAggregate,
    TrailingWindow,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestTrailingWindow:
    def test_members(self) -> None:
        assert {m.name for m in TrailingWindow} == {"FIVE_DAYS", "TWENTY_DAYS", "INCEPTION"}

    def test_string_values(self) -> None:
        assert TrailingWindow.FIVE_DAYS == "FIVE_DAYS"
        assert TrailingWindow.TWENTY_DAYS == "TWENTY_DAYS"
        assert TrailingWindow.INCEPTION == "INCEPTION"

    def test_exact_count(self) -> None:
        assert len(TrailingWindow) == 3


class TestInvalidationTimingClass:
    def test_members(self) -> None:
        assert {m.name for m in InvalidationTimingClass} == {"EARLY", "ON_TIME", "LATE"}

    def test_string_values(self) -> None:
        assert InvalidationTimingClass.EARLY == "EARLY"
        assert InvalidationTimingClass.ON_TIME == "ON_TIME"
        assert InvalidationTimingClass.LATE == "LATE"

    def test_exact_count(self) -> None:
        assert len(InvalidationTimingClass) == 3


# ---------------------------------------------------------------------------
# ResolutionWindowCounts
# ---------------------------------------------------------------------------


class TestResolutionWindowCounts:
    def _valid(self, **overrides: object) -> dict[str, Any]:
        base: dict[str, Any] = {
            "window": TrailingWindow.FIVE_DAYS,
            "total_resolutions": 10,
            "validated": 4,
            "profitable_but_wrong": 2,
            "invalidated_stopped_correctly": 2,
            "invalidated_wrong_on_exit": 1,
            "cancelled_never_entered": 1,
        }
        base.update(overrides)
        return base

    def test_valid_construction(self) -> None:
        obj = ResolutionWindowCounts(**self._valid())
        assert obj.window == TrailingWindow.FIVE_DAYS
        assert obj.total_resolutions == 10

    def test_validation_rate_nonzero(self) -> None:
        obj = ResolutionWindowCounts(**self._valid())
        assert obj.validation_rate == pytest.approx(4 / 10)

    def test_validation_rate_zero_denominator(self) -> None:
        obj = ResolutionWindowCounts(
            **self._valid(
                total_resolutions=0,
                validated=0,
                profitable_but_wrong=0,
                invalidated_stopped_correctly=0,
                invalidated_wrong_on_exit=0,
                cancelled_never_entered=0,
            )
        )
        assert obj.validation_rate is None

    def test_conservation_violation_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            ResolutionWindowCounts(**self._valid(validated=5))  # 5+2+2+1+1 != 10

    def test_negative_count_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            ResolutionWindowCounts(**self._valid(validated=-1))


# ---------------------------------------------------------------------------
# ThesisDurationStat
# ---------------------------------------------------------------------------


class TestThesisDurationStat:
    def test_valid_with_ratios(self) -> None:
        obj = ThesisDurationStat(
            window=TrailingWindow.TWENTY_DAYS,
            mean_actual_to_expected_ratio=1.2,
            median_actual_to_expected_ratio=1.0,
            count=5,
        )
        assert obj.count == 5
        assert obj.mean_actual_to_expected_ratio == pytest.approx(1.2)

    def test_valid_with_none_ratios(self) -> None:
        obj = ThesisDurationStat(
            window=TrailingWindow.INCEPTION,
            mean_actual_to_expected_ratio=None,
            median_actual_to_expected_ratio=None,
            count=0,
        )
        assert obj.mean_actual_to_expected_ratio is None
        assert obj.median_actual_to_expected_ratio is None

    def test_negative_count_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisDurationStat(
                window=TrailingWindow.FIVE_DAYS,
                mean_actual_to_expected_ratio=None,
                median_actual_to_expected_ratio=None,
                count=-1,
            )

    def test_non_finite_mean_ratio_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisDurationStat(
                window=TrailingWindow.FIVE_DAYS,
                mean_actual_to_expected_ratio=float("inf"),
                median_actual_to_expected_ratio=1.0,
                count=3,
            )


# ---------------------------------------------------------------------------
# InvalidationTimingStat
# ---------------------------------------------------------------------------


class TestInvalidationTimingStat:
    def _valid_dist(self) -> dict[InvalidationTimingClass, int]:
        return {
            InvalidationTimingClass.EARLY: 3,
            InvalidationTimingClass.ON_TIME: 5,
            InvalidationTimingClass.LATE: 2,
        }

    def test_valid_construction(self) -> None:
        obj = InvalidationTimingStat(
            window=TrailingWindow.FIVE_DAYS,
            class_distribution=self._valid_dist(),
            mean_position_age_at_invalidation_hours=24.0,
        )
        assert obj.class_distribution[InvalidationTimingClass.EARLY] == 3

    def test_valid_none_mean_age(self) -> None:
        obj = InvalidationTimingStat(
            window=TrailingWindow.FIVE_DAYS,
            class_distribution=self._valid_dist(),
            mean_position_age_at_invalidation_hours=None,
        )
        assert obj.mean_position_age_at_invalidation_hours is None

    def test_missing_class_distribution_key_raises(self) -> None:
        dist = {
            InvalidationTimingClass.EARLY: 3,
            InvalidationTimingClass.ON_TIME: 5,
            # LATE missing
        }
        with pytest.raises((ValueError, TypeError)):
            InvalidationTimingStat(
                window=TrailingWindow.FIVE_DAYS,
                class_distribution=dist,
                mean_position_age_at_invalidation_hours=None,
            )

    def test_negative_value_in_distribution_raises(self) -> None:
        dist = dict(self._valid_dist())
        dist[InvalidationTimingClass.EARLY] = -1
        with pytest.raises((ValueError, TypeError)):
            InvalidationTimingStat(
                window=TrailingWindow.FIVE_DAYS,
                class_distribution=dist,
                mean_position_age_at_invalidation_hours=None,
            )

    def test_non_finite_mean_age_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            InvalidationTimingStat(
                window=TrailingWindow.FIVE_DAYS,
                class_distribution=self._valid_dist(),
                mean_position_age_at_invalidation_hours=float("nan"),
            )

    def test_negative_mean_age_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            InvalidationTimingStat(
                window=TrailingWindow.FIVE_DAYS,
                class_distribution=self._valid_dist(),
                mean_position_age_at_invalidation_hours=-1.0,
            )


# ---------------------------------------------------------------------------
# ThesisQualityAggregate
# ---------------------------------------------------------------------------


_TS_AWARE = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
_TS_NAIVE = datetime.fromisoformat("2024-06-01T12:00:00")  # intentionally naive


def _make_resolution_counts(window: TrailingWindow) -> ResolutionWindowCounts:
    return ResolutionWindowCounts(
        window=window,
        total_resolutions=5,
        validated=2,
        profitable_but_wrong=1,
        invalidated_stopped_correctly=1,
        invalidated_wrong_on_exit=1,
        cancelled_never_entered=0,
    )


def _make_duration_stat(window: TrailingWindow) -> ThesisDurationStat:
    return ThesisDurationStat(
        window=window,
        mean_actual_to_expected_ratio=1.1,
        median_actual_to_expected_ratio=1.0,
        count=5,
    )


def _make_invalidation_stat(window: TrailingWindow) -> InvalidationTimingStat:
    return InvalidationTimingStat(
        window=window,
        class_distribution={
            InvalidationTimingClass.EARLY: 1,
            InvalidationTimingClass.ON_TIME: 1,
            InvalidationTimingClass.LATE: 0,
        },
        mean_position_age_at_invalidation_hours=12.0,
    )


def _make_full_aggregate() -> ThesisQualityAggregate:
    """Build a minimal but complete aggregate."""
    w5 = TrailingWindow.FIVE_DAYS
    w20 = TrailingWindow.TWENTY_DAYS
    wi = TrailingWindow.INCEPTION

    return ThesisQualityAggregate(
        as_of_timestamp=_TS_AWARE,
        resolution_counts_by_window=(
            _make_resolution_counts(w5),
            _make_resolution_counts(w20),
            _make_resolution_counts(wi),
        ),
        duration_stats_by_window=(
            _make_duration_stat(w5),
            _make_duration_stat(w20),
            _make_duration_stat(wi),
        ),
        invalidation_timing_stats_by_window=(
            _make_invalidation_stat(w5),
            _make_invalidation_stat(w20),
            _make_invalidation_stat(wi),
        ),
    )


class TestThesisQualityAggregate:
    def test_happy_path_construction(self) -> None:
        agg = _make_full_aggregate()
        assert agg.as_of_timestamp == _TS_AWARE
        assert len(agg.resolution_counts_by_window) == 3

    def test_counts_for_existing_window(self) -> None:
        agg = _make_full_aggregate()
        result = agg.counts_for(TrailingWindow.FIVE_DAYS)
        assert result is not None
        assert result.window == TrailingWindow.FIVE_DAYS

    def test_counts_for_absent_window(self) -> None:
        # Build aggregate with only FIVE_DAYS window counts
        agg = ThesisQualityAggregate(
            as_of_timestamp=_TS_AWARE,
            resolution_counts_by_window=(_make_resolution_counts(TrailingWindow.FIVE_DAYS),),
            duration_stats_by_window=(),
            invalidation_timing_stats_by_window=(),
        )
        assert agg.counts_for(TrailingWindow.INCEPTION) is None

    def test_naive_timestamp_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisQualityAggregate(
                as_of_timestamp=_TS_NAIVE,
                resolution_counts_by_window=(),
                duration_stats_by_window=(),
                invalidation_timing_stats_by_window=(),
            )

    def test_duplicate_window_in_resolution_counts_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisQualityAggregate(
                as_of_timestamp=_TS_AWARE,
                resolution_counts_by_window=(
                    _make_resolution_counts(TrailingWindow.FIVE_DAYS),
                    _make_resolution_counts(TrailingWindow.FIVE_DAYS),  # duplicate
                ),
                duration_stats_by_window=(),
                invalidation_timing_stats_by_window=(),
            )
