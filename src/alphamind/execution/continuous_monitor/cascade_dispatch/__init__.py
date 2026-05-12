"""Engine-envelope cascade dispatcher sub-package (story 04a / ALP-438).

The dispatcher is the ``on_immediate_breach`` callback the breach loop (story
03b) awaits. For each immediate-action ``HARD_BLOCK`` breach it selects the
position to close per the breach's deterministic selection rule, runs the
secondary-breach check, composes the engine envelope, and submits via
``submit_engine_envelope``. For cascades (margin call, primary + secondary
breach), it routes through the existing ``orchestrate_*_cascade`` primitives
to chain envelopes under a shared ``cascade_id``.

Public surface:

* :class:`TriggerIdGenerator` — per-session monotonic trigger-id counter.
* :data:`RULE_SELECTOR_DISPATCH`, :func:`selector_for` — per-rule selector
  dispatch table.
* :class:`CascadeDispatcher` — the orchestrating class wired as the
  ``on_immediate_breach`` callback.
* :class:`BreachDispatchContext` — per-tick context value the dispatcher
  receives from its context provider.
* :class:`DeferralEvent` — frozen value object surfaced when a protective
  CLOSE defers to the PM rather than submitting.
"""

from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    BreachDispatchContext,
    CascadeDispatcher,
    DeferralEvent,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.envelope_adapter import (
    to_oms_engine_envelope,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.selectors import (
    RULE_SELECTOR_DISPATCH,
    PositionSelector,
    selector_for,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.trigger_ids import (
    TriggerIdGenerator,
)

__all__ = [
    "RULE_SELECTOR_DISPATCH",
    "BreachDispatchContext",
    "CascadeDispatcher",
    "DeferralEvent",
    "PositionSelector",
    "TriggerIdGenerator",
    "selector_for",
    "to_oms_engine_envelope",
]
