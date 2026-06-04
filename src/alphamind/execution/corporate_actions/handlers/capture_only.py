"""Capture-only corporate-action handler (ALP-849 / W1c).

The v1beta1 types the fetcher used to drop — ``WorthlessRemoval`` / ``UnitSplit``
/ ``Redemption`` — carry no defined position / cash mutation, so there is no
integration math to run. They are still genuine broker facts that must land on
the gap-free ``broker_event_log`` (ADR-0002): ``integrate_ca_activity`` appends
the event row (capture) before dispatching here, and this handler only writes the
integration-ledger anchor so the fetcher does not re-surface the activity. No
position, cash, or bracket state is touched — the capture is the whole point.
"""

from __future__ import annotations

from alphamind.execution.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.state.invocation_context.context import InvocationHandle

from ..types import AlpacaPositionLookup, CorporateActionActivity


async def handle_capture_only(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    _: AlpacaPositionLookup | None = None,
) -> None:
    """Write the integration-ledger anchor for a capture-only CA; no mutation.

    The broker fact is already on the append-only ``broker_event_log`` (the
    capture step in ``integrate_ca_activity``). This anchor stops the fetcher
    from re-surfacing the activity on the next poll.
    """
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


__all__ = ["handle_capture_only"]
