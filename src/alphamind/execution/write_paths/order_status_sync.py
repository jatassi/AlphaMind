"""Terminal non-fill order-status sync (ALP-739).

The fill-stream consumer observes broker ``canceled`` / ``expired``
trade-update events, but :func:`fill_report_to_fill_record` drops them — they
append no fill. Their terminal disposition still has to reach the local
``orders`` row, or an accepted entry that expires / cancels unfilled stays
``PENDING`` in state (the ZS symptom in ALP-739) and the ``entry_no_fill``
alert never sees it.

This helper is the narrow write that closes that gap: load the order, and if
it is still in a non-terminal status, stamp the terminal status. The caller
(the consumer) owns the transaction boundary, mirroring
:func:`alphamind.execution.write_paths.fill_persistence.append_fill_record`.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.state.tables.orders import OrderRow

log = logging.getLogger(__name__)

# A zero-fill terminal sync applies to a PENDING or PENDING_SUBMIT order: the
# consumer gates this path on ``cumulative_filled_quantity == 0`` (see the caller
# ``_sync_terminal_status_if_any``), and a zero-fill order has never been advanced
# past those pre-fill states by Phase 1's fill integration. PENDING_SUBMIT
# (ALP-836) is included so a broker ``canceled`` / ``expired`` event for an order
# whose post-submit ``alpaca_order_id`` backfill was lost (still PENDING_SUBMIT)
# still drives it terminal rather than leaving it wedged. Restricting the source
# state to these two makes the write idempotent — a re-processed terminal event
# (live + recovery overlap) finds an already-terminal status and no-ops, so
# ``last_update_timestamp`` is not bumped twice. (A partially-filled order is
# handled by the fill path + Phase 1, not here, so there is no "don't clobber a
# FILLED status" case for this guard to defend.)
_TRANSITIONABLE_FROM: frozenset[str] = frozenset(
    {OrderStatus.PENDING.value, OrderStatus.PENDING_SUBMIT.value}
)


async def sync_terminal_order_status(
    session: AsyncSession,
    *,
    order_id: str,
    terminal_status: OrderStatus,
    observed_at: datetime,
) -> bool:
    """Stamp *order_id*'s ``orders.status`` to a terminal non-fill value.

    Transitions only from ``PENDING`` (the sole state a zero-fill order can be
    in — see ``_TRANSITIONABLE_FROM``); a no-op (returns ``False``) when the
    order is unknown or already past PENDING. ``last_update_timestamp`` is set
    to *observed_at* — the moment the monitor recorded the transition, not the
    broker event time — so the ``entry_no_fill`` alert's lookback window
    catches a status synced late (e.g. recovered after a monitor outage).

    The caller controls commit / rollback; this helper only mutates the row.
    Returns ``True`` iff the row transitioned.
    """
    row = await session.get(OrderRow, order_id)
    if row is None:
        log.debug(
            "terminal-status sync: order_id=%s not found; skipping (target=%s)",
            order_id,
            terminal_status.value,
        )
        return False
    if row.status not in _TRANSITIONABLE_FROM:
        log.debug(
            "terminal-status sync: order_id=%s already %s; not overwriting with %s",
            order_id,
            row.status,
            terminal_status.value,
        )
        return False
    row.status = terminal_status.value
    row.last_update_timestamp = observed_at.isoformat()
    return True


__all__ = ["sync_terminal_order_status"]
