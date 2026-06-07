"""Decision-layer process metrics (ALP-883 story 06a).

Pure ``Metric`` cores over the ``WindowDataset`` ``pm_decision_log`` bundle — the
high-frequency, low-noise *process* metrics the weekly digest's process pulse, the
validation skill's watched-metrics, and the retrospective read. Computed from the PM
decision envelopes persisted as :class:`PMDecisionDetail` activity-log entries, plus the
analyst / strategist proposal bodies carried in ``originating_proposal_json``.

Functional core / imperative shell (P1): every ``compute`` here is a pure function of an
already-loaded ``WindowDataset`` and a ``Conditioning`` slice — no I/O, no session, no
sqlalchemy import (enforced by the ``feedback-loop-metric-cores-no-sqlalchemy``
``.importlinter`` contract; ``WindowDataset`` is imported under ``TYPE_CHECKING`` only so
the loader's transitive DB imports never reach this module statically).

Each metric is a scalar rate/count addressable by a stable snake_case ``MetricId`` — a
distribution (verdict, conviction histogram, status, action, transition matrix) is
expressed as one ``MetricId`` per bin so a single bin flows losslessly into
``validations.watched_metric_ids`` and the digest's per-bin notable-shift detectors.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    ConditioningDimension,
    Metric,
    MetricId,
    MetricResult,
    Window,
)
from alphamind.portfolio_state.events.types import EventType, PMVerdict

if TYPE_CHECKING:
    from alphamind.feedback_loop.dataset import WindowDataset
    from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
    from alphamind.portfolio_state.events.types import ActivityLogEntry


# ---------------------------------------------------------------------------
# Conditioning — restrict pm_decision_log to a slice
# ---------------------------------------------------------------------------


def _conditioned(dataset: WindowDataset, conditioning: Conditioning) -> WindowDataset:
    """Return *dataset* with ``pm_decision_log`` narrowed to the conditioning slice.

    The unconditioned slice (``dimension is None``) returns the dataset unchanged.
    A ``MODEL_VERSION`` / ``PROMPT_VERSION`` slice keeps only PM decisions whose owning
    invocation produced a ``portfolio_manager`` agent call matching the held-fixed
    value — the join key is ``invocation_id``, present on both bundles. Dimensions with
    no reachable field in the existing bundles (e.g. ``REGIME``) are not declared as
    ``supported_conditioning`` by any metric here, so they never reach this filter.
    """
    if conditioning.dimension is None:
        return dataset
    invocation_ids = _invocations_matching(dataset, conditioning)
    sliced = tuple(
        entry for entry in dataset.pm_decision_log if entry.invocation_id in invocation_ids
    )
    return _replace_pm_decision_log(dataset, sliced)


def _invocations_matching(dataset: WindowDataset, conditioning: Conditioning) -> frozenset[str]:
    """Invocation ids whose ``portfolio_manager`` agent call matches the slice value."""
    field = _CONDITIONING_AGENT_CALL_FIELD[conditioning.dimension]
    return frozenset(
        call.invocation_id
        for call in dataset.agent_calls
        if call.agent_name == "portfolio_manager"
        and getattr(call, field) == conditioning.value
    )


def _replace_pm_decision_log(
    dataset: WindowDataset, pm_decision_log: tuple[ActivityLogEntry, ...]
) -> WindowDataset:
    return WindowDataset(
        start=dataset.start,
        end=dataset.end,
        agent_calls=dataset.agent_calls,
        pm_decision_log=pm_decision_log,
        validations=dataset.validations,
        refs=dataset.refs,
        replays=dataset.replays,
    )


#: The ``AgentCallRecord`` field each supported conditioning dimension filters on.
_CONDITIONING_AGENT_CALL_FIELD: dict[ConditioningDimension | None, str] = {
    ConditioningDimension.MODEL_VERSION: "model_id",
    ConditioningDimension.PROMPT_VERSION: "prompt_git_sha",
}


# ---------------------------------------------------------------------------
# PM-decision extraction over the pm_decision_log bundle
# ---------------------------------------------------------------------------


def _pm_decisions(dataset: WindowDataset) -> tuple[PMDecisionDetail, ...]:
    """The ``PMDecisionDetail`` payloads in the window's ``pm_decision_log``.

    The bundle is a sliding window of activity-log entries; only ``PM_DECISION``
    entries carry a ``PMDecisionDetail`` (the bundle's loader already filters to that
    event type, but the guard keeps the core total over any entry shape).
    """
    return tuple(
        entry.detail
        for entry in dataset.pm_decision_log
        if entry.event_type == EventType.PM_DECISION
    )


# ---------------------------------------------------------------------------
# Rate metric
# ---------------------------------------------------------------------------


def _rate_result(metric_id: MetricId, numerator: int, denominator: int) -> MetricResult:
    """A process-tier fraction reading: ``numerator / denominator``.

    Empty denominator yields ``value=None`` (the design's "no honest reading" signal)
    rather than a divide-by-zero. ``insufficient_sample`` is left ``False`` here — the
    operator-tunable sample-size gate is applied by the digest, not the metric core.
    """
    value = None if denominator == 0 else numerator / denominator
    return MetricResult(
        metric_id=metric_id,
        value=value,
        posterior_band=None,
        sample_size=denominator,
        insufficient_sample=False,
    )


# ---------------------------------------------------------------------------
# Distribution factory — one MetricId per bin
# ---------------------------------------------------------------------------


def _distribution_metrics[T](
    *,
    id_prefix: str,
    bins: tuple[tuple[str, T], ...],
    population: Callable[[WindowDataset], tuple[T, ...]],
    supported_conditioning: tuple[ConditioningDimension, ...] = (),
) -> tuple[Metric, ...]:
    """Mint one rate ``Metric`` per ``bins`` member: ``count(member) / len(population)``.

    ``bins`` pairs a snake_case id suffix with the value that suffix counts; the metric
    id is ``{id_prefix}__{suffix}``. ``population`` extracts the (conditioned) sequence
    the rate is taken over, so every bin shares one denominator — the bins of a single
    distribution sum to 1.0 on a non-empty population.
    """
    metrics: list[Metric] = []
    for suffix, member in bins:
        metric_id = MetricId(f"{id_prefix}__{suffix}")
        metrics.append(
            Metric(
                metric_id=metric_id,
                po_type="process",
                default_window=Window.WEEKLY,
                supported_conditioning=supported_conditioning,
                compute=_bin_rate_compute(metric_id, member, population),
            )
        )
    return tuple(metrics)


def _bin_rate_compute[T](
    metric_id: MetricId,
    member: T,
    population: Callable[[WindowDataset], tuple[T, ...]],
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    def _compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
        values = population(_conditioned(dataset, conditioning))
        return _rate_result(metric_id, sum(v == member for v in values), len(values))

    return _compute


# ---------------------------------------------------------------------------
# PM verdict metrics
# ---------------------------------------------------------------------------

_PM_APPROVAL_RATE = MetricId("pm_approval_rate")
_PM_MODIFICATION_RATE = MetricId("pm_modification_rate")

_VERDICT_BINS: tuple[tuple[str, PMVerdict], ...] = (
    ("approve", PMVerdict.APPROVE),
    ("approve_with_modification", PMVerdict.APPROVE_WITH_MODIFICATION),
    ("reject", PMVerdict.REJECT),
    ("override_with_corrective_action", PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION),
)


def _verdicts(dataset: WindowDataset) -> tuple[PMVerdict, ...]:
    return tuple(d.verdict for d in _pm_decisions(dataset))


def _compute_pm_approval_rate(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
    verdicts = _verdicts(_conditioned(dataset, conditioning))
    approved = sum(
        v in (PMVerdict.APPROVE, PMVerdict.APPROVE_WITH_MODIFICATION) for v in verdicts
    )
    return _rate_result(_PM_APPROVAL_RATE, approved, len(verdicts))


def _compute_pm_modification_rate(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    verdicts = _verdicts(_conditioned(dataset, conditioning))
    modified = sum(v == PMVerdict.APPROVE_WITH_MODIFICATION for v in verdicts)
    return _rate_result(_PM_MODIFICATION_RATE, modified, len(verdicts))


_PM_METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=_PM_APPROVAL_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_pm_approval_rate,
    ),
    Metric(
        metric_id=_PM_MODIFICATION_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_pm_modification_rate,
    ),
    *_distribution_metrics(
        id_prefix="pm_verdict_rate",
        bins=_VERDICT_BINS,
        population=_verdicts,
    ),
)


METRICS: tuple[Metric, ...] = _PM_METRICS
