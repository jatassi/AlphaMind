"""Notable-shift detectors for the weekly digest (§ Section 5, ALP-888).

The seven deterministic detectors that flag a week's anomalies, each driven by its
``config/digest.yaml`` block (typed as :class:`~alphamind.config.models.digest.DigestConfig`).
Pure functions over the same per-week ``(week_label, WindowDataset)`` sequence the
generator consumes — no I/O — so the digest stays deterministic over its input.

Each detector compares the **current week** (the last input) to its
``baseline_window_weeks`` trailing window and yields a :class:`ShiftFinding` when its
threshold is crossed, or ``None`` when it is not. Five detectors read registry metrics by
canonical id (anti-pattern / regime / per-sector P-L are gated by later stories and
degrade to "no finding"; citation-chain / signal-survival are registered by 06d). The two
validation detectors read the dataset's validation bundles directly.

A ``ShiftFinding`` is the renderer's row: the shift kind, a stable subject (which sector /
source / pattern / validation), and a short human-readable detail. The thresholds
themselves live only in config; the finding records the crossing, not the knob.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, get_args

from alphamind.commands.pm_envelope import AntiPattern
from alphamind.feedback_loop.citation.chain import metric_id_for
from alphamind.feedback_loop.citation.parser import CitationSource
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricId

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from alphamind.config.models.digest import (
        AntiPatternSpike,
        CitationChainShift,
        DigestConfig,
        SectorUnderperform,
        SourceSignalSurvivalDrop,
        ValidationWindowEnd,
    )
    from alphamind.feedback_loop.dataset import WindowDataset

    WeekInput = tuple[str, WindowDataset]


# ---------------------------------------------------------------------------
# Shift finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShiftFinding:
    """One flagged notable shift — a renderer row (§ Section 5).

    * ``kind`` — the detector's config-block key (``anti_pattern_spike`` etc.).
    * ``subject`` — the specific thing that shifted (sector / source / pattern name /
      validation id), or ``""`` for whole-system shifts (regime change).
    * ``detail`` — a short, deterministic human-readable description of the crossing.
    """

    kind: str
    subject: str
    detail: str


# ---------------------------------------------------------------------------
# Canonical (later-story) metric id references
# ---------------------------------------------------------------------------

#: Per-pattern anti-pattern-frequency id prefix (06b). ``<prefix>__<pattern>``.
_ANTI_PATTERN_FREQUENCY_PREFIX = "anti_pattern_frequency"
#: Sourced from the single ``AntiPattern`` Literal so a sixth member auto-flows into the
#: anti-pattern-spike detector rather than being silently dropped at five (``decision.py``
#: and ``generator.py`` read the same ``get_args`` source).
_ANTI_PATTERN_NAMES: tuple[str, ...] = tuple(get_args(AntiPattern))

#: Per-invocation regime classification (06b). A scalar-coded label per week.
_M_REGIME_CLASSIFICATION = MetricId("regime_classification")

#: Per-sector rolling P/L id prefix (06b). ``<prefix>__<sector>``.
_SECTOR_PL_PREFIX = "sector_rolling_pl"


def _metric_value(dataset: WindowDataset, metric_id: MetricId) -> float | None:
    """Compute *metric_id* over *dataset*, or ``None`` when unregistered / empty.

    The single graceful-degradation seam for the detectors: a gated metric (anti-
    pattern / regime / per-sector before 06b) returns ``None``, so the detector
    yields no finding rather than erroring.
    """
    metric = get_metric(metric_id)
    if metric is None:
        return None
    return metric.compute(dataset, UNCONDITIONED).value


def _baseline_weeks(weeks: Sequence[WeekInput], baseline_window_weeks: int) -> Sequence[WeekInput]:
    """The trailing ``baseline_window_weeks`` *before* the current week.

    The current week is ``weeks[-1]``; the baseline is the ``baseline_window_weeks``
    weeks immediately preceding it (clamped to what the sequence holds).
    """
    if len(weeks) <= 1:
        return ()
    start = max(0, len(weeks) - 1 - baseline_window_weeks)
    return weeks[start : len(weeks) - 1]


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _current_vs_baseline_mean(
    *,
    current: WindowDataset,
    baseline: Sequence[WeekInput],
    subjects: Sequence[tuple[str, MetricId]],
) -> Iterator[tuple[str, float, float]]:
    """Yield ``(subject, this_week, baseline_mean)`` per metric with both readings.

    The shared current-week-value + trailing-baseline-mean loop behind the
    anti-pattern-spike and per-source percentage-point-shift detectors. For each
    ``(subject, metric_id)`` pair it computes the current week's value and the mean
    of the trailing baseline weeks' values, skipping any metric where the current
    reading or the baseline is missing (a gated metric degrades to no finding). Each
    detector keeps its own threshold test and :class:`ShiftFinding` construction.
    """
    for subject, metric_id in subjects:
        this_week = _metric_value(current, metric_id)
        if this_week is None:
            continue
        baseline_values = [
            value
            for _, dataset in baseline
            if (value := _metric_value(dataset, metric_id)) is not None
        ]
        baseline_mean = _mean(baseline_values)
        if baseline_mean is None:
            continue
        yield subject, this_week, baseline_mean


# ---------------------------------------------------------------------------
# Detector 1 — anti-pattern spike
# ---------------------------------------------------------------------------


def detect_anti_pattern_spike(
    weeks: Sequence[WeekInput], config: AntiPatternSpike
) -> tuple[ShiftFinding, ...]:
    """Fire per pattern whose weekly count exceeds ``multiplier * baseline mean`` AND
    meets the ``min_occurrences_this_week`` floor."""
    subjects = [
        (pattern, MetricId(f"{_ANTI_PATTERN_FREQUENCY_PREFIX}__{pattern}"))
        for pattern in _ANTI_PATTERN_NAMES
    ]
    findings: list[ShiftFinding] = []
    for pattern, this_week, baseline_mean in _current_vs_baseline_mean(
        current=weeks[-1][1],
        baseline=_baseline_weeks(weeks, config.baseline_window_weeks),
        subjects=subjects,
    ):
        if (
            this_week >= config.min_occurrences_this_week
            and this_week > config.multiplier_vs_baseline * baseline_mean
        ):
            findings.append(
                ShiftFinding(
                    kind="anti_pattern_spike",
                    subject=pattern,
                    detail=(
                        f"{this_week:g} this week vs baseline mean {baseline_mean:g} "
                        f"(x{config.multiplier_vs_baseline:g})"
                    ),
                )
            )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Detector 2 — regime change
# ---------------------------------------------------------------------------


def detect_regime_change(weeks: Sequence[WeekInput], *, enabled: bool) -> tuple[ShiftFinding, ...]:
    """Fire when the current week's regime label differs from the prior week's."""
    if not enabled or len(weeks) < 2:
        return ()
    current = _metric_value(weeks[-1][1], _M_REGIME_CLASSIFICATION)
    prior = _metric_value(weeks[-2][1], _M_REGIME_CLASSIFICATION)
    if current is None or prior is None or current == prior:
        return ()
    return (
        ShiftFinding(
            kind="regime_change",
            subject="",
            detail=f"regime classification changed from {prior:g} to {current:g}",
        ),
    )


# ---------------------------------------------------------------------------
# Detector 3 — sector underperforming
# ---------------------------------------------------------------------------

#: The traded sectors whose rolling P/L the detector compares against each other.
#: Mirrors the per-sector P/L metric family (06b); the cross-sector comparison needs
#: a fixed sector set so a missing sector reads as no-data, not a silent drop.
_SECTORS: tuple[str, ...] = (
    "technology",
    "financials",
    "energy",
    "healthcare",
    "consumer",
    "industrials",
)


def _stddev(values: Sequence[float]) -> float:
    """Sample standard deviation, computing its own mean (0.0 for < 2 values)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return float(variance**0.5)


def detect_sector_underperform(
    weeks: Sequence[WeekInput], config: SectorUnderperform
) -> tuple[ShiftFinding, ...]:
    """Fire per sector whose rolling P/L falls below
    ``(median of other sectors - median_offset_sigma * stddev)``."""
    current = weeks[-1][1]
    sector_pl: dict[str, float] = {}
    for sector in _SECTORS:
        value = _metric_value(current, MetricId(f"{_SECTOR_PL_PREFIX}__{sector}"))
        if value is not None:
            sector_pl[sector] = value
    if len(sector_pl) < 2:
        return ()
    findings: list[ShiftFinding] = []
    for sector, pl in sector_pl.items():
        others = [v for s, v in sector_pl.items() if s != sector]
        median = statistics.median(others)
        sigma = _stddev(others)
        threshold = median - config.median_offset_sigma * sigma
        if pl < threshold:
            findings.append(
                ShiftFinding(
                    kind="sector_underperform",
                    subject=sector,
                    detail=(
                        f"rolling P/L {pl:g} below peer median {median:g} "
                        f"- {config.median_offset_sigma:g} stddev ({threshold:g})"
                    ),
                )
            )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Detectors 4 & 5 — per-source rate shifts (citation chain / signal survival)
# ---------------------------------------------------------------------------


def _per_source_pp_shift(
    weeks: Sequence[WeekInput],
    *,
    metric_name: str,
    baseline_window_weeks: int,
    delta_pp_threshold: float,
    kind: str,
    direction: str,
) -> tuple[ShiftFinding, ...]:
    """Shared core for the two per-source percentage-point shift detectors.

    For each :class:`CitationSource`, compares the current week's rate (a 0-1
    fraction) to the trailing baseline mean. ``direction`` is ``"any"`` (fire on a
    change of magnitude > threshold, either way — citation chain) or ``"drop"``
    (fire only on a decrease > threshold — signal survival). The threshold is in
    percentage points, so the 0-1 rate is scaled by 100.
    """
    subjects = [(source.value, metric_id_for(metric_name, source)) for source in CitationSource]
    findings: list[ShiftFinding] = []
    for subject, this_week, baseline_mean in _current_vs_baseline_mean(
        current=weeks[-1][1],
        baseline=_baseline_weeks(weeks, baseline_window_weeks),
        subjects=subjects,
    ):
        delta_pp = (this_week - baseline_mean) * 100.0
        fired = (
            abs(delta_pp) > delta_pp_threshold
            if direction == "any"
            else -delta_pp > delta_pp_threshold
        )
        if fired:
            findings.append(
                ShiftFinding(
                    kind=kind,
                    subject=subject,
                    detail=(
                        f"{metric_name} {this_week * 100:g}% vs baseline "
                        f"{baseline_mean * 100:g}% (Δ {delta_pp:+g}pp)"
                    ),
                )
            )
    return tuple(findings)


def detect_citation_chain_shift(
    weeks: Sequence[WeekInput], config: CitationChainShift
) -> tuple[ShiftFinding, ...]:
    """Fire per source whose synthesizer citation rate changed by more than
    ``delta_pp_threshold`` percentage points from the baseline mean."""
    return _per_source_pp_shift(
        weeks,
        metric_name="synthesizer_citation_rate",
        baseline_window_weeks=config.baseline_window_weeks,
        delta_pp_threshold=config.delta_pp_threshold,
        kind="citation_chain_shift",
        direction="any",
    )


def detect_source_signal_survival_drop(
    weeks: Sequence[WeekInput], config: SourceSignalSurvivalDrop
) -> tuple[ShiftFinding, ...]:
    """Fire per source whose signal survival rate dropped by more than
    ``delta_pp_threshold`` percentage points from the baseline mean."""
    return _per_source_pp_shift(
        weeks,
        metric_name="signal_survival_rate",
        baseline_window_weeks=config.baseline_window_weeks,
        delta_pp_threshold=config.delta_pp_threshold,
        kind="source_signal_survival_drop",
        direction="drop",
    )


# ---------------------------------------------------------------------------
# Detectors 6 & 7 — validation lifecycle shifts
# ---------------------------------------------------------------------------


def detect_validation_window_end(
    weeks: Sequence[WeekInput], config: ValidationWindowEnd
) -> tuple[ShiftFinding, ...]:
    """Fire per pending validation within ``days_before_due`` days of evaluation-due
    (or already overdue), measured against the current week's window end."""
    current = weeks[-1][1]
    now = current.end
    cutoff = now + timedelta(days=config.days_before_due)
    findings: list[ShiftFinding] = []
    for validation in current.validations:
        if validation.evaluation_due_at <= cutoff:
            overdue = validation.evaluation_due_at < now
            state = "overdue" if overdue else "due soon"
            findings.append(
                ShiftFinding(
                    kind="validation_window_end",
                    subject=str(validation.validation_id),
                    detail=f"{state}: evaluation due {validation.evaluation_due_at.isoformat()}",
                )
            )
    return tuple(findings)


def detect_validation_superseded(
    weeks: Sequence[WeekInput], *, enabled: bool
) -> tuple[ShiftFinding, ...]:
    """Fire per validation auto-superseded during the current week.

    Reads ``WindowDataset.superseded_validations`` — the validations whose
    ``superseded_at`` falls in the current week's ``[start, end)`` — and records the
    supersession reason on each finding (§ Section 5).
    """
    if not enabled:
        return ()
    current = weeks[-1][1]
    findings: list[ShiftFinding] = []
    for validation in current.superseded_validations:
        reason = (
            validation.superseded_reason.value
            if validation.superseded_reason is not None
            else "unknown"
        )
        findings.append(
            ShiftFinding(
                kind="validation_superseded",
                subject=str(validation.validation_id),
                detail=f"superseded ({reason})",
            )
        )
    return tuple(findings)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def detect_shifts(weeks: Sequence[WeekInput], config: DigestConfig) -> tuple[ShiftFinding, ...]:
    """Run all seven detectors over the current week and return every finding.

    Findings are emitted in detector order (the § Section 5 listing order); within a
    detector, per-subject order is the fixed sector / source / pattern / validation
    iteration order, so the result is deterministic over the input sequence.
    """
    findings: list[ShiftFinding] = []
    findings.extend(detect_anti_pattern_spike(weeks, config.anti_pattern_spike))
    findings.extend(detect_regime_change(weeks, enabled=config.regime_change.enabled))
    findings.extend(detect_sector_underperform(weeks, config.sector_underperform))
    findings.extend(detect_citation_chain_shift(weeks, config.citation_chain_shift))
    findings.extend(detect_source_signal_survival_drop(weeks, config.source_signal_survival_drop))
    findings.extend(detect_validation_window_end(weeks, config.validation_window_end))
    findings.extend(
        detect_validation_superseded(weeks, enabled=config.validation_superseded.enabled)
    )
    return tuple(findings)


__all__ = [
    "ShiftFinding",
    "detect_anti_pattern_spike",
    "detect_citation_chain_shift",
    "detect_regime_change",
    "detect_sector_underperform",
    "detect_shifts",
    "detect_source_signal_survival_drop",
    "detect_validation_superseded",
    "detect_validation_window_end",
]
