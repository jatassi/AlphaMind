"""Phase 2 command-execution write path (story 08 / ALP-366).

Two entry points the engine-stub ``submit_envelope`` MCP wrapper invokes
once an :class:`InvocationHandle` is supplied: ``persist_envelope_outcome``
for accepted envelopes and ``persist_envelope_parse_failure`` for Layer-1
parse failures (per ALP-353).

Both join the open ``InvocationContext`` transaction; the surrounding
context commits on clean exit and rolls back on exception.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy import select

from alphamind.decision.portfolio_manager.models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    OMSCommand,
    OpenCommand,
    PMEnvelope,
)
from alphamind.decision.portfolio_manager.validation import ValidationError as PMValidationError
from alphamind.execution.oms.submit_envelope_mcp import (
    FailedSubmissionEntry,
    SubmissionResult,
)
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
    stamp_phase_completion,
)
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.execution.state_persistence.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.execution.state_persistence.tables.orders import OrderRow
from alphamind.execution.state_persistence.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.execution.state_persistence.tables.orders_codec import (
    row_to_record as order_row_to_record,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.tables.theses import ThesisRow
from alphamind.execution.state_persistence.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    BracketDissolvedDetail,
    BracketModificationSource,
    BracketModifiedDetail,
    CapitalReleasedDetail,
    CapitalReservedDetail,
    CommandAbandonedDetail,
    EnvelopeParseFailedDetail,
    EventSource,
    EventType,
    GuardrailRejectionDetail,
    OrderCancelledDetail,
    OrderSubmittedDetail,
    PMDecisionDetail,
    PMVerdict,
    ThesisComponentAddedDetail,
    ThesisCreatedDetail,
    ThesisResolvedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegModification,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)

# Token sizing the engine-stub uses for OPEN/ADD: $1k = 1% of $100k portfolio.
# Matches ``alphamind.execution.oms.submit_envelope_mcp._build_constructive_request``.
_STUB_TOKEN_DOLLAR_VALUE = 1_000.0
_STUB_TOKEN_QUANTITY = 1.0
_STUB_FILL_PRICE = _STUB_TOKEN_DOLLAR_VALUE / _STUB_TOKEN_QUANTITY  # $1000 per share token
_STUB_STOP_PCT_BELOW_ENTRY = 0.05

# Verdict mapping from PMEnvelope literal to PMVerdict StrEnum the activity log uses.
_VERDICT_TO_PM_VERDICT: dict[str, PMVerdict] = {
    "approve": PMVerdict.APPROVE,
    "approve_with_modification": PMVerdict.APPROVE_WITH_MODIFICATION,
    "reject": PMVerdict.REJECT,
}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def persist_envelope_outcome(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist per-command writebacks + one ``pm_decision`` for an accepted envelope."""
    del config  # No knobs consumed at this story; signature is forward-shaped.

    accepted_command_ids: list[str] = []
    for result, command in zip(submission_results, envelope.commands, strict=True):
        if result.status != "accepted":
            continue
        await _dispatch_command_writeback(handle, command=command, result=result)
        accepted_command_ids.append(result.command_id)

    await _emit_pm_decision(handle, envelope=envelope, command_ids=tuple(accepted_command_ids))
    # Layer-1 parse rejections deliberately skip this — only an accepted
    # envelope's full writeback counts as a Phase 2 commit.
    await stamp_phase_completion(handle, column="phase2_completed_at")


async def persist_envelope_parse_failure(
    handle: InvocationHandle,
    failed_entry: FailedSubmissionEntry,
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist a Layer-1 parse failure as one ``envelope_parse_failed`` entry."""
    del config  # No knobs consumed at this story; signature is forward-shaped.

    detail = EnvelopeParseFailedDetail(
        attempted_envelope_id=str(failed_entry.raw_args.get("envelope_id", "ENV-REC-INVALID")),
        attempted_command_id=failed_entry.command_id,
        validation_error_repr=failed_entry.validation_error_repr,
        raw_args_json=json.dumps(failed_entry.raw_args),
    )
    await _emit(
        handle,
        event_type=EventType.ENVELOPE_PARSE_FAILED,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.COMMAND_EXECUTOR,
    )


async def persist_guardrail_rejection(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    errors: tuple[PMValidationError, ...],
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist a Layer-2/3 envelope-level rejection as one ``guardrail_rejection`` entry.

    Symmetric with ``persist_envelope_parse_failure`` — the envelope parsed
    cleanly at Layer-1 but failed an invariant or cross-command coherence
    check (per ``validate_pm_envelope``). The first error's message becomes
    ``suggested_modification``; every error's ``criterion`` (when set) feeds
    ``blocking_rule_ids`` so a feedback-loop query can correlate envelope
    rejections back to their failing rules.

    The envelope's ``position_id`` is *not* forwarded into the activity_log
    row's ``position_id`` column. A common Layer-3 rejection criterion is
    ``position_id_resolves`` — the envelope's ``position_id`` is precisely
    the orphan id that has no row in ``positions``, and the FK constraint
    on ``activity_log.position_id`` (DEFERRABLE INITIALLY DEFERRED, validated
    at COMMIT) would roll back the entire invocation transaction. The
    envelope id and any referenced position id are preserved verbatim in the
    JSON ``command_summary`` for operator forensics; an orphan position_id
    is unqueryable against ``positions`` anyway, which is what that column
    is for.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    if not errors:
        msg = "persist_guardrail_rejection requires at least one ValidationError"
        raise ValueError(msg)

    summary = f"envelope {envelope.envelope_id} failed Layer-2/3 validation"
    if envelope.position_id is not None:
        summary += f" (referenced position_id={envelope.position_id!r})"
    detail = GuardrailRejectionDetail(
        command_summary=summary,
        blocking_rule_ids=tuple(e.criterion for e in errors if e.criterion is not None),
        current_limit_values_json={},
        headroom_json={},
        suggested_modification=errors[0].message,
    )
    await _emit(
        handle,
        event_type=EventType.GUARDRAIL_REJECTION,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.GUARDRAIL_LAYER,
    )


async def persist_command_abandoned(
    handle: InvocationHandle,
    *,
    envelope_id: str,
    command_id: str,
    originating_agent: str,
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"],
    failure_reason: str,
    retry_attempt_count: int,
) -> None:
    """Emit one ``command_abandoned`` entry post-rollback of the failed command txn.

    The caller opens this on a fresh ``InvocationHandle`` *after* rolling back
    the per-command transaction so the audit trail survives the rollback per
    the design doc's contract.
    """
    detail = CommandAbandonedDetail(
        envelope_id=envelope_id,
        command_id=command_id,
        originating_agent=originating_agent,
        command_type=command_type,
        failure_reason=failure_reason,
        retry_attempt_count=retry_attempt_count,
    )
    await _emit(
        handle,
        event_type=EventType.COMMAND_ABANDONED,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.COMMAND_EXECUTOR,
    )


# ---------------------------------------------------------------------------
# Per-command-type dispatch
# ---------------------------------------------------------------------------


async def _dispatch_command_writeback(
    handle: InvocationHandle,
    *,
    command: OMSCommand,
    result: SubmissionResult,
) -> None:
    if isinstance(command, OpenCommand):
        await _writeback_open(handle, command=command, result=result)
        return
    if isinstance(command, CloseCommand):
        await _writeback_close(handle, command=command, result=result)
        return
    if isinstance(command, AdjustCommand):
        await _writeback_adjust(handle, command=command, result=result)
        return
    if isinstance(command, CancelCommand):
        await _writeback_cancel(handle, command=command, result=result)
        return
    if isinstance(command, AddCommand):
        await _writeback_add(handle, command=command, result=result)
        return
    msg = f"unsupported command variant: {type(command).__name__}"
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------
# OPEN
# ---------------------------------------------------------------------------


async def _writeback_open(
    handle: InvocationHandle,
    *,
    command: OpenCommand,
    result: SubmissionResult,
) -> None:
    """OPEN: insert position (PENDING), thesis (ACTIVE w/ components), bracket
    (PENDING_ENTRY), entry order, protective stop leg order. Reserve capital.
    Emit order_submitted, thesis_created, capital_reserved.
    """
    ticker = command.instrument.underlying
    timestamp = datetime.now(UTC)
    ids = _new_open_ids(ticker, command_id=result.command_id)

    position = _build_pending_position(
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        bracket_id=ids["bracket_id"],
        ticker=ticker,
    )
    thesis = _build_active_thesis(
        thesis_id=ids["thesis_id"],
        position_id=ids["position_id"],
        ticker=ticker,
        timestamp=timestamp,
    )
    bracket = _build_pending_bracket(
        bracket_id=ids["bracket_id"],
        position_id=ids["position_id"],
        ticker=ticker,
        entry_order_id=ids["entry_order_id"],
        stop_leg_order_id=ids["stop_leg_order_id"],
    )
    entry_order = _build_pending_entry_order(
        order_id=ids["entry_order_id"],
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        ticker=ticker,
        pm_command_id=result.command_id,
        timestamp=timestamp,
    )
    stop_leg_order = _build_pending_protective_order(
        order_id=ids["stop_leg_order_id"],
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        ticker=ticker,
        pm_command_id=result.command_id,
        timestamp=timestamp,
    )

    handle.session.add(position_record_to_row(position))
    parent_thesis_row, child_rows = thesis_record_to_rows(thesis)
    handle.session.add(parent_thesis_row)
    await handle.session.flush()
    for crow in child_rows:
        handle.session.add(crow)
    parent_bracket_row, leg_rows = bracket_record_to_rows(bracket)
    handle.session.add(parent_bracket_row)
    await handle.session.flush()
    for lrow in leg_rows:
        handle.session.add(lrow)
    handle.session.add(order_record_to_row(entry_order))
    handle.session.add(order_record_to_row(stop_leg_order))
    await handle.session.flush()

    await _reserve_capital(handle, amount_usd=_STUB_TOKEN_DOLLAR_VALUE)

    await _emit_order_submitted(
        handle,
        order=entry_order,
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )
    await _emit(
        handle,
        event_type=EventType.THESIS_CREATED,
        order_id=None,
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        timestamp=timestamp,
        detail=ThesisCreatedDetail(
            thesis_id=ids["thesis_id"],
            summary=thesis.summary,
        ),
    )
    await _emit_capital_reserved(
        handle,
        order_id=ids["entry_order_id"],
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        amount_usd=_STUB_TOKEN_DOLLAR_VALUE,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# CLOSE
# ---------------------------------------------------------------------------


async def _writeback_close(
    handle: InvocationHandle,
    *,
    command: CloseCommand,
    result: SubmissionResult,
) -> None:
    """CLOSE: insert close order (status PENDING). Emit order_submitted.

    The bracket-leg cancellation and position closure happen on the close
    fill in Phase 1 (per design doc: state transitions from fills happen in
    Phase 1).
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"CLOSE references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)

    close_order = _build_close_order(
        order_id=_close_order_id(command.position_id, result.command_id),
        position_id=command.position_id,
        bracket_id=position.bracket_id or "",
        thesis_id=position.thesis_id,
        ticker=_position_ticker(position),
        share_count=position.details.share_count
        if isinstance(position.details, EquityPositionDetails)
        else _STUB_TOKEN_QUANTITY,
        pm_command_id=result.command_id,
        timestamp=timestamp,
    )
    handle.session.add(order_record_to_row(close_order))
    await _emit_order_submitted(
        handle,
        order=close_order,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )


# ---------------------------------------------------------------------------
# ADJUST
# ---------------------------------------------------------------------------


async def _writeback_adjust(
    handle: InvocationHandle,
    *,
    command: AdjustCommand,
    result: SubmissionResult,
) -> None:
    """ADJUST: insert new protective leg order (PENDING), mark old protective
    orders CANCELLED, append bracket modification history. Emit
    order_cancelled + order_submitted + bracket_modified.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"ADJUST references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    if position.bracket_id is None:
        msg = f"ADJUST references position with no bracket: {command.position_id!r}"
        raise ValueError(msg)

    cancelled_orders = await _cancel_pending_protective_orders(
        handle,
        bracket_id=position.bracket_id,
        timestamp=timestamp,
    )
    for cancelled in cancelled_orders:
        await _emit_order_cancelled(
            handle,
            order=cancelled,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            cancel_reason="adjust_command",
            timestamp=timestamp,
        )

    new_order_id = _adjust_replacement_order_id(command.position_id, result.command_id)
    new_protective = _build_pending_protective_order(
        order_id=new_order_id,
        position_id=command.position_id,
        bracket_id=position.bracket_id,
        thesis_id=position.thesis_id,
        ticker=_position_ticker(position),
        pm_command_id=result.command_id,
        timestamp=timestamp,
    )
    handle.session.add(order_record_to_row(new_protective))
    await _emit_order_submitted(
        handle,
        order=new_protective,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )

    await _append_bracket_modification(
        handle,
        bracket_id=position.bracket_id,
        old_order_ids=tuple(o.order_id for o in cancelled_orders),
        new_order_id=new_order_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )
    await _emit(
        handle,
        event_type=EventType.BRACKET_MODIFIED,
        order_id=None,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        detail=BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed="protective_leg_order",
            old_value=",".join(o.order_id for o in cancelled_orders) or "<none>",
            new_value=new_order_id,
            rationale=f"PM adjust via {result.command_id}",
        ),
    )


# ---------------------------------------------------------------------------
# CANCEL
# ---------------------------------------------------------------------------


async def _writeback_cancel(
    handle: InvocationHandle,
    *,
    command: CancelCommand,
    result: SubmissionResult,
) -> None:
    """CANCEL: mark target order CANCELLED. If the target is an entry leg:
    cancel all bracket legs, resolve thesis CANCELLED, dissolve bracket,
    release reserved capital. Emit order_cancelled + capital_released +
    (entry case) thesis_resolved + bracket_dissolved.
    """
    del result  # Symmetric dispatch signature; CANCEL reads from command + DB.
    timestamp = datetime.now(UTC)
    target_row = await handle.session.get(OrderRow, command.order_id)
    if target_row is None:
        msg = f"CANCEL references missing order_id={command.order_id!r}"
        raise ValueError(msg)
    target = order_row_to_record(target_row)

    target_row.status = OrderStatus.CANCELLED.value
    target_row.last_update_timestamp = timestamp.isoformat()

    cancel_reason = command.cancel_reason or "pm_cancel"
    await _emit_order_cancelled(
        handle,
        order=target,
        position_id=target.position_id,
        thesis_id=target.originating_thesis_id,
        cancel_reason=cancel_reason,
        timestamp=timestamp,
    )
    await _release_capital(
        handle,
        order_id=target.order_id,
        position_id=target.position_id,
        thesis_id=target.originating_thesis_id,
        amount_usd=_STUB_TOKEN_DOLLAR_VALUE,
        timestamp=timestamp,
    )

    if target.role != OrderRole.ENTRY:
        return

    bracket_row = await handle.session.get(BracketRow, target.bracket_id)
    if bracket_row is None:
        return

    cancelled_legs = await _cancel_pending_protective_orders(
        handle, bracket_id=target.bracket_id, timestamp=timestamp
    )
    leg_order_ids = tuple(o.order_id for o in cancelled_legs)
    bracket_row.status = BracketStatus.DISSOLVED.value
    await _emit(
        handle,
        event_type=EventType.BRACKET_DISSOLVED,
        order_id=None,
        position_id=bracket_row.position_id,
        thesis_id=target.originating_thesis_id,
        timestamp=timestamp,
        detail=BracketDissolvedDetail(cancelled_leg_order_ids=leg_order_ids),
    )

    if target.originating_thesis_id is not None:
        await _resolve_thesis_cancelled(
            handle,
            thesis_id=target.originating_thesis_id,
            position_id=bracket_row.position_id,
            timestamp=timestamp,
        )


# ---------------------------------------------------------------------------
# ADD
# ---------------------------------------------------------------------------


async def _writeback_add(
    handle: InvocationHandle,
    *,
    command: AddCommand,
    result: SubmissionResult,
) -> None:
    """ADD: insert add-entry order (PENDING). Append a new thesis component.
    Reserve capital. Emit order_submitted + thesis_component_added +
    capital_reserved.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"ADD references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    bracket_id = position.bracket_id or ""
    ticker = command.instrument.underlying

    add_order_id = _add_order_id(command.position_id, result.command_id)
    add_order = _build_add_entry_order(
        order_id=add_order_id,
        position_id=command.position_id,
        bracket_id=bracket_id,
        thesis_id=position.thesis_id,
        ticker=ticker,
        pm_command_id=result.command_id,
        timestamp=timestamp,
    )
    handle.session.add(order_record_to_row(add_order))
    await _emit_order_submitted(
        handle,
        order=add_order,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )

    if position.thesis_id is not None:
        component_id = _new_component_id(position.thesis_id, result.command_id)
        await _emit(
            handle,
            event_type=EventType.THESIS_COMPONENT_ADDED,
            order_id=None,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            detail=ThesisComponentAddedDetail(
                component_id=component_id,
                component_type=ThesisComponentType.ENTRY_RATIONALE.value,
            ),
        )

    await _reserve_capital(handle, amount_usd=_STUB_TOKEN_DOLLAR_VALUE)
    await _emit_capital_reserved(
        handle,
        order_id=add_order_id,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        amount_usd=_STUB_TOKEN_DOLLAR_VALUE,
        timestamp=timestamp,
    )


# ---------------------------------------------------------------------------
# Per-envelope PM decision emission
# ---------------------------------------------------------------------------


async def _emit_pm_decision(
    handle: InvocationHandle,
    *,
    envelope: PMEnvelope,
    command_ids: tuple[str, ...],
) -> None:
    """Emit one ``pm_decision`` capturing the full envelope provenance."""
    detail = PMDecisionDetail(
        envelope_id=envelope.envelope_id,
        source_provenance_json={
            "source_provenance": envelope.source_provenance,
            "source_recommendation_id": envelope.source_recommendation_id,
            "recommendation_type": envelope.recommendation_type,
            "position_id": envelope.position_id,
        },
        evaluation_json=envelope.evaluation.model_dump(mode="json"),
        modifications_json=[m.model_dump(mode="json") for m in envelope.modifications],
        resulting_command_ids=command_ids,
        verdict=_VERDICT_TO_PM_VERDICT[envelope.verdict],
    )
    await _emit(
        handle,
        event_type=EventType.PM_DECISION,
        order_id=None,
        position_id=envelope.position_id,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Cash ledger primitives
# ---------------------------------------------------------------------------


async def _read_cash_row(handle: InvocationHandle) -> CashLedgerRow:
    row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if row is None:
        msg = "cash_ledger singleton missing — Phase 2 cannot reserve capital"
        raise ValueError(msg)
    return row


async def _reserve_capital(handle: InvocationHandle, *, amount_usd: float) -> None:
    cash_row = await _read_cash_row(handle)
    cash_row.reserved_capital_usd = cash_row.reserved_capital_usd + amount_usd
    cash_row.last_updated_at = datetime.now(UTC).isoformat()


async def _emit_capital_reserved(
    handle: InvocationHandle,
    *,
    order_id: str,
    position_id: str | None,
    thesis_id: str | None,
    amount_usd: float,
    timestamp: datetime,
) -> None:
    await _emit(
        handle,
        event_type=EventType.CAPITAL_RESERVED,
        order_id=order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=CapitalReservedDetail(order_id=order_id, amount_usd=amount_usd),
    )


async def _release_capital(
    handle: InvocationHandle,
    *,
    order_id: str,
    position_id: str | None,
    thesis_id: str | None,
    amount_usd: float,
    timestamp: datetime,
) -> None:
    cash_row = await _read_cash_row(handle)
    cash_row.reserved_capital_usd = max(cash_row.reserved_capital_usd - amount_usd, 0.0)
    cash_row.last_updated_at = datetime.now(UTC).isoformat()
    await _emit(
        handle,
        event_type=EventType.CAPITAL_RELEASED,
        order_id=order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=CapitalReleasedDetail(order_id=order_id, amount_usd=amount_usd),
    )


# ---------------------------------------------------------------------------
# Order helpers
# ---------------------------------------------------------------------------


# Roles whose pending orders ADJUST and the entry-CANCEL path leave alone.
# ADJUST replaces the protective legs but never touches an unfilled entry.
# Entry-CANCEL has just CANCELLED the entry itself before this sweep runs.
_NON_PROTECTIVE_ORDER_ROLES = frozenset({OrderRole.ENTRY.value, OrderRole.ADD_ENTRY.value})


async def _cancel_pending_protective_orders(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    timestamp: datetime,
    skip_roles: frozenset[str] = _NON_PROTECTIVE_ORDER_ROLES,
) -> tuple[OrderRecord, ...]:
    """Mark every PENDING protective order on the bracket CANCELLED in place.

    Returns the typed records so callers can use the order_ids and other
    fields for activity-log emission. Entry / add-entry roles are skipped:
    ADJUST never touches them, and the CANCEL entry-path has already
    cancelled the entry before this sweep runs.
    """
    stmt = (
        select(OrderRow)
        .where(OrderRow.bracket_id == bracket_id)
        .where(OrderRow.status == OrderStatus.PENDING.value)
    )
    rows = list((await handle.session.execute(stmt)).scalars())
    cancelled: list[OrderRecord] = []
    for row in rows:
        if row.order_role in skip_roles:
            continue
        row.status = OrderStatus.CANCELLED.value
        row.last_update_timestamp = timestamp.isoformat()
        cancelled.append(order_row_to_record(row))
    return tuple(cancelled)


async def _resolve_thesis_cancelled(
    handle: InvocationHandle,
    *,
    thesis_id: str,
    position_id: str,
    timestamp: datetime,
) -> None:
    thesis_row = await handle.session.get(ThesisRow, thesis_id)
    if thesis_row is None:
        return
    thesis_row.status = ThesisRecordStatus.CANCELLED.value
    thesis_row.resolution_timestamp = timestamp.isoformat().replace("+00:00", "Z")
    thesis_row.resolution_category = "CANCELLED_NEVER_ENTERED"
    await _emit(
        handle,
        event_type=EventType.THESIS_RESOLVED,
        order_id=None,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=ThesisResolvedDetail(
            resolution_category="CANCELLED_NEVER_ENTERED",
            component_outcomes_json={},
        ),
    )


_MODIFICATION_HISTORY_ADAPTER: TypeAdapter[tuple[BracketLegModification, ...]] = TypeAdapter(
    tuple[BracketLegModification, ...]
)


async def _append_bracket_modification(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    old_order_ids: tuple[str, ...],
    new_order_id: str,
    timestamp: datetime,
    pm_command_id: str,
) -> None:
    """Append one entry to the bracket's ``modification_history_json``.

    Reads + writes the JSON column directly rather than round-tripping the
    whole bracket through the codec — the leg rows aren't needed and the
    history vocabulary is the same one ``brackets_codec`` uses.
    """
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        return
    history = _MODIFICATION_HISTORY_ADAPTER.validate_json(bracket_row.modification_history_json)
    new_history = (
        *history,
        BracketLegModification(
            timestamp=timestamp,
            pm_command_id=pm_command_id,
            source=BracketModificationSource.PM.value,
            field_changed="protective_leg_order",
            old_value=",".join(old_order_ids) or "<none>",
            new_value=new_order_id,
            rationale="ADJUST command",
        ),
    )
    bracket_row.modification_history_json = _MODIFICATION_HISTORY_ADAPTER.dump_json(
        new_history
    ).decode()


# ---------------------------------------------------------------------------
# Order construction helpers
# ---------------------------------------------------------------------------


def _build_pending_entry_order(
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str,
    ticker: str,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    return _build_stub_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.ENTRY,
        order_class=OrderClass.BRACKET,
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        price_parameters=PriceParameters(),
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
    )


def _build_pending_protective_order(
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    stop_price = _STUB_FILL_PRICE * (1.0 - _STUB_STOP_PCT_BELOW_ENTRY)
    return _build_stub_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.PRICE_STOP,
        order_class=OrderClass.OTO,
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        price_parameters=PriceParameters(stop_trigger_price=stop_price),
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
    )


def _build_close_order(
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    share_count: float,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    return _build_stub_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.CLOSE,
        order_class=OrderClass.SIMPLE,
        direction=OrderDirection.SELL,
        order_type=OrderType.MARKET,
        price_parameters=PriceParameters(),
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        quantity=max(share_count, _STUB_TOKEN_QUANTITY),
    )


def _build_add_entry_order(
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    return _build_stub_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.ADD_ENTRY,
        order_class=OrderClass.SIMPLE,
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        price_parameters=PriceParameters(),
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
    )


def _build_stub_order(  # noqa: PLR0913 — captures every NOT-NULL OrderRecord field once.
    *,
    order_id: str,
    position_id: str | None,
    bracket_id: str,
    role: OrderRole,
    order_class: OrderClass,
    direction: OrderDirection,
    order_type: OrderType,
    price_parameters: PriceParameters,
    ticker: str,
    pm_command_id: str,
    thesis_id: str | None,
    timestamp: datetime,
    quantity: float = _STUB_TOKEN_QUANTITY,
) -> OrderRecord:
    return OrderRecord(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=role,
        instrument_spec=EquityInstrumentSpec(ticker=ticker),
        direction=direction,
        order_type=order_type,
        order_class=order_class,
        price_parameters=price_parameters,
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=f"alp-{order_id}",
        alpaca_order_id_chain=(f"alp-{order_id}",),
        submission_timestamp=timestamp,
        last_update_timestamp=timestamp,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=0,
        originating_thesis_id=thesis_id,
        originating_pm_command_id=pm_command_id,
        age_hours=0.0,
    )


# ---------------------------------------------------------------------------
# Position / thesis / bracket builders
# ---------------------------------------------------------------------------


def _build_pending_position(
    *,
    position_id: str,
    thesis_id: str,
    bracket_id: str,
    ticker: str,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=ticker,
        share_count=0.0,
        average_cost_basis_per_share=0.0,
    )
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.PENDING,
            "direction": Direction.LONG,
            "entry_timestamp": None,
            "details": details,
            "execution_history": (),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _build_active_thesis(
    *,
    thesis_id: str,
    position_id: str,
    ticker: str,
    timestamp: datetime,
) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=ticker,
            narrative=f"Stub {ct.value} narrative for {ticker}",
            key_assumptions=(KeyAssumption(text=f"{ticker} stub assumption", outcome=None),),
            generation_timestamp=timestamp,
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    time_expectation_hours = 24.0
    return ThesisRecord(
        thesis_id=thesis_id,
        position_id=position_id,
        summary=f"Stub thesis for {ticker}",
        key_catalyst="stub-catalyst",
        position_size_rationale=None,
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=timestamp,
        time_expectation_hours=time_expectation_hours,
        age_hours=0.0,
        expected_resolution_at=timestamp + timedelta(hours=time_expectation_hours),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _build_pending_bracket(
    *,
    bracket_id: str,
    position_id: str,
    ticker: str,
    entry_order_id: str,
    stop_leg_order_id: str,
) -> BracketRecord:
    stop_price = _STUB_FILL_PRICE * (1.0 - _STUB_STOP_PCT_BELOW_ENTRY)
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=stop_leg_order_id,
        trigger=PriceTrigger(underlying_ticker=ticker, threshold_usd=stop_price, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=entry_order_id,
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


# ---------------------------------------------------------------------------
# ID minting + helpers
# ---------------------------------------------------------------------------


def _new_open_ids(ticker: str, *, command_id: str) -> dict[str, str]:
    suffix = _short_token(command_id)
    return {
        "position_id": f"POS-{ticker}-{suffix}",
        "thesis_id": f"THE-{ticker}-{suffix}",
        "bracket_id": f"BRK-{ticker}-{suffix}",
        "entry_order_id": f"ORD-{ticker}-entry-{suffix}",
        "stop_leg_order_id": f"ORD-{ticker}-stop-{suffix}",
    }


def _close_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-CLOSE-{position_id}-{_short_token(command_id)}"


def _adjust_replacement_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADJUST-{position_id}-{_short_token(command_id)}"


def _add_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADD-{position_id}-{_short_token(command_id)}"


def _new_component_id(thesis_id: str, command_id: str) -> str:
    return f"{thesis_id}-add-{_short_token(command_id)}"


def _short_token(command_id: str) -> str:
    """Stable suffix derived from the synthetic command id.

    Uses the full 32-hex UUID — collision-resistant under any realistic
    invocation volume; truncation buys nothing operationally and risks
    overlap as the corpus grows.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, command_id).hex


def _position_ticker(position: PositionRecord) -> str:
    if isinstance(position.details, EquityPositionDetails):
        return position.details.ticker
    return getattr(position.details, "underlying_ticker", "STUB")


# ---------------------------------------------------------------------------
# Activity-log emission helpers
# ---------------------------------------------------------------------------


async def _emit_order_submitted(
    handle: InvocationHandle,
    *,
    order: OrderRecord,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    pm_command_id: str,
) -> None:
    parameters = {
        "order_id": order.order_id,
        "role": order.role.value,
        "direction": order.direction.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "instrument_ticker": getattr(order.instrument_spec, "ticker", ""),
    }
    await _emit(
        handle,
        event_type=EventType.ORDER_SUBMITTED,
        order_id=order.order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=OrderSubmittedDetail(
            order_parameters_json=parameters,
            pm_command_id=pm_command_id,
        ),
    )


async def _emit_order_cancelled(
    handle: InvocationHandle,
    *,
    order: OrderRecord,
    position_id: str | None,
    thesis_id: str | None,
    cancel_reason: str,
    timestamp: datetime,
) -> None:
    await _emit(
        handle,
        event_type=EventType.ORDER_CANCELLED,
        order_id=order.order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        detail=OrderCancelledDetail(
            cancel_reason=cancel_reason,
            filled_quantity_at_cancellation=int(order.filled_quantity),
        ),
    )


async def _emit(
    handle: InvocationHandle,
    *,
    event_type: EventType,
    order_id: str | None,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
) -> None:
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


__all__ = [
    "persist_command_abandoned",
    "persist_envelope_outcome",
    "persist_envelope_parse_failure",
    "persist_guardrail_rejection",
]
