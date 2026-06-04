"""Option-lifecycle handlers — the imperative shell of the poll (ALP-846 / W1b).

Each handler loads the open option ``PositionRow`` the lifecycle event refers to
(matched by OCC symbol), runs the pure booking math
(:mod:`alphamind.execution.account_activities.booking`), and persists the
result: append the activity to ``broker_event_log`` (idempotent on
``event_key``), book the realized PnL into ``thesis_pnl_ledger``, close the
option (no ``OPEN/0`` husk), and — for an assignment / exercise — open the
resulting equity position at the strike with the option's thesis link (ADR-0002:
the position→thesis Intent edge, never a parsed ``client_order_id``).

All writes join the caller's open ``InvocationHandle`` transaction so the
event-log append, the ledger booking, and the position transitions commit
atomically (single-writer = pipeline, ADR-0005).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from alphamind._kernel.ids import PositionId
from alphamind._kernel.money import Money, money, signed_money
from alphamind.execution.account_activities.booking import (
    book_assignment_or_exercise,
    book_expiry,
)
from alphamind.execution.account_activities.records import (
    BookingResult,
    LifecycleActivityType,
    LifecycleEvent,
)
from alphamind.portfolio_state.records.positions import (
    OptionContractType,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.records_broker_event_log import BrokerEventType
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import record_to_row, row_to_record
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow


def event_key_for(activity_id: str) -> str:
    """Stable ``broker_event_log`` idempotency key for an account activity.

    The activity id is broker-unique, so ``activity:{id}`` is the natural
    ``event_key`` — a re-poll of the same activity collapses to the one row
    (the idempotency guarantee ADR-0002's gap-free log relies on).
    """
    return f"activity:{activity_id}"


def _occ_symbol_for_option(details: OptionsPositionDetails) -> str:
    """Bare OCC symbol Alpaca keys an options position by (no ``O:`` prefix).

    Mirrors
    :func:`alphamind.execution.corporate_actions.reconciliation._alpaca_occ_symbol`
    so the lifecycle event's ``occ_symbol`` matches a local option position.
    """
    underlying = details.underlying_ticker.replace(".", "")
    expiry = details.expiration_date.strftime("%y%m%d")
    cp = "C" if details.contract_type is OptionContractType.CALL else "P"
    strike_milli = round(details.strike_price * 1000)
    return f"{underlying}{expiry}{cp}{strike_milli:08d}"


async def _append_event_idempotent(
    handle: InvocationHandle,
    *,
    event_key: str,
    event_type: BrokerEventType,
    position_id: str | None,
    thesis_id: str | None,
    raw_payload: dict[str, object],
    broker_timestamp: datetime | None,
) -> bool:
    """Append one ``broker_event_log`` row; return whether it was newly inserted.

    Idempotent on the ``event_key`` PRIMARY KEY: a re-poll of the same activity
    is a no-op and returns ``False`` so the caller does not double-book PnL.
    """
    existing = await handle.session.get(BrokerEventLogRow, event_key)
    if existing is not None:
        return False
    handle.session.add(
        BrokerEventLogRow(
            event_key=event_key,
            event_type=event_type.value,
            thesis_id=thesis_id,
            invocation_id=handle.invocation_id,
            position_id=position_id,
            raw_payload_json=json.dumps(raw_payload, default=str, sort_keys=True),
            broker_timestamp=broker_timestamp.isoformat() if broker_timestamp is not None else None,
            captured_at=datetime.now(UTC).isoformat(),
        )
    )
    await handle.session.flush()
    return True


async def _find_open_option_position(
    handle: InvocationHandle, occ_symbol: str
) -> tuple[PositionRow, PositionRecord] | None:
    """Find the OPEN option ``PositionRow`` whose OCC symbol equals *occ_symbol*."""
    stmt = select(PositionRow).where(
        PositionRow.status.in_(
            (PositionStatus.OPEN.value, PositionStatus.PENDING.value)
        )
    )
    rows = (await handle.session.execute(stmt)).scalars().all()
    for row in rows:
        record = row_to_record(row)
        details = record.details
        if isinstance(details, OptionsPositionDetails) and (
            _occ_symbol_for_option(details) == occ_symbol
        ):
            return row, record
    return None


async def _book_realized_pnl(
    handle: InvocationHandle,
    *,
    thesis_id: str,
    realized_pnl_usd: Money,
) -> None:
    """Accumulate *realized_pnl_usd* into the thesis's ``thesis_pnl_ledger`` row.

    Reads the existing entry (if any) and adds — the ledger is a per-thesis
    running total (single-writer = pipeline, ADR-0005). Cost basis is not
    touched here; the per-thesis cost-basis derivation is story 03c's concern.
    """
    row = await handle.session.get(ThesisPnlLedgerRow, thesis_id)
    now = datetime.now(UTC).isoformat()
    if row is None:
        handle.session.add(
            ThesisPnlLedgerRow(
                thesis_id=thesis_id,
                realized_pnl_usd=realized_pnl_usd,
                cost_basis_usd=money(0),
                provenance_json=json.dumps({"source": "account_activities"}),
                derived_from_invocation_id=handle.invocation_id,
                updated_at=now,
            )
        )
    else:
        row.realized_pnl_usd = signed_money(row.realized_pnl_usd + realized_pnl_usd)
        row.derived_from_invocation_id = handle.invocation_id
        row.updated_at = now
    await handle.session.flush()


def _persist_position_record(row: PositionRow, record: PositionRecord) -> None:
    """Project a mutated ``PositionRecord`` back onto its existing row."""
    new_row = record_to_row(record)
    row.status = new_row.status
    row.entry_timestamp = new_row.entry_timestamp
    row.details_json = new_row.details_json
    row.execution_history_json = new_row.execution_history_json
    row.realized_pnl_to_date_usd = new_row.realized_pnl_to_date_usd
    row.corporate_action_adjustment_needed = new_row.corporate_action_adjustment_needed


async def _persist_booking(
    handle: InvocationHandle,
    *,
    option_row: PositionRow,
    result: BookingResult,
) -> None:
    """Persist a booking result: close the option, open the equity, book PnL."""
    _persist_position_record(option_row, result.closed_option)
    if result.opened_equity is not None:
        handle.session.add(record_to_row(result.opened_equity))
    thesis_id = result.closed_option.thesis_id
    if thesis_id is not None:
        await _book_realized_pnl(
            handle, thesis_id=thesis_id, realized_pnl_usd=result.realized_pnl_usd
        )
    await handle.session.flush()


async def handle_expiry(handle: InvocationHandle, event: LifecycleEvent) -> None:
    """Book an OTM expiry (``OPEXP``): realized PnL = -premium; close the option."""
    newly = await _append_event_idempotent(
        handle,
        event_key=event_key_for(event.activity_id),
        event_type=BrokerEventType.OPEXP,
        position_id=None,
        thesis_id=None,
        raw_payload={"activity_id": event.activity_id, "occ_symbol": event.occ_symbol},
        broker_timestamp=event.transaction_time,
    )
    if not newly:
        return
    found = await _find_open_option_position(handle, event.occ_symbol)
    if found is None:
        msg = (
            f"OPEXP {event.activity_id!r} references option {event.occ_symbol!r} "
            f"with no matching OPEN/PENDING local position"
        )
        raise ValueError(msg)
    option_row, option_record = found
    await _backfill_event_link(handle, event.activity_id, option_record)
    result = book_expiry(option_record, event)
    await _persist_booking(handle, option_row=option_row, result=result)


async def handle_assignment_or_exercise(
    handle: InvocationHandle, event: LifecycleEvent
) -> None:
    """Book an assignment / exercise: -premium on the option + open the equity leg."""
    event_type = (
        BrokerEventType.OPASN
        if event.activity_type is LifecycleActivityType.OPASN
        else BrokerEventType.OPEXC
    )
    newly = await _append_event_idempotent(
        handle,
        event_key=event_key_for(event.activity_id),
        event_type=event_type,
        position_id=None,
        thesis_id=None,
        raw_payload={"activity_id": event.activity_id, "occ_symbol": event.occ_symbol},
        broker_timestamp=event.transaction_time,
    )
    # The paired OPTRD is the second event-log row (priced equity leg).
    if event.paired_trade is not None:
        await _append_event_idempotent(
            handle,
            event_key=event_key_for(event.paired_trade.activity_id),
            event_type=BrokerEventType.OPTRD,
            position_id=None,
            thesis_id=None,
            raw_payload={
                "activity_id": event.paired_trade.activity_id,
                "equity_symbol": event.paired_trade.equity_symbol,
            },
            broker_timestamp=event.transaction_time,
        )
    if not newly:
        return
    found = await _find_open_option_position(handle, event.occ_symbol)
    if found is None:
        msg = (
            f"{event.activity_type.value} {event.activity_id!r} references option "
            f"{event.occ_symbol!r} with no matching OPEN/PENDING local position"
        )
        raise ValueError(msg)
    option_row, option_record = found
    await _backfill_event_link(handle, event.activity_id, option_record)
    equity_position_id = PositionId(f"pos-eq-{event.activity_id}")
    result = book_assignment_or_exercise(
        option_record, event, equity_position_id=equity_position_id
    )
    await _persist_booking(handle, option_row=option_row, result=result)


async def _backfill_event_link(
    handle: InvocationHandle, activity_id: str, option: PositionRecord
) -> None:
    """Stamp the resolved position/thesis link onto the activity's event-log row.

    Lifecycle events carry no ``client_order_id`` (ADR-0002), so the link is
    resolved from the matched option position's thesis edge once the position is
    found, then written back onto the already-appended row.
    """
    row = await handle.session.get(BrokerEventLogRow, event_key_for(activity_id))
    if row is not None:
        row.position_id = option.position_id
        row.thesis_id = option.thesis_id


__all__ = [
    "event_key_for",
    "handle_assignment_or_exercise",
    "handle_expiry",
]
