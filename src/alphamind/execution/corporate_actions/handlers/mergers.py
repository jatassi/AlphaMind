"""Cash-merger and stock-merger handlers (ALP-413).

A **cash merger** terminates the position: quantity zeroed, status →
:data:`PositionStatus.CLOSED`, realized P/L accumulated from deal price vs.
cost basis, deal proceeds credited via :data:`CashCreditReason.CASH_MERGER_PROCEEDS`,
position closed with :data:`PositionExitMethod.CORPORATE_ACTION_CASH_MERGER`,
linked thesis (if any) marked :data:`ThesisRecordStatus.RESOLVED`.

A **stock merger** swaps the position to the acquirer: new ticker
(``activity.new_ticker``), new quantity + cost basis from
:class:`AlpacaPositionLookup`, optional partial cash credit for cash-and-stock
mixes, ``corporate_action_adjustment_needed`` set so the strategist re-evaluates
on the next invocation per ``strategist.md``'s default-close stance.

Both handlers cancel the bracket via the shared mechanism and write the
dedup ledger row through ``mark_ca_activity_processed``.
"""

from __future__ import annotations

import dataclasses

from alphamind._kernel.ids import Symbol
from alphamind._kernel.money import money, signed_money
from alphamind.execution.broker_adapter.queries import PositionSnapshot
from alphamind.execution.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.portfolio_state.events.activity_log import (
    CashCreditReason,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.state.tables.theses import ThesisRow

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._shared import (
    _apply_signed_cash_movement,
    _cancel_bracket_for_corporate_action,
    _emit,
    _emit_corporate_action_applied,
    _persist_position_update,
)

_PositionDetails = EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails


# ---------------------------------------------------------------------------
# Cash merger
# ---------------------------------------------------------------------------


async def handle_cash_merger(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    _: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a cash merger to the position.

    Zeros quantity, transitions ``status`` to :data:`PositionStatus.CLOSED`,
    accumulates realized P/L (deal proceeds vs. cost basis), credits cash via
    :data:`CashCreditReason.CASH_MERGER_PROCEEDS`, emits ``POSITION_CLOSED``
    with ``exit_method=CORPORATE_ACTION_CASH_MERGER``, cancels the bracket,
    resolves the linked thesis if any, and writes the dedup ledger row.
    """
    pos_row = await handle.session.get(PositionRow, activity.position_id)
    if pos_row is None:
        msg = (
            f"CA activity {activity.alpaca_activity_id!r} references missing "
            f"position_id={activity.position_id!r}"
        )
        raise ValueError(msg)
    position = position_row_to_record(pos_row)

    pre_qty, pre_basis, realized_pnl, new_details = _close_for_cash_merger(position, activity)

    updated = dataclasses.replace(
        position,
        details=new_details,
        status=PositionStatus.CLOSED,
        realized_pnl_to_date_usd=(position.realized_pnl_to_date_usd or 0.0) + realized_pnl,
    )
    _persist_position_update(pos_row, updated)

    await _apply_signed_cash_movement(
        handle,
        activity.signed_cash_impact_usd,
        reason=CashCreditReason.CASH_MERGER_PROCEEDS.value,
        timestamp=activity.transaction_time,
        position_id=activity.position_id,
    )

    if not pre_qty:
        msg = (
            "cash-merger handler invoked with pre_qty == 0; cannot emit "
            "POSITION_CLOSED with a meaningful exit_price"
        )
        raise ValueError(msg)
    exit_price_value = activity.signed_cash_impact_usd / pre_qty
    _emit(
        handle,
        event_type=EventType.POSITION_CLOSED,
        order_id=None,
        position_id=updated.position_id,
        thesis_id=updated.thesis_id,
        timestamp=activity.transaction_time,
        detail=PositionClosedDetail(
            exit_method=PositionExitMethod.CORPORATE_ACTION_CASH_MERGER,
            exit_price=money(exit_price_value),
            realized_pnl_usd=signed_money(updated.realized_pnl_to_date_usd or 0.0),
            thesis_resolution_category="corporate_action_cash_merger",
        ),
    )

    await _emit_corporate_action_applied(
        handle,
        activity=activity,
        position=updated,
        pre_qty=pre_qty,
        post_qty=0.0,
        pre_basis=pre_basis,
        post_basis=pre_basis,
    )
    await _cancel_bracket_for_corporate_action(handle, position.bracket_id, activity)
    await _resolve_linked_thesis(handle, position, activity)
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


def _close_for_cash_merger(
    position: PositionRecord, activity: CorporateActionActivity
) -> tuple[float, float, float, _PositionDetails]:
    """Compute (pre_qty, pre_basis, realized_pnl, new_details) for any instrument type."""
    details = position.details
    proceeds = activity.signed_cash_impact_usd
    if isinstance(details, EquityPositionDetails):
        pre_qty = details.share_count
        pre_basis = details.average_cost_basis_per_share
        realized = proceeds - (pre_qty * pre_basis)
        return (
            pre_qty,
            pre_basis,
            realized,
            dataclasses.replace(details, share_count=0.0),
        )
    if isinstance(details, OptionsPositionDetails):
        pre_qty = details.contract_count
        pre_basis = details.premium_paid_per_contract
        realized = proceeds - (pre_qty * pre_basis * details.contract_multiplier)
        return (
            pre_qty,
            pre_basis,
            realized,
            dataclasses.replace(details, contract_count=0.0),
        )
    if isinstance(details, StrategyPositionDetails):
        # Aggregate entry cost across legs (signed by per-leg direction).
        entry_cost = sum(
            (-1.0 if leg.direction == Direction.SHORT else 1.0)
            * leg.options.contract_count
            * leg.options.premium_paid_per_contract
            * leg.options.contract_multiplier
            for leg in details.legs
        )
        realized = proceeds - entry_cost
        pre_qty = sum(leg.options.contract_count for leg in details.legs)
        zeroed_legs = tuple(
            StrategyLeg(
                leg_id=leg.leg_id,
                direction=leg.direction,
                options=dataclasses.replace(leg.options, contract_count=0.0),
            )
            for leg in details.legs
        )
        return pre_qty, entry_cost, realized, dataclasses.replace(details, legs=zeroed_legs)
    msg = f"unsupported instrument_type for cash merger: {type(details).__name__}"
    raise NotImplementedError(msg)


async def _resolve_linked_thesis(
    handle: InvocationHandle,
    position: PositionRecord,
    activity: CorporateActionActivity,
) -> None:
    """Mark the linked thesis ``RESOLVED`` with the activity's transaction time."""
    if position.thesis_id is None:
        return
    thesis_row = await handle.session.get(ThesisRow, position.thesis_id)
    if thesis_row is None:
        msg = (
            f"position {position.position_id!r} references thesis "
            f"{position.thesis_id!r}, but thesis row is missing"
        )
        raise ValueError(msg)
    thesis_row.status = ThesisRecordStatus.RESOLVED.value
    thesis_row.resolution_timestamp = activity.transaction_time.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Stock merger
# ---------------------------------------------------------------------------


async def handle_stock_merger(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    lookup: AlpacaPositionLookup | None = None,
) -> None:
    """Swap the position to the acquirer using post-adjustment Alpaca state.

    Reads ``lookup.get_position(activity.new_ticker)`` for the post-merger
    quantity and cost basis, projects them onto the position, sets
    ``corporate_action_adjustment_needed`` so the strategist re-evaluates,
    optionally credits partial cash for a cash-and-stock mix, cancels the
    bracket, and writes the dedup ledger row.
    """
    if lookup is None:
        msg = (
            f"stock merger {activity.alpaca_activity_id!r} requires "
            "alpaca_position_lookup; got None"
        )
        raise ValueError(msg)
    if activity.new_ticker is None:
        msg = f"stock merger {activity.alpaca_activity_id!r} requires activity.new_ticker; got None"
        raise ValueError(msg)

    pos_row = await handle.session.get(PositionRow, activity.position_id)
    if pos_row is None:
        msg = (
            f"CA activity {activity.alpaca_activity_id!r} references missing "
            f"position_id={activity.position_id!r}"
        )
        raise ValueError(msg)
    position = position_row_to_record(pos_row)

    snapshot = lookup.get_position(activity.new_ticker)
    if snapshot is None:
        msg = (
            f"stock merger {activity.alpaca_activity_id!r} expected Alpaca position "
            f"for new_ticker={activity.new_ticker!r}, got None"
        )
        raise ValueError(msg)

    pre_qty, pre_basis, post_qty, post_basis, new_details = _swap_for_stock_merger(
        position, activity.new_ticker, snapshot
    )

    updated = dataclasses.replace(
        position, details=new_details, corporate_action_adjustment_needed=True
    )
    _persist_position_update(pos_row, updated)

    if activity.signed_cash_impact_usd > 0:
        await _apply_signed_cash_movement(
            handle,
            activity.signed_cash_impact_usd,
            reason=CashCreditReason.CASH_MERGER_PROCEEDS.value,
            timestamp=activity.transaction_time,
            position_id=activity.position_id,
        )

    await _emit_corporate_action_applied(
        handle,
        activity=activity,
        position=updated,
        pre_qty=pre_qty,
        post_qty=post_qty,
        pre_basis=pre_basis,
        post_basis=post_basis,
    )
    await _cancel_bracket_for_corporate_action(handle, position.bracket_id, activity)
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


def _swap_for_stock_merger(
    position: PositionRecord,
    new_ticker_str: str,
    snapshot: PositionSnapshot,
) -> tuple[float, float, float, float, _PositionDetails]:
    """Project the post-merger Alpaca snapshot onto the position's details."""
    new_ticker = Symbol(new_ticker_str)
    details = position.details
    # Stock-merger creates a new OCC contract whose underlying differs from
    # the prior contract — prior greeks were computed against a different
    # instrument and would be misleading even with ``refresh_failed=True``,
    # so we zero them outright (unlike ``reverse_splits`` / ``stock_dividends``
    # where the OCC strike adjusts but the underlying is unchanged and
    # ``_stale_greeks`` preserves the prior values). The next monitor refresh
    # populates fresh values.
    stale_greeks = OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0, refresh_failed=True)

    # ALP-462 — ``snapshot.avg_entry_price`` is ``Price`` (Decimal); cast at the
    # legacy PositionRecord boundary (records.positions is outside ALP-462).
    new_avg_entry_price = float(snapshot.avg_entry_price)
    if isinstance(details, EquityPositionDetails):
        pre_qty = details.share_count
        pre_basis = details.average_cost_basis_per_share
        new_equity = dataclasses.replace(
            details,
            ticker=new_ticker,
            share_count=snapshot.qty,
            average_cost_basis_per_share=new_avg_entry_price,
        )
        return pre_qty, pre_basis, snapshot.qty, new_avg_entry_price, new_equity
    if isinstance(details, OptionsPositionDetails):
        pre_qty = details.contract_count
        pre_basis = details.premium_paid_per_contract
        new_options = dataclasses.replace(
            details,
            underlying_ticker=new_ticker,
            contract_count=snapshot.qty,
            premium_paid_per_contract=new_avg_entry_price,
            greeks=stale_greeks,
        )
        return pre_qty, pre_basis, snapshot.qty, new_avg_entry_price, new_options
    if isinstance(details, StrategyPositionDetails):
        # Per-leg projection requires identifying each post-adjusted leg by its
        # OCC symbol; corporate-actions.md treats sequence-handling as an
        # operator-surfaced edge.
        msg = (
            "stock merger on multi-leg strategy positions is not implemented in "
            "Phase 1; surface the activity to the operator (see corporate-actions.md "
            "options-positions section: Alpaca emits these as a sequence of "
            "activities and the OMS applies the post-adjustment state)"
        )
        raise NotImplementedError(msg)
    msg = f"unsupported instrument_type for stock merger: {type(details).__name__}"
    raise NotImplementedError(msg)


__all__ = [
    "handle_cash_merger",
    "handle_stock_merger",
]
