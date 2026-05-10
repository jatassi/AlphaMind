"""SPIN_OFF handler — parent basis reduction + orphan child creation (ALP-414).

The parent equity position keeps its share count and thesis/bracket binding but
has its average cost basis reduced by Alpaca's allocated portion (Alpaca is the
authoritative source for the post-spin parent basis).  A new child position is
created representing the spun-off shares with ``thesis_id=None`` /
``bracket_id=None`` / ``corporate_action_adjustment_needed=True`` and an
``origin="spin_off_from_<parent>"`` provenance string — the structural
exception to AlphaMind's mandatory thesis + bracket bindings, guarded by
:meth:`PositionRecord._check_spinoff_invariant`.

Activity log emissions:

* parent: ``CORPORATE_ACTION_APPLIED`` and ``BRACKET_CANCELLED_CORPORATE_ACTION``
* child:  ``POSITION_OPENED`` with ``mechanism=SPIN_OFF_FROM_PARENT``

Optioned-underlying parents are out of scope (design § Options positions calls
this case "messier" with no explicit spec); the handler raises
``NotImplementedError`` if encountered.
"""

from __future__ import annotations

import uuid

from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.portfolio_state.events.activity_log import (
    EventSource,
    EventType,
    PositionOpenedDetail,
    PositionOpenMechanism,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._shared import (
    _cancel_bracket_for_corporate_action,
    _emit,
    _emit_corporate_action_applied,
    _persist_position_update,
)


def _require_long_equity_parent(position: PositionRecord) -> EquityPositionDetails:
    """Narrow the parent payload to long equity, or raise ``NotImplementedError``."""
    details = position.details
    if not isinstance(details, EquityPositionDetails):
        msg = "SPIN_OFF on non-equity parent position not supported"
        raise NotImplementedError(msg)
    if position.direction != Direction.LONG:
        msg = "SPIN_OFF on non-LONG parent position not supported"
        raise NotImplementedError(msg)
    return details


def _require_lookup_position(
    lookup: AlpacaPositionLookup | None,
    symbol: str,
    *,
    role: str,
) -> tuple[float, float]:
    """Return ``(qty, avg_entry_price)`` from *lookup* for *symbol*.

    Raises ``ValueError`` if the lookup is absent or returns ``None``.
    """
    if lookup is None:
        msg = f"SPIN_OFF handler requires AlpacaPositionLookup to read {role} state"
        raise ValueError(msg)
    snapshot = lookup.get_position(symbol)
    if snapshot is None:
        msg = f"Alpaca lookup returned no {role} position for symbol={symbol!r}"
        raise ValueError(msg)
    return snapshot.qty, snapshot.avg_entry_price


def _build_spin_off_child(
    parent: PositionRecord,
    activity: CorporateActionActivity,
    *,
    child_ticker: str,
    child_qty: float,
    child_basis: float,
) -> PositionRecord:
    """Construct the child ``PositionRecord`` representing the spun-off shares.

    Constructs the (``origin``, ``parent_position_id``,
    ``corporate_action_adjustment_needed``) triplet that
    :meth:`PositionRecord._check_spinoff_invariant` validates.
    """
    fill = PositionFill(
        fill_timestamp=activity.transaction_time,
        fill_price=child_basis,
        fill_quantity=child_qty,
        slippage=0.0,
        fees=0.0,
        live_execution_estimate=None,
    )
    details = EquityPositionDetails(
        ticker=child_ticker,
        share_count=child_qty,
        average_cost_basis_per_share=child_basis,
    )
    return PositionRecord(
        position_id=uuid.uuid4().hex,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=activity.transaction_time,
        details=details,
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=True,
        parent_position_id=parent.position_id,
        origin=f"spin_off_from_{parent.position_id}",
    )


async def handle_spin_off(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    lookup: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a spin-off: reduce parent basis, create the orphan child.

    * Parent ``share_count`` unchanged.
    * Parent ``average_cost_basis_per_share`` reduced to Alpaca's post-spin
      value (read via *lookup*).
    * Parent ``corporate_action_adjustment_needed`` set to ``True``.
    * New child ``PositionRecord`` inserted with ``thesis_id=None``,
      ``bracket_id=None``, ``corporate_action_adjustment_needed=True``,
      ``parent_position_id=<parent>``, and
      ``origin="spin_off_from_<parent>"``.
    * Parent's bracket cancelled, bracket-cancellation entry emitted.
    * Parent emits ``CORPORATE_ACTION_APPLIED``; child emits
      ``POSITION_OPENED`` (mechanism ``SPIN_OFF_FROM_PARENT``).
    * One row written to the CA integration ledger keyed on the activity ID.
    """
    pos_row = await handle.session.get(PositionRow, activity.position_id)
    if pos_row is None:
        msg = (
            f"CA activity {activity.alpaca_activity_id!r} references missing "
            f"position_id={activity.position_id!r}"
        )
        raise ValueError(msg)
    parent = position_row_to_record(pos_row)
    parent_details = _require_long_equity_parent(parent)

    # The spin-off has no legitimate ``new_ticker=None`` interpretation — Alpaca
    # always reports the spun-off symbol on a SPIN activity.
    if activity.new_ticker is None:
        msg = (
            f"SPIN_OFF activity {activity.alpaca_activity_id!r} is missing the "
            "new_ticker field — Alpaca should always populate it"
        )
        raise ValueError(msg)

    pre_qty = parent_details.share_count
    pre_basis = parent_details.average_cost_basis_per_share
    _, post_basis = _require_lookup_position(lookup, activity.ticker, role="parent")
    child_qty, child_basis = _require_lookup_position(lookup, activity.new_ticker, role="child")

    new_parent_details = parent_details.model_copy(
        update={"average_cost_basis_per_share": post_basis}
    )
    updated_parent = parent.model_copy(
        update={
            "details": new_parent_details,
            "corporate_action_adjustment_needed": True,
        }
    )
    _persist_position_update(pos_row, updated_parent)

    child = _build_spin_off_child(
        updated_parent,
        activity,
        child_ticker=activity.new_ticker,
        child_qty=child_qty,
        child_basis=child_basis,
    )
    handle.session.add(position_record_to_row(child))

    await _emit_corporate_action_applied(
        handle,
        activity=activity,
        position=updated_parent,
        pre_qty=pre_qty,
        post_qty=pre_qty,
        pre_basis=pre_basis,
        post_basis=post_basis,
    )
    await _cancel_bracket_for_corporate_action(handle, parent.bracket_id, activity)
    await _emit(
        handle,
        event_type=EventType.POSITION_OPENED,
        order_id=None,
        position_id=child.position_id,
        thesis_id=None,
        timestamp=activity.transaction_time,
        detail=PositionOpenedDetail(
            ticker=activity.new_ticker,
            direction=Direction.LONG.value,
            fill_price=child_basis,
            quantity=child_qty,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.SPIN_OFF_FROM_PARENT,
            parent_position_id=parent.position_id,
        ),
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
    )
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


__all__ = ["handle_spin_off"]
