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
from dataclasses import replace
from typing import TYPE_CHECKING, get_args

from alphamind.commands.pm_envelope import (
    AdjustmentCategory,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
)
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
    return replace(dataset, pm_decision_log=sliced)


def _invocations_matching(dataset: WindowDataset, conditioning: Conditioning) -> frozenset[str]:
    """Invocation ids whose ``portfolio_manager`` agent call matches the slice value.

    A conditioning dimension with no reachable ``AgentCallRecord`` field (anything
    other than ``MODEL_VERSION`` / ``PROMPT_VERSION``) yields an empty set, so the
    metric degrades to a no-data reading rather than raising ``KeyError`` —
    ``supported_conditioning`` is descriptive metadata the registry does not enforce
    on ``compute`` callers.
    """
    field = _CONDITIONING_AGENT_CALL_FIELD.get(conditioning.dimension)
    if field is None:
        return frozenset()
    return frozenset(
        call.invocation_id
        for call in dataset.agent_calls
        if call.agent_name == "portfolio_manager" and getattr(call, field) == conditioning.value
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
    approved = sum(v in (PMVerdict.APPROVE, PMVerdict.APPROVE_WITH_MODIFICATION) for v in verdicts)
    return _rate_result(_PM_APPROVAL_RATE, approved, len(verdicts))


def _compute_pm_modification_rate(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    verdicts = _verdicts(_conditioned(dataset, conditioning))
    modified = sum(v == PMVerdict.APPROVE_WITH_MODIFICATION for v in verdicts)
    return _rate_result(_PM_MODIFICATION_RATE, modified, len(verdicts))


# ---------------------------------------------------------------------------
# PM per-criterion fail rate (per source agent x criterion)
# ---------------------------------------------------------------------------

#: Canonical criterion keys per source provenance — derived from the field names on the
#: ``ThesisQualityEvaluation`` (analyst) / ``PositionActionEvaluation`` (strategist)
#: models that produce ``evaluation_json``, so they cannot drift from the envelope
#: contract.
_ANALYST_CRITERIA: tuple[str, ...] = tuple(ThesisQualityEvaluation.model_fields)
_STRATEGIST_CRITERIA: tuple[str, ...] = tuple(PositionActionEvaluation.model_fields)


def _criterion_fail_rate_compute(
    metric_id: MetricId, source_provenance: str, criterion: str
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    def _compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
        decisions = [
            d
            for d in _pm_decisions(_conditioned(dataset, conditioning))
            if d.source_provenance_json.get("source_provenance") == source_provenance
        ]
        failed = sum(
            d.evaluation_json.get(criterion, {}).get("status") == "fail" for d in decisions
        )
        return _rate_result(metric_id, failed, len(decisions))

    return _compute


def _criterion_fail_rate_metrics(
    source_provenance: str, criteria: tuple[str, ...]
) -> tuple[Metric, ...]:
    id_prefix = f"pm_{source_provenance.removeprefix('pm_')}_criterion_fail_rate"
    metrics: list[Metric] = []
    for criterion in criteria:
        metric_id = MetricId(f"{id_prefix}__{criterion}")
        metrics.append(
            Metric(
                metric_id=metric_id,
                po_type="process",
                default_window=Window.WEEKLY,
                supported_conditioning=(),
                compute=_criterion_fail_rate_compute(metric_id, source_provenance, criterion),
            )
        )
    return tuple(metrics)


# ---------------------------------------------------------------------------
# PM modification-category distribution (denominator = total modifications)
# ---------------------------------------------------------------------------

#: Canonical adjustment categories — the ``AdjustmentCategory`` Literal members
#: persisted in each ``ModificationRecord.adjustment_category``.
_ADJUSTMENT_CATEGORIES: tuple[str, ...] = get_args(AdjustmentCategory)


def _modification_categories(dataset: WindowDataset) -> tuple[str, ...]:
    return tuple(
        mod.get("adjustment_category", "")
        for d in _pm_decisions(dataset)
        for mod in d.modifications_json
    )


# The conditioning dimensions reachable from the existing WindowDataset bundles: the
# pm_decision_log↔agent_calls join on invocation_id exposes the PM agent call's
# model_id (MODEL_VERSION) and prompt_git_sha (PROMPT_VERSION). REGIME / SECTOR / etc.
# have no field in either bundle, so no metric here declares them.
_PM_SUPPORTED_CONDITIONING: tuple[ConditioningDimension, ...] = (
    ConditioningDimension.MODEL_VERSION,
    ConditioningDimension.PROMPT_VERSION,
)


_PM_METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=_PM_APPROVAL_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=_PM_SUPPORTED_CONDITIONING,
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
    *_criterion_fail_rate_metrics("pm_analyst", _ANALYST_CRITERIA),
    *_criterion_fail_rate_metrics("pm_strategist", _STRATEGIST_CRITERIA),
    *_distribution_metrics(
        id_prefix="pm_modification_category_rate",
        bins=tuple((c, c) for c in _ADJUSTMENT_CATEGORIES),
        population=_modification_categories,
    ),
)


# ---------------------------------------------------------------------------
# Analyst metrics
# ---------------------------------------------------------------------------

#: The analyst conviction scale (``Recommendation.conviction_level``, ge=1 le=5).
_CONVICTION_LEVELS: tuple[int, ...] = (1, 2, 3, 4, 5)


def _analyst_convictions(dataset: WindowDataset) -> tuple[int, ...]:
    """Conviction levels carried by ``pm_analyst`` envelopes' originating proposals.

    Strategist envelopes carry no conviction and are excluded; a proposal missing the
    field contributes no observation (kept out of the denominator).
    """
    convictions: list[int] = []
    for d in _pm_decisions(dataset):
        if d.source_provenance_json.get("source_provenance") != "pm_analyst":
            continue
        level = d.originating_proposal_json.get("conviction_level")
        if isinstance(level, int):
            convictions.append(level)
    return tuple(convictions)


_ANALYST_METRICS: tuple[Metric, ...] = _distribution_metrics(
    id_prefix="analyst_conviction_rate",
    bins=tuple((str(level), level) for level in _CONVICTION_LEVELS),
    population=_analyst_convictions,
)


# ---------------------------------------------------------------------------
# Strategist metrics
# ---------------------------------------------------------------------------

# Strategist wire vocabularies are lowercase-hyphenated (``on-track``,
# ``adjust-bracket``); the metric-id suffix is the snake_case form. Hard-coded here
# rather than imported from ``decision.strategist.models`` because that module pulls in
# sqlalchemy transitively, which the metric-core purity contract forbids.
_STATUS_BINS: tuple[tuple[str, str], ...] = (
    ("on_track", "on-track"),
    ("partially_realized", "partially-realized"),
    ("at_risk", "at-risk"),
    ("stale", "stale"),
    ("invalidated", "invalidated"),
)
_ACTION_BINS: tuple[tuple[str, str], ...] = (
    ("hold", "hold"),
    ("reduce", "reduce"),
    ("close", "close"),
    ("adjust_bracket", "adjust-bracket"),
    ("add", "add"),
)

#: Strategist statuses that are not ``on-track`` — the denominator for the
#: hold-on-non-on-track rate.
_NON_ON_TRACK_STATUSES: frozenset[str] = frozenset({"at-risk", "stale"})


def _strategist_assessments(dataset: WindowDataset) -> tuple[dict[str, object], ...]:
    """Originating-proposal bodies of ``pm_strategist`` envelopes in the window."""
    return tuple(
        d.originating_proposal_json
        for d in _pm_decisions(dataset)
        if d.source_provenance_json.get("source_provenance") == "pm_strategist"
    )


def _strategist_field(dataset: WindowDataset, field: str) -> tuple[str, ...]:
    return tuple(
        str(value)
        for assessment in _strategist_assessments(dataset)
        if isinstance((value := assessment.get(field)), str)
    )


def _strategist_statuses(dataset: WindowDataset) -> tuple[str, ...]:
    return _strategist_field(dataset, "thesis_status")


def _strategist_actions(dataset: WindowDataset) -> tuple[str, ...]:
    return _strategist_field(dataset, "recommended_action")


_STRATEGIST_HOLD_ON_NON_ON_TRACK_RATE = MetricId("strategist_hold_on_non_on_track_rate")


def _strategist_transitions(dataset: WindowDataset) -> tuple[tuple[str, str], ...]:
    """``(prior_status, thesis_status)`` pairs over strategist assessments.

    Only assessments carrying a (string) ``prior_status`` contribute — a first-ever
    classification (``prior_status`` ``None``) is not a transition.
    """
    transitions: list[tuple[str, str]] = []
    for assessment in _strategist_assessments(dataset):
        prior = assessment.get("prior_status")
        current = assessment.get("thesis_status")
        if isinstance(prior, str) and isinstance(current, str):
            transitions.append((prior, current))
    return tuple(transitions)


_TRANSITION_BINS: tuple[tuple[str, tuple[str, str]], ...] = tuple(
    (f"{from_suffix}__to__{to_suffix}", (from_value, to_value))
    for from_suffix, from_value in _STATUS_BINS
    for to_suffix, to_value in _STATUS_BINS
)


def _compute_hold_on_non_on_track_rate(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    conditioned = _conditioned(dataset, conditioning)
    non_on_track = [
        assessment
        for assessment in _strategist_assessments(conditioned)
        if assessment.get("thesis_status") in _NON_ON_TRACK_STATUSES
    ]
    held = sum(a.get("recommended_action") == "hold" for a in non_on_track)
    return _rate_result(_STRATEGIST_HOLD_ON_NON_ON_TRACK_RATE, held, len(non_on_track))


_STRATEGIST_METRICS: tuple[Metric, ...] = (
    *_distribution_metrics(
        id_prefix="strategist_status_rate",
        bins=_STATUS_BINS,
        population=_strategist_statuses,
    ),
    *_distribution_metrics(
        id_prefix="strategist_action_rate",
        bins=_ACTION_BINS,
        population=_strategist_actions,
    ),
    *_distribution_metrics(
        id_prefix="strategist_status_transition_rate",
        bins=_TRANSITION_BINS,
        population=_strategist_transitions,
    ),
    Metric(
        metric_id=_STRATEGIST_HOLD_ON_NON_ON_TRACK_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_hold_on_non_on_track_rate,
    ),
)


METRICS: tuple[Metric, ...] = _PM_METRICS + _ANALYST_METRICS + _STRATEGIST_METRICS
