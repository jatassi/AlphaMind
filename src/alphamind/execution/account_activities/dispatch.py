"""Per-type dispatch for one option-lifecycle event (ALP-846 / W1b).

``integrate_lifecycle_event`` is the single entry point the poll calls per typed
:class:`~alphamind.execution.account_activities.records.LifecycleEvent`. It
routes to the expiry handler (``OPEXP``) or the assignment / exercise handler
(``OPASN`` / ``OPEXC``); ``OPTRD`` never reaches dispatch as a standalone event
(the classifier consumes it as the pair of an assignment / exercise).
"""

from __future__ import annotations

from collections.abc import Callable

from alphamind.execution.account_activities.handlers import (
    handle_assignment_or_exercise,
    handle_expiry,
)
from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
    LifecycleEvent,
)
from alphamind.state.invocation_context.context import InvocationHandle


async def integrate_lifecycle_event(
    handle: InvocationHandle,
    event: LifecycleEvent,
    *,
    borrow_cost_resolver: Callable[[str], float | None],
) -> None:
    """Integrate one typed lifecycle event through its per-type handler.

    ``borrow_cost_resolver`` is forwarded only to the assignment / exercise
    handler, where a SHORT equity delivery needs it to stamp the short-only
    fields; the expiry handler opens no equity leg and ignores it.

    Raises ``ValueError`` for an ``OPTRD`` — it is never a standalone lifecycle
    event; the classifier folds it into its assignment / exercise pair, so one
    reaching dispatch is a classifier-contract violation, not a guessable case.
    """
    if event.activity_type is LifecycleActivityType.OPEXP:
        await handle_expiry(handle, event)
    elif event.activity_type in (LifecycleActivityType.OPASN, LifecycleActivityType.OPEXC):
        await handle_assignment_or_exercise(
            handle, event, borrow_cost_resolver=borrow_cost_resolver
        )
    else:
        msg = (
            f"OPTRD {event.activity_id!r} reached dispatch as a standalone event; "
            f"it must be the paired leg of an assignment/exercise"
        )
        raise ValueError(msg)


__all__ = ["integrate_lifecycle_event"]
