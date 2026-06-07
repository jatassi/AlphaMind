"""Cost + key execution-process metrics (ALP-884 story 06b).

Two metric families, both pure ``Metric`` cores over the ``WindowDataset`` (functional
core / imperative shell, P1 — no I/O, no session, no sqlalchemy import, enforced by the
``feedback-loop-metric-cores-no-sqlalchemy`` ``.importlinter`` contract):

* **Cost metrics** over the ``agent_calls`` bundle — token cost per invocation / per
  agent, cache hit rate per agent, latency-budget headroom per agent (against the
  ``agents.yaml`` envelope stamped onto the dataset by the loader), failure overhead
  (retry tokens / total), and cost per resolved thesis (the ``outcomes`` bundle's
  resolved-thesis count as denominator).
* **Execution-process metrics** over the ``activity_events`` bundle — guardrail-rejection
  count, command-abandonment rate, engine-originated-CLOSE frequency, each counting the
  corresponding ``EventType`` over the window.

Per the parent decision (F) the execution *outcome* metrics (slippage / fill / bracket
accuracy) and the data/distillation process metrics are out of scope; only metrics with
an in-tree digest / retrospective consumer ship here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from alphamind.config.models.agents import AgentName
from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    Metric,
    MetricId,
    MetricResult,
    Window,
)
from alphamind.portfolio_state.events.pm_decision import PMDecisionDetail
from alphamind.portfolio_state.events.types import EventSource, EventType

if TYPE_CHECKING:
    from alphamind.feedback_loop.dataset import WindowDataset
    from alphamind.portfolio_state.events.types import ActivityLogEntry
    from alphamind.state.tables.agent_calls import AgentCallRecord

#: The closed roster of pipeline agents the per-agent cost metrics fan out over —
#: the same ``agents.yaml`` slot roster the latency budgets are keyed by.
_AGENT_NAMES: tuple[str, ...] = tuple(name.value for name in AgentName)


# ---------------------------------------------------------------------------
# Token-cost helpers
# ---------------------------------------------------------------------------


def _billable_tokens(call: AgentCallRecord) -> int:
    """Billable tokens for one call: ``input + output`` (cache reads bill separately).

    The cost surface measures generative spend — the ``input_tokens`` the model is
    charged for plus the ``output_tokens`` it produced. Cache-read tokens are tracked
    by the cache-hit-rate metric, not added here (they are billed at a discount and
    would double-count the cached prefix against the cache metric).
    """
    return call.input_tokens + call.output_tokens


def _count_result(metric_id: MetricId, count: int) -> MetricResult:
    """A process-tier count reading — the integer ``count`` as both value and sample size.

    A count metric's reading *is* the count (e.g. guardrail rejections this week), so the
    number of underlying observations equals the count itself. ``insufficient_sample`` is
    left ``False``; the operator-tunable sample gate is the digest's responsibility, not
    the core's.
    """
    return MetricResult(
        metric_id=metric_id,
        value=float(count),
        posterior_band=None,
        sample_size=count,
        insufficient_sample=False,
    )


def _ratio_result(metric_id: MetricId, numerator: float, denominator: int) -> MetricResult:
    """A process-tier fraction reading: ``numerator / denominator``.

    Empty denominator yields ``value=None`` (the design's "no honest reading" signal)
    rather than a divide-by-zero; ``sample_size`` carries the denominator.
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
# Token cost per invocation
# ---------------------------------------------------------------------------

_COST_TOKEN_PER_INVOCATION = MetricId("cost_token_per_invocation")


def _compute_cost_token_per_invocation(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    calls = dataset.agent_calls
    invocations = {call.invocation_id for call in calls}
    total = sum(_billable_tokens(call) for call in calls)
    return _ratio_result(_COST_TOKEN_PER_INVOCATION, total, len(invocations))


# ---------------------------------------------------------------------------
# Token cost per agent (one MetricId per agent slot)
# ---------------------------------------------------------------------------


def _agent_calls(dataset: WindowDataset, agent_name: str) -> tuple[AgentCallRecord, ...]:
    return tuple(call for call in dataset.agent_calls if call.agent_name == agent_name)


def _cost_per_agent_compute(
    metric_id: MetricId, agent_name: str
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    def _compute(dataset: WindowDataset, _conditioning: Conditioning) -> MetricResult:
        calls = _agent_calls(dataset, agent_name)
        if not calls:
            return _ratio_result(metric_id, 0, 0)
        total = sum(_billable_tokens(call) for call in calls)
        return MetricResult(
            metric_id=metric_id,
            value=float(total),
            posterior_band=None,
            sample_size=len(calls),
            insufficient_sample=False,
        )

    return _compute


def _per_agent_metrics(
    id_prefix: str,
    compute_factory: Callable[
        [MetricId, str], Callable[[WindowDataset, Conditioning], MetricResult]
    ],
) -> tuple[Metric, ...]:
    """Mint one ``Metric`` per agent slot: ``{id_prefix}__{agent_name}``."""
    metrics: list[Metric] = []
    for agent_name in _AGENT_NAMES:
        metric_id = MetricId(f"{id_prefix}__{agent_name}")
        metrics.append(
            Metric(
                metric_id=metric_id,
                po_type="process",
                default_window=Window.WEEKLY,
                supported_conditioning=(),
                compute=compute_factory(metric_id, agent_name),
            )
        )
    return tuple(metrics)


_COST_PER_AGENT_METRICS: tuple[Metric, ...] = _per_agent_metrics(
    "cost_token_per_agent", _cost_per_agent_compute
)


# ---------------------------------------------------------------------------
# Cache hit rate per agent — cache_read / (cache_read + input)
# ---------------------------------------------------------------------------


def _cache_hit_rate_compute(
    metric_id: MetricId, agent_name: str
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    def _compute(dataset: WindowDataset, _conditioning: Conditioning) -> MetricResult:
        calls = _agent_calls(dataset, agent_name)
        cache_read = sum(call.cache_read_tokens for call in calls)
        input_tokens = sum(call.input_tokens for call in calls)
        return _ratio_result(metric_id, cache_read, cache_read + input_tokens)

    return _compute


_CACHE_HIT_RATE_METRICS: tuple[Metric, ...] = _per_agent_metrics(
    "cache_hit_rate", _cache_hit_rate_compute
)


# ---------------------------------------------------------------------------
# Failure overhead — retry tokens (attempt_number > 1) / total tokens
# ---------------------------------------------------------------------------

_FAILURE_OVERHEAD = MetricId("failure_overhead")


def _compute_failure_overhead(dataset: WindowDataset, _conditioning: Conditioning) -> MetricResult:
    total = sum(_billable_tokens(call) for call in dataset.agent_calls)
    retry = sum(_billable_tokens(call) for call in dataset.agent_calls if call.attempt_number > 1)
    return _ratio_result(_FAILURE_OVERHEAD, retry, total)


# ---------------------------------------------------------------------------
# Latency budget headroom per agent: (budget - mean wall-clock) / budget
# ---------------------------------------------------------------------------

#: Milliseconds per second — wall-clock telemetry is recorded in ms, the agents.yaml
#: budget in seconds. Definitional unit conversion, not a tunable.
_MS_PER_SECOND = 1000.0


def _latency_headroom_compute(
    metric_id: MetricId, agent_name: str
) -> Callable[[WindowDataset, Conditioning], MetricResult]:
    def _compute(dataset: WindowDataset, _conditioning: Conditioning) -> MetricResult:
        calls = _agent_calls(dataset, agent_name)
        if not calls:
            return _ratio_result(metric_id, 0, 0)
        budget_seconds = dataset.budgets.agent_latency_budget_seconds[agent_name]
        mean_wall_seconds = sum(call.wall_clock_ms for call in calls) / len(calls) / _MS_PER_SECOND
        headroom = (budget_seconds - mean_wall_seconds) / budget_seconds
        return MetricResult(
            metric_id=metric_id,
            value=headroom,
            posterior_band=None,
            sample_size=len(calls),
            insufficient_sample=False,
        )

    return _compute


_LATENCY_HEADROOM_METRICS: tuple[Metric, ...] = _per_agent_metrics(
    "latency_budget_headroom", _latency_headroom_compute
)


# ---------------------------------------------------------------------------
# Cost per resolved thesis — total window token cost / resolved-thesis count
# ---------------------------------------------------------------------------

_COST_PER_RESOLVED_THESIS = MetricId("cost_per_resolved_thesis")


def _compute_cost_per_resolved_thesis(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    total = sum(_billable_tokens(call) for call in dataset.agent_calls)
    resolved = len(dataset.outcomes.theses)
    return _ratio_result(_COST_PER_RESOLVED_THESIS, total, resolved)


# ---------------------------------------------------------------------------
# Execution-process metrics over the activity_events bundle
# ---------------------------------------------------------------------------


def _events_of_type(dataset: WindowDataset, event_type: EventType) -> tuple[ActivityLogEntry, ...]:
    return tuple(
        entry for entry in dataset.activity_events.entries if entry.event_type == event_type
    )


# --- Guardrail rejection count ---------------------------------------------

_GUARDRAIL_REJECTION_COUNT = MetricId("guardrail_rejection_count")


def _compute_guardrail_rejection_count(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    rejections = _events_of_type(dataset, EventType.GUARDRAIL_REJECTION)
    return _count_result(_GUARDRAIL_REJECTION_COUNT, len(rejections))


# --- Command abandonment rate ----------------------------------------------

_COMMAND_ABANDONMENT_RATE = MetricId("command_abandonment_rate")


def _resulting_command_count(dataset: WindowDataset) -> int:
    """Total OMS commands that successfully resulted from PM decisions in the window.

    The denominator's "succeeded" leg — every ``resulting_command_ids`` entry across the
    window's ``PM_DECISION`` envelopes (engine-originated cascades included; they are
    real commands that did not abandon).
    """
    return sum(
        len(entry.detail.resulting_command_ids)
        for entry in _events_of_type(dataset, EventType.PM_DECISION)
        if isinstance(entry.detail, PMDecisionDetail)
    )


def _compute_command_abandonment_rate(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    abandoned = len(_events_of_type(dataset, EventType.COMMAND_ABANDONED))
    total_attempted = _resulting_command_count(dataset) + abandoned
    return _ratio_result(_COMMAND_ABANDONMENT_RATE, abandoned, total_attempted)


# --- Engine-originated CLOSE frequency -------------------------------------

_ENGINE_ORIGINATED_CLOSE_FREQUENCY = MetricId("engine_originated_close_frequency")

#: Activity-log sources that issue a ``POSITION_CLOSED`` *directly* without a PM in the
#: loop — the continuous monitor's bracket-stop / margin / guardrail firings. Mirrors
#: ``portfolio_state.consumers.strategist._MONITOR_DIRECT_SOURCES``. (Hard-coded against
#: the ``EventSource`` enum, not imported from the consumer, to keep this metric core off
#: the execution/consumer import path.)
_MONITOR_DIRECT_SOURCES: frozenset[EventSource] = frozenset(
    {EventSource.BRACKET_MANAGER, EventSource.MARGIN_MONITOR, EventSource.GUARDRAIL_LAYER}
)


def _engine_guardrail_command_ids(dataset: WindowDataset) -> frozenset[str]:
    """OMS command ids that cascaded from an ``engine_guardrail``-provenance PM envelope.

    The continuous monitor's protective CLOSE flows through the engine-envelope path; its
    ``PM_DECISION`` row records ``source_provenance=engine_guardrail`` and the resulting
    command ids. The eventual ``POSITION_CLOSED`` carries that command id as its
    ``order_id`` (the fill-driven close), so this set is the cascade-close join key —
    matching ``strategist._collect_engine_guardrail_command_ids``.
    """
    command_ids: set[str] = set()
    for entry in _events_of_type(dataset, EventType.PM_DECISION):
        detail = entry.detail
        if not isinstance(detail, PMDecisionDetail):
            continue
        if detail.source_provenance_json.get("source_provenance") == "engine_guardrail":
            command_ids.update(detail.resulting_command_ids)
    return frozenset(command_ids)


def _is_engine_originated_close(
    entry: ActivityLogEntry, cascade_command_ids: frozenset[str]
) -> bool:
    """Whether a ``POSITION_CLOSED`` entry was driven by the engine, not the PM.

    Two inclusion paths (mirroring
    ``portfolio_state.consumers.strategist._project_between_invocation_closures``):
    a monitor-direct source firing, or a cascade close whose ``order_id`` is one of an
    ``engine_guardrail`` envelope's resulting command ids.
    """
    if entry.source in _MONITOR_DIRECT_SOURCES:
        return True
    return entry.order_id is not None and entry.order_id in cascade_command_ids


def _compute_engine_originated_close_frequency(
    dataset: WindowDataset, _conditioning: Conditioning
) -> MetricResult:
    cascade_command_ids = _engine_guardrail_command_ids(dataset)
    count = sum(
        _is_engine_originated_close(entry, cascade_command_ids)
        for entry in _events_of_type(dataset, EventType.POSITION_CLOSED)
    )
    return _count_result(_ENGINE_ORIGINATED_CLOSE_FREQUENCY, count)


METRICS: tuple[Metric, ...] = (
    Metric(
        metric_id=_COST_TOKEN_PER_INVOCATION,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_cost_token_per_invocation,
    ),
    *_COST_PER_AGENT_METRICS,
    *_CACHE_HIT_RATE_METRICS,
    Metric(
        metric_id=_FAILURE_OVERHEAD,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_failure_overhead,
    ),
    *_LATENCY_HEADROOM_METRICS,
    Metric(
        metric_id=_COST_PER_RESOLVED_THESIS,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_cost_per_resolved_thesis,
    ),
    Metric(
        metric_id=_GUARDRAIL_REJECTION_COUNT,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_guardrail_rejection_count,
    ),
    Metric(
        metric_id=_COMMAND_ABANDONMENT_RATE,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_command_abandonment_rate,
    ),
    Metric(
        metric_id=_ENGINE_ORIGINATED_CLOSE_FREQUENCY,
        po_type="process",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=_compute_engine_originated_close_frequency,
    ),
)
