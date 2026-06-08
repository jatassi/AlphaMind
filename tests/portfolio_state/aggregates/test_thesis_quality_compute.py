"""Tests for the pure thesis-quality aggregation helper (ALP-878).

The helper computes the three §6a ``ThesisQualityAggregate`` fields
(``resolution_counts_by_window``, ``duration_stats_by_window``,
``invalidation_timing_stats_by_window``) from a sequence of RESOLVED
``ThesisRecord`` objects + config-sourced trailing windows.
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime, timedelta

import pytest

from alphamind._kernel.ids import PositionId, ThesisId
from alphamind.portfolio_state.aggregates.thesis_quality import (
    InvalidationTimingClass,
    ThesisQualityAggregate,
    TrailingWindow,
)
from alphamind.portfolio_state.aggregates.thesis_quality_compute import (
    compute_thesis_quality_aggregate,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)

_AS_OF = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)
_WINDOWS = (5, 20)


def _component(
    *,
    cid: str,
    thesis_id: str,
    ctype: ThesisComponentType,
) -> ThesisComponent:
    return ThesisComponent(
        component_id=cid,
        thesis_id=ThesisId(thesis_id),
        component_type=ctype,
        linked_bracket_leg_type=None,
        instrument_reference="AAPL",
        narrative="Narrative.",
        key_assumptions=(),
        generation_timestamp=_AS_OF - timedelta(hours=48),
        # RESOLVED theses require every component to carry a scored outcome
        # (ThesisRecord validator); the specific value is not read by the §6a
        # compute, which no longer inspects components.
        resolution_outcome=ThesisComponentOutcome.VALIDATED,
        resolution_notes=None,
    )


def _resolved_thesis(
    *,
    thesis_id: str = "T-1",
    position_id: str = "POS-1",
    category: ThesisResolutionCategory = ThesisResolutionCategory.VALIDATED,
    generated_at: datetime | None = None,
    resolved_at: datetime | None = None,
    time_expectation_hours: float = 24.0,
) -> ThesisRecord:
    gen = generated_at if generated_at is not None else _AS_OF - timedelta(hours=24)
    res = resolved_at if resolved_at is not None else _AS_OF - timedelta(hours=1)
    types = (
        ThesisComponentType.ENTRY_RATIONALE,
        ThesisComponentType.TARGET_RATIONALE,
        ThesisComponentType.INVALIDATION_RATIONALE,
    )
    components = tuple(
        _component(cid=f"{thesis_id}-c{i}", thesis_id=thesis_id, ctype=types[i]) for i in range(3)
    )
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="Summary.",
        key_catalyst="catalyst",
        components=components,
        status=ThesisRecordStatus.RESOLVED,
        generation_timestamp=gen,
        time_expectation_hours=time_expectation_hours,
        age_hours=(res - gen).total_seconds() / 3600.0,
        expected_resolution_at=gen + timedelta(hours=time_expectation_hours),
        resolution_timestamp=res,
        resolution_category=category,
        resolution_pnl_usd=100.0,
        entry_fill_gap_usd=None,
    )


class TestEmptyInput:
    def test_zero_theses_returns_zeroed_shape(self) -> None:
        agg = compute_thesis_quality_aggregate(
            resolved_theses=(),
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        assert isinstance(agg, ThesisQualityAggregate)
        assert agg.as_of_timestamp == _AS_OF
        # One ResolutionWindowCounts per window (5, 20, INCEPTION), all zero.
        windows = {e.window for e in agg.resolution_counts_by_window}
        assert windows == {
            TrailingWindow.FIVE_DAYS,
            TrailingWindow.TWENTY_DAYS,
            TrailingWindow.INCEPTION,
        }
        for entry in agg.resolution_counts_by_window:
            assert entry.total_resolutions == 0
            assert entry.validation_rate is None


class TestResolutionCounts:
    def test_counts_by_category(self) -> None:
        theses = (
            _resolved_thesis(thesis_id="T-1", category=ThesisResolutionCategory.VALIDATED),
            _resolved_thesis(thesis_id="T-2", category=ThesisResolutionCategory.VALIDATED),
            _resolved_thesis(
                thesis_id="T-3",
                category=ThesisResolutionCategory.PROFITABLE_BUT_WRONG,
            ),
            _resolved_thesis(
                thesis_id="T-4",
                category=ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY,
            ),
            _resolved_thesis(
                thesis_id="T-5",
                category=ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT,
            ),
        )
        agg = compute_thesis_quality_aggregate(
            resolved_theses=theses,
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        inception = agg.counts_for(TrailingWindow.INCEPTION)
        assert inception is not None
        assert inception.total_resolutions == 5
        assert inception.validated == 2
        assert inception.profitable_but_wrong == 1
        assert inception.invalidated_stopped_correctly == 1
        assert inception.invalidated_wrong_on_exit == 1
        assert inception.cancelled_never_entered == 0
        assert inception.validation_rate == pytest.approx(2 / 5)

    def test_window_filters_by_resolution_timestamp(self) -> None:
        # One resolved 2 days ago (in 5d window), one 10 days ago (only in 20d + inception).
        recent = _resolved_thesis(
            thesis_id="T-recent",
            resolved_at=_AS_OF - timedelta(days=2),
            generated_at=_AS_OF - timedelta(days=3),
        )
        older = _resolved_thesis(
            thesis_id="T-older",
            resolved_at=_AS_OF - timedelta(days=10),
            generated_at=_AS_OF - timedelta(days=11),
        )
        agg = compute_thesis_quality_aggregate(
            resolved_theses=(recent, older),
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        five = agg.counts_for(TrailingWindow.FIVE_DAYS)
        twenty = agg.counts_for(TrailingWindow.TWENTY_DAYS)
        inception = agg.counts_for(TrailingWindow.INCEPTION)
        assert five is not None and five.total_resolutions == 1
        assert twenty is not None and twenty.total_resolutions == 2
        assert inception is not None and inception.total_resolutions == 2


class TestDurationStats:
    def test_ratio_actual_to_expected(self) -> None:
        # actual = 12h, expected = 24h => ratio 0.5; actual = 36h, expected 24h => 1.5
        fast = _resolved_thesis(
            thesis_id="T-fast",
            generated_at=_AS_OF - timedelta(hours=14),
            resolved_at=_AS_OF - timedelta(hours=2),
            time_expectation_hours=24.0,
        )
        slow = _resolved_thesis(
            thesis_id="T-slow",
            generated_at=_AS_OF - timedelta(hours=38),
            resolved_at=_AS_OF - timedelta(hours=2),
            time_expectation_hours=24.0,
        )
        agg = compute_thesis_quality_aggregate(
            resolved_theses=(fast, slow),
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        inception = next(
            e for e in agg.duration_stats_by_window if e.window == TrailingWindow.INCEPTION
        )
        assert inception.count == 2
        assert inception.mean_actual_to_expected_ratio == pytest.approx(
            statistics.fmean([0.5, 1.5])
        )
        assert inception.median_actual_to_expected_ratio == pytest.approx(
            statistics.median([0.5, 1.5])
        )

    def test_zero_resolutions_in_window_yields_none_ratio(self) -> None:
        agg = compute_thesis_quality_aggregate(
            resolved_theses=(),
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        for entry in agg.duration_stats_by_window:
            assert entry.count == 0
            assert entry.mean_actual_to_expected_ratio is None
            assert entry.median_actual_to_expected_ratio is None


class TestInvalidationTiming:
    def test_classifies_early_on_time_late(self) -> None:
        # Only invalidated categories count. ratio = actual/expected.
        # EARLY: ratio 0.5 (12h/24h); ON_TIME: ratio 1.0 (24h/24h); LATE: ratio 2.0 (48h/24h)
        early = _resolved_thesis(
            thesis_id="T-early",
            category=ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY,
            generated_at=_AS_OF - timedelta(hours=14),
            resolved_at=_AS_OF - timedelta(hours=2),
            time_expectation_hours=24.0,
        )
        on_time = _resolved_thesis(
            thesis_id="T-ontime",
            category=ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT,
            generated_at=_AS_OF - timedelta(hours=26),
            resolved_at=_AS_OF - timedelta(hours=2),
            time_expectation_hours=24.0,
        )
        late = _resolved_thesis(
            thesis_id="T-late",
            category=ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY,
            generated_at=_AS_OF - timedelta(hours=50),
            resolved_at=_AS_OF - timedelta(hours=2),
            time_expectation_hours=24.0,
        )
        # A non-invalidated thesis must be excluded.
        validated = _resolved_thesis(
            thesis_id="T-val",
            category=ThesisResolutionCategory.VALIDATED,
        )
        agg = compute_thesis_quality_aggregate(
            resolved_theses=(early, on_time, late, validated),
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        inception = next(
            e
            for e in agg.invalidation_timing_stats_by_window
            if e.window == TrailingWindow.INCEPTION
        )
        assert inception.class_distribution[InvalidationTimingClass.EARLY] == 1
        assert inception.class_distribution[InvalidationTimingClass.ON_TIME] == 1
        assert inception.class_distribution[InvalidationTimingClass.LATE] == 1
        # mean position age at invalidation = mean of actual durations (12, 24, 48)
        assert inception.mean_position_age_at_invalidation_hours == pytest.approx(
            statistics.fmean([12.0, 24.0, 48.0])
        )

    def test_no_invalidations_yields_zero_distribution(self) -> None:
        agg = compute_thesis_quality_aggregate(
            resolved_theses=(_resolved_thesis(category=ThesisResolutionCategory.VALIDATED),),
            trailing_windows_days=_WINDOWS,
            as_of=_AS_OF,
        )
        inception = next(
            e
            for e in agg.invalidation_timing_stats_by_window
            if e.window == TrailingWindow.INCEPTION
        )
        assert sum(inception.class_distribution.values()) == 0
        assert inception.mean_position_age_at_invalidation_hours is None


class TestWindowMapping:
    def test_unmappable_window_day_raises(self) -> None:
        with pytest.raises(ValueError, match="trailing window"):
            compute_thesis_quality_aggregate(
                resolved_theses=(),
                trailing_windows_days=(7,),
                as_of=_AS_OF,
            )

    def test_naive_as_of_raises(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            compute_thesis_quality_aggregate(
                resolved_theses=(),
                trailing_windows_days=_WINDOWS,
                as_of=datetime(2026, 6, 7, 12, 0, 0),  # noqa: DTZ001 — deliberately naive
            )
