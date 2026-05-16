"""Tests for thesis quality aggregate records (story 03f)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any

import pytest

from alphamind.portfolio_state.records.thesis_quality import (
    AlphaBetaDecomposition,
    AttributionDimension,
    ConvictionCalibrationEntry,
    ConvictionSizingDeviation,
    InvalidationTimingClass,
    InvalidationTimingStat,
    PerformanceAttributionEntry,
    RegimeLabel,
    ResolutionWindowCounts,
    SignalHitRate,
    SignalToThesisConversion,
    ThesisDurationStat,
    ThesisQualityAggregate,
    ThesisType,
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


class TestThesisType:
    def test_members(self) -> None:
        assert {m.name for m in ThesisType} == {
            "EVENT_DRIVEN",
            "MEAN_REVERSION",
            "MOMENTUM",
            "CROSS_ASSET_DIVERGENCE",
        }

    def test_string_values(self) -> None:
        assert ThesisType.EVENT_DRIVEN == "EVENT_DRIVEN"
        assert ThesisType.MEAN_REVERSION == "MEAN_REVERSION"
        assert ThesisType.MOMENTUM == "MOMENTUM"
        assert ThesisType.CROSS_ASSET_DIVERGENCE == "CROSS_ASSET_DIVERGENCE"

    def test_exact_count(self) -> None:
        assert len(ThesisType) == 4


class TestInvalidationTimingClass:
    def test_members(self) -> None:
        assert {m.name for m in InvalidationTimingClass} == {"EARLY", "ON_TIME", "LATE"}

    def test_string_values(self) -> None:
        assert InvalidationTimingClass.EARLY == "EARLY"
        assert InvalidationTimingClass.ON_TIME == "ON_TIME"
        assert InvalidationTimingClass.LATE == "LATE"

    def test_exact_count(self) -> None:
        assert len(InvalidationTimingClass) == 3


class TestAttributionDimension:
    def test_members(self) -> None:
        assert {m.name for m in AttributionDimension} == {"SECTOR", "THESIS_TYPE", "REGIME"}

    def test_string_values(self) -> None:
        assert AttributionDimension.SECTOR == "SECTOR"
        assert AttributionDimension.THESIS_TYPE == "THESIS_TYPE"
        assert AttributionDimension.REGIME == "REGIME"

    def test_exact_count(self) -> None:
        assert len(AttributionDimension) == 3


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
# SignalHitRate
# ---------------------------------------------------------------------------


class TestSignalHitRate:
    def test_valid_construction(self) -> None:
        obj = SignalHitRate(
            signal_type="unusual_options_activity",
            window=TrailingWindow.FIVE_DAYS,
            cited_count=10,
            validated_count=7,
        )
        assert obj.signal_type == "unusual_options_activity"

    def test_hit_rate_nonzero(self) -> None:
        obj = SignalHitRate(
            signal_type="earnings_revisions",
            window=TrailingWindow.TWENTY_DAYS,
            cited_count=4,
            validated_count=3,
        )
        assert obj.hit_rate == pytest.approx(3 / 4)

    def test_hit_rate_zero_cited(self) -> None:
        obj = SignalHitRate(
            signal_type="earnings_revisions",
            window=TrailingWindow.TWENTY_DAYS,
            cited_count=0,
            validated_count=0,
        )
        assert obj.hit_rate is None

    def test_validated_count_greater_than_cited_permitted(self) -> None:
        # No constraint — counts track different slices across windows
        obj = SignalHitRate(
            signal_type="x", window=TrailingWindow.INCEPTION, cited_count=3, validated_count=5
        )
        assert obj.hit_rate == pytest.approx(5 / 3)


# ---------------------------------------------------------------------------
# SignalToThesisConversion
# ---------------------------------------------------------------------------


class TestSignalToThesisConversion:
    def test_valid_construction(self) -> None:
        obj = SignalToThesisConversion(
            signal_type="prediction_market_shift",
            window=TrailingWindow.FIVE_DAYS,
            signal_observed_count=8,
            pm_approved_count=3,
        )
        assert obj.conversion_rate == pytest.approx(3 / 8)

    def test_conversion_rate_zero_denominator(self) -> None:
        obj = SignalToThesisConversion(
            signal_type="prediction_market_shift",
            window=TrailingWindow.FIVE_DAYS,
            signal_observed_count=0,
            pm_approved_count=0,
        )
        assert obj.conversion_rate is None


# ---------------------------------------------------------------------------
# ConvictionCalibrationEntry
# ---------------------------------------------------------------------------


class TestConvictionCalibrationEntry:
    def test_valid_construction(self) -> None:
        obj = ConvictionCalibrationEntry(
            conviction_level=3,
            window=TrailingWindow.FIVE_DAYS,
            count=5,
            validation_rate=0.6,
            mean_realized_pnl_pct=2.5,
        )
        assert obj.conviction_level == 3

    def test_conviction_level_zero_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ConvictionCalibrationEntry(
                conviction_level=0,
                window=TrailingWindow.FIVE_DAYS,
                count=5,
                validation_rate=0.6,
                mean_realized_pnl_pct=None,
            )

    def test_conviction_level_six_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ConvictionCalibrationEntry(
                conviction_level=6,
                window=TrailingWindow.FIVE_DAYS,
                count=5,
                validation_rate=0.6,
                mean_realized_pnl_pct=None,
            )

    def test_non_finite_validation_rate_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ConvictionCalibrationEntry(
                conviction_level=3,
                window=TrailingWindow.FIVE_DAYS,
                count=5,
                validation_rate=float("inf"),
                mean_realized_pnl_pct=None,
            )

    def test_none_validation_rate_when_count_zero(self) -> None:
        obj = ConvictionCalibrationEntry(
            conviction_level=1,
            window=TrailingWindow.FIVE_DAYS,
            count=0,
            validation_rate=None,
            mean_realized_pnl_pct=None,
        )
        assert obj.validation_rate is None


# ---------------------------------------------------------------------------
# ConvictionSizingDeviation
# ---------------------------------------------------------------------------


class TestConvictionSizingDeviation:
    def _valid(self, **overrides: object) -> dict[str, Any]:
        base: dict[str, Any] = {
            "window": TrailingWindow.FIVE_DAYS,
            "total_proposals": 10,
            "pm_sized_above_advisory_count": 3,
            "pm_sized_below_advisory_count": 2,
            "pm_sized_within_advisory_count": 5,
            "outcome_correlation_above": 1.5,
            "outcome_correlation_below": -0.5,
        }
        base.update(overrides)
        return base

    def test_valid_construction(self) -> None:
        obj = ConvictionSizingDeviation(**self._valid())
        assert obj.total_proposals == 10

    def test_deviation_rate_nonzero(self) -> None:
        obj = ConvictionSizingDeviation(**self._valid())
        assert obj.deviation_rate == pytest.approx((3 + 2) / 10)

    def test_deviation_rate_zero_denominator(self) -> None:
        obj = ConvictionSizingDeviation(
            **self._valid(
                total_proposals=0,
                pm_sized_above_advisory_count=0,
                pm_sized_below_advisory_count=0,
                pm_sized_within_advisory_count=0,
            )
        )
        assert obj.deviation_rate is None

    def test_conservation_violation_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            ConvictionSizingDeviation(**self._valid(pm_sized_above_advisory_count=4))

    def test_finite_outcome_correlation_accepted(self) -> None:
        obj = ConvictionSizingDeviation(**self._valid(outcome_correlation_above=0.5))
        assert obj.outcome_correlation_above == pytest.approx(0.5)

    def test_none_outcome_correlation_accepted(self) -> None:
        obj = ConvictionSizingDeviation(
            **self._valid(outcome_correlation_above=None, outcome_correlation_below=None)
        )
        assert obj.outcome_correlation_above is None


# ---------------------------------------------------------------------------
# PerformanceAttributionEntry
# ---------------------------------------------------------------------------


class TestPerformanceAttributionEntry:
    def test_valid_sector_dimension(self) -> None:
        obj = PerformanceAttributionEntry(
            dimension=AttributionDimension.SECTOR,
            key="Technology",
            window=TrailingWindow.FIVE_DAYS,
            cumulative_realized_pnl_usd=1500.0,
            realized_pnl_pct_of_window_capital=2.5,
            count=3,
        )
        assert obj.dimension == AttributionDimension.SECTOR

    def test_valid_thesis_type_dimension(self) -> None:
        obj = PerformanceAttributionEntry(
            dimension=AttributionDimension.THESIS_TYPE,
            key=ThesisType.MOMENTUM,
            window=TrailingWindow.TWENTY_DAYS,
            cumulative_realized_pnl_usd=-200.0,
            realized_pnl_pct_of_window_capital=None,
            count=1,
        )
        assert obj.key == "MOMENTUM"

    def test_valid_regime_dimension(self) -> None:
        obj = PerformanceAttributionEntry(
            dimension=AttributionDimension.REGIME,
            key=RegimeLabel.CRISIS,
            window=TrailingWindow.INCEPTION,
            cumulative_realized_pnl_usd=300.0,
            realized_pnl_pct_of_window_capital=None,
            count=2,
        )
        assert obj.key == "CRISIS"

    def test_non_finite_pnl_usd_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            PerformanceAttributionEntry(
                dimension=AttributionDimension.SECTOR,
                key="Energy",
                window=TrailingWindow.FIVE_DAYS,
                cumulative_realized_pnl_usd=float("nan"),
                realized_pnl_pct_of_window_capital=None,
                count=0,
            )


# ---------------------------------------------------------------------------
# AlphaBetaDecomposition
# ---------------------------------------------------------------------------


class TestAlphaBetaDecomposition:
    def _valid(self, **overrides: object) -> dict[str, Any]:
        base: dict[str, Any] = {
            "window": TrailingWindow.FIVE_DAYS,
            "total_realized_pnl_usd": 1000.0,
            "market_component_usd": 400.0,
            "sector_component_usd": 200.0,
            "alpha_component_usd": 400.0,
        }
        base.update(overrides)
        return base

    def test_valid_construction(self) -> None:
        obj = AlphaBetaDecomposition(**self._valid())
        assert obj.total_realized_pnl_usd == pytest.approx(1000.0)

    def test_attribution_ratio_nonzero(self) -> None:
        obj = AlphaBetaDecomposition(**self._valid())
        assert obj.attribution_ratio == pytest.approx(400.0 / 1000.0)

    def test_attribution_ratio_zero_total(self) -> None:
        obj = AlphaBetaDecomposition(
            **self._valid(
                total_realized_pnl_usd=0.0,
                market_component_usd=0.0,
                sector_component_usd=0.0,
                alpha_component_usd=0.0,
            )
        )
        assert obj.attribution_ratio is None

    def test_attribution_ratio_negative_alpha(self) -> None:
        obj = AlphaBetaDecomposition(**self._valid(alpha_component_usd=-200.0))
        assert obj.attribution_ratio is not None
        assert obj.attribution_ratio < 0

    def test_non_finite_component_raises(self) -> None:
        with pytest.raises((FrozenInstanceError, ValueError, TypeError)):
            AlphaBetaDecomposition(**self._valid(alpha_component_usd=float("inf")))


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


def _make_sizing_deviation(window: TrailingWindow) -> ConvictionSizingDeviation:
    return ConvictionSizingDeviation(
        window=window,
        total_proposals=4,
        pm_sized_above_advisory_count=1,
        pm_sized_below_advisory_count=1,
        pm_sized_within_advisory_count=2,
        outcome_correlation_above=None,
        outcome_correlation_below=None,
    )


def _make_alpha_beta(window: TrailingWindow) -> AlphaBetaDecomposition:
    return AlphaBetaDecomposition(
        window=window,
        total_realized_pnl_usd=500.0,
        market_component_usd=200.0,
        sector_component_usd=100.0,
        alpha_component_usd=200.0,
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
        signal_hit_rates=(
            SignalHitRate(
                signal_type="options_activity", window=w5, cited_count=4, validated_count=3
            ),
            SignalHitRate(
                signal_type="earnings_revisions", window=w5, cited_count=2, validated_count=1
            ),
            SignalHitRate(
                signal_type="options_activity", window=w20, cited_count=8, validated_count=5
            ),
        ),
        signal_to_thesis_conversions=(
            SignalToThesisConversion(
                signal_type="options_activity",
                window=w5,
                signal_observed_count=6,
                pm_approved_count=4,
            ),
            SignalToThesisConversion(
                signal_type="earnings_revisions",
                window=w5,
                signal_observed_count=3,
                pm_approved_count=2,
            ),
        ),
        conviction_calibration=(
            ConvictionCalibrationEntry(
                conviction_level=3,
                window=w5,
                count=4,
                validation_rate=0.75,
                mean_realized_pnl_pct=1.5,
            ),
            ConvictionCalibrationEntry(
                conviction_level=5,
                window=w5,
                count=2,
                validation_rate=1.0,
                mean_realized_pnl_pct=3.0,
            ),
            ConvictionCalibrationEntry(
                conviction_level=3,
                window=w20,
                count=7,
                validation_rate=0.6,
                mean_realized_pnl_pct=1.0,
            ),
        ),
        conviction_sizing_deviation_by_window=(
            _make_sizing_deviation(w5),
            _make_sizing_deviation(w20),
            _make_sizing_deviation(wi),
        ),
        performance_attribution=(
            PerformanceAttributionEntry(
                dimension=AttributionDimension.SECTOR,
                key="Technology",
                window=w5,
                cumulative_realized_pnl_usd=500.0,
                realized_pnl_pct_of_window_capital=1.5,
                count=3,
            ),
            PerformanceAttributionEntry(
                dimension=AttributionDimension.THESIS_TYPE,
                key="MOMENTUM",
                window=w5,
                cumulative_realized_pnl_usd=200.0,
                realized_pnl_pct_of_window_capital=None,
                count=1,
            ),
        ),
        alpha_beta_decomposition_by_window=(
            _make_alpha_beta(w5),
            _make_alpha_beta(w20),
            _make_alpha_beta(wi),
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
            signal_hit_rates=(),
            signal_to_thesis_conversions=(),
            conviction_calibration=(),
            conviction_sizing_deviation_by_window=(),
            performance_attribution=(),
            alpha_beta_decomposition_by_window=(),
        )
        assert agg.counts_for(TrailingWindow.INCEPTION) is None

    def test_signal_hit_rate_existing(self) -> None:
        agg = _make_full_aggregate()
        result = agg.signal_hit_rate("options_activity", TrailingWindow.FIVE_DAYS)
        assert result is not None
        assert result.signal_type == "options_activity"
        assert result.window == TrailingWindow.FIVE_DAYS

    def test_signal_hit_rate_absent(self) -> None:
        agg = _make_full_aggregate()
        assert agg.signal_hit_rate("nonexistent", TrailingWindow.FIVE_DAYS) is None

    def test_conviction_entries_for_existing_window(self) -> None:
        agg = _make_full_aggregate()
        entries = agg.conviction_entries_for(TrailingWindow.FIVE_DAYS)
        assert len(entries) == 2
        levels = {e.conviction_level for e in entries}
        assert levels == {3, 5}

    def test_conviction_entries_for_absent_window(self) -> None:
        agg = _make_full_aggregate()
        entries = agg.conviction_entries_for(TrailingWindow.INCEPTION)
        assert entries == ()

    def test_naive_timestamp_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisQualityAggregate(
                as_of_timestamp=_TS_NAIVE,
                resolution_counts_by_window=(),
                duration_stats_by_window=(),
                invalidation_timing_stats_by_window=(),
                signal_hit_rates=(),
                signal_to_thesis_conversions=(),
                conviction_calibration=(),
                conviction_sizing_deviation_by_window=(),
                performance_attribution=(),
                alpha_beta_decomposition_by_window=(),
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
                signal_hit_rates=(),
                signal_to_thesis_conversions=(),
                conviction_calibration=(),
                conviction_sizing_deviation_by_window=(),
                performance_attribution=(),
                alpha_beta_decomposition_by_window=(),
            )

    def test_duplicate_signal_type_window_in_signal_hit_rates_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisQualityAggregate(
                as_of_timestamp=_TS_AWARE,
                resolution_counts_by_window=(),
                duration_stats_by_window=(),
                invalidation_timing_stats_by_window=(),
                signal_hit_rates=(
                    SignalHitRate(
                        signal_type="x",
                        window=TrailingWindow.FIVE_DAYS,
                        cited_count=1,
                        validated_count=1,
                    ),
                    SignalHitRate(
                        signal_type="x",
                        window=TrailingWindow.FIVE_DAYS,
                        cited_count=2,
                        validated_count=2,
                    ),
                ),
                signal_to_thesis_conversions=(),
                conviction_calibration=(),
                conviction_sizing_deviation_by_window=(),
                performance_attribution=(),
                alpha_beta_decomposition_by_window=(),
            )

    def test_duplicate_conviction_level_window_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisQualityAggregate(
                as_of_timestamp=_TS_AWARE,
                resolution_counts_by_window=(),
                duration_stats_by_window=(),
                invalidation_timing_stats_by_window=(),
                signal_hit_rates=(),
                signal_to_thesis_conversions=(),
                conviction_calibration=(
                    ConvictionCalibrationEntry(
                        conviction_level=3,
                        window=TrailingWindow.FIVE_DAYS,
                        count=0,
                        validation_rate=None,
                        mean_realized_pnl_pct=None,
                    ),
                    ConvictionCalibrationEntry(
                        conviction_level=3,
                        window=TrailingWindow.FIVE_DAYS,
                        count=0,
                        validation_rate=None,
                        mean_realized_pnl_pct=None,
                    ),
                ),
                conviction_sizing_deviation_by_window=(),
                performance_attribution=(),
                alpha_beta_decomposition_by_window=(),
            )

    def test_duplicate_dimension_key_window_in_performance_attribution_raises(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            ThesisQualityAggregate(
                as_of_timestamp=_TS_AWARE,
                resolution_counts_by_window=(),
                duration_stats_by_window=(),
                invalidation_timing_stats_by_window=(),
                signal_hit_rates=(),
                signal_to_thesis_conversions=(),
                conviction_calibration=(),
                conviction_sizing_deviation_by_window=(),
                performance_attribution=(
                    PerformanceAttributionEntry(
                        dimension=AttributionDimension.SECTOR,
                        key="Tech",
                        window=TrailingWindow.FIVE_DAYS,
                        cumulative_realized_pnl_usd=100.0,
                        realized_pnl_pct_of_window_capital=None,
                        count=1,
                    ),
                    PerformanceAttributionEntry(
                        dimension=AttributionDimension.SECTOR,
                        key="Tech",
                        window=TrailingWindow.FIVE_DAYS,
                        cumulative_realized_pnl_usd=200.0,
                        realized_pnl_pct_of_window_capital=None,
                        count=2,
                    ),
                ),
                alpha_beta_decomposition_by_window=(),
            )
