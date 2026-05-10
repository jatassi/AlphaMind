"""Cash-dividend handlers (ALP-412 / story 03b).

Implements the per-action-type handlers for ``CASH_DIVIDEND_LONG`` and
``CASH_DIVIDEND_SHORT`` (short-dividend obligation pass-through).  Both
handlers apply ``activity.signed_cash_impact_usd`` to the cash ledger via
the shared ``_apply_signed_cash_movement`` helper, leave the position's
quantity / cost basis unchanged, set ``corporate_action_adjustment_needed``
to ``True`` so the strategist re-evaluates per ``strategist.md``'s
cash-dividend default action (``adjust-bracket``), and cancel the bracket
per the cancel-and-review policy.
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
from alphamind.portfolio_state.events.activity_log import (
    CashCreditReason,
    CashDebitReason,
)
from alphamind.portfolio_state.records.positions import EquityPositionDetails

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._shared import (
    _apply_signed_cash_movement,
    _cancel_bracket_for_corporate_action,
    _emit_corporate_action_applied,
    _persist_position_update,
)


async def _apply_cash_dividend(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    *,
    reason: str,
) -> None:
    """Shared body of both cash-dividend handlers.

    Loads the position; applies the signed cash movement (``reason`` selects
    the credit/debit log enum value); flips
    ``corporate_action_adjustment_needed`` to ``True`` (quantity and cost
    basis are unchanged); emits the standard ``CORPORATE_ACTION_APPLIED``
    entry; cancels the bracket; writes the CA-integration-ledger anchor.
    """
    pos_row = await handle.session.get(PositionRow, activity.position_id)
    if pos_row is None:
        msg = (
            f"CA activity {activity.alpaca_activity_id!r} references missing "
            f"position_id={activity.position_id!r}"
        )
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    details = position.details
    if not isinstance(details, EquityPositionDetails):
        msg = (
            f"cash dividend handler currently supports equity positions only; "
            f"got {details.instrument_type!r}"
        )
        raise NotImplementedError(msg)

    pre_qty = details.share_count
    pre_basis = details.average_cost_basis_per_share

    await _apply_signed_cash_movement(
        handle,
        activity.signed_cash_impact_usd,
        reason=reason,
        timestamp=activity.transaction_time,
        position_id=activity.position_id,
    )

    updated = position.model_copy(update={"corporate_action_adjustment_needed": True})
    _persist_position_update(pos_row, updated)

    await _emit_corporate_action_applied(
        handle,
        activity=activity,
        position=updated,
        pre_qty=pre_qty,
        post_qty=pre_qty,
        pre_basis=pre_basis,
        post_basis=pre_basis,
    )
    await _cancel_bracket_for_corporate_action(handle, position.bracket_id, activity)
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


async def handle_cash_dividend_long(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    _: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a long-side cash dividend.

    * Credits cash by ``activity.signed_cash_impact_usd`` (positive) via
      ``_apply_signed_cash_movement`` with ``CashCreditReason.CASH_DIVIDEND_LONG``.
    * Quantity and cost basis are unchanged.
    * ``corporate_action_adjustment_needed`` is set to ``True``.
    * The bracket (if any) is cancelled per the cancel-and-review policy.
    * A dedupe anchor is written to the CA integration ledger.
    """
    await _apply_cash_dividend(handle, activity, reason=CashCreditReason.CASH_DIVIDEND_LONG.value)


async def handle_cash_dividend_short(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    _: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a short-side cash dividend (obligation pass-through).

    * Debits cash by ``abs(activity.signed_cash_impact_usd)`` (input is
      negative) via ``_apply_signed_cash_movement`` with
      ``CashDebitReason.CASH_DIVIDEND_SHORT_OBLIGATION``.
    * Quantity and cost basis are unchanged.
    * ``corporate_action_adjustment_needed`` is set to ``True``.
    * The bracket (if any) is cancelled per the cancel-and-review policy.
    * A dedupe anchor is written to the CA integration ledger.
    """
    await _apply_cash_dividend(
        handle, activity, reason=CashDebitReason.CASH_DIVIDEND_SHORT_OBLIGATION.value
    )


__all__ = [
    "handle_cash_dividend_long",
    "handle_cash_dividend_short",
]
