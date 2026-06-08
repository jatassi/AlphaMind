"""Shared helpers used by multiple CA handlers (ALP-409).

``_cancel_bracket_for_corporate_action`` and ``_emit_corporate_action_applied``
are moved from ``state_persistence.write_paths.fill_collection`` so handler modules can
import them without a circular dependency on the legacy write path.

``_apply_signed_cash_movement`` is a new helper that cash-impact handlers
(stories 03b / 03c) call to update the cash ledger and emit the appropriate
``CASH_CREDITED`` / ``CASH_DEBITED`` activity-log entry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import select

from alphamind._kernel.money import money, signed_money
from alphamind.portfolio_state.events.activity_log import (
    BracketCancelledCorporateActionDetail,
    CashCreditedDetail,
    CashCreditReason,
    CashDebitedDetail,
    CashDebitReason,
    CorporateActionAppliedDetail,
    EventSource,
    EventType,
)
from alphamind.portfolio_state.records.orders import BracketLegStatus, BracketStatus
from alphamind.portfolio_state.records.positions import PositionRecord
from alphamind.state.invocation_context.activity_log import (
    emit_activity_log_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    rows_to_record as bracket_rows_to_record,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)

from ..types import CorporateActionActivity

# Sub-nanocent cash movements are floating-point noise rather than real
# economic events; mirrors ``fill_collection._QTY_EPSILON`` (1e-9) so the CA path's
# zero-amount gate is uniform with the fill-side gate.
_CASH_EPSILON: Final[float] = 1e-9


class _StateInconsistencyError(RuntimeError):
    """Raised when a bracket FK target is missing during CA integration.

    Mirrors ``fill_collection.StateInconsistencyError``; defined here to avoid a
    circular import between the corporate_actions package and fill_collection.
    """


# ---------------------------------------------------------------------------
# Internal emit helper
# ---------------------------------------------------------------------------


def _emit(
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
    emit_activity_log_entry(
        handle,
        event_type=event_type,
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=detail,
        source=source,
    )


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


async def _assert_bracket_readable(handle: InvocationHandle, bracket_id: str) -> None:
    """Re-materialize the bracket through the read codec as a write-time guard.

    Defense in depth (ALP-731): a DISSOLVED bracket whose legs are not all
    CANCELLED is committable but unreadable — every
    ``get_brackets_for_positions`` loader then raises. Rebuilding the record
    here runs the same ``BracketRecord`` invariants the read path enforces, so
    a state the reader forbids fails loudly at write time. Mirrors the command_execution
    cancel-path guard of the same name.
    """
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        return
    leg_rows = tuple(await _read_bracket_legs(handle, bracket_id))
    bracket_rows_to_record(bracket_row, leg_rows)


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
    # Transition the parallel ``bracket_legs`` rows to CANCELLED — including
    # order-less EVENT/advisory legs — so the DISSOLVED bracket stays readable
    # on every subsequent state load (ALP-731). Read once; derive the emitted
    # leg order_ids from the same rows.
    leg_rows = await _read_bracket_legs(handle, bracket_id)
    for leg_row in leg_rows:
        leg_row.leg_status = BracketLegStatus.CANCELLED.value
    leg_ids = tuple(row.order_id for row in leg_rows if row.order_id)
    _emit(
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
    # Write-time guard: the dissolved bracket must round-trip through the read
    # codec the continuous monitor and scheduled invocations use (ALP-731).
    await _assert_bracket_readable(handle, bracket_id)


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
    _emit(
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
            pre_action_cost_basis=money(pre_basis),
            post_action_cost_basis=money(post_basis),
            signed_cash_impact_usd=signed_money(activity.signed_cash_impact_usd),
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
    timestamp: datetime,
    position_id: str,
) -> None:
    """Update cash_ledger by *signed_cash_impact_usd* and emit the log entry.

    Positive values credit cash (``CASH_CREDITED``); negative values debit it
    (``CASH_DEBITED``).  The *reason* must be the string value of an appropriate
    :class:`~alphamind.portfolio_state.events.activity_log.CashCreditReason` or
    :class:`~alphamind.portfolio_state.events.activity_log.CashDebitReason` member.

    *timestamp* anchors the activity-log entry at the CA's
    ``transaction_time`` so every CA-driven entry shares the same monotonic
    timestamp (the cash-ledger row's ``last_updated_at`` keeps wall-clock
    ``datetime.now(UTC)`` to match the fill-side row-stamp convention).
    *position_id* threads the originating position onto the cash entry so
    operators can filter the cash audit by position.

    Zero-magnitude movements (``abs <= _CASH_EPSILON``) short-circuit before
    any state mutation so zero-rate dividends do not emit spurious
    ``amount_usd=0`` entries.
    """
    if abs(signed_cash_impact_usd) <= _CASH_EPSILON:
        return
    cash_row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if cash_row is None:
        msg = "cash_ledger singleton missing — CA handler cannot apply cash movement"
        raise ValueError(msg)
    # ALP-462 — current_cash_usd is ``Numeric``/Decimal-backed; thread the
    # caller-supplied float impact through ``Decimal(str(...))`` so the cash
    # accumulator stays exact across CA-driven movements.
    cash_row.current_cash_usd = cash_row.current_cash_usd + Decimal(str(signed_cash_impact_usd))
    # ALP-778: settled tracks current (no T+2 lag modelled in paper trading).
    cash_row.settled_cash_usd = cash_row.current_cash_usd
    cash_row.last_updated_at = datetime.now(UTC).isoformat()
    new_balance_money = signed_money(cash_row.current_cash_usd)
    if signed_cash_impact_usd >= 0:
        _emit(
            handle,
            event_type=EventType.CASH_CREDITED,
            order_id=None,
            position_id=position_id,
            thesis_id=None,
            timestamp=timestamp,
            detail=CashCreditedDetail(
                amount_usd=money(abs(signed_cash_impact_usd)),
                reason=CashCreditReason(reason),
                new_balance_usd=new_balance_money,
            ),
        )
    else:
        _emit(
            handle,
            event_type=EventType.CASH_DEBITED,
            order_id=None,
            position_id=position_id,
            thesis_id=None,
            timestamp=timestamp,
            detail=CashDebitedDetail(
                amount_usd=money(abs(signed_cash_impact_usd)),
                reason=CashDebitReason(reason),
                new_balance_usd=new_balance_money,
            ),
        )


__all__ = [
    "_apply_signed_cash_movement",
    "_cancel_bracket_for_corporate_action",
    "_emit",
    "_emit_corporate_action_applied",
    "_persist_position_update",
]
