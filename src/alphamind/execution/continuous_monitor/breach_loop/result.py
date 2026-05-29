"""Typed records for the breach-evaluation loop (story 03b / ALP-437).

The loop returns one ``BreachLoopResult`` per tick — a pure value object the
04a cascade dispatcher and 04b emergency-invocation trigger consume via the
callbacks the supervisor wires. ``RuleEvaluation`` is the per-rule projection
zone-classified and breach-response-classified; ``immediate_action_breaches``
is the pre-filtered view 04a iterates.

Frozen dataclasses, not Pydantic — the records are produced once per tick on
the hot path; runtime validation buys nothing because every field originates
from a validated source upstream (Phase 1 enforcement result, breach-behavior
primitive outputs, zone classifier).

Design reference:
* ``docs/design/05-execution-layer/architecture.md`` § 4b — periodic evaluation
* ``docs/design/06-risk-guardrails/breach-behavior.md`` § Per-rule breach
  response classification
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.guardrail_enforcement import Phase1EnforcementResult
from alphamind.risk_guardrails.breach_behavior import (
    DrawdownSample,
    HaltState,
    RiskZone,
)


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """One rule's per-tick evaluation result.

    ``overage`` is signed: positive when ``current_value`` exceeds
    ``limit_value`` (the breach direction), zero or negative for in-limit
    values. ``classification`` is ``None`` when the rule is not breaching;
    populated with the rule's static ``BreachResponse`` (``immediate_engine``
    or ``deferred_to_pm``) when ``zone == RiskZone.BLOCKED``.
    ``breaching_position_id`` is propagated verbatim from
    :class:`alphamind.risk_guardrails.guardrail_evaluation.RuleProjection`
    so the cascade dispatcher's per-position kwargs providers route the
    breach close envelope to the exact position the rule fired on.
    """

    rule_id: str
    current_value: float
    limit_value: float
    overage: float
    zone: RiskZone
    classification: BreachResponse | None
    breaching_position_id: str | None = None


@dataclass(frozen=True, slots=True)
class BreachLoopResult:
    """Aggregate output of one breach-evaluation tick.

    ``immediate_action_breaches`` is the subset of ``rule_evaluations`` whose
    ``classification == BreachResponse.immediate_engine`` — the cascade
    dispatcher (story 04a) iterates this directly. ``drawdown_velocity_sample``
    is the per-tick drawdown observation the emergency-invocation evaluator
    (story 04b) consumes for daily-drawdown-velocity windowing.
    """

    as_of: datetime
    phase1_result: Phase1EnforcementResult
    rule_evaluations: tuple[RuleEvaluation, ...]
    halt_state: HaltState | None
    immediate_action_breaches: tuple[RuleEvaluation, ...]
    drawdown_velocity_sample: DrawdownSample


@dataclass(frozen=True, slots=True)
class BreachLoopHealthSignal:
    """A breach-loop health-state transition (ALP-732 Gap 2).

    The loop's per-tick supervisor counts consecutive failed ticks. When the
    count crosses ``breach_loop_consecutive_failure_alert_threshold`` the loop
    emits one ``degraded=True`` signal (fired once on entering the degraded
    state, not on every subsequent failure); the first successful tick
    afterwards emits a ``degraded=False`` (recovered) signal and resets the
    counter. The monitor wires the consumer to its operator-visible surfaces
    (the SSE ``/events`` stream + loud logging) so "breach loop alive but
    failing every tick" is no longer indistinguishable from "healthy."

    ``consecutive_failures`` is the failure run length at the moment of the
    signal: the count that just crossed the threshold on a ``degraded=True``
    signal, or the length of the run that just ended on a ``degraded=False``
    one. ``last_error`` is the ``repr`` of the most recent tick exception on a
    ``degraded=True`` signal, and ``None`` on recovery.
    """

    degraded: bool
    consecutive_failures: int
    last_error: str | None
