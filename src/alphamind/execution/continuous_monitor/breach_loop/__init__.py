"""Periodic guardrail-evaluation loop sub-package (story 03b / ALP-437).

The continuous monitor's breach loop wakes every
``breach_evaluation_cadence_seconds``, composes the canonical
``ActiveRiskParameterSet`` via the same primitive the decision pipeline uses
(``compose_phase_1_enforcement``), evaluates every guardrail rule via the
guardrail-evaluation library against current portfolio state, and dispatches
two downstream paths via injected callbacks:

* ``on_immediate_breach`` — story 04a's cascade dispatcher.
* ``on_emergency_input`` — story 04b's emergency-invocation trigger.

Public surface:

* :class:`BreachLoopResult` — the per-tick frozen result record.
* :class:`RuleEvaluation` — per-rule zone-classified evaluation.
* :class:`HaltTransitionTracker` — in-memory tracker that emits
  ``HALT_ACTIVATED`` / ``HALT_LIFTED`` activity-log entries on transitions.
* :func:`run_breach_loop` — long-running asyncio task the monitor
  supervisor registers as ``breach_loop``.
* :func:`register_breach_loop_task` — entry-point wiring helper.
"""

from alphamind.execution.continuous_monitor.breach_loop.halt_tracker import (
    HaltTransitionTracker,
)
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopHealthSignal,
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.breach_loop.task import (
    MarketHoursClock,
    OnHealthSignal,
    run_breach_loop,
)
from alphamind.execution.continuous_monitor.breach_loop.wiring import (
    register_breach_loop_task,
)

__all__ = [
    "BreachLoopHealthSignal",
    "BreachLoopResult",
    "HaltTransitionTracker",
    "MarketHoursClock",
    "OnHealthSignal",
    "RuleEvaluation",
    "register_breach_loop_task",
    "run_breach_loop",
]
