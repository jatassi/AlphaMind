"""Shared helpers used by multiple CA handlers (ALP-409).

``_cancel_bracket_for_corporate_action`` and ``_emit_corporate_action_applied``
are moved from ``state_persistence.write_paths.phase1`` so handler modules can
import them without a circular dependency on the legacy write path.

``_apply_signed_cash_movement`` is a new helper that cash-impact handlers
(stories 03b / 03c) call to update the cash ledger and emit the appropriate
``CASH_CREDITED`` / ``CASH_DEBITED`` activity-log entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from alphamind.execution.state_persistence.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    BracketCancelledCorporateActionDetail,
    CashCreditedDetail,
    CashCreditReason,
    CashDebitedDetail,
    CashDebitReason,
    CorporateActionAppliedDetail,
    EventSource,
    EventType,
)
from alphamind.portfolio_state.records.orders import BracketStatus
from alphamind.portfolio_state.records.positions import PositionRecord

from ..types import CorporateActionActivity


class _StateInconsistencyError(RuntimeError):
    """Raised when a bracket FK target is missing during CA integration.

    Mirrors ``phase1.StateInconsistencyError``; defined here to avoid a
    circular import between the corporate_actions package and phase1.
    """


# ---------------------------------------------------------------------------
# Internal emit helper
# ---------------------------------------------------------------------------


async def _emit(
    handle: InvocationHandle,
    *,
    event_type: EventType,
    order_id: str | None,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource = EventSource.CORPORATE_ACTION_PROCESSOR,
) -> None:
    """Construct + persist one ``ActivityLogEntry`` joined to the current handle."""
    entry = ActivityLogEntry(
        entry_id=f"{handle.invocation_id}-{event_type.value}-{uuid.uuid4().hex}",
        invocation_id=handle.invocation_id,
        timestamp=timestamp,
        event_type=event_type,
        event_group=EVENT_TYPE_TO_GROUP[event_type],
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=detail,
    )
    await append_activity_log_entry(handle, entry)


# ---------------------------------------------------------------------------
# Position persistence
# ---------------------------------------------------------------------------


def _persist_position_update(row: PositionRow, position: PositionRecord) -> None:
    """Project the updated record back onto the existing ``PositionRow``."""
    new_row = position_record_to_row(position)
    row.status = new_row.status
    row.entry_timestamp = new_row.entry_timestamp
    row.details_json = new_row.details_json
    row.execution_history_json = new_row.execution_history_json
    row.realized_pnl_to_date_usd = new_row.realized_pnl_to_date_usd
    row.corporate_action_adjustment_needed = new_row.corporate_action_adjustment_needed


# ---------------------------------------------------------------------------
# Bracket lifecycle
# ---------------------------------------------------------------------------


async def _read_bracket_legs(handle: InvocationHandle, bracket_id: str) -> list[BracketLegRow]:
    leg_stmt = (
        select(BracketLegRow)
        .where(BracketLegRow.bracket_id == bracket_id)
        .order_by(BracketLegRow.leg_index.asc())
    )
    return list((await handle.session.execute(leg_stmt)).scalars())


async def _bracket_leg_order_ids(handle: InvocationHandle, bracket_id: str) -> tuple[str, ...]:
    return tuple(
        row.order_id for row in await _read_bracket_legs(handle, bracket_id) if row.order_id
    )


async def _cancel_bracket_for_corporate_action(
    handle: InvocationHandle,
    bracket_id: str | None,
    activity: CorporateActionActivity,
) -> None:
    """Mark the position's bracket DISSOLVED and emit the cancellation entry."""
    if bracket_id is None:
        return
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = (
            f"position {activity.position_id!r} references bracket {bracket_id!r}, "
            "but bracket row is missing"
        )
        raise _StateInconsistencyError(msg)
    cancellation_reason = f"corporate_action_{activity.action_type.value.lower()}"
    bracket_row.status = BracketStatus.DISSOLVED.value
    bracket_row.corporate_action_cancellation_reason = cancellation_reason
    leg_ids = await _bracket_leg_order_ids(handle, bracket_id)
    await _emit(
        handle,
        event_type=EventType.BRACKET_CANCELLED_CORPORATE_ACTION,
        order_id=None,
        position_id=activity.position_id,
        thesis_id=None,
        timestamp=activity.transaction_time,
        detail=BracketCancelledCorporateActionDetail(
            bracket_id=bracket_id,
            cancellation_reason=cancellation_reason,
            cancelled_leg_order_ids=leg_ids,
        ),
    )


# ---------------------------------------------------------------------------
# Emission helpers
# ---------------------------------------------------------------------------


async def _emit_corporate_action_applied(
    handle: InvocationHandle,
    *,
    activity: CorporateActionActivity,
    position: PositionRecord,
    pre_qty: float,
    post_qty: float,
    pre_basis: float,
    post_basis: float,
) -> None:
    await _emit(
        handle,
        event_type=EventType.CORPORATE_ACTION_APPLIED,
        order_id=None,
        position_id=position.position_id,
        thesis_id=None,
        timestamp=activity.transaction_time,
        detail=CorporateActionAppliedDetail(
            action_type=activity.action_type,
            alpaca_activity_id=activity.alpaca_activity_id,
            ticker=activity.ticker,
            new_ticker=activity.new_ticker,
            ratio_or_amount=activity.ratio_or_amount,
            pre_action_quantity=pre_qty,
            post_action_quantity=post_qty,
            pre_action_cost_basis=pre_basis,
            post_action_cost_basis=post_basis,
            signed_cash_impact_usd=activity.signed_cash_impact_usd,
            parent_position_id=None,
            resulting_position_status=position.status.value,
        ),
    )


# ---------------------------------------------------------------------------
# Cash movement
# ---------------------------------------------------------------------------


async def _apply_signed_cash_movement(
    handle: InvocationHandle,
    signed_cash_impact_usd: float,
    *,
    reason: str,
) -> None:
    """Update cash_ledger by *signed_cash_impact_usd* and emit the log entry.

    Positive values credit cash (``CASH_CREDITED``); negative values debit it
    (``CASH_DEBITED``).  The *reason* must be the string value of an appropriate
    :class:`~alphamind.portfolio_state.events.activity_log.CashCreditReason` or
    :class:`~alphamind.portfolio_state.events.activity_log.CashDebitReason` member.
    """
    cash_row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if cash_row is None:
        msg = "cash_ledger singleton missing — CA handler cannot apply cash movement"
        raise ValueError(msg)
    now = datetime.now(UTC)
    cash_row.current_cash_usd = cash_row.current_cash_usd + signed_cash_impact_usd
    cash_row.last_updated_at = now.isoformat()
    new_balance = cash_row.current_cash_usd
    if signed_cash_impact_usd >= 0:
        await _emit(
            handle,
            event_type=EventType.CASH_CREDITED,
            order_id=None,
            position_id=None,
            thesis_id=None,
            timestamp=now,
            detail=CashCreditedDetail(
                amount_usd=abs(signed_cash_impact_usd),
                reason=CashCreditReason(reason),
                new_balance_usd=new_balance,
            ),
        )
    else:
        await _emit(
            handle,
            event_type=EventType.CASH_DEBITED,
            order_id=None,
            position_id=None,
            thesis_id=None,
            timestamp=now,
            detail=CashDebitedDetail(
                amount_usd=abs(signed_cash_impact_usd),
                reason=CashDebitReason(reason),
                new_balance_usd=new_balance,
            ),
        )


__all__ = [
    "_apply_signed_cash_movement",
    "_cancel_bracket_for_corporate_action",
    "_emit_corporate_action_applied",
    "_persist_position_update",
]
