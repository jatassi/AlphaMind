"""Emergency-invocation trigger sub-package (story 04b / ALP-439).

The continuous monitor's emergency-trigger evaluator detects regime jumps,
multi-rule breaches, daily-drawdown velocity, and margin-call conditions and
writes an ``EMERGENCY_INVOCATION_REQUESTED`` activity-log entry. The pipeline
scheduler's emergency receiver task (ALP-447) reads these entries and
dispatches a ``run_invocation(trigger_type="emergency")`` autonomously.

Public surface:

* :class:`CooldownTracker` — pure in-memory tracker enforcing
  ``BreachBehaviorConfig.emergency_invocation_cooldown_minutes``.
* :class:`EmergencyTriggerEvaluator` — wraps the four trigger-evaluation
  primitives and the activity-log writer behind the ``on_emergency_input``
  callback the breach loop (ALP-437) awaits.
* :class:`MarginCallObserver` — protocol for sourcing :class:`MarginCallEvent`
  per loop tick; production wiring may consult the broker adapter, tests
  substitute fakes.
* :class:`TriggerIdGenerator` — monotonic per-session trigger-ID sequence
  shared with the cascade dispatcher (story 04a / ALP-438) so envelope and
  emergency-request trigger IDs do not collide.
* :func:`register_emergency_trigger` — supervisor-wiring helper the
  ``__main__.py`` daemon uses to construct the production callback.
"""

from alphamind.execution.continuous_monitor.emergency_trigger.cooldown import (
    CooldownTracker,
)
from alphamind.execution.continuous_monitor.emergency_trigger.evaluator import (
    ActivityLogWriter,
    EmergencyTriggerEvaluator,
    MarginCallObserver,
    NoMarginCallObserver,
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.emergency_trigger.wiring import (
    make_emergency_callback,
    make_emergency_invocation_writer,
    make_invocation_id_provider,
)

__all__ = [
    "ActivityLogWriter",
    "CooldownTracker",
    "EmergencyTriggerEvaluator",
    "MarginCallObserver",
    "NoMarginCallObserver",
    "TriggerIdGenerator",
    "make_emergency_callback",
    "make_emergency_invocation_writer",
    "make_invocation_id_provider",
]
