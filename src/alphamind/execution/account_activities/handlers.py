"""Option-lifecycle handlers — the imperative shell of the poll (ALP-846 / W1b).

Each handler loads the open option ``PositionRow`` the lifecycle event refers to
(matched by OCC symbol), runs the pure booking math
(:mod:`alphamind.execution.account_activities.booking`), and persists the
result: append the activity to ``broker_event_log`` (idempotent on
``event_key``) **carrying its realized-PnL delta on the payload**, close the
option (no ``OPEN/0`` husk), and — for an assignment / exercise — open the
resulting equity position at the strike with the option's thesis link (ADR-0002:
the position→thesis Intent edge, never a parsed ``client_order_id``).

The realized PnL is booked onto the **event log**, not directly into
``thesis_pnl_ledger`` — per-thesis PnL is a *derived view* of the log, written
solely by the 03c derivation
(:func:`alphamind.execution.write_paths.thesis_pnl_ledger.rederive_thesis_pnl_ledger`,
ADR-0005 single writer). So the handler stamps the realized-PnL delta
(``realized_pnl_delta_usd``) onto the ``OPEXP`` / ``OPASN`` / ``OPEXC`` payload
and the opened-equity cost basis (``cost_basis_delta_usd``) onto the paired
``OPTRD`` payload; the derivation aggregates them with the fills into one
coherent ledger (no double-count, invariant 3).

The position/thesis link is resolved BEFORE the event-log append, so every
event row — INCLUDING the paired ``OPTRD`` priced-equity leg — carries the
resolved ``thesis_id`` / ``position_id`` in its INITIAL insert (no read-then-write
backfill race). The 03c derivation reads the link off every row.

All writes join the caller's open ``InvocationHandle`` transaction so the
event-log append and the position transitions commit atomically (single-writer =
pipeline, ADR-0005).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from alphamind._kernel.ids import InvocationId, PositionId, ThesisId
from alphamind._kernel.money import Money
from alphamind.execution.account_activities.booking import (
    book_assignment_or_exercise,
    book_expiry,
)
from alphamind.execution.account_activities.records import (
    BookingResult,
    LifecycleActivityType,
    LifecycleEvent,
)
from alphamind.execution.corporate_actions.handlers._shared import _persist_position_update
from alphamind.execution.write_paths.broker_event_persistence import append_broker_event
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    InstrumentType,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
    alpaca_occ_symbol,
)
from alphamind.state.invocation_context.activity_log import append_activity_log_entry
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.records_broker_event_log import (
    BrokerEventRecord,
    BrokerEventType,
    serialize_event_payload,
)
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import record_to_row, row_to_record
from alphamind.state.tables.theses import ThesisRow


def event_key_for(activity_id: str) -> str:
    """Stable ``broker_event_log`` idempotency key for an account activity.

    The activity id is broker-unique, so ``activity:{id}`` is the natural
    ``event_key`` — a re-poll of the same activity collapses to the one row
    (the idempotency guarantee ADR-0002's gap-free log relies on).
    """
    return f"activity:{activity_id}"


async def _append_lifecycle_event(
    handle: InvocationHandle,
    *,
    event_key: str,
    event_type: BrokerEventType,
    position_id: PositionId | None,
    thesis_id: str | None,
    raw_payload: dict[str, object],
    broker_timestamp: datetime | None,
) -> bool:
    """Append one ``broker_event_log`` row carrying its resolved link; return newness.

    Delegates to the canonical append-only helper
    (:func:`alphamind.execution.write_paths.broker_event_persistence.append_broker_event`),
    which is an atomic ``INSERT … ON CONFLICT DO NOTHING`` on the ``event_key``
    PK — no non-atomic read-then-write existence check. The resolved
    ``thesis_id`` / ``position_id`` ride the INITIAL insert (the position is
    resolved before this call), so the row never lands with a NULL link to be
    backfilled later. Returns ``True`` when newly inserted so the caller books
    realized PnL exactly once.
    """
    record = BrokerEventRecord(
        event_key=event_key,
        event_type=event_type,
        thesis_id=ThesisId(thesis_id) if thesis_id is not None else None,
        invocation_id=InvocationId(handle.invocation_id),
        position_id=position_id,
        raw_payload_json=serialize_event_payload(raw_payload),
        broker_timestamp=broker_timestamp,
        captured_at=datetime.now(UTC),
    )
    return await append_broker_event(handle.session, record)


async def _find_open_option_position(
    handle: InvocationHandle, occ_symbol: str
) -> tuple[PositionRow, PositionRecord] | None:
    """Find the OPEN option ``PositionRow`` whose OCC symbol equals *occ_symbol*.

    The ``instrument_type`` discriminator narrows the query to OPTIONS rows in
    SQL, so the scan never loads (and decodes ``details_json`` for) every open
    equity / strategy position just to skip it in Python (F5). The OCC match
    still runs in Python — ``alpaca_occ_symbol`` is derived from the decoded
    option details, not a stored column.
    """
    stmt = select(PositionRow).where(
        PositionRow.status.in_((PositionStatus.OPEN.value, PositionStatus.PENDING.value)),
        PositionRow.instrument_type == InstrumentType.OPTIONS.value,
    )
    rows = (await handle.session.execute(stmt)).scalars().all()
    for row in rows:
        record = row_to_record(row)
        details = record.details
        if isinstance(details, OptionsPositionDetails) and (
            alpaca_occ_symbol(details) == occ_symbol
        ):
            return row, record
    return None


async def _persist_booking(
    handle: InvocationHandle,
    *,
    option_row: PositionRow,
    result: BookingResult,
) -> None:
    """Persist a booking result: close the option and open the resulting equity.

    The realized PnL is **not** written to ``thesis_pnl_ledger`` here — it rides
    the event-log payload (stamped at append time) and the 03c derivation
    aggregates it into the ledger as the single writer (ADR-0005, invariant 3).
    """
    _persist_position_update(option_row, result.closed_option)
    if result.opened_equity is not None:
        handle.session.add(record_to_row(result.opened_equity))
    await handle.session.flush()


def _emit_position_closed(
    handle: InvocationHandle,
    *,
    option_record: PositionRecord,
    exit_method: PositionExitMethod,
    realized_pnl_usd: Money,
    timestamp: datetime,
) -> None:
    """Append one POSITION_CLOSED activity-log entry for the closed option.

    The closed-position thesis resolver reads the exit method (and, for an
    expiry, resolves the thesis) off this entry — without it the option-close
    path leaves the thesis ``ACTIVE`` indefinitely. Built inline (mirroring the
    resolver's ``_emit_thesis_resolved``) rather than via the corporate-actions
    private ``_emit``. ``exit_price`` is the sanctioned 0 placeholder (no market
    sale — the option expired worthless or converted at strike), and
    ``thesis_resolution_category`` is the empty non-load-bearing hint (the
    resolver computes the real category) — both matching the equity
    ``fill_collection`` close convention.
    """
    entry = ActivityLogEntry(
        entry_id=f"{handle.invocation_id}-{EventType.POSITION_CLOSED.value}-{uuid.uuid4().hex}",
        invocation_id=handle.invocation_id,
        timestamp=timestamp,
        event_type=EventType.POSITION_CLOSED,
        event_group=EVENT_TYPE_TO_GROUP[EventType.POSITION_CLOSED],
        position_id=str(option_record.position_id),
        order_id=None,
        thesis_id=str(option_record.thesis_id) if option_record.thesis_id is not None else None,
        source=EventSource.ACCOUNT_ACTIVITIES_PROCESSOR,
        detail=PositionClosedDetail(
            exit_method=exit_method,
            exit_price=Money(Decimal(0)),
            realized_pnl_usd=realized_pnl_usd,
            thesis_resolution_category="",
        ),
    )
    append_activity_log_entry(handle, entry)


async def _already_booked(handle: InvocationHandle, event_key: str) -> bool:
    """Whether *event_key* is already in ``broker_event_log`` (a re-poll no-op).

    A re-poll arrives after the first booking closed the option, so the open
    position no longer exists to re-resolve — short-circuit on the durable
    event-log row before position resolution. This is an idempotency guard, not
    a link backfill: the link still rides the INITIAL insert on the first poll.
    """
    return await handle.session.get(BrokerEventLogRow, event_key) is not None


async def handle_expiry(handle: InvocationHandle, event: LifecycleEvent) -> None:
    """Book an OTM expiry (``OPEXP``): realized PnL = -premium; close the option.

    Crash-idempotent the same way the assignment path is: the booking is gated on
    the option still being OPEN, NOT on the event-log append's ``newly`` (AC1). If
    an OPEXP row ever co-exists with an OPEN option (a partial-commit / crash that
    committed the row but not the booking), a ``newly``-gated booking would skip
    the close forever. Resolving the option first makes the path self-healing:
    book while OPEN, clean no-op once closed, surface only when genuinely unknown.
    """
    found = await _find_open_option_position(handle, event.occ_symbol)
    if found is None:
        # No OPEN/PENDING option to re-resolve. Either (a) a clean post-booking
        # re-poll — the prior invocation closed the option and durably committed
        # the OPEXP row, so this is a no-op; or (b) a genuinely unknown event. The
        # durable OPEXP row distinguishes them: present → (a) no-op; absent → (b).
        if await _already_booked(handle, event_key_for(event.activity_id)):
            return
        msg = (
            f"OPEXP {event.activity_id!r} references option {event.occ_symbol!r} "
            f"with no matching OPEN/PENDING local position"
        )
        raise ValueError(msg)
    option_row, option_record = found
    # Compute the booking first (pure) so the realized-PnL delta can ride the
    # event-log payload — the 03c derivation reproduces it from the log alone.
    result = book_expiry(option_record)
    # Append the OPEXP row (idempotent on event_key — a crash-committed row from a
    # prior poll collapses to the one row), then book: the option is still OPEN so
    # the booking runs exactly once. Once it closes the option a later re-poll
    # resolves ``found is None`` above and never reaches here.
    await _append_lifecycle_event(
        handle,
        event_key=event_key_for(event.activity_id),
        event_type=BrokerEventType.OPEXP,
        position_id=option_record.position_id,
        thesis_id=option_record.thesis_id,
        raw_payload={
            "activity_id": event.activity_id,
            "occ_symbol": event.occ_symbol,
            "realized_pnl_delta_usd": str(result.realized_pnl_usd),
            # The closed contract count lets the 03c fold release the option lot
            # the buy FILL opened — without it the expired option stays "held"
            # (phantom cost basis).
            "closed_contract_qty": event.qty,
        },
        broker_timestamp=event.transaction_time,
    )
    await _persist_booking(handle, option_row=option_row, result=result)
    # Emit POSITION_CLOSED so the closed-position thesis resolver can resolve the
    # thesis at option-close (the option is the entire trade for an OTM expiry).
    # The emit rides the booking path — a later re-poll resolves ``found is None``
    # above and never reaches here, so the entry lands exactly once.
    _emit_position_closed(
        handle,
        option_record=option_record,
        exit_method=PositionExitMethod.OPTION_EXPIRY,
        realized_pnl_usd=result.realized_pnl_usd,
        timestamp=event.transaction_time,
    )


async def handle_assignment_or_exercise(
    handle: InvocationHandle,
    event: LifecycleEvent,
    *,
    borrow_cost_resolver: Callable[[str], float | None],
) -> None:
    """Book an assignment / exercise: -premium on the option + open the equity leg.

    ``borrow_cost_resolver`` (the single invocation-scoped resolver the
    orchestrator builds for the fill-collection write unit) stamps the four short-only
    fields when the delivery opens a SHORT equity leg — see
    :func:`alphamind.execution.account_activities.booking._opened_equity_from_trade`.
    """
    # Surface BEFORE any write: a missing paired OPTRD leaves the equity leg
    # underspecified (parent ALP-842 surfacing condition). Raising here, before
    # appending the event row, keeps a surfaced case from committing partial state.
    if event.paired_trade is None:
        msg = (
            f"{event.activity_type.value} {event.activity_id!r} on {event.occ_symbol!r} "
            f"has no paired OPTRD activity; the equity leg is underspecified — "
            f"cannot book the assignment/exercise"
        )
        raise ValueError(msg)

    # Resolve the position/thesis link BEFORE appending either event row, so the
    # OPASN/OPEXC row AND its paired OPTRD row both carry the resolved
    # attribution in their initial insert (the OPTRD row must not be left NULL —
    # 03c's per-thesis PnL join reads it).
    found = await _find_open_option_position(handle, event.occ_symbol)
    if found is None:
        # No OPEN/PENDING option to re-resolve. Two cases: (a) a clean post-booking
        # re-poll — the prior invocation closed the option and durably committed
        # BOTH event rows, so this is a no-op; or (b) a genuinely unknown event —
        # the option never existed. The durable OPASN/OPEXC row distinguishes them:
        # present → (a) no-op; absent → (b) surface. (The crash window the OPTRD
        # re-append repairs leaves the option OPEN — the booking never committed —
        # so it lands in the ``found is not None`` branch below.)
        if await _already_booked(handle, event_key_for(event.activity_id)):
            return
        msg = (
            f"{event.activity_type.value} {event.activity_id!r} references option "
            f"{event.occ_symbol!r} with no matching OPEN/PENDING local position"
        )
        raise ValueError(msg)
    option_row, option_record = found

    # Compute the booking first (pure) so the realized-PnL delta and the
    # opened-equity cost basis can ride the OPASN/OPEXC and OPTRD payloads — the
    # 03c derivation reproduces both from the log alone (invariant 3).
    equity_position_id = PositionId(f"pos-eq-{event.activity_id}")
    result = book_assignment_or_exercise(
        option_record,
        event,
        equity_position_id=equity_position_id,
        borrow_cost_resolver=borrow_cost_resolver,
    )

    event_type = (
        BrokerEventType.OPASN
        if event.activity_type is LifecycleActivityType.OPASN
        else BrokerEventType.OPEXC
    )
    # Append BOTH event rows on every invocation, independent of the booking
    # guard. Each is idempotent (``append_broker_event`` is INSERT … ON CONFLICT
    # DO NOTHING), so a retry after a crash that committed the OPASN/OPEXC row but
    # not its paired OPTRD re-appends the missing OPTRD — its ``cost_basis_delta_usd``
    # would otherwise be lost forever and the 03c fold would understate the
    # assigned equity's cost basis. The booking (``_persist_booking``) runs once,
    # gated by the option still being OPEN: once it closes the option a later
    # re-poll resolves to ``found is None`` above and never reaches this branch.
    await _append_lifecycle_event(
        handle,
        event_key=event_key_for(event.activity_id),
        event_type=event_type,
        position_id=option_record.position_id,
        thesis_id=option_record.thesis_id,
        raw_payload={
            "activity_id": event.activity_id,
            "occ_symbol": event.occ_symbol,
            "realized_pnl_delta_usd": str(result.realized_pnl_usd),
            # The closed contract count lets the 03c fold release the option lot
            # the buy FILL opened, so the assigned/exercised option does not
            # linger in the equity leg's cost basis.
            "closed_contract_qty": event.qty,
        },
        broker_timestamp=event.transaction_time,
    )
    # The paired OPTRD is the second event-log row (the priced equity leg). It
    # carries the SAME resolved thesis/position link as the OPASN/OPEXC row, plus
    # the opened-equity cost basis (qty x strike), the equity share count, AND the
    # equity delivery direction (``equity_side`` — the broker's buy/sell) so the
    # derivation opens the equity lot at the strike on the SIGNED side: a long
    # delivery (+N) a later sell closes, a short-call assignment (-N) a later
    # buy-to-cover closes. Without the side the fold always opened +N and a short
    # cover mis-classified as opening (no realized PnL). The option PnL is not
    # double-counted (it rides the OPASN/OPEXC row as -premium).
    await _append_lifecycle_event(
        handle,
        event_key=event_key_for(event.paired_trade.activity_id),
        event_type=BrokerEventType.OPTRD,
        position_id=option_record.position_id,
        thesis_id=option_record.thesis_id,
        raw_payload={
            "activity_id": event.paired_trade.activity_id,
            "equity_symbol": event.paired_trade.equity_symbol,
            "cost_basis_delta_usd": str(_equity_cost_basis(result)),
            "equity_qty": event.paired_trade.qty,
            "equity_side": event.paired_trade.side,
        },
        broker_timestamp=event.transaction_time,
    )
    await _persist_booking(handle, option_row=option_row, result=result)


def _equity_cost_basis(result: BookingResult) -> Money:
    """Cost basis (qty x strike) of the equity leg an assignment / exercise opened.

    The paired ``OPTRD`` event carries this so the 03c derivation folds the
    strike economics into the thesis's cost basis from the log alone. Zero when
    the booking opened no equity leg (it always does for an assignment / exercise).
    """
    equity = result.opened_equity
    if equity is None or not isinstance(equity.details, EquityPositionDetails):
        return Money(Decimal(0))
    details = equity.details
    return Money(
        Decimal(str(details.share_count)) * Decimal(str(details.average_cost_basis_per_share))
    )


__all__ = [
    "event_key_for",
    "handle_assignment_or_exercise",
    "handle_expiry",
]
