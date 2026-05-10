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
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from pydantic import TypeAdapter
from sqlalchemy import select

from alphamind.decision.portfolio_manager.models import PMEnvelope
from alphamind.decision.portfolio_manager.validation import ValidationError as PMValidationError
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
from alphamind.execution.oms.command_models import (
    AddCommand,
    AdjustCommand,
    BracketAdjustment,
    BracketOrderParameters,
    BracketOrderType,
    CancelCommand,
    CloseCommand,
    EntryOrder,
    EntryOrderType,
    EquityInstrument,
    EventLeg,
    InvalidationLeg,
    NewStopLevel,
    NewTargetLevel,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    PriceLeg,
    StrategyInstrument,
    Target,
    TimeLeg,
)
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
    EnvelopeRejectionDetail,
    EventSource,
    EventType,
    OrderCancelledDetail,
    OrderSubmittedDetail,
    PMDecisionDetail,
    PMVerdict,
    ThesisComponentAddedDetail,
    ThesisComponentUpdatedDetail,
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
    EventTrigger,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
    TimeTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import Greeks

# Verdict mapping from PMEnvelope literal to PMVerdict StrEnum the activity log uses.
_VERDICT_TO_PM_VERDICT: dict[str, PMVerdict] = {
    "approve": PMVerdict.APPROVE,
    "approve_with_modification": PMVerdict.APPROVE_WITH_MODIFICATION,
    "reject": PMVerdict.REJECT,
}


def _instrument_ticker_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return the ticker/underlying key for a canonical OMS instrument.

    Equity instruments expose ``ticker``; option / strategy expose
    ``underlying``. Mirrors helpers in
    :mod:`alphamind.decision.portfolio_manager.validation` and
    :mod:`alphamind.execution.oms.submit_envelope_mcp`.
    """
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def persist_envelope_outcome(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    *,
    config: StatePersistenceConfig,
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None,
) -> None:
    """Persist per-command writebacks + one ``pm_decision`` for an accepted envelope.

    When ``dispatch_results`` is supplied (engine-stub coordinated swap,
    story 03e / ALP-390), each accepted command's writeback consumes the
    matching :class:`BrokerDispatchResult` so the persisted entry / close /
    add / adjust order carries Alpaca's real ``alpaca_order_id`` rather than
    the synthetic ``alp-{order_id}`` placeholder. ``None`` entries (legacy
    callers and per-command failures) fall back to the synthetic id.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    if dispatch_results is None:
        dispatch_results = tuple(None for _ in submission_results)

    accepted_command_ids: list[str] = []
    for result, command, dispatch in zip(
        submission_results, envelope.commands, dispatch_results, strict=True
    ):
        if result.status != "accepted":
            continue
        await _dispatch_command_writeback(
            handle, command=command, result=result, dispatch_result=dispatch
        )
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


async def persist_envelope_rejection(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    errors: tuple[PMValidationError, ...],
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist a Layer-2/3 envelope-level rejection as one ``envelope_rejected`` entry.

    Symmetric with ``persist_envelope_parse_failure`` — the envelope parsed
    cleanly at Layer-1 but failed an invariant or cross-command coherence
    check (per ``validate_pm_envelope``). Every error's ``criterion`` (when
    set) feeds ``blocking_criteria`` so a feedback-loop query can correlate
    envelope rejections back to their failing rules; the full error inventory
    serializes into ``validation_errors_json`` for operator forensics.

    The envelope's ``position_id`` is *not* forwarded into the activity_log
    row's ``position_id`` column. A common Layer-3 rejection criterion is
    ``position_id_resolves`` — the envelope's ``position_id`` is precisely
    the orphan id that has no row in ``positions``, and the FK constraint
    on ``activity_log.position_id`` (DEFERRABLE INITIALLY DEFERRED, validated
    at COMMIT) would roll back the entire invocation transaction. The
    envelope id and any referenced position id are preserved in the JSON
    detail's ``envelope_id`` and ``referenced_position_id`` fields for
    operator forensics; an orphan position_id is unqueryable against
    ``positions`` anyway, which is what that column is for.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    if not errors:
        msg = "persist_envelope_rejection requires at least one ValidationError"
        raise ValueError(msg)

    detail = EnvelopeRejectionDetail(
        envelope_id=envelope.envelope_id,
        referenced_position_id=envelope.position_id,
        attempted_command_count=len(envelope.commands),
        blocking_criteria=tuple(e.criterion for e in errors if e.criterion is not None),
        validation_errors_json=json.dumps([e.model_dump(mode="json") for e in errors]),
    )
    await _emit(
        handle,
        event_type=EventType.ENVELOPE_REJECTED,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.GUARDRAIL_LAYER,
    )


async def persist_engine_envelope_outcome(
    handle: InvocationHandle,
    *,
    close_command: CloseCommand,
    command_id: str,
    extra_metadata: dict[str, Any],
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """Persist the protective CLOSE for an engine-originated envelope.

    Sibling to :func:`persist_envelope_outcome` for the engine path
    (story 04 / ALP-375). Engine envelopes carry exactly one CLOSE command
    with no PM verdict — there is no ``pm_decision`` activity-log entry to
    emit. The function delegates to :func:`_writeback_close` with engine
    context threaded through ``extra_metadata`` (cascade_id,
    position_selection_rationale, rule_breached) and ``EventSource.BRACKET_MANAGER``
    as the activity-log source per ``oms-commands.md § Command origins``.

    When ``submitted_alpaca_order_id`` is supplied (engine-stub coordinated
    swap, story 03e / ALP-390), the persisted close order carries the broker's
    real Alpaca order id rather than the synthetic ``alp-{order_id}`` placeholder.

    The continuous monitor produced the envelope; the OMS owns persistence.
    The surrounding ``InvocationContext`` commits or rolls back atomically.
    """
    synthetic_result = SubmissionResult(
        command_ordinal=0,
        status="accepted",
        command_id=command_id,
        acknowledgment=None,
    )
    await _writeback_close(
        handle,
        command=close_command,
        result=synthetic_result,
        extra_metadata=extra_metadata,
        source=EventSource.BRACKET_MANAGER,
        submitted_alpaca_order_id=submitted_alpaca_order_id,
    )
    await stamp_phase_completion(handle, column="phase2_completed_at")


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
    dispatch_result: BrokerDispatchResult | None = None,
) -> None:
    submitted_id = dispatch_result.alpaca_order_id if dispatch_result is not None else None
    if isinstance(command, OpenCommand):
        await _writeback_open(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    if isinstance(command, CloseCommand):
        await _writeback_close(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    if isinstance(command, AdjustCommand):
        await _writeback_adjust(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    if isinstance(command, CancelCommand):
        await _writeback_cancel(handle, command=command, result=result)
        return
    if isinstance(command, AddCommand):
        await _writeback_add(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
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
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """OPEN: insert position (PENDING), thesis (ACTIVE w/ components), bracket
    (PENDING_ENTRY), entry order, take-profit + invalidation leg orders.
    Reserve capital. Emit order_submitted, thesis_created, capital_reserved.

    Every persisted field traces back to a canonical command field:

    * position quantity ← ``command.position_size.quantity`` (carried at
      ``share_count=0.0`` until the entry fills, but the entry order quantity
      is the command's quantity)
    * entry order parameters ← ``command.entry_order``
    * bracket protective legs ← ``command.invalidation_legs`` + ``command.target``
    * thesis summary + components ← ``command.thesis``
    * capital reservation amount ← ``command.position_size.dollar_value``

    When ``submitted_alpaca_order_id`` is supplied (engine-stub coordinated
    swap, story 03e / ALP-390), the persisted entry order carries the broker's
    real Alpaca order id; protective leg orders keep the synthetic
    ``alp-{order_id}`` placeholder until ``trade_updates`` ack each child leg.
    """
    ticker = _instrument_ticker_key(command.instrument)
    timestamp = datetime.now(UTC)
    ids = _new_open_ids(ticker, command_id=result.command_id)

    # Mint per-leg order ids. The bracket carries one TAKE_PROFIT leg from
    # ``command.target`` plus one leg per ``command.invalidation_legs`` entry
    # (PRICE_STOP / TIME_EXPIRATION / EVENT_INVALIDATION). Event legs have no
    # underlying broker order; price/time legs do.
    target_order_id = f"ORD-{ticker}-target-{_id_suffix(result.command_id)}"
    invalidation_leg_orders: list[tuple[InvalidationLeg, str | None]] = []
    for idx, wire_leg in enumerate(command.invalidation_legs):
        if isinstance(wire_leg, EventLeg):
            invalidation_leg_orders.append((wire_leg, None))
        else:
            invalidation_leg_orders.append(
                (wire_leg, f"ORD-{ticker}-inv{idx}-{_id_suffix(result.command_id)}")
            )

    validation_greeks: Greeks | None = None
    validation_iv: float | None = None
    if result.acknowledgment is not None and result.acknowledgment.validation_metadata is not None:
        validation_greeks = result.acknowledgment.validation_metadata.greeks
        validation_iv = result.acknowledgment.validation_metadata.implied_volatility
    position = _build_pending_position(
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        bracket_id=ids["bracket_id"],
        instrument=command.instrument,
        direction=_direction_from_instrument(command.instrument),
        validation_greeks=validation_greeks,
        validation_iv=validation_iv,
    )
    thesis = _build_active_thesis(
        thesis_id=ids["thesis_id"],
        position_id=ids["position_id"],
        thesis=command.thesis,
        timestamp=timestamp,
    )
    bracket = _build_pending_bracket(
        bracket_id=ids["bracket_id"],
        position_id=ids["position_id"],
        ticker=ticker,
        entry_order_id=ids["entry_order_id"],
        target=command.target,
        target_order_id=target_order_id,
        invalidation_leg_orders=tuple(invalidation_leg_orders),
    )
    entry_order = _build_entry_order_from_command(
        order_id=ids["entry_order_id"],
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        ticker=ticker,
        entry_order=command.entry_order,
        quantity=command.position_size.quantity,
        direction=_direction_from_instrument(command.instrument),
        pm_command_id=result.command_id,
        timestamp=timestamp,
        role=OrderRole.ENTRY,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    target_order = _build_take_profit_order(
        order_id=target_order_id,
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        ticker=ticker,
        target=command.target,
        quantity=command.position_size.quantity,
        direction=_direction_from_instrument(command.instrument),
        pm_command_id=result.command_id,
        timestamp=timestamp,
    )
    invalidation_orders: list[OrderRecord] = []
    for wire_leg, leg_order_id in invalidation_leg_orders:
        if leg_order_id is None or isinstance(wire_leg, EventLeg):
            continue
        invalidation_orders.append(
            _build_invalidation_leg_order(
                order_id=leg_order_id,
                position_id=ids["position_id"],
                bracket_id=ids["bracket_id"],
                thesis_id=ids["thesis_id"],
                ticker=ticker,
                wire_leg=wire_leg,
                quantity=command.position_size.quantity,
                direction=_direction_from_instrument(command.instrument),
                pm_command_id=result.command_id,
                timestamp=timestamp,
            )
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
    handle.session.add(order_record_to_row(target_order))
    for inv_order in invalidation_orders:
        handle.session.add(order_record_to_row(inv_order))
    await handle.session.flush()

    await _reserve_capital(handle, amount_usd=command.position_size.dollar_value)

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
        amount_usd=command.position_size.dollar_value,
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
    extra_metadata: dict[str, Any] | None = None,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """CLOSE: insert close order (status PENDING). Emit order_submitted.

    Reads canonical CloseCommand fields:

    * ``command.quantity`` — numeric (partial close) or ``"all"`` (full close).
      Numeric values become the close order's ``quantity``; ``"all"`` resolves
      to the position's full share count.
    * ``command.order_type`` — ``"market"`` or ``"limit"`` (mapped to
      :class:`OrderType`).
    * ``command.limit_price`` — populated for limit orders.
    * ``command.close_rationale_type`` — feeds the eventual thesis resolution
      category in Phase 1; recorded on the order via the originating
      pm_command_id linkage so post-fill processing can re-classify.

    The bracket-leg cancellation and position closure happen on the close
    fill in Phase 1 (per design doc: state transitions from fills happen in
    Phase 1).

    ``extra_metadata`` is folded into the ``order_parameters_json`` payload of
    the emitted ``order_submitted`` activity log entry — the engine-envelope
    submission path (story 04 / ALP-375) uses it to thread
    ``cascade_id`` / ``position_selection_rationale`` / ``rule_breached``
    through to the activity log alongside the existing rationale metadata.

    ``source`` overrides the default ``COMMAND_EXECUTOR`` event source — the
    engine-envelope path tags entries with ``BRACKET_MANAGER`` per
    ``oms-commands.md § Command origins`` and the ``EventSource`` enum docstring.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"CLOSE references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)

    # Resolve quantity from the union: numeric → that quantity; "all" → the
    # position's full share count.
    if command.quantity == "all":
        if isinstance(position.details, EquityPositionDetails):
            close_qty = position.details.share_count
        else:
            close_qty = getattr(position.details, "contract_count", 0.0)
    else:
        close_qty = float(command.quantity)
    # ``close_qty <= 0`` after the union resolution above can only occur on a
    # CLOSE-all against a PENDING (zero-fill) position — closing-before-fill
    # is a structural contract violation per oms-commands.md § Command origins.
    # Raise rather than fabricate a phantom 1-share order; mirrors the
    # missing-position branch above.
    if close_qty <= 0:
        msg = (
            f"CLOSE references PENDING position_id={command.position_id!r} "
            f"with no fills; cannot resolve close quantity. The PM must wait "
            "for the entry to fill before issuing a CLOSE."
        )
        raise ValueError(msg)

    if command.order_type == "limit":
        order_type = OrderType.LIMIT
        price_parameters = PriceParameters(limit_price=command.limit_price)
    else:
        order_type = OrderType.MARKET
        price_parameters = PriceParameters()

    close_order = _build_close_order(
        order_id=_close_order_id(command.position_id, result.command_id),
        position_id=command.position_id,
        bracket_id=position.bracket_id or "",
        thesis_id=position.thesis_id,
        ticker=_position_ticker(position),
        direction=position.direction,
        quantity=close_qty,
        order_type=order_type,
        price_parameters=price_parameters,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    handle.session.add(order_record_to_row(close_order))
    # CLOSE-specific rationale metadata: close_rationale_type + invalidation_reason
    # + risk_management_subtype feed the eventual thesis resolution in Phase 1.
    # We surface them on the order_submitted detail via the order_parameters_json
    # so the post-fill processor can read them without re-fetching the command.
    rationale_metadata: dict[str, Any] = {
        "close_rationale_type": command.close_rationale_type,
        "invalidation_reason": command.invalidation_reason,
        "risk_management_subtype": command.risk_management_subtype,
        "requested_quantity": ("all" if command.quantity == "all" else float(command.quantity)),
    }
    if extra_metadata:
        rationale_metadata.update(extra_metadata)
    await _emit_order_submitted(
        handle,
        order=close_order,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
        extra_parameters=rationale_metadata,
        source=source,
    )


# ---------------------------------------------------------------------------
# ADJUST
# ---------------------------------------------------------------------------


_ADJUST_FIELD_LABELS: dict[str, str] = {
    "new_stop_level": "stop_level",
    "new_target_level": "target_level",
    "new_time_expiration": "time_expiration",
    "new_event_invalidation": "event_invalidation",
    "thesis_component_updates": "thesis_components",
}


def _adjust_field_set(command: AdjustCommand) -> str:
    """Return a deterministic label for whichever change-field(s) are set.

    Multi-field adjustments concatenate the labels separated by ``+`` so a
    BracketModifiedDetail.field_changed value still pinpoints what moved.
    """
    set_labels = [
        label for attr, label in _ADJUST_FIELD_LABELS.items() if getattr(command, attr) is not None
    ]
    return "+".join(set_labels) if set_labels else "no_op"


async def _writeback_adjust(
    handle: InvocationHandle,
    *,
    command: AdjustCommand,
    result: SubmissionResult,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """ADJUST: insert new protective leg order(s) and/or thesis component
    updates per the change-field(s) set on the canonical command.

    Dispatches on whichever of ``new_stop_level``, ``new_target_level``,
    ``new_time_expiration``, ``new_event_invalidation``,
    ``thesis_component_updates`` is set. For each price/time/target leg
    change, the existing protective order is CANCELLED and a new PENDING
    replacement is inserted. For thesis_component_updates, no order
    mutations occur — only the activity-log emit captures the change.
    ``BracketModifiedDetail.rationale`` is populated from
    ``command.adjustment_rationale``.
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
        target_roles=_protective_roles_for_change_fields(
            new_stop_level=command.new_stop_level,
            new_target_level=command.new_target_level,
            new_time_expiration_present=command.new_time_expiration is not None,
        ),
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

    new_protective = _build_replacement_protective_order(
        command=command,
        position=position,
        result=result,
        timestamp=timestamp,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    new_order_id: str
    if new_protective is not None:
        new_order_id = new_protective.order_id
        handle.session.add(order_record_to_row(new_protective))
        await _emit_order_submitted(
            handle,
            order=new_protective,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            pm_command_id=result.command_id,
        )
    else:
        # Thesis-only adjustments produce no protective replacement; the
        # bracket modification history still records the adjustment intent.
        new_order_id = "<no_order>"

    await _append_bracket_modification(
        handle,
        bracket_id=position.bracket_id,
        old_order_ids=tuple(o.order_id for o in cancelled_orders),
        new_order_id=new_order_id,
        timestamp=timestamp,
        pm_command_id=result.command_id,
        rationale=command.adjustment_rationale,
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
            field_changed=_adjust_field_set(command),
            old_value=",".join(o.order_id for o in cancelled_orders) or "<none>",
            new_value=new_order_id,
            rationale=command.adjustment_rationale,
        ),
    )

    if command.thesis_component_updates is not None and position.thesis_id is not None:
        for wc in command.thesis_component_updates:
            component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wc.component_type]
            await _emit(
                handle,
                event_type=EventType.THESIS_COMPONENT_UPDATED,
                order_id=None,
                position_id=command.position_id,
                thesis_id=position.thesis_id,
                timestamp=timestamp,
                detail=ThesisComponentUpdatedDetail(
                    component_id=f"{position.thesis_id}-{component_type.value.lower()}",
                    field_changed="narrative",
                    old_value="",
                    new_value=wc.narrative,
                ),
            )


def _new_stop_level_to_order_shape(
    stop: NewStopLevel,
) -> tuple[OrderRole, OrderType, PriceParameters]:
    if stop.order_type == "limit":
        return OrderRole.PRICE_STOP, OrderType.LIMIT, PriceParameters(limit_price=stop.limit_price)
    if stop.order_type == "stop_limit":
        return (
            OrderRole.PRICE_STOP,
            OrderType.STOP_LIMIT,
            PriceParameters(
                limit_price=stop.limit_price,
                stop_trigger_price=stop.trigger_price,
            ),
        )
    return (
        OrderRole.PRICE_STOP,
        OrderType.STOP,
        PriceParameters(stop_trigger_price=stop.trigger_price),
    )


def _new_target_level_to_order_shape(
    tgt: NewTargetLevel,
) -> tuple[OrderRole, OrderType, PriceParameters]:
    if tgt.order_type == "market":
        return OrderRole.TAKE_PROFIT, OrderType.MARKET, PriceParameters()
    return OrderRole.TAKE_PROFIT, OrderType.LIMIT, PriceParameters(limit_price=tgt.price)


def _build_replacement_protective_order(
    *,
    command: AdjustCommand,
    position: PositionRecord,
    result: SubmissionResult,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord | None:
    """Build the replacement protective order for an ADJUST.

    Returns ``None`` for thesis-only or event-only adjustments (no broker
    order changes — event invalidation is advisory). Stop / target / time
    changes each map to a single replacement order. When multiple
    change-fields are set on one command, the precedence is
    stop -> target -> time so the resulting order is deterministic.
    """
    new_order_id = _adjust_replacement_order_id(command.position_id, result.command_id)
    return _build_replacement_order_for_change_fields(
        new_order_id=new_order_id,
        new_stop_level=command.new_stop_level,
        new_target_level=command.new_target_level,
        new_time_expiration_present=command.new_time_expiration is not None,
        position=position,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _build_replacement_order_for_change_fields(
    *,
    new_order_id: str,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
    position: PositionRecord,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord | None:
    """Shared replacement-order construction used by ADJUST and ADD's
    optional ``bracket_adjustment``.

    Returns ``None`` when no protective change-field is set (event-only or
    thesis-only paths produce no broker order).
    """
    if new_stop_level is None and new_target_level is None and not new_time_expiration_present:
        return None
    bracket_id = position.bracket_id or ""
    common: dict[str, Any] = {
        "order_id": new_order_id,
        "position_id": position.position_id,
        "bracket_id": bracket_id,
        "order_class": OrderClass.OTO,
        "direction": _order_direction_for_close(position.direction),
        "quantity": _position_quantity(position),
        "ticker": _position_ticker(position),
        "pm_command_id": pm_command_id,
        "thesis_id": position.thesis_id,
        "timestamp": timestamp,
        "alpaca_order_id_override": alpaca_order_id_override,
    }
    if new_stop_level is not None:
        role, order_type, price_parameters = _new_stop_level_to_order_shape(new_stop_level)
    elif new_target_level is not None:
        role, order_type, price_parameters = _new_target_level_to_order_shape(new_target_level)
    else:
        # new_time_expiration set: time-stop becomes a market order at deadline.
        role, order_type, price_parameters = (
            OrderRole.TIME_STOP,
            OrderType.MARKET,
            PriceParameters(),
        )
    return _build_pending_order(
        role=role,
        order_type=order_type,
        price_parameters=price_parameters,
        **common,
    )


def _position_quantity(position: PositionRecord) -> float:
    """Best-effort quantity for a position used to size replacement orders.

    For OPEN positions, returns the share/contract count from the typed
    details payload. For PENDING positions (no fills yet), returns ``1.0``
    so the persisted OrderRecord row's quantity invariant holds; the actual
    quantity will be set when the entry fills (Phase 1).
    """
    if isinstance(position.details, EquityPositionDetails):
        qty = position.details.share_count
        return qty if qty > 0 else 1.0
    contracts = getattr(position.details, "contract_count", None)
    if isinstance(contracts, int | float) and contracts > 0:
        return float(contracts)
    return 1.0


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
    release reserved capital.

    Reads ``command.order_id`` (target order to cancel) and
    ``command.cancel_reason`` (drives :class:`OrderCancelledDetail`).
    Emit order_cancelled + capital_released + (entry case) thesis_resolved
    + bracket_dissolved.
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

    await _emit_order_cancelled(
        handle,
        order=target,
        position_id=target.position_id,
        thesis_id=target.originating_thesis_id,
        cancel_reason=command.cancel_reason,
        timestamp=timestamp,
    )
    # Capital release is only valid for entry-class orders (ENTRY / ADD_ENTRY).
    # Those are the only roles that reserve capital on submission via
    # ``_reserve_capital``; protective legs (TAKE_PROFIT / PRICE_STOP /
    # TIME_STOP) never reserved any. CANCELling a protective leg must NOT
    # release a phantom amount — the ``max(... - amount_usd, 0.0)`` floor in
    # ``_release_capital`` would mask the symptom but leave the ledger off by
    # the protective leg's notional for the remainder of the cell's life.
    if target.role in (OrderRole.ENTRY, OrderRole.ADD_ENTRY):
        # Capital release amount derived from the cancelled order's notional
        # (quantity * limit/stop price for non-market orders, or zero for
        # market orders without price parameters — those have no capital
        # reservation because a market order is filled immediately on
        # submission and the reservation flowed through Phase 1 already).
        release_amount = _order_notional_estimate(target)
        await _release_capital(
            handle,
            order_id=target.order_id,
            position_id=target.position_id,
            thesis_id=target.originating_thesis_id,
            amount_usd=release_amount,
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


def _order_notional_estimate(order: OrderRecord) -> float:
    """Best-effort capital estimate for a cancelled order.

    Uses the order's price parameters (limit price preferred, stop trigger
    fallback) times the remaining quantity. Falls back to ``0.0`` for market
    orders with no parameters.
    """
    pp = order.price_parameters
    px = pp.limit_price if pp.limit_price is not None else pp.stop_trigger_price
    if px is None:
        return 0.0
    return float(px) * float(order.remaining_quantity)


# ---------------------------------------------------------------------------
# ADD
# ---------------------------------------------------------------------------


async def _writeback_add(
    handle: InvocationHandle,
    *,
    command: AddCommand,
    result: SubmissionResult,
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """ADD: insert add-entry order (PENDING). Append a new thesis component.
    Reserve capital. Optionally cancel + resubmit modified bracket legs.
    Emit order_submitted + thesis_component_added + capital_reserved
    (+ bracket_modified entries if a bracket_adjustment was supplied).

    Reads canonical :class:`AddCommand` fields:

    * ``additional_quantity`` — entry order quantity
    * ``additional_dollar_value`` — capital reservation amount
    * ``entry_order`` — order type / price parameters
    * ``thesis_addition_component`` — appended to the existing thesis
    * ``bracket_adjustment`` (optional) — cancel + resubmit modified protective legs

    Canonical :class:`AddCommand` (story 01a) carries no embedded instrument
    — it references an existing position by id. The ticker is derived from
    the position's details payload.
    """
    timestamp = datetime.now(UTC)
    pos_row = await handle.session.get(PositionRow, command.position_id)
    if pos_row is None:
        msg = f"ADD references missing position_id={command.position_id!r}"
        raise ValueError(msg)
    position = position_row_to_record(pos_row)
    bracket_id = position.bracket_id or ""
    ticker: str = (
        getattr(position.details, "ticker", None)
        or getattr(position.details, "underlying_ticker", None)
        or ""
    )

    add_order_id = _add_order_id(command.position_id, result.command_id)
    add_order = _build_entry_order_from_command(
        order_id=add_order_id,
        position_id=command.position_id,
        bracket_id=bracket_id,
        thesis_id=position.thesis_id,
        ticker=ticker,
        entry_order=command.entry_order,
        quantity=command.additional_quantity,
        direction=position.direction,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        role=OrderRole.ADD_ENTRY,
        alpaca_order_id_override=submitted_alpaca_order_id,
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
        wire_component_type = command.thesis_addition_component.component_type
        component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wire_component_type]
        await _emit(
            handle,
            event_type=EventType.THESIS_COMPONENT_ADDED,
            order_id=None,
            position_id=command.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            detail=ThesisComponentAddedDetail(
                component_id=component_id,
                component_type=component_type.value,
            ),
        )

    await _reserve_capital(handle, amount_usd=command.additional_dollar_value)
    await _emit_capital_reserved(
        handle,
        order_id=add_order_id,
        position_id=command.position_id,
        thesis_id=position.thesis_id,
        amount_usd=command.additional_dollar_value,
        timestamp=timestamp,
    )

    if command.bracket_adjustment is not None:
        await _apply_bracket_adjustment(
            handle,
            adjustment=command.bracket_adjustment,
            position=position,
            pm_command_id=result.command_id,
            timestamp=timestamp,
        )


async def _apply_bracket_adjustment(
    handle: InvocationHandle,
    *,
    adjustment: BracketAdjustment,
    position: PositionRecord,
    pm_command_id: str,
    timestamp: datetime,
) -> None:
    """Apply an ADD-time :class:`BracketAdjustment` — cancel old protective
    legs, insert the replacement, append bracket modification history.
    """
    if position.bracket_id is None:
        return
    cancelled = await _cancel_pending_protective_orders(
        handle,
        bracket_id=position.bracket_id,
        timestamp=timestamp,
        target_roles=_protective_roles_for_change_fields(
            new_stop_level=adjustment.new_stop_level,
            new_target_level=adjustment.new_target_level,
            new_time_expiration_present=adjustment.new_time_expiration is not None,
        ),
    )
    for cancelled_order in cancelled:
        await _emit_order_cancelled(
            handle,
            order=cancelled_order,
            position_id=position.position_id,
            thesis_id=position.thesis_id,
            cancel_reason="add_command_bracket_adjustment",
            timestamp=timestamp,
        )

    new_order_id = f"ORD-ADD-ADJ-{position.position_id}-{_id_suffix(pm_command_id)}"
    new_order = _build_replacement_order_for_change_fields(
        new_order_id=new_order_id,
        new_stop_level=adjustment.new_stop_level,
        new_target_level=adjustment.new_target_level,
        new_time_expiration_present=adjustment.new_time_expiration is not None,
        position=position,
        pm_command_id=pm_command_id,
        timestamp=timestamp,
    )
    new_order_label = new_order.order_id if new_order is not None else "<no_order>"
    if new_order is not None:
        handle.session.add(order_record_to_row(new_order))
        await _emit_order_submitted(
            handle,
            order=new_order,
            position_id=position.position_id,
            thesis_id=position.thesis_id,
            timestamp=timestamp,
            pm_command_id=pm_command_id,
        )
    await _append_bracket_modification(
        handle,
        bracket_id=position.bracket_id,
        old_order_ids=tuple(o.order_id for o in cancelled),
        new_order_id=new_order_label,
        timestamp=timestamp,
        pm_command_id=pm_command_id,
        rationale="ADD bracket_adjustment",
    )
    await _emit(
        handle,
        event_type=EventType.BRACKET_MODIFIED,
        order_id=None,
        position_id=position.position_id,
        thesis_id=position.thesis_id,
        timestamp=timestamp,
        detail=BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed="bracket_adjustment",
            old_value=",".join(o.order_id for o in cancelled) or "<none>",
            new_value=new_order_label,
            rationale="ADD bracket_adjustment",
        ),
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


# All protective-leg roles. The dissolve-bracket path (entry CANCEL) sweeps
# every protective leg; the ADJUST / BracketAdjustment paths narrow to the
# specific role(s) the change-field set targets so a stop-only ADJUST does
# not also CANCEL the take-profit leg in OMS state. Entry / add-entry roles
# are deliberately omitted — ADJUST never touches them, and the entry-CANCEL
# path cancels the entry itself separately before this sweep runs.
_ALL_PROTECTIVE_ROLES: frozenset[str] = frozenset(
    {OrderRole.PRICE_STOP.value, OrderRole.TAKE_PROFIT.value, OrderRole.TIME_STOP.value}
)


async def _cancel_pending_protective_orders(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    timestamp: datetime,
    target_roles: frozenset[str] = _ALL_PROTECTIVE_ROLES,
) -> tuple[OrderRecord, ...]:
    """Mark PENDING protective orders matching *target_roles* CANCELLED in place.

    Returns the typed records so callers can use the order_ids and other
    fields for activity-log emission. *target_roles* defaults to every
    protective leg role (used by the entry-CANCEL → dissolve-bracket path);
    the ADJUST / BracketAdjustment paths narrow it via
    :func:`_protective_roles_for_change_fields` so only the leg(s) the
    change-field set targets transition to CANCELLED — leaving the other
    protective legs PENDING at the broker. Entry / add-entry roles are
    never touched here (the entry-CANCEL path cancels the entry separately
    before this sweep runs).
    """
    stmt = (
        select(OrderRow)
        .where(OrderRow.bracket_id == bracket_id)
        .where(OrderRow.status == OrderStatus.PENDING.value)
    )
    rows = list((await handle.session.execute(stmt)).scalars())
    cancelled: list[OrderRecord] = []
    for row in rows:
        if row.order_role not in target_roles:
            continue
        row.status = OrderStatus.CANCELLED.value
        row.last_update_timestamp = timestamp.isoformat()
        cancelled.append(order_row_to_record(row))
    return tuple(cancelled)


def _protective_roles_for_change_fields(
    *,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
) -> frozenset[str]:
    """Return the set of protective-leg roles the change-fields target.

    NewStopLevel → PRICE_STOP only; NewTargetLevel → TAKE_PROFIT only;
    new_time_expiration → TIME_STOP only. The OMS-side broker dispatcher
    (:func:`alphamind.execution.oms.submit_envelope_mcp._adjust_command_context`)
    threads the matching protective leg's ``alpaca_order_id`` to
    :func:`submit_replace`; this helper keeps the OMS-state writeback in
    lockstep with the broker mutation so a stop-only ADJUST does not also
    mark the take-profit leg CANCELLED in OMS state.
    """
    roles: set[str] = set()
    if new_stop_level is not None:
        roles.add(OrderRole.PRICE_STOP.value)
    if new_target_level is not None:
        roles.add(OrderRole.TAKE_PROFIT.value)
    if new_time_expiration_present:
        roles.add(OrderRole.TIME_STOP.value)
    return frozenset(roles)


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
    rationale: str = "ADJUST command",
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
            rationale=rationale,
        ),
    )
    bracket_row.modification_history_json = _MODIFICATION_HISTORY_ADAPTER.dump_json(
        new_history
    ).decode()


# ---------------------------------------------------------------------------
# Order construction helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Wire-format → persisted-record translation helpers
# ---------------------------------------------------------------------------


_ENTRY_ORDER_TYPE_TO_PERSISTED: dict[EntryOrderType, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop_limit": OrderType.STOP_LIMIT,
}


_BRACKET_ORDER_TYPE_TO_PERSISTED: dict[BracketOrderType, OrderType] = {
    "market": OrderType.STOP,  # stop-with-market-on-trigger
    "limit": OrderType.STOP_LIMIT,
    "stop": OrderType.STOP,
    "stop_limit": OrderType.STOP_LIMIT,
}


def _direction_from_instrument(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> Direction:
    """Persisted ``Direction`` for an OPEN command's instrument.

    Strategy instruments carry direction per-leg; the position-level direction
    defaults to LONG (the leg-level direction is preserved on each leg).
    """
    if isinstance(instrument, StrategyInstrument):
        return Direction.LONG
    return Direction.LONG if instrument.direction == "long" else Direction.SHORT


def _order_direction_for_entry(direction: Direction) -> OrderDirection:
    return OrderDirection.BUY if direction == Direction.LONG else OrderDirection.SELL


def _order_direction_for_close(direction: Direction) -> OrderDirection:
    """Direction of the order that closes a position with the given direction."""
    return OrderDirection.SELL if direction == Direction.LONG else OrderDirection.BUY


def _entry_price_parameters(entry_order: EntryOrder) -> PriceParameters:
    """Project an ``EntryOrder`` to the persisted ``PriceParameters`` shape."""
    if entry_order.type == "market":
        return PriceParameters()
    if entry_order.type == "limit":
        return PriceParameters(limit_price=entry_order.limit_price)
    # stop_limit
    return PriceParameters(
        limit_price=entry_order.limit_price, stop_trigger_price=entry_order.stop_price
    )


def _bracket_leg_price_parameters(params: BracketOrderParameters) -> PriceParameters:
    """Project a wire ``BracketOrderParameters`` to ``PriceParameters``.

    The trigger price comes from the leg's condition (encoded by the caller);
    the limit price comes from ``params.limit_price`` for limit and stop_limit
    legs.
    """
    if params.order_type == "limit":
        return PriceParameters(limit_price=params.limit_price)
    return PriceParameters()


def _build_entry_order_from_command(  # noqa: PLR0913 — distinct ID, position, bracket, ticker, role threaded through.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    entry_order: EntryOrder,
    quantity: float,
    direction: Direction,
    pm_command_id: str,
    timestamp: datetime,
    role: OrderRole,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build the persisted entry / add-entry order from a canonical EntryOrder."""
    persisted_order_type = _ENTRY_ORDER_TYPE_TO_PERSISTED[entry_order.type]
    price_parameters = _entry_price_parameters(entry_order)
    order_class = OrderClass.SIMPLE if role == OrderRole.ADD_ENTRY else OrderClass.BRACKET
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=role,
        order_class=order_class,
        direction=_order_direction_for_entry(direction),
        order_type=persisted_order_type,
        price_parameters=price_parameters,
        quantity=quantity,
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _build_take_profit_order(  # noqa: PLR0913 — distinct identifiers + sizing must thread through.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    target: Target,
    quantity: float,
    direction: Direction,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    """Build the persisted take-profit order from a canonical Target."""
    if target.order_type == "market":
        order_type = OrderType.MARKET
        price_parameters = PriceParameters()
    else:
        order_type = OrderType.LIMIT
        price_parameters = PriceParameters(limit_price=target.price)
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.TAKE_PROFIT,
        order_class=OrderClass.OTO,
        direction=_order_direction_for_close(direction),
        order_type=order_type,
        price_parameters=price_parameters,
        quantity=quantity,
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
    )


def _build_invalidation_leg_order(  # noqa: PLR0913 — leg construction threads ids + ticker + sizing.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    wire_leg: PriceLeg | TimeLeg,
    quantity: float,
    direction: Direction,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    """Build the persisted protective-leg order for a price/time invalidation leg."""
    persisted_order_type = _BRACKET_ORDER_TYPE_TO_PERSISTED[wire_leg.order_parameters.order_type]
    if isinstance(wire_leg, PriceLeg):
        trigger_price = wire_leg.condition.trigger_price
        if persisted_order_type == OrderType.STOP_LIMIT:
            price_parameters = PriceParameters(
                limit_price=wire_leg.order_parameters.limit_price,
                stop_trigger_price=trigger_price,
            )
        else:
            price_parameters = PriceParameters(stop_trigger_price=trigger_price)
        role = OrderRole.PRICE_STOP
    else:
        # TimeLeg: the broker order is a stop-on-time at the deadline. Persisted
        # as STOP with a sentinel trigger price (the ledger row's broker-grade
        # stop price is recorded in the BracketLeg's TimeTrigger; the order row
        # stops on the deadline at the engine's time-stop monitor).
        # Persisted limit/stop arithmetic: the protective-leg ``OrderRecord``
        # mirrors the broker shape; for time-based legs the broker emits a
        # market order at the deadline, which we record as a market sell with
        # no price parameters.
        price_parameters = PriceParameters()
        persisted_order_type = OrderType.MARKET
        role = OrderRole.TIME_STOP
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=role,
        order_class=OrderClass.OTO,
        direction=_order_direction_for_close(direction),
        order_type=persisted_order_type,
        price_parameters=price_parameters,
        quantity=quantity,
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
    )


def _build_close_order(  # noqa: PLR0913 — close construction threads ids + sizing + price params.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    direction: Direction,
    quantity: float,
    order_type: OrderType,
    price_parameters: PriceParameters,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.CLOSE,
        order_class=OrderClass.SIMPLE,
        direction=_order_direction_for_close(direction),
        order_type=order_type,
        price_parameters=price_parameters,
        ticker=ticker,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        quantity=quantity,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _build_pending_order(  # noqa: PLR0913 — captures every NOT-NULL OrderRecord field once.
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
    quantity: float,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build a fresh PENDING :class:`OrderRecord`.

    When ``alpaca_order_id_override`` is supplied (engine-stub coordinated
    swap, story 03e / ALP-390), the persisted order carries the broker's real
    ``alpaca_order_id``; otherwise it falls back to the synthetic
    ``alp-{order_id}`` placeholder used by the legacy engine-stub path.
    """
    alpaca_id = alpaca_order_id_override or f"alp-{order_id}"
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
        alpaca_order_id=alpaca_id,
        alpaca_order_id_chain=(alpaca_id,),
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
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
    direction: Direction,
    validation_greeks: Greeks | None = None,
    validation_iv: float | None = None,
) -> PositionRecord:
    """Build a PENDING position; fills happen in Phase 1, so size is 0.

    The position record's ``share_count`` (equity) / ``contract_count``
    (options) is always zero at OPEN time — the record reflects state, not
    intent. The OPEN command's ``position_size.quantity`` flows into the
    entry order; once the entry fills, Phase 1 transitions the position to
    OPEN and writes the actual size from the fill.

    Dispatches on instrument variant: :class:`EquityInstrument` lands an
    :class:`EquityPositionDetails`; :class:`OptionInstrument` lands an
    :class:`OptionsPositionDetails` carrying ``validation_greeks`` (the
    per-leg greeks computed by the guardrail-evaluation library at
    OPEN-validation time) plus ``validation_iv`` (the IV the library
    consumed; surfaced through ``Acknowledgment.validation_metadata.implied_volatility``,
    persisted as ``OptionGreeks.iv_used`` per ALP-399). Phase 1's
    ``_apply_options_entry_fill`` preserves these greeks unchanged when the
    entry fills — refresh is the continuous monitor's job (architecture.md
    § 4d).
    """
    if isinstance(instrument, OptionInstrument):
        if validation_greeks is None or validation_iv is None:
            msg = (
                f"OPEN-options writeback requires validation_metadata.greeks "
                f"and .implied_volatility on the Acknowledgment for "
                f"instrument={instrument!r}; got greeks={validation_greeks}, "
                f"iv={validation_iv}"
            )
            raise ValueError(msg)
        details: EquityPositionDetails | OptionsPositionDetails = OptionsPositionDetails(
            underlying_ticker=instrument.underlying,
            strike_price=instrument.strike,
            expiration_date=date.fromisoformat(instrument.expiration),
            contract_type=(
                OptionContractType.CALL
                if instrument.contract_type == "call"
                else OptionContractType.PUT
            ),
            contract_count=0.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=0.0,
            greeks=OptionGreeks(
                delta=validation_greeks.delta,
                gamma=validation_greeks.gamma,
                theta=validation_greeks.theta,
                vega=validation_greeks.vega,
                iv_used=validation_iv,
            ),
        )
    elif isinstance(instrument, EquityInstrument):
        short_fields_present = direction == Direction.SHORT
        details = EquityPositionDetails(
            ticker=instrument.ticker,
            share_count=0.0,
            average_cost_basis_per_share=0.0,
            borrow_rate_pct=0.0 if short_fields_present else None,
            locate_status=LocateStatus.LOCATED if short_fields_present else None,
            margin_held_usd=0.0 if short_fields_present else None,
        )
    else:
        msg = (
            f"OPEN writeback for instrument variant "
            f"{type(instrument).__name__} is not yet supported; "
            "extend _build_pending_position when adding STRATEGY support."
        )
        raise NotImplementedError(msg)
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.PENDING,
            "direction": direction,
            "entry_timestamp": None,
            "details": details,
            "execution_history": (),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


_OMS_COMPONENT_TYPE_TO_PERSISTED: dict[str, ThesisComponentType] = {
    "entry_rationale": ThesisComponentType.ENTRY_RATIONALE,
    "target_rationale": ThesisComponentType.TARGET_RATIONALE,
    "invalidation_rationale": ThesisComponentType.INVALIDATION_RATIONALE,
}


def _build_active_thesis(
    *,
    thesis_id: str,
    position_id: str,
    thesis: Any,
    timestamp: datetime,
) -> ThesisRecord:
    """Build an ACTIVE thesis from the canonical command's :class:`Thesis`.

    Components are constructed one-per-wire-component; missing required
    component types (entry / target / invalidation rationale) are filled with
    placeholder narratives derived from the wire summary so the thesis
    coverage invariant holds. The wire ``Thesis.summary`` becomes the
    persisted ``ThesisRecord.summary``; per-component narrative + key
    assumptions are projected verbatim.
    """
    wire_components = list(thesis.components)
    seen_types = {c.component_type for c in wire_components}
    summary = thesis.summary

    persisted_components: list[ThesisComponent] = []
    for wc in wire_components:
        component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wc.component_type]
        persisted_components.append(
            ThesisComponent(
                component_id=f"{thesis_id}-{component_type.value.lower()}",
                thesis_id=thesis_id,
                component_type=component_type,
                linked_bracket_leg_type=None,
                linked_bracket_leg_id=None,
                instrument_reference=wc.instrument_reference,
                narrative=wc.narrative,
                key_assumptions=tuple(
                    KeyAssumption(text=a, outcome=None) for a in wc.key_assumptions
                ),
                generation_timestamp=timestamp,
                resolution_outcome=None,
                resolution_notes=None,
            )
        )

    # Coverage backfill: ThesisRecord requires entry / target / invalidation
    # rationales. Wire-format thesis is producer-validated as having at least
    # one component but does not enforce mandatory coverage; the writeback
    # injects placeholder components for any missing required type.
    required_wire_types = (
        "entry_rationale",
        "target_rationale",
        "invalidation_rationale",
    )
    for required in required_wire_types:
        if required in seen_types:
            continue
        component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[required]
        persisted_components.append(
            ThesisComponent(
                component_id=f"{thesis_id}-{component_type.value.lower()}",
                thesis_id=thesis_id,
                component_type=component_type,
                linked_bracket_leg_type=None,
                linked_bracket_leg_id=None,
                instrument_reference=summary,
                narrative=summary,
                key_assumptions=(KeyAssumption(text=summary, outcome=None),),
                generation_timestamp=timestamp,
                resolution_outcome=None,
                resolution_notes=None,
            )
        )

    time_expectation_hours = 24.0
    return ThesisRecord(
        thesis_id=thesis_id,
        position_id=position_id,
        summary=summary,
        key_catalyst=summary,
        position_size_rationale=None,
        components=tuple(persisted_components),
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


def _wire_leg_to_bracket_leg(
    *,
    leg_id: str,
    wire_leg: InvalidationLeg,
    leg_order_id: str | None,
    ticker: str,
) -> BracketLeg:
    """Translate a wire-format invalidation leg to a persisted :class:`BracketLeg`.

    Per parent decision (G), wire-format and persisted leg shapes remain
    distinct — this helper bridges them at the writeback. Price legs use
    :class:`PriceTrigger` against the underlying; time legs use
    :class:`TimeTrigger` with the deadline; event legs use
    :class:`EventTrigger` (advisory only — no broker order).
    """
    if isinstance(wire_leg, PriceLeg):
        cmp = wire_leg.condition.comparator
        # PriceTrigger.direction: LTE for stop-on-decline (most common LONG
        # stop), GTE for stop-on-rise (most common SHORT stop / LONG target).
        direction: Literal["LTE", "GTE"] = "LTE" if cmp in ("<=", "<") else "GTE"
        return BracketLeg(
            leg_id=leg_id,
            leg_type=BracketLegType.PRICE_STOP,
            order_id=leg_order_id,
            trigger=PriceTrigger(
                underlying_ticker=wire_leg.condition.underlying_trigger or ticker,
                threshold_usd=wire_leg.condition.trigger_price,
                direction=direction,
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
    if isinstance(wire_leg, TimeLeg):
        return BracketLeg(
            leg_id=leg_id,
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=leg_order_id,
            trigger=TimeTrigger(deadline=wire_leg.condition.deadline),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
    # EventLeg — soft, no broker order.
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.EVENT_INVALIDATION,
        order_id=None,
        trigger=EventTrigger(description=wire_leg.condition.event_description),
        enforcement=BracketLegEnforcement.ADVISORY,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _target_to_bracket_leg(
    *,
    leg_id: str,
    target: Target,
    target_order_id: str,
    ticker: str,
    direction: Direction,
) -> BracketLeg:
    """Translate the canonical :class:`Target` to a persisted TAKE_PROFIT leg.

    Long take-profit fires on price >= threshold (GTE); short on price <= (LTE).
    """
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=target_order_id,
        trigger=PriceTrigger(
            underlying_ticker=ticker,
            threshold_usd=target.price if target.price is not None else 0.01,
            direction="GTE" if direction == Direction.LONG else "LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _build_pending_bracket(
    *,
    bracket_id: str,
    position_id: str,
    ticker: str,
    entry_order_id: str,
    target: Target,
    target_order_id: str,
    invalidation_leg_orders: tuple[tuple[InvalidationLeg, str | None], ...],
) -> BracketRecord:
    """Build a PENDING_ENTRY bracket.

    Take-profit leg comes from ``target``; one leg per ``invalidation_leg``
    entry. The bracket record carries no creation timestamp; per-leg
    submission timestamps live on the broker orders.
    """
    target_leg = _target_to_bracket_leg(
        leg_id=f"{bracket_id}-leg-target",
        target=target,
        target_order_id=target_order_id,
        ticker=ticker,
        direction=Direction.LONG,
    )
    invalidation_legs: list[BracketLeg] = []
    for idx, (wire_leg, leg_order_id) in enumerate(invalidation_leg_orders):
        invalidation_legs.append(
            _wire_leg_to_bracket_leg(
                leg_id=f"{bracket_id}-leg-inv{idx}",
                wire_leg=wire_leg,
                leg_order_id=leg_order_id,
                ticker=ticker,
            )
        )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=entry_order_id,
        protective_legs=(target_leg, *invalidation_legs),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


# ---------------------------------------------------------------------------
# ID minting + helpers
# ---------------------------------------------------------------------------


def _new_open_ids(ticker: str, *, command_id: str) -> dict[str, str]:
    suffix = _id_suffix(command_id)
    return {
        "position_id": f"POS-{ticker}-{suffix}",
        "thesis_id": f"THE-{ticker}-{suffix}",
        "bracket_id": f"BRK-{ticker}-{suffix}",
        "entry_order_id": f"ORD-{ticker}-entry-{suffix}",
        "stop_leg_order_id": f"ORD-{ticker}-stop-{suffix}",
    }


def _close_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-CLOSE-{position_id}-{_id_suffix(command_id)}"


def _adjust_replacement_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADJUST-{position_id}-{_id_suffix(command_id)}"


def _add_order_id(position_id: str, command_id: str) -> str:
    return f"ORD-ADD-{position_id}-{_id_suffix(command_id)}"


def _new_component_id(thesis_id: str, command_id: str) -> str:
    return f"{thesis_id}-add-{_id_suffix(command_id)}"


def _id_suffix(command_id: str) -> str:
    """Stable suffix derived from the synthetic command id.

    Uses the full 32-hex UUID — collision-resistant under any realistic
    invocation volume; truncation buys nothing operationally and risks
    overlap as the corpus grows.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, command_id).hex


def _position_ticker(position: PositionRecord) -> str:
    """Return the ticker / underlying for the typed position-details payload.

    Mirrors :func:`_instrument_ticker_key` for the OMS instrument union — a
    discriminated dispatch over the position-detail variants in
    :mod:`alphamind.portfolio_state.records.positions`. Raises on an
    unsupported variant (rather than fabricating a sentinel string) so a new
    ``InstrumentType`` flag must update this helper before persistence
    silently writes an unrouteable broker order shape.
    """
    if isinstance(position.details, EquityPositionDetails):
        return position.details.ticker
    if isinstance(position.details, OptionsPositionDetails):
        return position.details.underlying_ticker
    if isinstance(position.details, StrategyPositionDetails):
        # Strategy positions don't carry a single underlying — every leg's
        # underlying must agree per the typed-record invariants. The legs are
        # all OptionsPositionDetails with an ``underlying_ticker`` field.
        return position.details.legs[0].options.underlying_ticker
    msg = (
        f"_position_ticker: unsupported position details variant "
        f"{type(position.details).__name__!r}; extend the dispatch when adding "
        "a new InstrumentType."
    )
    raise ValueError(msg)


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
    extra_parameters: dict[str, Any] | None = None,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
) -> None:
    parameters: dict[str, Any] = {
        "order_id": order.order_id,
        "role": order.role.value,
        "direction": order.direction.value,
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "instrument_ticker": getattr(order.instrument_spec, "ticker", ""),
    }
    if order.price_parameters.limit_price is not None:
        parameters["limit_price"] = order.price_parameters.limit_price
    if order.price_parameters.stop_trigger_price is not None:
        parameters["stop_trigger_price"] = order.price_parameters.stop_trigger_price
    if extra_parameters:
        parameters.update(extra_parameters)
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
        source=source,
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
    "persist_engine_envelope_outcome",
    "persist_envelope_outcome",
    "persist_envelope_parse_failure",
    "persist_envelope_rejection",
]
