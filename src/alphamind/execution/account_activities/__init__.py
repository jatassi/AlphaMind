"""Account-activities option-lifecycle poll + handlers (ALP-846 / W1b).

Closes the option-lifecycle husk-by-design (ADR-0002): wires the previously
dead ``get_account_activities`` endpoint into a pipeline-cadence poll that
appends ``OPEXP`` / ``OPEXC`` / ``OPASN`` / ``OPTRD`` activities to the
append-only ``broker_event_log`` (idempotent on ``event_key``), then books the
realized PnL and opens / closes the resulting positions.

Public API:

* :func:`poll_account_activities` — the pipeline-cadence poll (the shell).
* :func:`integrate_lifecycle_event` — dispatch one typed lifecycle event
  through its per-type handler.
* :func:`classify_lifecycle_activities` — pure classifier (broker stream →
  typed, paired lifecycle events).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alphamind.execution.account_activities.classify import (
        classify_lifecycle_activities,
    )
    from alphamind.execution.account_activities.dispatch import (
        integrate_lifecycle_event,
    )
    from alphamind.execution.account_activities.poll import poll_account_activities

__all__ = [
    "classify_lifecycle_activities",
    "integrate_lifecycle_event",
    "poll_account_activities",
]


def __getattr__(name: str) -> object:
    """Lazy-load public names to avoid import-time cycles (mirrors corporate_actions)."""
    if name == "classify_lifecycle_activities":
        from alphamind.execution.account_activities.classify import (
            classify_lifecycle_activities,
        )

        return classify_lifecycle_activities
    if name == "integrate_lifecycle_event":
        from alphamind.execution.account_activities.dispatch import (
            integrate_lifecycle_event,
        )

        return integrate_lifecycle_event
    if name == "poll_account_activities":
        from alphamind.execution.account_activities.poll import poll_account_activities

        return poll_account_activities
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
