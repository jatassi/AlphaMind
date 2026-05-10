"""Equity SPLIT handler (ALP-409).

The SPLIT handler is the full implementation moved from
``state_persistence.write_paths.phase1._integrate_one_ca_activity``.

All other ``CorporateActionType`` handlers (reverse split, stock dividend,
cash dividends, mergers, spin-off, symbol change) live in dedicated modules
under this package.
"""

from __future__ import annotations

from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.portfolio_state.records.positions import EquityPositionDetails, PositionRecord

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._shared import (
    _cancel_bracket_for_corporate_action,
    _emit_corporate_action_applied,
    _persist_position_update,
)


def _require_equity_details(position: PositionRecord, label: str) -> EquityPositionDetails:
    """Narrow ``PositionRecord.details`` to ``EquityPositionDetails``."""
    details = position.details
    if not isinstance(details, EquityPositionDetails):
        msg = f"{label} currently supports equity positions only; got {details.instrument_type!r}"
        raise NotImplementedError(msg)
    return details


async def handle_split(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    _: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a forward stock split to the position.

    * Quantity scales up by ``ratio_or_amount``.
    * Cost basis scales down by ``ratio_or_amount``.
    * ``corporate_action_adjustment_needed`` is set to ``True``.
    * The bracket (if any) is cancelled.
    * A dedupe anchor is written to the CA integration ledger.
    """
    pos_row = await handle.session.get(PositionRow, activity.position_id)
    if pos_row is None:
        msg = (
            f"CA activity {activity.alpaca_activity_id!r} references missing "
            f"position_id={activity.position_id!r}"
        )
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    details = _require_equity_details(position, "CA integration")

    pre_qty = details.share_count
    pre_basis = details.average_cost_basis_per_share
    new_qty = pre_qty * activity.ratio_or_amount
    new_basis = pre_basis / activity.ratio_or_amount

    new_details = details.model_copy(
        update={"share_count": new_qty, "average_cost_basis_per_share": new_basis}
    )
    updated = position.model_copy(
        update={"details": new_details, "corporate_action_adjustment_needed": True}
    )
    _persist_position_update(pos_row, updated)

    await _emit_corporate_action_applied(
        handle,
        activity=activity,
        position=updated,
        pre_qty=pre_qty,
        post_qty=new_qty,
        pre_basis=pre_basis,
        post_basis=new_basis,
    )
    await _cancel_bracket_for_corporate_action(handle, position.bracket_id, activity)
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


__all__ = ["_require_equity_details", "handle_split"]
