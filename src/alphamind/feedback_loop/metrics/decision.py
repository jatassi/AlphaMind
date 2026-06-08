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
    AntiPattern,
    PositionActionEvaluation,
    SourceProvenance,
    ThesisQualityEvaluation,
)
from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    ConditioningDimension,
    Metric,
    MetricId,
    MetricResult,
    Window,
    rate_result,
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
    value; a ``REGIME`` slice keeps decisions whose invocation held that
    ``invocations.active_regime`` (resolved through the loader's regime map). The join
    key is ``invocation_id``, present on every bundle.
    """
    matching = _conditioning_invocation_filter(dataset, conditioning)
    if matching is None:
        return dataset
    sliced = tuple(entry for entry in dataset.pm_decision_log if entry.invocation_id in matching)
    return replace(dataset, pm_decision_log=sliced)


def _conditioning_invocation_filter(
    dataset: WindowDataset, conditioning: Conditioning
) -> frozenset[str] | None:
    """The invocation-id set a slice narrows to, or ``None`` when unconditioned.

    ``None`` (the unconditioned case) means "apply no narrowing". A conditioned slice
    on an unreachable dimension yields the *empty* set, so the metric degrades to a
    no-data reading rather than raising — ``supported_conditioning`` is descriptive
    metadata the registry does not enforce on ``compute`` callers.
    """
    if conditioning.dimension is None:
        return None
    if conditioning.dimension is ConditioningDimension.REGIME:
        return _regime_invocation_filter(dataset, conditioning.value)
    field = _CONDITIONING_AGENT_CALL_FIELD.get(conditioning.dimension)
    if field is None:
        return frozenset()
    return frozenset(
        call.invocation_id
        for call in dataset.agent_calls
        if call.agent_name == "portfolio_manager" and getattr(call, field) == conditioning.value
    )


def _regime_invocation_filter(dataset: WindowDataset, regime: str | None) -> frozenset[str]:
    """The invocation ids whose held ``active_regime`` is *regime* (loader's regime map).

    The single REGIME-narrowing comprehension, shared by the PM-decision slice and the
    analyst-observation slice so the two paths cannot drift apart.
    """
    return frozenset(
        invocation_id
        for invocation_id, held in dataset.regimes.by_invocation.items()
        if held == regime
    )


#: The ``AgentCallRecord`` field each agent-call-sourced conditioning dimension
#: filters on. ``REGIME`` resolves through the loader's regime map instead (see
#: :func:`_conditioning_invocation_filter`).
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
        return rate_result(metric_id, sum(v == member for v in values), len(values))

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
    return rate_result(_PM_APPROVAL_RATE, approved, len(verdicts))


def _compute_pm_modification_rate(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    verdicts = _verdicts(_conditioned(dataset, conditioning))
    modified = sum(v == PMVerdict.APPROVE_WITH_MODIFICATION for v in verdicts)
    return rate_result(_PM_MODIFICATION_RATE, modified, len(verdicts))


# ---------------------------------------------------------------------------
# PM per-criterion fail rate (per source agent x criterion)
# ---------------------------------------------------------------------------

#: The two source-provenance variants, named off the ``SourceProvenance`` Literal so the
#: filters and metric registration below carry no bare ``"pm_analyst"`` / ``"pm_strategist"``
#: strings. ``get_args(SourceProvenance)`` is the authoritative member set; if a variant is
#: added to the Literal, the completeness assert on ``_CRITERIA_BY_PROVENANCE`` fails until
#: it is wired here rather than being silently dropped from the per-criterion denominators.
_PM_ANALYST: SourceProvenance = "pm_analyst"
_PM_STRATEGIST: SourceProvenance = "pm_strategist"

#: Canonical criterion keys per source provenance — derived from the field names on the
#: ``ThesisQualityEvaluation`` (analyst) / ``PositionActionEvaluation`` (strategist)
#: models that produce ``evaluation_json``, so they cannot drift from the envelope
#: contract.
_CRITERIA_BY_PROVENANCE: dict[SourceProvenance, tuple[str, ...]] = {
    _PM_ANALYST: tuple(ThesisQualityEvaluation.model_fields),
    _PM_STRATEGIST: tuple(PositionActionEvaluation.model_fields),
}

# Completeness guard: every ``SourceProvenance`` variant must register a criteria set, so
# a variant added to the Literal cannot be silently dropped from the fail-rate metrics.
assert set(_CRITERIA_BY_PROVENANCE) == set(get_args(SourceProvenance))


def _criterion_fail_rate_compute(
    metric_id: MetricId, source_provenance: SourceProvenance, criterion: str
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
        return rate_result(metric_id, failed, len(decisions))

    return _compute


def _criterion_fail_rate_metrics(
    source_provenance: SourceProvenance, criteria: tuple[str, ...]
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


# ---------------------------------------------------------------------------
# Anti-pattern frequency (per-pattern rate over the window's PM decisions)
# ---------------------------------------------------------------------------

#: The five canonical anti-pattern strings (``commands.pm_envelope.AntiPattern``),
#: sourced via ``get_args`` so the metric ids cannot drift from the envelope contract.
#: A pattern added to the Literal mints its frequency metric here automatically — and
#: the digest's ``_anti_pattern_metric_id`` reads the same names.
_ANTI_PATTERN_NAMES: tuple[str, ...] = get_args(AntiPattern)

#: The metric-id prefix the digest's ``_anti_pattern_metric_id`` joins on.
_ANTI_PATTERN_ID_PREFIX = "anti_pattern_frequency"


def _anti_pattern_frequency_compute(
    metric_id: MetricId, pattern: str
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    def _compute(dataset: WindowDataset, conditioning: Conditioning) -> MetricResult:
        decisions = _pm_decisions(_conditioned(dataset, conditioning))
        tagged = sum(pattern in d.anti_patterns_json for d in decisions)
        return rate_result(metric_id, tagged, len(decisions))

    return _compute


# The conditioning dimensions reachable from the WindowDataset bundles: the
# pm_decision_log↔agent_calls join on invocation_id exposes the PM agent call's
# model_id (MODEL_VERSION) and prompt_git_sha (PROMPT_VERSION), and the
# pm_decision_log↔regimes join (ALP-911) resolves each decision's invocation
# active_regime (REGIME). SECTOR / CONVICTION / etc. still have no reachable field.
_PM_SUPPORTED_CONDITIONING: tuple[ConditioningDimension, ...] = (
    ConditioningDimension.MODEL_VERSION,
    ConditioningDimension.PROMPT_VERSION,
    ConditioningDimension.REGIME,
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
        supported_conditioning=_PM_SUPPORTED_CONDITIONING,
        compute=_compute_pm_modification_rate,
    ),
    *_distribution_metrics(
        id_prefix="pm_verdict_rate",
        bins=_VERDICT_BINS,
        population=_verdicts,
        supported_conditioning=_PM_SUPPORTED_CONDITIONING,
    ),
    *(
        metric
        for provenance, criteria in _CRITERIA_BY_PROVENANCE.items()
        for metric in _criterion_fail_rate_metrics(provenance, criteria)
    ),
    *_distribution_metrics(
        id_prefix="pm_modification_category_rate",
        bins=tuple((c, c) for c in _ADJUSTMENT_CATEGORIES),
        population=_modification_categories,
    ),
)


def _anti_pattern_metrics() -> tuple[Metric, ...]:
    """One per-pattern frequency ``Metric`` per canonical ``AntiPattern`` string.

    Each id is ``anti_pattern_frequency__<name>`` — the exact id the digest's
    ``_anti_pattern_metric_id`` reads. Unlike a distribution, the patterns do not
    partition (a decision may carry several), so each is its own rate over the same
    PM-decision denominator rather than a ``_distribution_metrics`` bin group.
    """
    metrics: list[Metric] = []
    for name in _ANTI_PATTERN_NAMES:
        metric_id = MetricId(f"{_ANTI_PATTERN_ID_PREFIX}__{name}")
        metrics.append(
            Metric(
                metric_id=metric_id,
                po_type="process",
                default_window=Window.WEEKLY,
                supported_conditioning=_PM_SUPPORTED_CONDITIONING,
                compute=_anti_pattern_frequency_compute(metric_id, name),
            )
        )
    return tuple(metrics)


_ANTI_PATTERN_METRICS: tuple[Metric, ...] = _anti_pattern_metrics()


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
        if d.source_provenance_json.get("source_provenance") != _PM_ANALYST:
            continue
        level = d.originating_proposal_json.get("conviction_level")
        if isinstance(level, int):
            convictions.append(level)
    return tuple(convictions)


_ANALYST_CONVICTION_METRICS: tuple[Metric, ...] = _distribution_metrics(
    id_prefix="analyst_conviction_rate",
    bins=tuple((str(level), level) for level in _CONVICTION_LEVELS),
    population=_analyst_convictions,
)


# ---------------------------------------------------------------------------
# Analyst proposal-volume metrics (story 06f — over the analyst-proposals bundle)
# ---------------------------------------------------------------------------

_ANALYST_INACTION_RATE = MetricId("analyst_inaction_rate")
_ANALYST_PROPOSALS_PER_INVOCATION = MetricId("analyst_proposals_per_invocation")

#: The analyst proposal-volume metrics condition on ``REGIME`` — the
#: per-invocation ``active_regime`` resolves an observation's held regime through
#: the loader's regime map (the analyst run's model/prompt are not the PM-call
#: fields the agent-call join exposes, so only the regime slice is reachable here).
_ANALYST_SUPPORTED_CONDITIONING: tuple[ConditioningDimension, ...] = (ConditioningDimension.REGIME,)


def _analyst_proposal_counts(dataset: WindowDataset, conditioning: Conditioning) -> tuple[int, ...]:
    """The (conditioned) per-invocation analyst proposal counts.

    One entry per analyst invocation in the window; ``0`` for a watchlist run or a
    normal run that emitted no recommendations (both inactions). Only ``REGIME`` is a
    reachable slice here (``_ANALYST_SUPPORTED_CONDITIONING``): a ``REGIME`` slice
    narrows to the observations whose invocation held that regime (the loader's regime
    map); any other conditioned dimension yields no observations, so the metric degrades
    to a no-data reading rather than borrowing the ``pm_decision_log`` slice's PM
    agent-call provenance — which is not the analyst run's provenance.
    """
    observations = dataset.analyst_proposals.observations
    if conditioning.dimension is None:
        return tuple(o.proposal_count for o in observations)
    if conditioning.dimension is not ConditioningDimension.REGIME:
        return ()
    matching = _regime_invocation_filter(dataset, conditioning.value)
    return tuple(o.proposal_count for o in observations if o.invocation_id in matching)


def _compute_analyst_inaction_rate(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    counts = _analyst_proposal_counts(dataset, conditioning)
    inactions = sum(c == 0 for c in counts)
    return rate_result(_ANALYST_INACTION_RATE, inactions, len(counts))


def _compute_analyst_proposals_per_invocation(
    dataset: WindowDataset, conditioning: Conditioning
) -> MetricResult:
    counts = _analyst_proposal_counts(dataset, conditioning)
    # rate_result computes numerator / denominator as a float (types.py), so the
    # per-invocation mean is sum(counts) / len(counts); a zero denominator yields the
    # shared no-data shape.
    return rate_result(_ANALYST_PROPOSALS_PER_INVOCATION, sum(counts), len(counts))


_ANALYST_PROPOSAL_METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=_ANALYST_INACTION_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=_ANALYST_SUPPORTED_CONDITIONING,
        compute=_compute_analyst_inaction_rate,
    ),
    Metric(
        metric_id=_ANALYST_PROPOSALS_PER_INVOCATION,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=_ANALYST_SUPPORTED_CONDITIONING,
        compute=_compute_analyst_proposals_per_invocation,
    ),
)

_ANALYST_METRICS: tuple[Metric, ...] = _ANALYST_CONVICTION_METRICS + _ANALYST_PROPOSAL_METRICS


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
        if d.source_provenance_json.get("source_provenance") == _PM_STRATEGIST
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
    return rate_result(_STRATEGIST_HOLD_ON_NON_ON_TRACK_RATE, held, len(non_on_track))


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


METRICS: tuple[Metric, ...] = (
    _PM_METRICS + _ANTI_PATTERN_METRICS + _ANALYST_METRICS + _STRATEGIST_METRICS
)
