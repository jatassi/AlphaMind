"""Phase 1 fill-integration write path (story 07 / ALP-365).

Drains every ``processing_status='unprocessed'`` fill record (and any
unprocessed CA activity), integrates each into state (orders / positions /
brackets / theses / cash_ledger / drawdown_state), emits the corresponding
activity-log entries, and marks the fills processed — all inside the open
``InvocationContext`` transaction.

The caller passes the ``InvocationHandle`` from the surrounding
``InvocationContext``; this module never opens its own transaction. On
exception, the surrounding context manager rolls back so fills remain
unprocessed for the next invocation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
    stamp_phase_completion,
)
from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.execution.state_persistence.tables.drawdown_state import (
    DRAWDOWN_STATE_SINGLETON_ID,
    DrawdownStateRow,
)
from alphamind.execution.state_persistence.tables.fill_records import FillRecordRow
from alphamind.execution.state_persistence.tables.fill_records_codec import (
    row_to_record as fill_row_to_record,
)
from alphamind.execution.state_persistence.tables.orders import OrderRow
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
from alphamind.execution.state_persistence.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.execution.state_persistence.write_paths.records import (
    FillProcessingStatus,
    FillRecord,
)
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    BracketActivatedDetail,
    BracketCancelledCorporateActionDetail,
    BracketDissolvedDetail,
    BracketIncompleteWarningDetail,
    CapitalReleasedDetail,
    CashCreditedDetail,
    CashCreditReason,
    CashDebitedDetail,
    CashDebitReason,
    CorporateActionAppliedDetail,
    CorporateActionType,
    EventSource,
    EventType,
    OrderFilledDetail,
    PositionClosedDetail,
    PositionExitMethod,
    PositionOpenedDetail,
    PositionOpenMechanism,
    PositionReducedDetail,
    ThesisResolvedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegStatus,
    BracketStatus,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderRecord,
    OrderStatus,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import ThesisRecordStatus

# Buy-side directions debit cash (purchase consideration); sell-side credit
# cash (sale proceeds).
_BUY_DIRECTIONS = frozenset(
    {OrderDirection.BUY, OrderDirection.BUY_TO_OPEN, OrderDirection.BUY_TO_CLOSE}
)

# Tolerance for "remaining quantity zero" comparisons after float arithmetic.
_QTY_EPSILON = 1e-9


class StateInconsistencyError(RuntimeError):
    """Raised when a Tier-1 row references a parent FK target that is missing.

    The Phase 1 mutators (``_activate_bracket``, ``_dissolve_bracket``,
    ``_maybe_resolve_thesis``, ``_cancel_bracket_for_corporate_action``)
    expect every FK reference they read to point at a live row. A missing
    target would silently no-op and mask state corruption — we raise
    instead so the surrounding ``InvocationContext`` rolls back and the
    operator sees the violation.
    """


@dataclass(frozen=True)
class _FillIntegrationOutcome:
    """Per-fill diff handed to the activity-log emitter.

    Bundles the fields the per-fill emitter needs so its signature stays
    cohesive instead of spreading across positional / kw arguments.
    ``strategy_incomplete_legs``, when populated, marks an mleg cancel-mid-fill
    scenario surfaced by the strategy-fill handler — it triggers a
    ``BRACKET_INCOMPLETE_WARNING`` activity-log entry naming the unfilled
    siblings.
    """

    fill: FillRecord
    order: OrderRecord
    position_before: PositionRecord
    position_after: PositionRecord
    bracket_status_change: BracketStatus | None
    thesis_resolved: bool
    cash_delta_usd: float
    direction_is_buy: bool
    strategy_incomplete_legs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Phase1Summary:
    """Outcome of one ``process_unprocessed_fills`` invocation.

    ``reconciliation_deltas`` is reserved for the broker-reconciliation step
    surfaced in :issue:`ALP-119` Pre-resolved decision (E); for now this
    story emits an empty mapping.
    """

    fills_processed: int
    fills_quarantined: int
    ca_activities_processed: int
    reconciliation_deltas: dict[str, float]


class CorporateActionActivity(BaseModel):
    """Typed handle for a single Alpaca CA activity awaiting integration.

    Phase 1 integrates one ``CorporateActionActivity`` per row drained from
    Alpaca's ``GET /v2/account/activities``. The continuous-monitor work
    that produces these isn't built (:issue:`ALP-123`); tests construct
    instances directly until that lands.
    """

    model_config = ConfigDict(frozen=True)

    alpaca_activity_id: str
    action_type: CorporateActionType
    ticker: str
    new_ticker: str | None
    ratio_or_amount: float
    position_id: str
    signed_cash_impact_usd: float
    transaction_time: datetime


async def process_unprocessed_fills(
    handle: InvocationHandle,
    *,
    config: StatePersistenceConfig,
    ca_activities: tuple[CorporateActionActivity, ...] = (),
) -> Phase1Summary:
    """Drain every unprocessed fill + CA activity and integrate them atomically.

    All state mutations and activity-log emissions join the open
    ``handle.session`` transaction; the surrounding ``InvocationContext``
    commits on clean exit and rolls back on exception, leaving fills
    unprocessed for the next invocation's Phase 1 to retry.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    fill_rows = await _read_unprocessed_fill_rows(handle)
    fills = tuple(fill_row_to_record(row) for row in fill_rows)
    rows_by_fill_id = {row.fill_id: row for row in fill_rows}

    valid_fills, quarantined_count = _quarantine_invalid(handle, fills, rows_by_fill_id)

    fills_processed = 0
    for fill in valid_fills:
        await _integrate_one_fill(handle, fill)
        _mark_processed(rows_by_fill_id[fill.fill_id], handle.invocation_id)
        fills_processed += 1

    for activity in ca_activities:
        await _integrate_one_ca_activity(handle, activity)

    await stamp_phase_completion(handle, column="phase1_completed_at")

    return Phase1Summary(
        fills_processed=fills_processed,
        fills_quarantined=quarantined_count,
        ca_activities_processed=len(ca_activities),
        reconciliation_deltas={},
    )


# ---------------------------------------------------------------------------
# Fill-integration core
# ---------------------------------------------------------------------------


async def _read_unprocessed_fill_rows(
    handle: InvocationHandle,
) -> list[FillRecordRow]:
    """Pull every ``processing_status='unprocessed'`` row in fill-timestamp order.

    Backed by ``ix_fill_records_processing_status`` (story ALP-363) so the
    sweep stays cheap as the table grows.
    """
    stmt = (
        select(FillRecordRow)
        .where(FillRecordRow.processing_status == FillProcessingStatus.UNPROCESSED.value)
        .order_by(FillRecordRow.fill_timestamp.asc(), FillRecordRow.fill_id.asc())
    )
    return list((await handle.session.execute(stmt)).scalars())


def _quarantine_invalid(
    handle: InvocationHandle,
    fills: tuple[FillRecord, ...],
    rows_by_fill_id: dict[str, FillRecordRow],
) -> tuple[tuple[FillRecord, ...], int]:
    """Validate each fill and quarantine the malformed ones in place.

    Returns the surviving fills (in original order) plus the quarantine
    count. Validation rejects non-positive quantity; future revisions can
    extend this with price-plausibility and order-existence checks once the
    upstream contract surfaces them.
    """
    surviving: list[FillRecord] = []
    quarantined = 0
    now_iso = datetime.now(UTC).isoformat()
    for fill in fills:
        if fill.fill_quantity > 0:
            surviving.append(fill)
            continue
        row = rows_by_fill_id[fill.fill_id]
        row.processing_status = FillProcessingStatus.QUARANTINED.value
        row.processing_invocation_id = handle.invocation_id
        row.processing_timestamp = now_iso
        quarantined += 1
    return tuple(surviving), quarantined


async def _integrate_one_fill(
    handle: InvocationHandle,
    fill: FillRecord,
) -> None:
    """Apply the design-doc nine-step sequence for one fill.

    Composes per-entity update helpers — each helper takes the typed
    record, returns the updated typed record, and then the row is
    re-projected via the matching codec. This keeps the per-step logic
    pure and testable through the public entry point.

    Strategy / mleg positions take a per-leg-aware path that consults
    sibling per-leg ``OrderRow`` statuses to gate the atomic PENDING → OPEN
    transition (per ``broker-adapter.md § Multi-leg fill events``).
    """
    order = await _read_order(handle, fill.order_id)
    updated_order = _apply_fill_to_order(order, fill)
    await _persist_order_update(handle, updated_order)

    position_row, position = await _read_position_for_order(handle, order)
    direction_is_buy = order.direction in _BUY_DIRECTIONS

    if isinstance(position.details, StrategyPositionDetails):
        updated_position, incomplete_legs = await _apply_strategy_fill_to_position(
            handle,
            position,
            position.details,
            fill,
            updated_order=updated_order,
        )
    else:
        updated_position = _apply_fill_to_position(position, fill, is_buy_side=direction_is_buy)
        incomplete_legs = ()
    _persist_position_update(position_row, updated_position)

    bracket_status_change = await _maybe_update_bracket(handle, position, updated_position)
    if incomplete_legs:
        # Cancel-mid-fill: dissolve the bracket once (subsequent fills observe
        # the already-DISSOLVED bracket and skip a duplicate warning).
        bracket_status_change, incomplete_legs = await _dissolve_bracket_for_incomplete_strategy(
            handle,
            updated_position.bracket_id,
            bracket_status_change,
            incomplete_legs,
        )
    thesis_resolved = await _maybe_resolve_thesis(handle, position, updated_position, fill)
    cash_delta = await _apply_cash_movement(handle, order, fill)

    if direction_is_buy:
        await _emit_capital_release(handle, order, fill)

    outcome = _FillIntegrationOutcome(
        fill=fill,
        order=updated_order,
        position_before=position,
        position_after=updated_position,
        bracket_status_change=bracket_status_change,
        thesis_resolved=thesis_resolved,
        cash_delta_usd=cash_delta,
        direction_is_buy=direction_is_buy,
        strategy_incomplete_legs=incomplete_legs,
    )
    await _emit_fill_activity_log_entries(handle, outcome)

    await _stamp_drawdown_state(handle)


def _mark_processed(row: FillRecordRow, invocation_id: str) -> None:
    """Stamp the fill row's processing-status fields per the design doc."""
    row.processing_status = FillProcessingStatus.PROCESSED.value
    row.processing_invocation_id = invocation_id
    row.processing_timestamp = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Order updates
# ---------------------------------------------------------------------------


async def _read_order(handle: InvocationHandle, order_id: str) -> OrderRecord:
    row = await handle.session.get(OrderRow, order_id)
    if row is None:
        msg = f"Phase 1 fill references missing order_id={order_id!r}"
        raise ValueError(msg)
    return order_row_to_record(row)


def _apply_fill_to_order(order: OrderRecord, fill: FillRecord) -> OrderRecord:
    """Increment filled_quantity, recompute weighted avg fill price, advance status."""
    new_filled = order.filled_quantity + fill.fill_quantity
    new_remaining = max(order.quantity - new_filled, 0.0)
    prior_avg = order.avg_fill_price if order.avg_fill_price is not None else 0.0
    new_avg = (
        (prior_avg * order.filled_quantity) + (fill.fill_price * fill.fill_quantity)
    ) / new_filled
    new_status = (
        OrderStatus.FILLED if new_remaining <= _QTY_EPSILON else OrderStatus.PARTIALLY_FILLED
    )
    return order.model_copy(
        update={
            "filled_quantity": new_filled,
            "remaining_quantity": new_remaining,
            "avg_fill_price": new_avg,
            "status": new_status,
            "last_update_timestamp": fill.fill_timestamp,
        }
    )


async def _persist_order_update(handle: InvocationHandle, order: OrderRecord) -> None:
    # The order row is identity-mapped from the just-completed read, so this
    # is a cache hit — no extra round-trip.
    row = await handle.session.get(OrderRow, order.order_id)
    if row is None:  # pragma: no cover — read+write inside same transaction
        msg = f"order row {order.order_id!r} disappeared between read and write"
        raise RuntimeError(msg)
    row.status = order.status.value
    row.filled_quantity = order.filled_quantity
    row.average_fill_price = order.avg_fill_price
    row.remaining_quantity = order.remaining_quantity
    row.last_update_timestamp = order.last_update_timestamp.isoformat()


# ---------------------------------------------------------------------------
# Position updates
# ---------------------------------------------------------------------------


async def _read_position_for_order(
    handle: InvocationHandle, order: OrderRecord
) -> tuple[PositionRow, PositionRecord]:
    """Resolve the PositionRow this order acts on and decode it.

    Buy-side entries: ``order.position_id`` may be ``None`` (the OPEN
    command pre-creates the position with this order's bracket_id). Resolve
    via ``brackets.position_id``. Exit / add fills already carry
    ``position_id``.
    """
    position_id = order.position_id or await _resolve_position_id_via_bracket(handle, order)
    row = await handle.session.get(PositionRow, position_id)
    if row is None:
        msg = f"Phase 1 fill references missing position_id={position_id!r}"
        raise ValueError(msg)
    return row, position_row_to_record(row)


async def _resolve_position_id_via_bracket(handle: InvocationHandle, order: OrderRecord) -> str:
    bracket_row = await handle.session.get(BracketRow, order.bracket_id)
    if bracket_row is None:
        msg = (
            f"Phase 1 fill references missing bracket_id={order.bracket_id!r} "
            "while resolving position for entry fill"
        )
        raise ValueError(msg)
    return bracket_row.position_id


def _apply_fill_to_position(
    position: PositionRecord,
    fill: FillRecord,
    *,
    is_buy_side: bool,
) -> PositionRecord:
    """Dispatch entry / add / exit handling based on instrument type, status, and direction.

    Strategy / mleg positions never reach this function: the caller
    (:func:`_integrate_one_fill`) routes them to
    :func:`_apply_strategy_fill_to_position` because the strategy path needs
    DB access to consult sibling per-leg order statuses.
    """
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return _apply_fill_to_equity_position(position, details, fill, is_buy_side=is_buy_side)
    if isinstance(details, OptionsPositionDetails):
        return _apply_fill_to_options_position(position, details, fill, is_buy_side=is_buy_side)
    # Strategy positions are intercepted upstream; this branch defends against
    # a future detail variant being added without updating the dispatcher.
    msg = f"Phase 1 fill integration: unhandled instrument type {details.instrument_type!r}"
    raise NotImplementedError(msg)


def _apply_fill_to_equity_position(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
    *,
    is_buy_side: bool,
) -> PositionRecord:
    """Equity branch: PENDING entry, OPEN add, or sell-side exit."""
    if is_buy_side and position.status == PositionStatus.PENDING:
        return _apply_entry_fill(position, details, fill)
    if is_buy_side and position.status == PositionStatus.OPEN:
        return _apply_add_fill(position, details, fill)
    if not is_buy_side and position.status == PositionStatus.PENDING:
        msg = "SHORT entry fills not yet supported by Phase 1; supported direction is LONG only."
        raise NotImplementedError(msg)
    return _apply_exit_fill(position, details, fill)


def _require_equity_details(position: PositionRecord, label: str) -> EquityPositionDetails:
    """Narrow ``PositionRecord.details`` to ``EquityPositionDetails``.

    Used by the corporate-action integration path which still ships only
    equity coverage (per-action-type matrix for options lands with
    ALP-124). Phase 1 fill integration handles equity + options via
    discriminator-based dispatch in ``_apply_fill_to_position``.
    """
    details = position.details
    if not isinstance(details, EquityPositionDetails):
        msg = f"{label} currently supports equity positions only; got {details.instrument_type!r}"
        raise NotImplementedError(msg)
    return details


def _apply_fill_to_options_position(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
    *,
    is_buy_side: bool,
) -> PositionRecord:
    """Options branch: dispatch entry / add / exit per status and direction.

    SHORT options entries (SELL_TO_OPEN) are first-class — unlike equity, where
    short entry requires a borrow leg ``OptionsPositionDetails`` doesn't model.
    The fill direction for an options entry always aligns with the position's
    direction (LONG ⇐ BUY_TO_OPEN, SHORT ⇐ SELL_TO_OPEN), so PENDING positions
    dispatch unconditionally to the entry-fill helper.

    On OPEN positions, an "opening" fill (one that grows the position) is
    same-sided as the position direction; an exit fill is opposite-sided.
    """
    if position.status == PositionStatus.PENDING:
        return _apply_options_entry_fill(position, details, fill)
    if position.status == PositionStatus.OPEN:
        if _is_opening_fill(position.direction, is_buy_side):
            return _apply_options_add_fill(position, details, fill)
        return _apply_options_exit_fill(position, details, fill)
    msg = f"Phase 1 cannot integrate fill against position status {position.status!r}"
    raise ValueError(msg)


def _is_opening_fill(direction: Direction, is_buy_side: bool) -> bool:
    """Same-sided fills grow the position; opposite-sided fills exit it."""
    return is_buy_side == (direction == Direction.LONG)


def _apply_entry_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """PENDING → OPEN: set entry timestamp, quantity, cost basis, history."""
    new_details = details.model_copy(
        update={
            "share_count": fill.fill_quantity,
            "average_cost_basis_per_share": fill.fill_price,
        }
    )
    return position.model_copy(
        update={
            "status": PositionStatus.OPEN,
            "entry_timestamp": fill.fill_timestamp,
            "details": new_details,
            "execution_history": (_position_fill_from_record(fill),),
        }
    )


def _apply_add_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """ADD-side fill on an OPEN position: increment quantity, recompute cost basis."""
    new_qty = details.share_count + fill.fill_quantity
    weighted_cost = (
        (details.average_cost_basis_per_share * details.share_count)
        + (fill.fill_price * fill.fill_quantity)
    ) / new_qty
    new_details = details.model_copy(
        update={"share_count": new_qty, "average_cost_basis_per_share": weighted_cost}
    )
    return position.model_copy(
        update={
            "details": new_details,
            "execution_history": (*position.execution_history, _position_fill_from_record(fill)),
        }
    )


def _apply_exit_fill(
    position: PositionRecord,
    details: EquityPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """Sell-side fill: decrement quantity, compute realized P/L for the exited portion."""
    qty_after = details.share_count - fill.fill_quantity
    if qty_after < -_QTY_EPSILON:
        msg = (
            f"exit fill quantity ({fill.fill_quantity}) exceeds open share count "
            f"({details.share_count}) for position_id={position.position_id!r}"
        )
        raise ValueError(msg)
    pnl_per_share = fill.fill_price - details.average_cost_basis_per_share
    direction_sign = -1.0 if position.direction == Direction.SHORT else 1.0
    realized_delta = pnl_per_share * fill.fill_quantity * direction_sign
    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta

    closed = abs(qty_after) < _QTY_EPSILON
    update: dict[str, object] = {
        "details": details.model_copy(update={"share_count": 0.0 if closed else qty_after}),
        "execution_history": (*position.execution_history, _position_fill_from_record(fill)),
        "realized_pnl_to_date_usd": cumulative_realized,
    }
    if closed:
        update["status"] = PositionStatus.CLOSED
    return position.model_copy(update=update)


def _apply_options_entry_fill(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """PENDING → OPEN options entry: set premium, contract count, history.

    ``premium_paid_per_contract`` stores the absolute per-contract premium
    (positive for both long and short positions) — symmetric with equity's
    ``average_cost_basis_per_share`` and consistent with the snapshot
    assembler's price-like usage of the field. Direction sign is applied
    where total cost basis or P/L is reported.

    Greeks set at OPEN-validation time by the guardrail-evaluation library
    are preserved unchanged — refresh is the continuous monitor's job.
    """
    new_details = details.model_copy(
        update={
            "contract_count": fill.fill_quantity,
            "premium_paid_per_contract": fill.fill_price,
        }
    )
    return position.model_copy(
        update={
            "status": PositionStatus.OPEN,
            "entry_timestamp": fill.fill_timestamp,
            "details": new_details,
            "execution_history": (_position_fill_from_record(fill),),
        }
    )


def _apply_options_add_fill(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """ADD-side options fill on an OPEN position: increment quantity, recompute premium."""
    new_count = details.contract_count + fill.fill_quantity
    weighted_premium = (
        (details.premium_paid_per_contract * details.contract_count)
        + (fill.fill_price * fill.fill_quantity)
    ) / new_count
    new_details = details.model_copy(
        update={
            "contract_count": new_count,
            "premium_paid_per_contract": weighted_premium,
        }
    )
    return position.model_copy(
        update={
            "details": new_details,
            "execution_history": (*position.execution_history, _position_fill_from_record(fill)),
        }
    )


def _apply_options_exit_fill(
    position: PositionRecord,
    details: OptionsPositionDetails,
    fill: FillRecord,
) -> PositionRecord:
    """Exit options fill: decrement quantity, accumulate realized P/L (multiplier-scaled).

    Realized P/L mirrors the equity formula: ``(exit - entry) * qty * dir_sign``,
    further scaled by ``contract_multiplier``. dir_sign is +1 for LONG (long
    closed for higher than paid is profit) and -1 for SHORT (short covered
    for less than received is profit).
    """
    qty_after = details.contract_count - fill.fill_quantity
    if qty_after < -_QTY_EPSILON:
        msg = (
            f"options exit fill quantity ({fill.fill_quantity}) exceeds open contract count "
            f"({details.contract_count}) for position_id={position.position_id!r}"
        )
        raise ValueError(msg)
    pnl_per_contract = fill.fill_price - details.premium_paid_per_contract
    direction_sign = -1.0 if position.direction == Direction.SHORT else 1.0
    realized_delta = (
        pnl_per_contract * fill.fill_quantity * details.contract_multiplier * direction_sign
    )
    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta

    closed = abs(qty_after) < _QTY_EPSILON
    update: dict[str, object] = {
        "details": details.model_copy(update={"contract_count": 0.0 if closed else qty_after}),
        "execution_history": (*position.execution_history, _position_fill_from_record(fill)),
        "realized_pnl_to_date_usd": cumulative_realized,
    }
    if closed:
        update["status"] = PositionStatus.CLOSED
    return position.model_copy(update=update)


# ---------------------------------------------------------------------------
# Strategy / mleg fill integration (story 04b / ALP-392)
# ---------------------------------------------------------------------------


async def _apply_strategy_fill_to_position(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Apply a per-leg fill to an mleg strategy position.

    Per ``broker-adapter.md § Multi-leg fill events``, a strategy position is
    not considered open until every leg has reached ``filled`` status. Each
    per-leg fill updates the matching ``StrategyLeg.options`` (contract_count,
    premium_paid_per_contract) and appends to ``execution_history``; the
    PENDING → OPEN transition fires only when the post-fill sibling-leg
    statuses say every leg is FILLED.

    The fill correlates to a leg via the order's ``OptionsInstrumentSpec``:
    legs are uniquely identified by ``(contract_type, strike, expiration)`` —
    the OCC identity that distinguishes one option contract from another in
    a multi-leg structure. Entry and close orders against the same leg carry
    different ``order_id`` values but the same option identity.

    Returns ``(updated_position, incomplete_leg_ids)``: ``incomplete_leg_ids``
    is non-empty only when the parent strategy was canceled mid-fill (one or
    more sibling legs are CANCELLED while not all legs are FILLED), signalling
    that :func:`_emit_fill_activity_log_entries` should write a
    ``BRACKET_INCOMPLETE_WARNING`` entry for operator follow-up.
    """
    leg = _strategy_leg_for_order(details.legs, updated_order)
    if position.status == PositionStatus.PENDING:
        return await _apply_strategy_entry_or_continuation(
            handle, position, details, fill, updated_order=updated_order, leg=leg
        )
    if position.status == PositionStatus.OPEN:
        return await _apply_strategy_open_fill(
            handle, position, details, fill, updated_order=updated_order, leg=leg
        )
    msg = f"Phase 1 cannot integrate strategy fill against position status {position.status!r}"
    raise ValueError(msg)


async def _apply_strategy_entry_or_continuation(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
    leg: StrategyLeg,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Update one leg's contract_count + premium and gate the atomic OPEN."""
    new_legs = _set_leg_entry(details.legs, leg=leg, fill=fill)
    new_details = details.model_copy(update={"legs": new_legs})

    sibling_statuses = await _read_sibling_leg_statuses(
        handle,
        position_id=position.position_id,
        excluded_order_id=updated_order.order_id,
    )
    all_other_filled = all(status == OrderStatus.FILLED for status in sibling_statuses.values())
    any_canceled = any(status == OrderStatus.CANCELLED for status in sibling_statuses.values())
    this_leg_filled = updated_order.status == OrderStatus.FILLED

    update: dict[str, object] = {
        "details": new_details,
        "execution_history": (*position.execution_history, _position_fill_from_record(fill)),
    }
    incomplete: tuple[str, ...] = ()
    if this_leg_filled and all_other_filled and not any_canceled:
        # Atomic PENDING → OPEN at the last leg's filled event.
        update["status"] = PositionStatus.OPEN
        update["entry_timestamp"] = fill.fill_timestamp
    elif any_canceled:
        # Cancel mid-fill: surface the unfilled sibling leg ids so the
        # surrounding integrator emits a BRACKET_INCOMPLETE_WARNING.
        incomplete = tuple(
            sorted(
                order_id
                for order_id, status in sibling_statuses.items()
                if status != OrderStatus.FILLED
            )
        )
    return position.model_copy(update=update), incomplete


async def _apply_strategy_open_fill(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
    leg: StrategyLeg,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Apply a fill against an OPEN strategy: ADD (same-side) or CLOSE (opposite-side)."""
    is_buy_side = updated_order.direction in _BUY_DIRECTIONS
    leg_direction_is_long = leg.direction != Direction.SHORT
    is_opening_for_leg = is_buy_side == leg_direction_is_long
    if is_opening_for_leg:
        new_legs = _add_to_leg(details.legs, leg=leg, fill=fill)
        new_details = details.model_copy(update={"legs": new_legs})
        return (
            position.model_copy(
                update={
                    "details": new_details,
                    "execution_history": (
                        *position.execution_history,
                        _position_fill_from_record(fill),
                    ),
                }
            ),
            (),
        )
    return await _apply_strategy_close_fill(
        handle,
        position,
        details,
        fill,
        updated_order=updated_order,
        leg=leg,
    )


async def _apply_strategy_close_fill(
    handle: InvocationHandle,
    position: PositionRecord,
    details: StrategyPositionDetails,
    fill: FillRecord,
    *,
    updated_order: OrderRecord,
    leg: StrategyLeg,
) -> tuple[PositionRecord, tuple[str, ...]]:
    """Apply a closing fill: decrement leg quantity, accumulate signed P/L,
    flip status to CLOSED only when the last leg fully closes."""
    leg_options = leg.options
    qty_after = leg_options.contract_count - fill.fill_quantity
    if qty_after < -_QTY_EPSILON:
        msg = (
            f"strategy exit fill quantity ({fill.fill_quantity}) exceeds open contract count "
            f"({leg_options.contract_count}) for position_id={position.position_id!r}, "
            f"leg_id={leg.leg_id!r}"
        )
        raise ValueError(msg)
    closed_for_this_leg = abs(qty_after) < _QTY_EPSILON
    leg_direction_sign = -1.0 if leg.direction == Direction.SHORT else 1.0
    pnl_per_contract = fill.fill_price - leg_options.premium_paid_per_contract
    realized_delta = (
        pnl_per_contract * fill.fill_quantity * leg_options.contract_multiplier * leg_direction_sign
    )
    cumulative_realized = (position.realized_pnl_to_date_usd or 0.0) + realized_delta

    new_options = leg.options.model_copy(
        update={"contract_count": 0.0 if closed_for_this_leg else qty_after},
    )
    new_legs = _replace_leg(details.legs, leg_id=leg.leg_id, new_options=new_options)
    new_details = details.model_copy(update={"legs": new_legs})

    update: dict[str, object] = {
        "details": new_details,
        "execution_history": (
            *position.execution_history,
            _position_fill_from_record(fill),
        ),
        "realized_pnl_to_date_usd": cumulative_realized,
    }
    sibling_statuses = await _read_sibling_leg_statuses(
        handle,
        position_id=position.position_id,
        excluded_order_id=updated_order.order_id,
    )
    all_others_terminal = all(
        status in (OrderStatus.FILLED, OrderStatus.CANCELLED)
        for status in sibling_statuses.values()
    )
    if (
        closed_for_this_leg
        and updated_order.status == OrderStatus.FILLED
        and all_others_terminal
        and _every_leg_closed(new_legs)
    ):
        update["status"] = PositionStatus.CLOSED
    return position.model_copy(update=update), ()


def _strategy_leg_for_order(legs: tuple[StrategyLeg, ...], order: OrderRecord) -> StrategyLeg:
    """Return the strategy leg the order targets, matched on OCC option identity.

    Per-leg orders carry an ``OptionsInstrumentSpec``; the matching
    ``StrategyLeg.options`` agrees on ``(contract_type, strike,
    expiration_date)`` because a strategy is structurally a fixed set of
    option contracts. Entry / ADD / CLOSE orders against the same leg share
    the option identity but carry distinct ``order_id`` values.
    """
    spec = order.instrument_spec
    if not isinstance(spec, OptionsInstrumentSpec):
        msg = (
            f"strategy fill: order {order.order_id!r} carries non-options "
            f"instrument_spec {type(spec).__name__}; per-leg orders must use "
            "OptionsInstrumentSpec"
        )
        raise TypeError(msg)
    for leg in legs:
        opts = leg.options
        if (
            opts.contract_type == spec.contract_type
            and opts.strike_price == spec.strike
            and opts.expiration_date == spec.expiration
        ):
            return leg
    msg = (
        f"strategy fill: no leg matches order {order.order_id!r} on OCC identity "
        f"({spec.contract_type!r}, strike={spec.strike}, expiration={spec.expiration})"
    )
    raise ValueError(msg)


def _set_leg_entry(
    legs: tuple[StrategyLeg, ...],
    *,
    leg: StrategyLeg,
    fill: FillRecord,
) -> tuple[StrategyLeg, ...]:
    """Set the matching leg's contract_count and premium from a PENDING entry fill.

    A strategy enters all legs simultaneously at OPEN time, so each leg's
    pre-fill ``contract_count`` is zero — the entry fill establishes the
    initial size and premium.
    """
    new_options = leg.options.model_copy(
        update={
            "contract_count": fill.fill_quantity,
            "premium_paid_per_contract": fill.fill_price,
        }
    )
    return _replace_leg(legs, leg_id=leg.leg_id, new_options=new_options)


def _add_to_leg(
    legs: tuple[StrategyLeg, ...],
    *,
    leg: StrategyLeg,
    fill: FillRecord,
) -> tuple[StrategyLeg, ...]:
    """Increment a leg's contract_count and recompute weighted-average premium."""
    prior_count = leg.options.contract_count
    new_count = prior_count + fill.fill_quantity
    weighted_premium = (
        (leg.options.premium_paid_per_contract * prior_count)
        + (fill.fill_price * fill.fill_quantity)
    ) / new_count
    new_options = leg.options.model_copy(
        update={
            "contract_count": new_count,
            "premium_paid_per_contract": weighted_premium,
        }
    )
    return _replace_leg(legs, leg_id=leg.leg_id, new_options=new_options)


def _replace_leg(
    legs: tuple[StrategyLeg, ...],
    *,
    leg_id: str,
    new_options: OptionsPositionDetails,
) -> tuple[StrategyLeg, ...]:
    return tuple(
        StrategyLeg(
            leg_id=existing.leg_id,
            direction=existing.direction,
            options=new_options,
        )
        if existing.leg_id == leg_id
        else existing
        for existing in legs
    )


def _every_leg_closed(legs: tuple[StrategyLeg, ...]) -> bool:
    return all(abs(leg.options.contract_count) < _QTY_EPSILON for leg in legs)


async def _read_sibling_leg_statuses(
    handle: InvocationHandle,
    *,
    position_id: str,
    excluded_order_id: str,
) -> dict[str, OrderStatus]:
    """Return ``{leg_order_id: OrderStatus}`` for all per-leg orders on a strategy.

    Excludes the parent strategy order (``order_class=MLEG``) and the leg the
    current fill just updated — the caller has the latter's post-fill status
    in hand and gates the OPEN transition by combining it with the sibling
    statuses.
    """
    stmt = (
        select(OrderRow.order_id, OrderRow.status)
        .where(OrderRow.position_id == position_id)
        .where(OrderRow.order_class != OrderClass.MLEG.value)
        .where(OrderRow.order_id != excluded_order_id)
    )
    rows = (await handle.session.execute(stmt)).all()
    return {row.order_id: OrderStatus(row.status) for row in rows}


async def _dissolve_bracket_for_incomplete_strategy(
    handle: InvocationHandle,
    bracket_id: str | None,
    prior_change: BracketStatus | None,
    incomplete_legs: tuple[str, ...],
) -> tuple[BracketStatus | None, tuple[str, ...]]:
    """Dissolve the strategy bracket on cancel-mid-fill.

    The strategist resolves the partial residual on its next invocation per
    ``broker-adapter.md § Multi-leg fill events § Cancellation``; dissolving
    here surfaces the broken contract immediately rather than leaving the
    bracket in an inconsistent PENDING_ENTRY / ACTIVE state.

    Returns the (possibly updated) bracket status change plus the
    ``incomplete_legs`` tuple. The leg list is cleared when the bracket is
    already DISSOLVED — a prior fill in the same Phase 1 invocation has
    already emitted the warning, so subsequent fills suppress duplicates.
    """
    if bracket_id is None:
        return prior_change, incomplete_legs
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = f"position references bracket {bracket_id!r}, but bracket row is missing"
        raise StateInconsistencyError(msg)
    if bracket_row.status == BracketStatus.DISSOLVED.value:
        # A prior fill in this invocation already dissolved the bracket and
        # emitted the warning; suppress the duplicate.
        return prior_change, ()
    bracket_row.status = BracketStatus.DISSOLVED.value
    for leg_row in await _read_bracket_legs(handle, bracket_id):
        leg_row.leg_status = BracketLegStatus.CANCELLED.value
    return BracketStatus.DISSOLVED, incomplete_legs


def _persist_position_update(row: PositionRow, position: PositionRecord) -> None:
    """Project the updated record back onto the existing ``PositionRow``."""
    new_row = position_record_to_row(position)
    row.status = new_row.status
    row.entry_timestamp = new_row.entry_timestamp
    row.details_json = new_row.details_json
    row.execution_history_json = new_row.execution_history_json
    row.realized_pnl_to_date_usd = new_row.realized_pnl_to_date_usd
    row.corporate_action_adjustment_needed = new_row.corporate_action_adjustment_needed


def _position_fill_from_record(fill: FillRecord) -> PositionFill:
    return PositionFill(
        fill_timestamp=fill.fill_timestamp,
        fill_price=fill.fill_price,
        fill_quantity=fill.fill_quantity,
        slippage=fill.slippage_usd if fill.slippage_usd is not None else 0.0,
        fees=max(fill.fees_usd, 0.0),
        live_execution_estimate=fill.live_execution_estimate,
    )


# ---------------------------------------------------------------------------
# Bracket updates
# ---------------------------------------------------------------------------


async def _maybe_update_bracket(
    handle: InvocationHandle,
    position_before: PositionRecord,
    position_after: PositionRecord,
) -> BracketStatus | None:
    """Advance the bracket state machine based on the position transition.

    * PENDING → OPEN: bracket PENDING_ENTRY → ACTIVE, legs ACTIVE.
    * OPEN → CLOSED: bracket → DISSOLVED, legs CANCELLED.
    """
    bracket_id = position_after.bracket_id
    if bracket_id is None:
        return None
    transition = (position_before.status, position_after.status)
    if transition == (PositionStatus.PENDING, PositionStatus.OPEN):
        await _activate_bracket(handle, bracket_id)
        return BracketStatus.ACTIVE
    if transition == (PositionStatus.OPEN, PositionStatus.CLOSED):
        await _dissolve_bracket(handle, bracket_id)
        return BracketStatus.DISSOLVED
    return None


async def _activate_bracket(handle: InvocationHandle, bracket_id: str) -> None:
    """Flip a PENDING_ENTRY bracket to ACTIVE with all legs ACTIVE."""
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = f"position references bracket {bracket_id!r}, but bracket row is missing"
        raise StateInconsistencyError(msg)
    bracket_row.status = BracketStatus.ACTIVE.value
    for leg_row in await _read_bracket_legs(handle, bracket_id):
        leg_row.leg_status = BracketLegStatus.ACTIVE.value


async def _dissolve_bracket(handle: InvocationHandle, bracket_id: str) -> None:
    """Flip an ACTIVE bracket to DISSOLVED with all legs CANCELLED."""
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        msg = f"position references bracket {bracket_id!r}, but bracket row is missing"
        raise StateInconsistencyError(msg)
    bracket_row.status = BracketStatus.DISSOLVED.value
    for leg_row in await _read_bracket_legs(handle, bracket_id):
        leg_row.leg_status = BracketLegStatus.CANCELLED.value


async def _read_bracket_legs(handle: InvocationHandle, bracket_id: str) -> list[BracketLegRow]:
    leg_stmt = (
        select(BracketLegRow)
        .where(BracketLegRow.bracket_id == bracket_id)
        .order_by(BracketLegRow.leg_index.asc())
    )
    return list((await handle.session.execute(leg_stmt)).scalars())


async def _bracket_leg_order_ids(handle: InvocationHandle, bracket_id: str) -> tuple[str, ...]:
    """Return the bracket's leg order_ids — empty for legs without an Alpaca order."""
    return tuple(
        row.order_id for row in await _read_bracket_legs(handle, bracket_id) if row.order_id
    )


# ---------------------------------------------------------------------------
# Thesis updates
# ---------------------------------------------------------------------------


async def _maybe_resolve_thesis(
    handle: InvocationHandle,
    position_before: PositionRecord,
    position_after: PositionRecord,
    fill: FillRecord,
) -> bool:
    """On position closure, mark the linked thesis RESOLVED with timestamp.

    ``resolution_category`` and component-level outcomes are owned by the
    analysis pipeline; this story only records the closure timestamp.
    """
    if (position_before.status, position_after.status) != (
        PositionStatus.OPEN,
        PositionStatus.CLOSED,
    ):
        return False
    thesis_id = position_after.thesis_id
    if thesis_id is None:
        return False
    thesis_row = await handle.session.get(ThesisRow, thesis_id)
    if thesis_row is None:
        msg = (
            f"position {position_after.position_id!r} references thesis "
            f"{thesis_id!r}, but thesis row is missing"
        )
        raise StateInconsistencyError(msg)
    thesis_row.status = ThesisRecordStatus.RESOLVED.value
    thesis_row.resolution_timestamp = fill.fill_timestamp.isoformat().replace("+00:00", "Z")
    return True


# ---------------------------------------------------------------------------
# Cash + drawdown
# ---------------------------------------------------------------------------


async def _apply_cash_movement(
    handle: InvocationHandle, order: OrderRecord, fill: FillRecord
) -> float:
    """Debit / credit the cash ledger by the consideration of this fill.

    Options consideration scales by the contract multiplier from the order's
    ``OptionsInstrumentSpec`` (typically 100). Equity consideration is
    ``fill_price * fill_quantity`` directly.

    Buy-side fills additionally drain the per-order capital reservation
    Phase 2 staked when the order was submitted; the decrement caps at
    zero (defensive — partial fills, rounding, or mid-flight adjustments
    can leave the seeded reservation smaller than the fill consideration).

    Returns the *signed cash delta* — positive for credits (sell-side
    proceeds), negative for debits (buy-side consideration).
    """
    consideration = _fill_consideration_usd(order, fill)
    fees = max(fill.fees_usd, 0.0)
    is_buy = order.direction in _BUY_DIRECTIONS
    delta = -(consideration + fees) if is_buy else (consideration - fees)
    cash_row = await _read_cash_row_or_raise(handle)
    cash_row.current_cash_usd = cash_row.current_cash_usd + delta
    if is_buy:
        cash_row.reserved_capital_usd = max(
            cash_row.reserved_capital_usd - (consideration + fees), 0.0
        )
    cash_row.last_updated_at = datetime.now(UTC).isoformat()
    return delta


def _fill_consideration_usd(order: OrderRecord, fill: FillRecord) -> float:
    """USD notional moved by the fill — multiplier-scaled for options."""
    base = fill.fill_price * fill.fill_quantity
    spec = order.instrument_spec
    if isinstance(spec, OptionsInstrumentSpec):
        return base * spec.contract_multiplier
    return base


async def _stamp_drawdown_state(handle: InvocationHandle) -> None:
    """Touch the drawdown_state singleton's ``last_updated_at`` after each fill.

    The full drawdown recomputation (HWM + current drawdown vs. live equity)
    requires market-price context the snapshot assembler owns; for the
    persistence layer this story leaves the running fields untouched and
    simply stamps the row so observers see Phase 1 ran. The richer
    recomputation lands in the snapshot-assembler / breach-behavior wiring.
    """
    row = await handle.session.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID)
    if row is None:
        msg = "drawdown_state singleton missing — Phase 1 cannot integrate fill"
        raise ValueError(msg)
    row.last_updated_at = datetime.now(UTC).isoformat()


async def _read_cash_row_or_raise(handle: InvocationHandle) -> CashLedgerRow:
    row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if row is None:
        msg = "cash_ledger singleton missing — Phase 1 cannot integrate fill"
        raise ValueError(msg)
    return row


# ---------------------------------------------------------------------------
# Activity-log emission
# ---------------------------------------------------------------------------


async def _emit_capital_release(
    handle: InvocationHandle, order: OrderRecord, fill: FillRecord
) -> None:
    """Buy-side fills release the per-order capital reservation made by Phase 2."""
    amount = _fill_consideration_usd(order, fill)
    await _emit(
        handle,
        event_type=EventType.CAPITAL_RELEASED,
        order_id=order.order_id,
        position_id=order.position_id,
        thesis_id=order.originating_thesis_id,
        timestamp=fill.fill_timestamp,
        detail=CapitalReleasedDetail(order_id=order.order_id, amount_usd=amount),
    )


async def _emit_fill_activity_log_entries(
    handle: InvocationHandle,
    outcome: _FillIntegrationOutcome,
) -> None:
    """Emit one entry per state mutation produced by this fill."""
    fill = outcome.fill
    order = outcome.order
    position_before = outcome.position_before
    position_after = outcome.position_after
    bracket_status_change = outcome.bracket_status_change
    thesis_resolved = outcome.thesis_resolved
    cash_delta_usd = outcome.cash_delta_usd
    direction_is_buy = outcome.direction_is_buy
    bracket_id = position_after.bracket_id
    pos_id = position_after.position_id
    thesis_id = position_after.thesis_id

    await _emit(
        handle,
        event_type=EventType.ORDER_FILLED,
        order_id=order.order_id,
        position_id=pos_id,
        thesis_id=thesis_id,
        timestamp=fill.fill_timestamp,
        detail=OrderFilledDetail(
            fill_price=fill.fill_price,
            fill_quantity=fill.fill_quantity,
            slippage=fill.slippage_usd if fill.slippage_usd is not None else 0.0,
            fees=max(fill.fees_usd, 0.0),
        ),
    )

    if (position_before.status, position_after.status) == (
        PositionStatus.PENDING,
        PositionStatus.OPEN,
    ):
        await _emit(
            handle,
            event_type=EventType.POSITION_OPENED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=PositionOpenedDetail(
                ticker=_ticker_of(position_after),
                direction=position_after.direction.value,
                fill_price=fill.fill_price,
                quantity=fill.fill_quantity,
                thesis_id=thesis_id,
                bracket_id=bracket_id,
                mechanism=PositionOpenMechanism.ORDER_FILL,
                parent_position_id=None,
            ),
        )

    if bracket_status_change == BracketStatus.ACTIVE and bracket_id is not None:
        await _emit(
            handle,
            event_type=EventType.BRACKET_ACTIVATED,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=BracketActivatedDetail(
                bracket_id=bracket_id,
                protective_leg_order_ids=await _bracket_leg_order_ids(handle, bracket_id),
            ),
        )

    partial_close = (position_before.status, position_after.status) == (
        PositionStatus.OPEN,
        PositionStatus.OPEN,
    ) and not _is_opening_fill(position_after.direction, direction_is_buy)
    if partial_close:
        partial_pnl = (position_after.realized_pnl_to_date_usd or 0.0) - (
            position_before.realized_pnl_to_date_usd or 0.0
        )
        await _emit(
            handle,
            event_type=EventType.POSITION_REDUCED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=PositionReducedDetail(
                reduced_quantity=fill.fill_quantity,
                partial_realized_pnl_usd=partial_pnl,
                close_rationale_classification="",
            ),
        )

    if (position_before.status, position_after.status) == (
        PositionStatus.OPEN,
        PositionStatus.CLOSED,
    ):
        await _emit(
            handle,
            event_type=EventType.POSITION_CLOSED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=PositionClosedDetail(
                exit_method=PositionExitMethod.PM_DECISION,
                exit_price=fill.fill_price,
                realized_pnl_usd=position_after.realized_pnl_to_date_usd or 0.0,
                thesis_resolution_category="",
            ),
        )

    if bracket_status_change == BracketStatus.DISSOLVED and bracket_id is not None:
        await _emit(
            handle,
            event_type=EventType.BRACKET_DISSOLVED,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=BracketDissolvedDetail(
                cancelled_leg_order_ids=await _bracket_leg_order_ids(handle, bracket_id),
            ),
        )

    if outcome.strategy_incomplete_legs:
        await _emit(
            handle,
            event_type=EventType.BRACKET_INCOMPLETE_WARNING,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=BracketIncompleteWarningDetail(
                missing_leg_types=outcome.strategy_incomplete_legs,
                expected_resolution=(
                    "strategist re-evaluates the partial mleg residual on its next "
                    "invocation per broker-adapter.md § Multi-leg fill events § Cancellation"
                ),
            ),
        )

    if thesis_resolved and thesis_id is not None:
        await _emit(
            handle,
            event_type=EventType.THESIS_RESOLVED,
            order_id=None,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=ThesisResolvedDetail(
                resolution_category="",
                component_outcomes_json={},
            ),
        )

    new_balance = await _current_cash_balance(handle)
    if direction_is_buy:
        await _emit(
            handle,
            event_type=EventType.CASH_DEBITED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=CashDebitedDetail(
                amount_usd=abs(cash_delta_usd),
                reason=CashDebitReason.ENTRY_FILL,
                new_balance_usd=new_balance,
            ),
        )
    else:
        await _emit(
            handle,
            event_type=EventType.CASH_CREDITED,
            order_id=order.order_id,
            position_id=pos_id,
            thesis_id=thesis_id,
            timestamp=fill.fill_timestamp,
            detail=CashCreditedDetail(
                amount_usd=abs(cash_delta_usd),
                reason=CashCreditReason.EXIT_FILL,
                new_balance_usd=new_balance,
            ),
        )


async def _current_cash_balance(handle: InvocationHandle) -> float:
    return (await _read_cash_row_or_raise(handle)).current_cash_usd


async def _emit(
    handle: InvocationHandle,
    *,
    event_type: EventType,
    order_id: str | None,
    position_id: str | None,
    thesis_id: str | None,
    timestamp: datetime,
    detail: object,
    source: EventSource = EventSource.FILL_PROCESSOR,
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


def _ticker_of(position: PositionRecord) -> str:
    """Best-effort ticker extraction across instrument variants for log details."""
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    return getattr(details, "underlying_ticker", "")


# ---------------------------------------------------------------------------
# Corporate-action integration
# ---------------------------------------------------------------------------


async def _integrate_one_ca_activity(
    handle: InvocationHandle, activity: CorporateActionActivity
) -> None:
    """Apply the per-action-type matrix for one CA activity.

    This story implements the SPLIT path end-to-end (covers the test
    surface) and registers the dedupe anchor. Per-action-type variants
    extend here in follow-up stories without touching the surrounding
    atomicity contract.
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
    new_qty, new_basis = _apply_ca_to_quantity_and_basis(
        activity, pre_qty=pre_qty, pre_basis=pre_basis
    )

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


def _apply_ca_to_quantity_and_basis(
    activity: CorporateActionActivity,
    *,
    pre_qty: float,
    pre_basis: float,
) -> tuple[float, float]:
    """Project the new (quantity, cost_basis) per the per-action-type matrix."""
    if activity.action_type == CorporateActionType.SPLIT:
        return pre_qty * activity.ratio_or_amount, pre_basis / activity.ratio_or_amount
    msg = f"CA action_type={activity.action_type!r} not yet supported by Phase 1"
    raise NotImplementedError(msg)


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
        raise StateInconsistencyError(msg)
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
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
    )


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
        source=EventSource.CORPORATE_ACTION_PROCESSOR,
    )


__all__ = [
    "CorporateActionActivity",
    "Phase1Summary",
    "StateInconsistencyError",
    "process_unprocessed_fills",
]
