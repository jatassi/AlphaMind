"""Pure compute-on-read aggregation for ``ThesisQualityAggregate`` (ALP-878).

Functional core (P1): the SQL repository (imperative shell) queries resolved
theses + their components and decodes them into ``ThesisRecord`` objects, then
hands the decoded records + the config-sourced trailing windows + an ``as_of``
timestamp to :func:`compute_thesis_quality_aggregate`. This module performs no
I/O, holds no clock, and never touches SQLAlchemy — it is unit-testable against
hand-built in-memory fixtures.

Per parent decision (C) — compute-on-read, no materialized table — and the
ALP-131 orchestration scope decision (2026-06-07), only the **4 fields
computable from ``theses`` + ``thesis_components``** are populated:

* ``resolution_counts_by_window`` — trailing resolution counts by
  ``resolution_category`` per window, plus validation rate (derived).
* ``duration_stats_by_window`` — actual/expected duration ratio statistics.
* ``invalidation_timing_stats_by_window`` — EARLY/ON_TIME/LATE classification
  of invalidated theses plus mean position age at invalidation.
* ``signal_hit_rates`` — per-assumption hit rates derived from
  ``thesis_components.key_assumptions`` outcomes (NOT ``supporting_signals``,
  which is hardcoded empty at write time).

The remaining 5 fields (``performance_attribution``,
``alpha_beta_decomposition_by_window``, ``conviction_calibration``,
``conviction_sizing_deviation_by_window``, ``signal_to_thesis_conversions``)
need sector/regime/thesis-type persistence, market data, or typed
conviction/sizing contracts that do not exist in the three permitted tables;
they are left at their documented-empty value and tracked by ALP-906.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime, timedelta

from alphamind.portfolio_state.aggregates.thesis_quality import (
    InvalidationTimingClass,
    InvalidationTimingStat,
    ResolutionWindowCounts,
    SignalHitRate,
    ThesisDurationStat,
    ThesisQualityAggregate,
    TrailingWindow,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)

__all__ = ["compute_thesis_quality_aggregate"]

# Maps a configured trailing-window length in calendar days to the fixed
# ``TrailingWindow`` enum member it represents. The enum models exactly the
# 5-day / 20-day / inception windows the design specifies
# (state-persistence.md § Tier 3 / portfolio-state.md § 6); a configured day
# value with no enum member is a config/enum mismatch and fails closed below.
_DAYS_TO_WINDOW: dict[int, TrailingWindow] = {
    5: TrailingWindow.FIVE_DAYS,
    20: TrailingWindow.TWENTY_DAYS,
}

# Actual/expected duration ratios within this band are ON_TIME; below is EARLY,
# above is LATE. The symmetric ±20% band tolerates ordinary resolution-timing
# jitter while still surfacing systematically-early stops (premature
# invalidation) and systematically-late stops (stale theses) — the two failure
# modes the PM watches for. Documented here as the helper's defensible
# definition per the story's § 1 requirement.
_ON_TIME_LOWER_RATIO = 0.8
_ON_TIME_UPPER_RATIO = 1.2

_INVALIDATED_CATEGORIES = frozenset(
    {
        ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY,
        ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT,
    }
)


def compute_thesis_quality_aggregate(
    *,
    resolved_theses: Sequence[ThesisRecord],
    trailing_windows_days: tuple[int, ...],
    as_of: datetime,
) -> ThesisQualityAggregate:
    """Compute the 4 in-scope ``ThesisQualityAggregate`` fields from resolved theses.

    ``resolved_theses`` must all carry ``status == RESOLVED`` (the shell filters
    by status before calling). ``trailing_windows_days`` comes from
    ``PortfolioStateConfig.thesis_quality_aggregates_trailing_windows_days``;
    each day value maps to a finite ``TrailingWindow``. The inception window is
    always computed in addition to the configured finite windows.

    The 5 deferred fields are returned empty (ALP-906).
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        msg = "as_of must be timezone-aware UTC"
        raise ValueError(msg)

    windows = _resolve_windows(trailing_windows_days, as_of)
    only_resolved = tuple(t for t in resolved_theses if t.status == ThesisRecordStatus.RESOLVED)

    resolution_counts: list[ResolutionWindowCounts] = []
    duration_stats: list[ThesisDurationStat] = []
    invalidation_stats: list[InvalidationTimingStat] = []
    signal_hit_rates: list[SignalHitRate] = []

    for window, cutoff in windows:
        in_window = _theses_in_window(only_resolved, cutoff)
        resolution_counts.append(_resolution_counts_for(window, in_window))
        duration_stats.append(_duration_stat_for(window, in_window))
        invalidation_stats.append(_invalidation_timing_for(window, in_window))
        signal_hit_rates.extend(_signal_hit_rates_for(window, in_window))

    return ThesisQualityAggregate(
        as_of_timestamp=as_of,
        resolution_counts_by_window=tuple(resolution_counts),
        duration_stats_by_window=tuple(duration_stats),
        invalidation_timing_stats_by_window=tuple(invalidation_stats),
        signal_hit_rates=tuple(signal_hit_rates),
        # The 5 fields below need data absent from theses + thesis_components
        # (sector/regime/thesis_type persistence, market data, typed
        # conviction/sizing contracts) and are deferred to ALP-906.
        signal_to_thesis_conversions=(),
        conviction_calibration=(),
        conviction_sizing_deviation_by_window=(),
        performance_attribution=(),
        alpha_beta_decomposition_by_window=(),
    )


def _resolve_windows(
    trailing_windows_days: tuple[int, ...],
    as_of: datetime,
) -> tuple[tuple[TrailingWindow, datetime | None], ...]:
    """Pair each configured day-window with its enum member + cutoff; append INCEPTION.

    A finite window's cutoff is ``as_of - days``; resolved theses with a
    ``resolution_timestamp`` at or after the cutoff fall inside it. The
    INCEPTION window carries no cutoff (``None``) — every resolved thesis. Config
    order is preserved and duplicate windows de-duplicated. A day value with no
    corresponding ``TrailingWindow`` member fails closed.
    """
    resolved: list[tuple[TrailingWindow, datetime | None]] = []
    seen: set[TrailingWindow] = set()
    for days in trailing_windows_days:
        window = _DAYS_TO_WINDOW.get(days)
        if window is None:
            msg = (
                f"trailing window of {days} day(s) has no corresponding TrailingWindow "
                f"member; expected one of {sorted(_DAYS_TO_WINDOW)}"
            )
            raise ValueError(msg)
        if window not in seen:
            resolved.append((window, as_of - timedelta(days=days)))
            seen.add(window)
    if TrailingWindow.INCEPTION not in seen:
        resolved.append((TrailingWindow.INCEPTION, None))
    return tuple(resolved)


def _theses_in_window(
    resolved: Sequence[ThesisRecord], cutoff: datetime | None
) -> tuple[ThesisRecord, ...]:
    if cutoff is None:
        return tuple(resolved)
    return tuple(
        t
        for t in resolved
        if t.resolution_timestamp is not None and t.resolution_timestamp >= cutoff
    )


def _resolution_counts_for(
    window: TrailingWindow, theses: Sequence[ThesisRecord]
) -> ResolutionWindowCounts:
    by_category: dict[ThesisResolutionCategory, int] = dict.fromkeys(ThesisResolutionCategory, 0)
    for thesis in theses:
        if thesis.resolution_category is not None:
            by_category[thesis.resolution_category] += 1
    return ResolutionWindowCounts(
        window=window,
        total_resolutions=sum(by_category.values()),
        validated=by_category[ThesisResolutionCategory.VALIDATED],
        profitable_but_wrong=by_category[ThesisResolutionCategory.PROFITABLE_BUT_WRONG],
        invalidated_stopped_correctly=by_category[
            ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY
        ],
        invalidated_wrong_on_exit=by_category[ThesisResolutionCategory.INVALIDATED_WRONG_ON_EXIT],
        cancelled_never_entered=by_category[ThesisResolutionCategory.CANCELLED_NEVER_ENTERED],
    )


def _duration_ratio(thesis: ThesisRecord) -> float | None:
    """Return actual/expected duration ratio, or None when not computable."""
    if thesis.resolution_timestamp is None or thesis.time_expectation_hours <= 0:
        return None
    actual_hours = (
        thesis.resolution_timestamp - thesis.generation_timestamp
    ).total_seconds() / 3600.0
    return actual_hours / thesis.time_expectation_hours


def _duration_stat_for(
    window: TrailingWindow, theses: Sequence[ThesisRecord]
) -> ThesisDurationStat:
    ratios = [r for r in (_duration_ratio(t) for t in theses) if r is not None]
    if not ratios:
        return ThesisDurationStat(
            window=window,
            mean_actual_to_expected_ratio=None,
            median_actual_to_expected_ratio=None,
            count=0,
        )
    return ThesisDurationStat(
        window=window,
        mean_actual_to_expected_ratio=statistics.fmean(ratios),
        median_actual_to_expected_ratio=statistics.median(ratios),
        count=len(ratios),
    )


def _classify_timing(ratio: float) -> InvalidationTimingClass:
    if ratio < _ON_TIME_LOWER_RATIO:
        return InvalidationTimingClass.EARLY
    if ratio > _ON_TIME_UPPER_RATIO:
        return InvalidationTimingClass.LATE
    return InvalidationTimingClass.ON_TIME


def _invalidation_timing_for(
    window: TrailingWindow, theses: Sequence[ThesisRecord]
) -> InvalidationTimingStat:
    distribution: dict[InvalidationTimingClass, int] = dict.fromkeys(InvalidationTimingClass, 0)
    ages_hours: list[float] = []
    for thesis in theses:
        if thesis.resolution_category not in _INVALIDATED_CATEGORIES:
            continue
        if thesis.resolution_timestamp is None:
            continue
        ratio = _duration_ratio(thesis)
        if ratio is None:
            continue
        distribution[_classify_timing(ratio)] += 1
        ages_hours.append(
            (thesis.resolution_timestamp - thesis.generation_timestamp).total_seconds() / 3600.0
        )
    mean_age = statistics.fmean(ages_hours) if ages_hours else None
    return InvalidationTimingStat(
        window=window,
        class_distribution=distribution,
        mean_position_age_at_invalidation_hours=mean_age,
    )


def _signal_hit_rates_for(
    window: TrailingWindow, theses: Sequence[ThesisRecord]
) -> tuple[SignalHitRate, ...]:
    """Aggregate per-assumption hit rates over resolved component assumptions.

    An assumption is *cited* once per occurrence across the window's resolved
    theses (keyed by assumption text — the signal identity available in the
    persisted ``key_assumptions``) and *validated* when its outcome is
    ``VALIDATED``. ``WRONG`` / ``INCONCLUSIVE`` / unscored (``None``) outcomes
    count toward citations but not validations. ``supporting_signals_json`` is
    deliberately not used: it is hardcoded empty at write time (theses_codec).
    """
    cited: dict[str, int] = {}
    validated: dict[str, int] = {}
    for thesis in theses:
        for component in thesis.components:
            for assumption in component.key_assumptions:
                signal = assumption.text
                cited[signal] = cited.get(signal, 0) + 1
                if assumption.outcome == ThesisComponentOutcome.VALIDATED:
                    validated[signal] = validated.get(signal, 0) + 1
    return tuple(
        SignalHitRate(
            signal_type=signal,
            window=window,
            cited_count=count,
            validated_count=validated.get(signal, 0),
        )
        for signal, count in sorted(cited.items())
    )
