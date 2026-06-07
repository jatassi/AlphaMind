"""Weekly-digest generator — the deterministic six-section data structure (ALP-888).

The functional core (P1) of the weekly digest: a **pure** function over a sequence of
per-week ``(week_label, WindowDataset)`` inputs. The imperative shell (``cli.py`` plus a
thin loader) builds that sequence by calling ``dataset.load_window`` once per week; the
generator never touches a session. Same input sequence → identical :class:`WeeklyDigest`
(the determinism contract).

The digest is assembled from the metric registry: every section value is a lookup of a
canonical :class:`~alphamind.feedback_loop.metrics.types.MetricId` against
:func:`~alphamind.feedback_loop.metrics.get_metric`, then a pure ``compute`` over that
week's :class:`~alphamind.feedback_loop.dataset.WindowDataset`. A metric that is not yet
registered (e.g. PM-accuracy before story 06e, the anti-pattern / guardrail / analyst-
inaction / regime / per-sector metrics before 06b / 06f) resolves to ``None`` and renders
as its insufficient/empty cell — never an error (the graceful-degradation contract).

Section layout follows ``docs/design/feedback-loop.md`` § Dashboard and digest curation:

* Section 1 — Headline outcomes (current-week point values).
* Section 2 — Process pulse (current week + week-over-week delta).
* Section 3 — Trajectory (8-12-week sparklines, one value per week).
* Section 4 — Validation status (one row per pending validation).
* Section 5 — Notable shifts (the seven detectors in :mod:`.shifts`).
* Section 6 — Open validation queue (counterfactual-replay buckets, empty until 06e).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alphamind.feedback_loop.digest.shifts import ShiftFinding, detect_shifts
from alphamind.feedback_loop.metrics import get_metric
from alphamind.feedback_loop.metrics.types import UNCONDITIONED, MetricId, MetricResult

if TYPE_CHECKING:
    from collections.abc import Sequence

    from alphamind.config.models.digest import DigestConfig
    from alphamind.feedback_loop.dataset import WindowDataset

# ---------------------------------------------------------------------------
# Week input alias
# ---------------------------------------------------------------------------

#: A label for one week's window — the ISO date of the week's Monday, as a string.
#: The generator treats it opaquely (it is carried through to the trajectory points
#: and never parsed), so the shell owns the label convention.
WeekLabel = str

#: One per-week input: the week's label paired with its loaded, in-memory dataset.
WeekInput = tuple[WeekLabel, "WindowDataset"]


# ---------------------------------------------------------------------------
# Canonical MetricId references
# ---------------------------------------------------------------------------
#
# The digest selects metrics *by* id. Ids already registered by 05/06a/06c/06d
# resolve to a live reading; ids that land with a later story (06b anti-pattern /
# analyst-inaction / regime / per-sector P-L; 06e PM-accuracy; 06f guardrail-
# rejection) resolve to ``None`` today and render insufficient/empty. Referencing
# them by their canonical id now means they populate automatically the moment the
# owning story registers the same id — no edit here.

# Section 1 — Headline outcomes.
_M_PL_7D = MetricId("outcome_pl_last_7d")  # 06f
_M_WIN_RATE_7D = MetricId("outcome_win_rate_last_7d")  # 06f
_M_DRAWDOWN = MetricId("outcome_drawdown")  # 06c (registered)
_M_TRADES_CLOSED_7D = MetricId("outcome_trades_closed_last_7d")  # 06f

# Section 2 — Process pulse (six metrics, each with a WoW delta).
_M_PM_REJECTION_RATE = MetricId("pm_verdict_rate__reject")  # 06a (registered)
_M_PM_MODIFICATION_RATE = MetricId("pm_modification_rate")  # 06a (registered)
_M_ANALYST_INACTION_RATE = MetricId("analyst_inaction_rate")  # 06b
_M_STRATEGIST_HOLD_RATE = MetricId("strategist_hold_on_non_on_track_rate")  # 06a (registered)
_M_GUARDRAIL_REJECTION_COUNT = MetricId("guardrail_rejection_count")  # 06f

#: Canonical anti-pattern names (``commands.pm_envelope.AntiPattern``). Each is a
#: per-pattern frequency metric the process pulse shows as a small bar group; the
#: metric ids land with 06b and degrade gracefully until then.
_ANTI_PATTERN_NAMES: tuple[str, ...] = (
    "sunk_cost_persistence",
    "rationalized_continuation",
    "thesis_contradiction_suppression",
    "engine_originated_closure_signal",
    "conviction_inflation",
)
_M_ANTI_PATTERN_FREQUENCY = "anti_pattern_frequency"  # 06b id prefix → "<prefix>__<name>"

# Section 3 — Trajectory (six sparklines over the trailing weeks).
_M_WEEKLY_PL = MetricId("outcome_weekly_pl")  # 06f
_M_WEEKLY_WIN_RATE = MetricId("outcome_win_rate")  # 06c (registered)
_M_COST_PER_RESOLVED_THESIS = MetricId("outcome_pl_per_resolved_thesis")  # 06c (registered)
_M_CONVICTION_CALIBRATION = MetricId("outcome_conviction_calibration_win_rate")  # 06c
_M_STATUS_CALIBRATION = MetricId("outcome_status_calibration_adverse_rate")  # 06c
_M_PM_REJECTION_ACCURACY = MetricId("pm_rejection_accuracy")  # 06e (gated on replays)


def _anti_pattern_metric_id(name: str) -> MetricId:
    """The canonical per-pattern anti-pattern-frequency :class:`MetricId`."""
    return MetricId(f"{_M_ANTI_PATTERN_FREQUENCY}__{name}")


# ---------------------------------------------------------------------------
# Cell — one rendered metric reading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetricCell:
    """One metric reading rendered for the digest.

    A thin presentation wrapper over a :class:`MetricResult`: it carries the
    ``metric_id`` and, when the metric is registered and produced a reading, the
    full result. ``result`` is ``None`` when the metric is unregistered (gated by a
    later story) — the graceful-degradation signal the renderer shows as an
    insufficient/empty value rather than an error.
    """

    metric_id: MetricId
    result: MetricResult | None

    @property
    def present(self) -> bool:
        """Whether the metric is registered and produced a reading."""
        return self.result is not None

    @property
    def value(self) -> float | None:
        """The point estimate, or ``None`` when absent / empty-sampled."""
        return None if self.result is None else self.result.value


def _read_cell(dataset: WindowDataset, metric_id: MetricId) -> MetricCell:
    """Look up *metric_id* in the registry and compute it over *dataset*.

    The single place metric absence is handled: an unregistered id yields a cell
    with ``result=None``. Every section item flows through here, so no section body
    branches on registration.
    """
    metric = get_metric(metric_id)
    result = None if metric is None else metric.compute(dataset, UNCONDITIONED)
    return MetricCell(metric_id=metric_id, result=result)


# ---------------------------------------------------------------------------
# Section 1 — Headline outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeadlineOutcomes:
    """Four current-week headline values (no trends), each a :class:`MetricCell`."""

    pl_last_7d: MetricCell
    win_rate_last_7d: MetricCell
    current_drawdown: MetricCell
    trades_closed_last_7d: MetricCell


def _headline_outcomes(current: WindowDataset) -> HeadlineOutcomes:
    return HeadlineOutcomes(
        pl_last_7d=_read_cell(current, _M_PL_7D),
        win_rate_last_7d=_read_cell(current, _M_WIN_RATE_7D),
        current_drawdown=_read_cell(current, _M_DRAWDOWN),
        trades_closed_last_7d=_read_cell(current, _M_TRADES_CLOSED_7D),
    )


# ---------------------------------------------------------------------------
# Section 2 — Process pulse
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PulseMetric:
    """A process-pulse reading: the current-week cell plus its week-over-week delta.

    ``delta`` is ``current.value - prior.value`` when both weeks produced a numeric
    reading; ``None`` when either side is absent / empty (no honest delta).
    """

    current: MetricCell
    prior: MetricCell
    delta: float | None


@dataclass(frozen=True, slots=True)
class ProcessPulse:
    """Six week-over-week process metrics, plus the anti-pattern bar group."""

    pm_rejection_rate: PulseMetric
    pm_modification_rate: PulseMetric
    analyst_inaction_rate: PulseMetric
    strategist_hold_rate: PulseMetric
    guardrail_rejection_count: PulseMetric
    #: Per-canonical-anti-pattern frequency cells, rendered together as a bar group.
    anti_pattern_frequencies: tuple[MetricCell, ...]


def _delta(current: MetricCell, prior: MetricCell) -> float | None:
    if current.value is None or prior.value is None:
        return None
    return current.value - prior.value


def _pulse_metric(
    current: WindowDataset, prior: WindowDataset | None, metric_id: MetricId
) -> PulseMetric:
    current_cell = _read_cell(current, metric_id)
    prior_cell = (
        MetricCell(metric_id=metric_id, result=None)
        if prior is None
        else _read_cell(prior, metric_id)
    )
    return PulseMetric(
        current=current_cell,
        prior=prior_cell,
        delta=_delta(current_cell, prior_cell),
    )


def _process_pulse(current: WindowDataset, prior: WindowDataset | None) -> ProcessPulse:
    return ProcessPulse(
        pm_rejection_rate=_pulse_metric(current, prior, _M_PM_REJECTION_RATE),
        pm_modification_rate=_pulse_metric(current, prior, _M_PM_MODIFICATION_RATE),
        analyst_inaction_rate=_pulse_metric(current, prior, _M_ANALYST_INACTION_RATE),
        strategist_hold_rate=_pulse_metric(current, prior, _M_STRATEGIST_HOLD_RATE),
        guardrail_rejection_count=_pulse_metric(current, prior, _M_GUARDRAIL_REJECTION_COUNT),
        anti_pattern_frequencies=tuple(
            _read_cell(current, _anti_pattern_metric_id(name)) for name in _ANTI_PATTERN_NAMES
        ),
    )


# ---------------------------------------------------------------------------
# Section 3 — Trajectory
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrajectoryPoint:
    """One week's reading on a sparkline — the week label and its cell."""

    week: WeekLabel
    cell: MetricCell


@dataclass(frozen=True, slots=True)
class Sparkline:
    """One trajectory metric over the trailing weeks — its id and per-week points.

    ``points`` is ordered oldest-to-newest (the input sequence order); the renderer
    draws direction from it and shows the newest point's absolute value alongside.
    """

    metric_id: MetricId
    points: tuple[TrajectoryPoint, ...]


@dataclass(frozen=True, slots=True)
class Trajectory:
    """The six trajectory sparklines (§ Section 3)."""

    weekly_pl: Sparkline
    win_rate: Sparkline
    cost_per_resolved_thesis: Sparkline
    conviction_calibration_spread: Sparkline
    status_calibration_spread: Sparkline
    pm_rejection_accuracy: Sparkline


def _sparkline(weeks: Sequence[WeekInput], metric_id: MetricId) -> Sparkline:
    points = tuple(
        TrajectoryPoint(week=label, cell=_read_cell(dataset, metric_id))
        for label, dataset in weeks
    )
    return Sparkline(metric_id=metric_id, points=points)


def _trajectory(weeks: Sequence[WeekInput]) -> Trajectory:
    return Trajectory(
        weekly_pl=_sparkline(weeks, _M_WEEKLY_PL),
        win_rate=_sparkline(weeks, _M_WEEKLY_WIN_RATE),
        cost_per_resolved_thesis=_sparkline(weeks, _M_COST_PER_RESOLVED_THESIS),
        conviction_calibration_spread=_sparkline(weeks, _M_CONVICTION_CALIBRATION),
        status_calibration_spread=_sparkline(weeks, _M_STATUS_CALIBRATION),
        pm_rejection_accuracy=_sparkline(weeks, _M_PM_REJECTION_ACCURACY),
    )


# ---------------------------------------------------------------------------
# Section 4 — Validation status
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationStatusRow:
    """One row per pending validation (§ Section 4).

    The watched-metric current reading is the first watched metric's cell over the
    current week (the row's headline metric); ``None`` when the validation watches
    no metric. Days-of-data and days-remaining are left to the renderer, which has
    the reference 'now'; the data row carries the registration facts the digest
    owns plus the live reading.
    """

    validation_id: str
    edited_artifact: str
    expected_direction: str
    watched_metric_reading: MetricCell | None
    evaluation_due_at: str


def _validation_status(current: WindowDataset) -> tuple[ValidationStatusRow, ...]:
    rows: list[ValidationStatusRow] = []
    for validation in current.validations:
        watched = validation.watched_metric_ids
        reading = _read_cell(current, watched[0]) if watched else None
        rows.append(
            ValidationStatusRow(
                validation_id=str(validation.validation_id),
                edited_artifact=validation.edited_artifact,
                expected_direction=validation.expected_direction.value,
                watched_metric_reading=reading,
                evaluation_due_at=validation.evaluation_due_at.isoformat(),
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Section 6 — Open validation queue (counterfactual replays)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayQueue:
    """Counterfactual-replay buckets for the current week (§ Section 6).

    Reads ``WindowDataset.replays`` — empty until story 06e wires the loader hook —
    and renders zero gracefully. The bucketed breakdowns (by ``replay_status`` /
    ``unevaluable_reason`` / ``confidence``) are populated once the replay record
    shape lands; until then ``total_attempted`` is 0 and the maps are empty.
    """

    total_attempted: int
    by_status: dict[str, int] = field(default_factory=dict)
    by_unevaluable_reason: dict[str, int] = field(default_factory=dict)
    by_confidence: dict[str, int] = field(default_factory=dict)


def _replay_queue(current: WindowDataset) -> ReplayQueue:
    # ``replays`` is a pre-declared empty sub-bundle until 06e; the count is the
    # only field derivable today and renders zero gracefully.
    return ReplayQueue(total_attempted=len(current.replays.replays))


# ---------------------------------------------------------------------------
# WeeklyDigest
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeeklyDigest:
    """The deterministic six-section weekly digest (ALP-888).

    Assembled purely from registry metric results over a trailing multi-week
    sequence of :class:`~alphamind.feedback_loop.dataset.WindowDataset` inputs. The
    current week (the last input) drives Sections 1, 2, 4, 6; the week-over-week
    delta (Section 2) compares it to the prior week; the trajectory (Section 3)
    spans every input week; the notable shifts (Section 5) compare the current week
    to each detector's baseline window.
    """

    week: WeekLabel
    headline: HeadlineOutcomes
    pulse: ProcessPulse
    trajectory: Trajectory
    validation_status: tuple[ValidationStatusRow, ...]
    notable_shifts: tuple[ShiftFinding, ...]
    replay_queue: ReplayQueue


def generate_digest(
    weeks: Sequence[WeekInput],
    shift_config: DigestConfig,
) -> WeeklyDigest:
    """Assemble the :class:`WeeklyDigest` purely from the per-week input sequence.

    *weeks* is ordered oldest-to-newest; the last element is the current week. It
    must hold enough trailing weeks to cover the longest trajectory span (8-12
    weeks) and the largest detector ``baseline_window_weeks``. Each element pairs a
    :data:`WeekLabel` with that week's already-loaded ``WindowDataset`` — the
    generator performs no I/O, so re-running it over the same sequence yields an
    identical digest (the determinism contract).

    Raises :class:`ValueError` on an empty sequence — a digest needs a current week.
    """
    if not weeks:
        msg = "generate_digest requires at least one week (the current week)"
        raise ValueError(msg)

    current_label, current = weeks[-1]
    prior = weeks[-2][1] if len(weeks) >= 2 else None

    return WeeklyDigest(
        week=current_label,
        headline=_headline_outcomes(current),
        pulse=_process_pulse(current, prior),
        trajectory=_trajectory(weeks),
        validation_status=_validation_status(current),
        notable_shifts=detect_shifts(weeks, shift_config),
        replay_queue=_replay_queue(current),
    )


__all__ = [
    "HeadlineOutcomes",
    "MetricCell",
    "ProcessPulse",
    "PulseMetric",
    "ReplayQueue",
    "Sparkline",
    "Trajectory",
    "TrajectoryPoint",
    "ValidationStatusRow",
    "WeekInput",
    "WeekLabel",
    "WeeklyDigest",
    "generate_digest",
]
