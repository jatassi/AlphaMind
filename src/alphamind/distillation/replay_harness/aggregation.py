"""Aggregation primitives — story 02-distillation-layer/replay-harness/06.

Consumes per-slice :class:`SliceReplayResult` collections produced by the
engine (story 05) and computes the four content sections the report
renderer (story 07) emits: per-regime flag-rate table, regime-label
distribution per slice, Class B baseline shape summary, calibration-state
breakdown.

Single-config aggregation accepts one ``dict[regime_label, list[SliceReplayResult]]``;
diff-mode aggregation accepts two and emits candidate / baseline / delta tuples
per cell. The function is purely functional — well-formed inputs never raise,
and identical inputs produce byte-deterministic output (every collection is
sorted alphabetically before being frozen into a tuple).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import numpy as np

from alphamind.distillation.replay_harness.engine import SliceReplayResult

# ---------------------------------------------------------------------------
# Mode literals — kept as bare constants (not StrEnum) so report consumers
# can compare against ``"single"`` / ``"diff"`` without importing the module.
# ---------------------------------------------------------------------------

_MODE_SINGLE = "single"
_MODE_DIFF = "diff"


# ---------------------------------------------------------------------------
# Per-regime flag-rate dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlagRateCell:
    """One cell of the per-regime flag-rate table."""

    flag_type: str
    regime_label: str
    flag_invocation_count: int
    total_invocation_count: int
    rate: float


@dataclass(frozen=True, slots=True)
class FlagRateDelta:
    """Diff-mode flag-rate cell carrying candidate, baseline, and rate delta."""

    candidate: FlagRateCell
    baseline: FlagRateCell
    delta_rate: float


@dataclass(frozen=True, slots=True)
class PerRegimeFlagRates:
    """Per-regime flag-rate aggregation across one or both configs."""

    mode: str
    single_cells: tuple[FlagRateCell, ...] | None
    diff_cells: tuple[FlagRateDelta, ...] | None
    flag_types: tuple[str, ...]
    regimes: tuple[str, ...]


# ---------------------------------------------------------------------------
# Regime-label distribution dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeLabelDistributionEntry:
    """One slice's per-orchestrator-classified-label counts."""

    slice_id: str
    slice_exemplary_regime: str
    candidate_label_counts: dict[str, int]
    baseline_label_counts: dict[str, int] | None


@dataclass(frozen=True, slots=True)
class RegimeLabelDistribution:
    """Ordered tuple of per-slice classification distributions."""

    entries: tuple[RegimeLabelDistributionEntry, ...]


# ---------------------------------------------------------------------------
# Class B baseline shape dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineShapeSummary:
    """One Class B baseline class x regime cell with median + IQR."""

    baseline_kind: str
    regime_label: str
    value_count: int
    value_median: float | None
    value_iqr: float | None


@dataclass(frozen=True, slots=True)
class BaselineShapeDelta:
    """Diff-mode baseline-shape cell carrying candidate and baseline summaries."""

    candidate: BaselineShapeSummary
    baseline: BaselineShapeSummary


@dataclass(frozen=True, slots=True)
class PerBaselineShapeSummary:
    """Per-baseline-class shape aggregation across one or both configs."""

    mode: str
    single_cells: tuple[BaselineShapeSummary, ...] | None
    diff_cells: tuple[BaselineShapeDelta, ...] | None


# ---------------------------------------------------------------------------
# Calibration-state breakdown dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalibrationStateBreakdownEntry:
    """One regime x calibration-state cell counting outputs across both kinds."""

    regime_label: str
    calibration_state: str
    candidate_count: int
    baseline_count: int | None


@dataclass(frozen=True, slots=True)
class CalibrationStateBreakdown:
    """Ordered tuple of regime x calibration-state cells."""

    entries: tuple[CalibrationStateBreakdownEntry, ...]


# ---------------------------------------------------------------------------
# Top-level aggregated report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AggregatedReport:
    """The four content aggregations the renderer consumes."""

    mode: str
    flag_rates: PerRegimeFlagRates
    regime_distribution: RegimeLabelDistribution
    baseline_shape: PerBaselineShapeSummary
    calibration_breakdown: CalibrationStateBreakdown


# ---------------------------------------------------------------------------
# Typed input-validation exception
# ---------------------------------------------------------------------------


class AggregationInputError(ValueError):
    """Raised when ``aggregate_replay_results`` receives malformed inputs."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def aggregate_replay_results(
    candidate_results: dict[str, list[SliceReplayResult]],
    baseline_results: dict[str, list[SliceReplayResult]] | None = None,
) -> AggregatedReport:
    """Compute the four content aggregations from per-slice replay results.

    The dict key is the slice's exemplary ``regime_label``; the value is the
    list of slice results for that regime under the named config. When
    ``baseline_results`` is ``None`` the function runs single-config
    aggregation; otherwise it computes diff-mode tuples (candidate /
    baseline / delta) and validates that both dicts cover the same regimes
    and the same ordered slice IDs per regime.
    """
    _validate_inputs(candidate_results, baseline_results)
    candidate_nonempty = _drop_empty_regimes(candidate_results)
    baseline_nonempty = (
        _drop_empty_regimes(baseline_results) if baseline_results is not None else None
    )
    mode = _MODE_DIFF if baseline_nonempty is not None else _MODE_SINGLE
    return AggregatedReport(
        mode=mode,
        flag_rates=_compute_flag_rates(candidate_nonempty, baseline_nonempty),
        regime_distribution=_compute_regime_distribution(candidate_nonempty, baseline_nonempty),
        baseline_shape=_compute_baseline_shape(candidate_nonempty, baseline_nonempty),
        calibration_breakdown=_compute_calibration_breakdown(candidate_nonempty, baseline_nonempty),
    )


def _drop_empty_regimes(
    results: dict[str, list[SliceReplayResult]],
) -> dict[str, list[SliceReplayResult]]:
    """Drop regimes whose slice list is empty.

    The story spec says: "A regime with zero slices is dropped from all
    aggregations (rather than zero-padding)" — applied uniformly across all
    four aggregations rather than re-asserted in each.
    """
    return {regime: slices for regime, slices in results.items() if slices}


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def _validate_inputs(
    candidate_results: dict[str, list[SliceReplayResult]],
    baseline_results: dict[str, list[SliceReplayResult]] | None,
) -> None:
    """Reject empty candidate dicts and diff-mode regime/slice-ID mismatches."""
    if not candidate_results:
        raise AggregationInputError("candidate_results is empty; nothing to aggregate")
    if baseline_results is None:
        return
    candidate_regimes = set(candidate_results)
    baseline_regimes = set(baseline_results)
    if candidate_regimes != baseline_regimes:
        only_candidate = sorted(candidate_regimes - baseline_regimes)
        only_baseline = sorted(baseline_regimes - candidate_regimes)
        raise AggregationInputError(
            "diff-mode regime keys mismatch: "
            f"only in candidate={only_candidate}, only in baseline={only_baseline}"
        )
    for regime in sorted(candidate_regimes):
        candidate_slice_ids = [s.slice_id for s in candidate_results[regime]]
        baseline_slice_ids = [s.slice_id for s in baseline_results[regime]]
        if candidate_slice_ids != baseline_slice_ids:
            raise AggregationInputError(
                f"diff-mode slice IDs mismatch in regime '{regime}': "
                f"candidate={candidate_slice_ids}, baseline={baseline_slice_ids}"
            )


# ---------------------------------------------------------------------------
# Per-regime flag-rate computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FlagCounts:
    """Per-regime per-flag invocation counts plus the regime's total invocations."""

    per_flag: dict[str, int]
    total: int


def _flag_counts_for_regime(slices: list[SliceReplayResult]) -> _FlagCounts:
    """Return per-flag invocation counts and total invocations for one regime.

    The per-flag count uses per-invocation deduplication: an invocation that
    fires three flags of the same type counts as 1 toward that flag.
    """
    per_flag: dict[str, int] = {}
    total = 0
    for slice_result in slices:
        for invocation in slice_result.invocations:
            total += 1
            for flag_type in {flag.flag_type for flag in invocation.anomaly_flags}:
                per_flag[flag_type] = per_flag.get(flag_type, 0) + 1
    return _FlagCounts(per_flag=per_flag, total=total)


def _build_flag_rate_cell(
    *, flag_type: str, regime_label: str, counts: _FlagCounts
) -> FlagRateCell:
    flag_count = counts.per_flag.get(flag_type, 0)
    rate = flag_count / counts.total if counts.total > 0 else 0.0
    return FlagRateCell(
        flag_type=flag_type,
        regime_label=regime_label,
        flag_invocation_count=flag_count,
        total_invocation_count=counts.total,
        rate=rate,
    )


def _compute_flag_rates(
    candidate_results: dict[str, list[SliceReplayResult]],
    baseline_results: dict[str, list[SliceReplayResult]] | None,
) -> PerRegimeFlagRates:
    """Compute the per-regime flag-rate table for one or both configs."""
    candidate_counts = {
        regime: _flag_counts_for_regime(slices) for regime, slices in candidate_results.items()
    }
    baseline_counts = (
        {regime: _flag_counts_for_regime(slices) for regime, slices in baseline_results.items()}
        if baseline_results is not None
        else None
    )

    flag_types_set: set[str] = set()
    for counts in candidate_counts.values():
        flag_types_set.update(counts.per_flag)
    if baseline_counts is not None:
        for counts in baseline_counts.values():
            flag_types_set.update(counts.per_flag)
    flag_types = tuple(sorted(flag_types_set))
    regimes = tuple(sorted(candidate_counts))

    if baseline_counts is None:
        single_cells = tuple(
            _build_flag_rate_cell(
                flag_type=flag_type, regime_label=regime, counts=candidate_counts[regime]
            )
            for regime in regimes
            for flag_type in flag_types
            if candidate_counts[regime].per_flag.get(flag_type, 0) > 0
        )
        return PerRegimeFlagRates(
            mode=_MODE_SINGLE,
            single_cells=single_cells,
            diff_cells=None,
            flag_types=flag_types,
            regimes=regimes,
        )

    diff_cells = tuple(
        _build_flag_rate_delta(
            flag_type=flag_type,
            regime=regime,
            candidate_counts=candidate_counts[regime],
            baseline_counts=baseline_counts[regime],
        )
        for regime in regimes
        for flag_type in flag_types
    )
    return PerRegimeFlagRates(
        mode=_MODE_DIFF,
        single_cells=None,
        diff_cells=diff_cells,
        flag_types=flag_types,
        regimes=regimes,
    )


def _build_flag_rate_delta(
    *,
    flag_type: str,
    regime: str,
    candidate_counts: _FlagCounts,
    baseline_counts: _FlagCounts,
) -> FlagRateDelta:
    """Build a candidate/baseline/delta tuple for one (flag_type, regime) cell."""
    candidate_cell = _build_flag_rate_cell(
        flag_type=flag_type, regime_label=regime, counts=candidate_counts
    )
    baseline_cell = _build_flag_rate_cell(
        flag_type=flag_type, regime_label=regime, counts=baseline_counts
    )
    return FlagRateDelta(
        candidate=candidate_cell,
        baseline=baseline_cell,
        delta_rate=candidate_cell.rate - baseline_cell.rate,
    )


# ---------------------------------------------------------------------------
# Regime-label distribution computation
# ---------------------------------------------------------------------------


def _label_counts(slice_result: SliceReplayResult) -> dict[str, int]:
    """Per-orchestrator-classified-label counts across one slice's invocations."""
    return dict(Counter(invocation.regime_label for invocation in slice_result.invocations))


def _baseline_values_by_kind_for_regime(
    slices: list[SliceReplayResult],
) -> dict[str, list[float]]:
    """Bucket non-null baseline values by ``baseline_kind`` across all invocations."""
    by_kind: dict[str, list[float]] = {}
    for slice_result in slices:
        for invocation in slice_result.invocations:
            for record in invocation.class_b_baseline_values:
                if record.value is None:
                    continue
                by_kind.setdefault(record.baseline_kind, []).append(record.value)
    return by_kind


def _summarize_values(
    *, baseline_kind: str, regime_label: str, values: list[float]
) -> BaselineShapeSummary:
    """Build a :class:`BaselineShapeSummary` from a list of non-null values.

    Uses :func:`numpy.median` and :func:`numpy.percentile` with the package
    default ``method='linear'`` interpolation. The cell carries ``None`` for
    median and IQR when the values list is empty (the caller filters empty
    kinds out before reaching this helper, so the ``None`` branch is
    defensive).
    """
    if not values:
        return BaselineShapeSummary(
            baseline_kind=baseline_kind,
            regime_label=regime_label,
            value_count=0,
            value_median=None,
            value_iqr=None,
        )
    arr = np.asarray(values, dtype=float)
    q1, q3 = np.percentile(arr, [25.0, 75.0])
    return BaselineShapeSummary(
        baseline_kind=baseline_kind,
        regime_label=regime_label,
        value_count=len(values),
        value_median=float(np.median(arr)),
        value_iqr=float(q3 - q1),
    )


def _compute_baseline_shape(
    candidate_results: dict[str, list[SliceReplayResult]],
    baseline_results: dict[str, list[SliceReplayResult]] | None,
) -> PerBaselineShapeSummary:
    """Compute per-baseline-class shape statistics across one or both configs."""
    candidate_buckets = {
        regime: _baseline_values_by_kind_for_regime(slices)
        for regime, slices in candidate_results.items()
    }
    baseline_buckets = (
        {
            regime: _baseline_values_by_kind_for_regime(slices)
            for regime, slices in baseline_results.items()
        }
        if baseline_results is not None
        else None
    )

    kinds_set: set[str] = set()
    for buckets in candidate_buckets.values():
        kinds_set.update(buckets)
    if baseline_buckets is not None:
        for buckets in baseline_buckets.values():
            kinds_set.update(buckets)

    if baseline_buckets is None:
        single_cells = tuple(
            _summarize_values(
                baseline_kind=kind,
                regime_label=regime,
                values=candidate_buckets[regime].get(kind, []),
            )
            for regime in sorted(candidate_buckets)
            for kind in sorted(kinds_set)
            if candidate_buckets[regime].get(kind)
        )
        return PerBaselineShapeSummary(
            mode=_MODE_SINGLE, single_cells=single_cells, diff_cells=None
        )

    diff_cells = tuple(
        BaselineShapeDelta(
            candidate=_summarize_values(
                baseline_kind=kind,
                regime_label=regime,
                values=candidate_buckets[regime].get(kind, []),
            ),
            baseline=_summarize_values(
                baseline_kind=kind,
                regime_label=regime,
                values=baseline_buckets[regime].get(kind, []),
            ),
        )
        for regime in sorted(candidate_buckets)
        for kind in sorted(kinds_set)
        if candidate_buckets[regime].get(kind) or baseline_buckets[regime].get(kind)
    )
    return PerBaselineShapeSummary(mode=_MODE_DIFF, single_cells=None, diff_cells=diff_cells)


def _calibration_state_counts(
    slices: list[SliceReplayResult],
) -> dict[str, int]:
    """Count outputs (anomaly flags + Class B baseline values) per calibration state."""
    counts: dict[str, int] = {}
    for slice_result in slices:
        for invocation in slice_result.invocations:
            for flag in invocation.anomaly_flags:
                counts[flag.calibration_state] = counts.get(flag.calibration_state, 0) + 1
            for record in invocation.class_b_baseline_values:
                counts[record.calibration_state] = counts.get(record.calibration_state, 0) + 1
    return counts


def _compute_calibration_breakdown(
    candidate_results: dict[str, list[SliceReplayResult]],
    baseline_results: dict[str, list[SliceReplayResult]] | None,
) -> CalibrationStateBreakdown:
    """Build the per-(regime, calibration-state) output-count table."""
    candidate_counts = {
        regime: _calibration_state_counts(slices) for regime, slices in candidate_results.items()
    }
    baseline_counts = (
        {regime: _calibration_state_counts(slices) for regime, slices in baseline_results.items()}
        if baseline_results is not None
        else {}
    )

    entries: list[CalibrationStateBreakdownEntry] = []
    for regime in sorted(candidate_counts):
        observed_states = set(candidate_counts[regime])
        if baseline_results is not None:
            observed_states.update(baseline_counts.get(regime, {}))
        for state in sorted(observed_states):
            entries.append(
                CalibrationStateBreakdownEntry(
                    regime_label=regime,
                    calibration_state=state,
                    candidate_count=candidate_counts[regime].get(state, 0),
                    baseline_count=(
                        baseline_counts.get(regime, {}).get(state, 0)
                        if baseline_results is not None
                        else None
                    ),
                )
            )
    return CalibrationStateBreakdown(entries=tuple(entries))


def _compute_regime_distribution(
    candidate_results: dict[str, list[SliceReplayResult]],
    baseline_results: dict[str, list[SliceReplayResult]] | None,
) -> RegimeLabelDistribution:
    """Compute the per-slice regime-label distribution table."""
    entries: list[RegimeLabelDistributionEntry] = []
    for regime in sorted(candidate_results):
        candidate_slices = candidate_results[regime]
        baseline_slices_by_id = (
            {s.slice_id: s for s in baseline_results[regime]}
            if baseline_results is not None
            else {}
        )
        for candidate_slice in sorted(candidate_slices, key=lambda s: s.slice_id):
            baseline_counts: dict[str, int] | None = None
            if baseline_results is not None:
                baseline_counts = _label_counts(baseline_slices_by_id[candidate_slice.slice_id])
            entries.append(
                RegimeLabelDistributionEntry(
                    slice_id=candidate_slice.slice_id,
                    slice_exemplary_regime=regime,
                    candidate_label_counts=_label_counts(candidate_slice),
                    baseline_label_counts=baseline_counts,
                )
            )
    return RegimeLabelDistribution(entries=tuple(entries))


__all__ = [
    "AggregatedReport",
    "AggregationInputError",
    "BaselineShapeDelta",
    "BaselineShapeSummary",
    "CalibrationStateBreakdown",
    "CalibrationStateBreakdownEntry",
    "FlagRateCell",
    "FlagRateDelta",
    "PerBaselineShapeSummary",
    "PerRegimeFlagRates",
    "RegimeLabelDistribution",
    "RegimeLabelDistributionEntry",
    "aggregate_replay_results",
]
