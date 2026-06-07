"""Shared helpers for the Phase 2 command-execution write path.

Cross-cutting machinery used by two or more of the per-command-kind modules
(:mod:`.open`, :mod:`.close`, :mod:`.adjust`, :mod:`.cancel`, :mod:`.add`):
activity-log emission, cash-ledger primitives, protective-leg cancellation,
bracket modification-history append, generic order construction, position-
derived adapters, and common direction / price-parameter / id helpers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    CommandId,
    OrderId,
    PositionId,
    ThesisId,
    make_symbol,
)
from alphamind._kernel.money import Money, Price, money
from alphamind.commands.command_models import (
    EntryOrder,
    EntryOrderType,
    EquityInstrument,
    NewStopLevel,
    NewTargetLevel,
    OptionInstrument,
    StrategyInstrument,
)
from alphamind.execution.oms.command_ids import synthesize_id_suffix
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    BracketModificationSource,
    CapitalReleasedDetail,
    CapitalReservedDetail,
    EventSource,
    EventType,
    OrderCancelledDetail,
    OrderSubmittedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLegModification,
    BracketLegStatus,
    EquityInstrumentSpec,
    InstrumentSpec,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    StrategyInstrumentSpec,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
    position_direction,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponentType,
)
from alphamind.state.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.records import FillProcessingStatus
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    rows_to_record as bracket_rows_to_record,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.orders_codec import (
    row_to_record as order_row_to_record,
)

logger = logging.getLogger(__name__)


def _instrument_ticker_key(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> str:
    """Return the ticker/underlying key for a canonical OMS instrument."""
    if isinstance(instrument, EquityInstrument):
        return instrument.ticker
    return instrument.underlying


_ENTRY_ORDER_TYPE_TO_PERSISTED: dict[EntryOrderType, OrderType] = {
    "market": OrderType.MARKET,
    "limit": OrderType.LIMIT,
    "stop_limit": OrderType.STOP_LIMIT,
}


_OMS_COMPONENT_TYPE_TO_PERSISTED: dict[str, ThesisComponentType] = {
    "entry_rationale": ThesisComponentType.ENTRY_RATIONALE,
    "target_rationale": ThesisComponentType.TARGET_RATIONALE,
    "invalidation_rationale": ThesisComponentType.INVALIDATION_RATIONALE,
}


def _order_direction_for_entry(direction: Direction) -> OrderDirection:
    return OrderDirection.BUY if direction == Direction.LONG else OrderDirection.SELL


def _order_direction_for_close(direction: Direction) -> OrderDirection:
    """Direction of the order that closes a position with the given direction."""
    return OrderDirection.SELL if direction == Direction.LONG else OrderDirection.BUY


def _entry_order_direction_for_position(position: PositionRecord) -> OrderDirection | None:
    """Order-side direction for opening an entry / add-entry on *position*.

    Mirrors the inverse :func:`_close_order_direction_for_position`. Returns
    ``None`` for a strategy position (the parent MLEG envelope's direction is
    a category error — per-leg side / position_intent ride on each
    :class:`StrategyLeg`). ALP-614.
    """
    direction = position_direction(position)
    return None if direction is None else _order_direction_for_entry(direction)


def _close_order_direction_for_position(position: PositionRecord) -> OrderDirection | None:
    """Order-side direction for closing *position*.

    Returns ``None`` for a strategy position (see
    :func:`_entry_order_direction_for_position`). ALP-614.
    """
    direction = position_direction(position)
    return None if direction is None else _order_direction_for_close(direction)


def _instrument_ticker_for_activity_log(spec: InstrumentSpec) -> str:
    """Resolve the activity-log ``instrument_ticker`` payload field for *spec*.

    Equity spec → ticker; options spec → underlying; strategy spec → first
    leg's underlying (every strategy leg shares the same underlying). Avoids
    the ``getattr(spec, "ticker", "")`` fallback that silently emitted an
    empty string for OptionsInstrumentSpec (attr is ``underlying``) and
    StrategyInstrumentSpec (no scalar ticker). ALP-614.
    """
    if isinstance(spec, EquityInstrumentSpec):
        return spec.ticker
    if isinstance(spec, OptionsInstrumentSpec):
        return spec.underlying
    # StrategyInstrumentSpec — every leg shares the same underlying.
    return spec.legs[0].underlying


def _instrument_spec_for_position(position: PositionRecord) -> InstrumentSpec:
    """Derive the :class:`InstrumentSpec` for an order against *position*.

    Strategy → :class:`StrategyInstrumentSpec` rebuilt from each
    :class:`StrategyLeg`'s options details — the order record then carries
    the strategy envelope shape the broker adapter sends over the wire, per
    ALP-614 + the design comment in
    ``continuous_monitor/fill_stream_consumer/translation.py`` ("OMS persists
    mleg orders as a single parent OrderRecord with order_class=MLEG and
    the legs encoded on instrument_spec").
    Equity / single-leg options → :class:`EquityInstrumentSpec` keyed by the
    underlying ticker. The pre-ALP-614 ``_build_pending_order`` defaulted to
    EquityInstrumentSpec for every non-strategy order regardless of the
    position's instrument type, and downstream cash-movement / consideration
    code (``_fill_consideration_usd`` in fill_collection) keys off the order's spec —
    so an instrument-faithful shape on single-leg options orders would
    silently change cash-ledger semantics (multiplier scaling). Preserve the
    pre-PR shape on non-strategy orders; the strategy-aware branch is the
    only behavior change ALP-614 introduces.
    """
    details = position.details
    if isinstance(details, StrategyPositionDetails):
        return StrategyInstrumentSpec(
            legs=tuple(
                OptionsInstrumentSpec(
                    underlying=leg.options.underlying_ticker,
                    strike=leg.options.strike_price,
                    expiration=leg.options.expiration_date,
                    contract_type=leg.options.contract_type,
                    contract_multiplier=leg.options.contract_multiplier,
                )
                for leg in details.legs
            )
        )
    if isinstance(details, EquityPositionDetails):
        return EquityInstrumentSpec(ticker=details.ticker)
    # OptionsPositionDetails — preserve pre-PR EquityInstrumentSpec(underlying)
    # shape to avoid changing the multiplier branch in _fill_consideration_usd.
    return EquityInstrumentSpec(ticker=details.underlying_ticker)


def _entry_price_parameters(entry_order: EntryOrder) -> PriceParameters:
    """Project an ``EntryOrder`` to the persisted ``PriceParameters`` shape."""
    if entry_order.type == "market":
        return PriceParameters()
    if entry_order.type == "limit":
        return PriceParameters(limit_price=entry_order.limit_price)
    # stop_limit
    return PriceParameters(
        limit_price=entry_order.limit_price,
        stop_trigger_price=entry_order.stop_price,
    )


def _position_quantity(position: PositionRecord) -> float:
    """Best-effort quantity used to size replacement orders.

    OPEN positions return their fill count; PENDING positions (zero fills)
    return ``1.0`` so the OrderRecord quantity invariant holds — Phase 1
    overwrites with the real quantity when the entry fills.
    """
    if isinstance(position.details, EquityPositionDetails):
        qty = position.details.share_count
        return qty if qty > 0 else 1.0
    contracts = getattr(position.details, "contract_count", None)
    if isinstance(contracts, int | float) and contracts > 0:
        return float(contracts)
    return 1.0


def _position_ticker(position: PositionRecord) -> str:
    """Return the ticker / underlying for the typed position-details payload.

    Mirrors :func:`_instrument_ticker_key` for positions. Raises on unsupported
    variants so a new InstrumentType must update this helper.
    """
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails):
        # Strategy legs all share the same underlying per typed-record invariant.
        return details.legs[0].options.underlying_ticker
    msg = f"_position_ticker: unsupported variant {type(details).__name__!r}"
    raise ValueError(msg)


def _id_suffix(command_id: str) -> str:
    """Stable 32-hex suffix from *command_id*."""
    return synthesize_id_suffix(command_id)


def _build_pending_order(  # noqa: PLR0913 — captures every NOT-NULL OrderRecord field once.
    *,
    order_id: str,
    position_id: str | None,
    bracket_id: str,
    role: OrderRole,
    order_class: OrderClass,
    direction: OrderDirection | None,
    order_type: OrderType,
    price_parameters: PriceParameters,
    instrument_spec: InstrumentSpec | None = None,
    ticker: str | None = None,
    pm_command_id: str,
    thesis_id: str | None,
    timestamp: datetime,
    quantity: float,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build a fresh PENDING :class:`OrderRecord`.

    Callers pass either ``instrument_spec`` (the typed payload directly) or
    ``ticker`` (for the common equity case — the function wraps it in an
    :class:`EquityInstrumentSpec`). Strategy callers must pass the
    :class:`StrategyInstrumentSpec` via ``instrument_spec`` together with
    ``order_class=OrderClass.MLEG`` and ``direction=None`` (ALP-614);
    options single-leg callers pass an :class:`OptionsInstrumentSpec`
    similarly.

    ``alpaca_order_id_override`` (broker-routing coordinated swap, story 03e /
    ALP-390) wires the broker's real id when one exists. When it is ``None`` the
    order carries NO broker id (``alpaca_order_id=None``, empty chain) — a
    not-yet-routed order (durable Intent keyed by ``client_order_id``, backfilled
    on dispatch) or a monitor-enforced protective leg (armed Intent the monitor
    enforces with no broker order). The synthetic ``alp-{order_id}`` placeholder
    is deleted (ADR-0003 / ALP-847, invariant 5): a leg with no broker order has
    no broker id, never a counterfeit one — which is what renders ALP-837
    (cancel/adjust of a synthetic-id leg) unrepresentable.
    """
    if instrument_spec is None:
        if ticker is None:
            msg = "_build_pending_order requires either instrument_spec or ticker"
            raise ValueError(msg)
        instrument_spec = EquityInstrumentSpec(ticker=make_symbol(ticker))
    alpaca_id = AlpacaOrderId(alpaca_order_id_override) if alpaca_order_id_override else None
    alpaca_chain = (alpaca_id,) if alpaca_id is not None else ()
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id) if position_id is not None else None,
        bracket_id=BracketId(bracket_id),
        role=role,
        instrument_spec=instrument_spec,
        direction=direction,
        order_type=order_type,
        order_class=order_class,
        price_parameters=price_parameters,
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=alpaca_id,
        alpaca_order_id_chain=alpaca_chain,
        submission_timestamp=timestamp,
        last_update_timestamp=timestamp,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=0,
        originating_thesis_id=ThesisId(thesis_id) if thesis_id is not None else None,
        originating_pm_command_id=CommandId(pm_command_id),
        age_hours=0.0,
    )


def _build_entry_order_from_command(  # noqa: PLR0913 — distinct ID, position, bracket, ticker, role threaded through.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    ticker: str,
    entry_order: EntryOrder,
    quantity: float,
    direction: Direction | None,
    instrument_spec: InstrumentSpec | None = None,
    pm_command_id: str,
    timestamp: datetime,
    role: OrderRole,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build the persisted entry / add-entry order from a canonical EntryOrder.

    For a strategy position the caller passes ``direction=None`` and the
    pre-built :class:`StrategyInstrumentSpec` via ``instrument_spec``; the
    resulting record is the MLEG parent envelope per ALP-614.
    """
    persisted_order_type = _ENTRY_ORDER_TYPE_TO_PERSISTED[entry_order.type]
    price_parameters = _entry_price_parameters(entry_order)
    if direction is None:
        order_class = OrderClass.MLEG
        order_direction: OrderDirection | None = None
    else:
        order_class = OrderClass.SIMPLE if role == OrderRole.ADD_ENTRY else OrderClass.BRACKET
        order_direction = _order_direction_for_entry(direction)
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=role,
        order_class=order_class,
        direction=order_direction,
        order_type=persisted_order_type,
        price_parameters=price_parameters,
        quantity=quantity,
        instrument_spec=instrument_spec,
        ticker=ticker if instrument_spec is None else None,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


async def _read_cash_row(handle: InvocationHandle) -> CashLedgerRow:
    row = await handle.session.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
    if row is None:
        msg = "cash_ledger singleton missing — Phase 2 cannot reserve capital"
        raise ValueError(msg)
    return row


def _order_notional_usd(*, price: Price, remaining_quantity: float) -> Money:
    """Capital-notional estimate ``price * remaining_quantity`` as ``Money``.

    The single basis the reservation lifecycle shares — ``_order_reserved_notional``
    (OPEN / ADD / CANCEL) and the entry-window reprice adjustment
    (``reprice._adjust_reservation_for_reprice``, ALP-740) — so a repriced-then-
    cancelled entry's reservation deltas and final release stay mutually
    consistent. ``remaining_quantity`` may be a float in the legacy record types;
    cast through ``str`` so binary drift never enters the monetary computation.
    """
    return money(Decimal(str(price)) * Decimal(str(remaining_quantity)))


def _order_reservation_price(order: OrderRecord) -> Price | None:
    """Reservation-price basis for an entry / add-entry order (ALP-741).

    ``limit_price`` preferred, ``stop_trigger_price`` the fallback; ``None`` for
    a market entry (it reserves nothing). The single price basis the whole
    reservation lifecycle keys off — OPEN / ADD reserve, the entry-window reprice
    adjusts, and the terminal / partial CANCEL releases against it.
    """
    pp = order.price_parameters
    return pp.limit_price if pp.limit_price is not None else pp.stop_trigger_price


def _order_reserved_notional(order: OrderRecord) -> Money:
    """Reserved-capital notional for an entry / add-entry order (ALP-741).

    The single basis the *whole* reservation lifecycle uses: OPEN / ADD reserve
    it at submission, the entry-window reprice adjusts it by the limit delta, and
    the terminal CANCEL releases it — so ``cash_ledger.reserved_capital_usd``
    stays equal to the sum of live pending-entry notionals at all times and can
    never drift negative. The price basis is ``_order_reservation_price``; a
    market entry carries no price → ``money(0)`` (a marketable order reserves
    nothing, matching the ``pending_order_capital_pct`` rule's
    ``reserves_capital`` convention and the read-side
    ``library_snapshot._build_position_reservations``).

    Before ALP-741, OPEN / ADD reserved ``position_size.dollar_value`` (the PM's
    intended capital) while the release paths used this ``price * quantity``
    notional — the basis mismatch let a repriced-then-cancelled entry drive the
    ledger negative and crash every subsequent decision-pipeline invocation.
    """
    px = _order_reservation_price(order)
    if px is None:
        return money(0)
    return _order_notional_usd(price=px, remaining_quantity=order.remaining_quantity)


async def _unprocessed_filled_quantity(handle: InvocationHandle, *, order_id: str) -> float:
    """Summed quantity of UNPROCESSED ``fill_records`` recorded against *order_id*.

    The continuous monitor appends a ``fill_records`` row (status ``unprocessed``)
    the instant a broker fill lands; Phase 1 later drains it into the order's
    ``filled_quantity``. Between those two events — exactly the stale-snapshot
    window a PM CANCEL is decided in (ALP-760) — the order row still reads
    zero-filled while shares already exist on the broker. Summing the unprocessed
    fills recovers that not-yet-integrated filled quantity. QUARANTINED fills are
    excluded (Phase 1 rejected them as malformed — they back no real shares), and
    PROCESSED fills are already folded into ``filled_quantity`` so counting them
    here would double-count.
    """
    stmt = select(FillRecordRow.fill_quantity).where(
        FillRecordRow.order_id == order_id,
        FillRecordRow.processing_status == FillProcessingStatus.UNPROCESSED.value,
    )
    return float(sum((await handle.session.execute(stmt)).scalars()))


async def _reserve_capital(handle: InvocationHandle, *, amount_usd: Money) -> None:
    cash_row = await _read_cash_row(handle)
    # Reserve adds non-negative to a non-negative pool — ``money()`` constructor.
    cash_row.reserved_capital_usd = money(cash_row.reserved_capital_usd + amount_usd)
    cash_row.last_updated_at = datetime.now(UTC).isoformat()


def _emit_capital_reserved(
    handle: InvocationHandle,
    *,
    order_id: str,
    position_id: str | None,
    thesis_id: str | None,
    amount_usd: Money,
    timestamp: datetime,
) -> None:
    _emit(
        handle,
        event_type=EventType.CAPITAL_RESERVED,
        order_id=order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        # ALP-463 — activity-log detail classes now carry ``Money`` directly;
        # pass through without a float round-trip.
        detail=CapitalReservedDetail(order_id=order_id, amount_usd=amount_usd),
    )


async def _release_capital(
    handle: InvocationHandle,
    *,
    order_id: str,
    position_id: str | None,
    thesis_id: str | None,
    amount_usd: Money,
    timestamp: datetime,
) -> None:
    cash_row = await _read_cash_row(handle)
    # Floor the pool at zero on every release (ALP-741). The reservation
    # lifecycle is conservative by construction — ``_order_reserved_notional``
    # is reserved at OPEN/ADD, adjusted on reprice, and released here — so this
    # floor only ever absorbs residual rounding; it is a defensive backstop, not
    # a substitute for that symmetry. A negative ``reserved_capital_usd`` is data
    # corruption that crashes every downstream decision-pipeline invocation
    # (the ``pending_order_capital_pct`` rule's ``_classify_zone`` rejects a
    # negative consumption), so clamping here keeps a stray over-release from
    # poisoning the singleton.
    net = cash_row.reserved_capital_usd - amount_usd
    if net < 0:
        # The floor fired: a release exceeded the running reservation. With the
        # ALP-741 basis unification this should never happen for more than
        # rounding dust, so a real clamp means an upstream asymmetry (a forgotten
        # market-entry guard, a release on a basis other than the reservation,
        # double-release). The floor keeps it from poisoning the singleton, but
        # log loudly so the over-release is observable instead of silently
        # absorbed (the pre-ALP-741 signed-balance surfaced it by going negative).
        logger.warning(
            "reserved_capital_usd over-release floored: order_id=%s reserved=%s "
            "release=%s deficit=%s",
            order_id,
            cash_row.reserved_capital_usd,
            amount_usd,
            -net,
        )
    cash_row.reserved_capital_usd = money(max(net, Decimal(0)))
    cash_row.last_updated_at = datetime.now(UTC).isoformat()
    _emit(
        handle,
        event_type=EventType.CAPITAL_RELEASED,
        order_id=order_id,
        position_id=position_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        # ALP-463 — see ``_emit_capital_reserved`` for the Money-direct migration.
        detail=CapitalReleasedDetail(order_id=order_id, amount_usd=amount_usd),
    )


# Protective-leg roles (entry / add-entry deliberately omitted — the entry-
# CANCEL path cancels the entry separately before any protective sweep).
_ALL_PROTECTIVE_ROLES: frozenset[str] = frozenset(
    {OrderRole.PRICE_STOP.value, OrderRole.TAKE_PROFIT.value, OrderRole.TIME_STOP.value}
)

# The options capital-floor OrderRow's ``order_id`` prefix (ALP-856). Mirrors
# ``command_execution.open._capital_floor_order_id`` (``ORD-FLOOR-{suffix}``); duplicated as a
# literal here because ``open`` imports from this module, so importing it back
# would cycle. The floor row shares the PRICE_STOP role with the invalidation
# stop, so its id prefix — not its role — is what identifies it.
_CAPITAL_FLOOR_ORDER_ID_PREFIX = "ORD-FLOOR-"


def _is_capital_floor_order(row: OrderRow) -> bool:
    """Is *row* the options OPEN's broker-enforced capital-floor OrderRow (ALP-856)?

    Identified by the ``ORD-FLOOR-`` ``order_id`` prefix, not by role (the floor
    shares the PRICE_STOP role with the invalidation stop).
    """
    return row.order_id.startswith(_CAPITAL_FLOOR_ORDER_ID_PREFIX)


async def _cancel_pending_protective_orders(
    handle: InvocationHandle,
    *,
    bracket_id: str,
    timestamp: datetime,
    target_roles: frozenset[str] = _ALL_PROTECTIVE_ROLES,
) -> tuple[OrderRecord, ...]:
    """Mark PENDING protective orders matching *target_roles* CANCELLED in place.

    Returns the typed records for activity-log emission. *target_roles*
    defaults to every protective leg role (entry-CANCEL → dissolve-bracket);
    ADJUST / BracketAdjustment narrow via
    :func:`_protective_roles_for_change_fields` so only the targeted leg(s)
    transition to CANCELLED. Entry / add-entry roles are never touched here.

    The options capital-floor OrderRow (ALP-856) is also cancelled when it is
    still ``PENDING_SUBMIT`` — its broker-id backfill never ran (a floor-submit
    failure leaves the entry live but the floor row durable-Intent only). On the
    entry-CANCEL teardown (``abandon_command`` → ``_writeback_cancel``) the
    bracket dissolves, so the floor must clear too: left ``PENDING_SUBMIT`` its
    ``inv-{id}.`` ``client_order_id`` keeps
    :func:`alphamind.execution.write_paths.command_execution.atomic.invocation_has_pending_submit_strand`
    True forever → ``command_execution_completed_at`` withheld with no recovery (FL3 /
    ALP-856). The floor reserves no capital (release none); cancelling the row
    clears the strand. A non-floor ``PENDING_SUBMIT`` row is a legitimately
    in-flight order and is NOT swept (only the floor leg is scoped in).
    """
    stmt = (
        select(OrderRow)
        .where(OrderRow.bracket_id == bracket_id)
        .where(OrderRow.status.in_((OrderStatus.PENDING.value, OrderStatus.PENDING_SUBMIT.value)))
    )
    rows = list((await handle.session.execute(stmt)).scalars())
    cancelled: list[OrderRecord] = []
    for row in rows:
        if row.order_role not in target_roles:
            continue
        # A PENDING_SUBMIT row is only swept when it is the capital floor; any
        # other PENDING_SUBMIT row is a legitimately in-flight order awaiting its
        # broker-id backfill and must not be cancelled here.
        if row.status == OrderStatus.PENDING_SUBMIT.value and not _is_capital_floor_order(row):
            continue
        row.status = OrderStatus.CANCELLED.value
        row.last_update_timestamp = timestamp.isoformat()
        cancelled.append(order_row_to_record(row))
    return tuple(cancelled)


async def _cancel_all_bracket_legs(handle: InvocationHandle, *, bracket_id: str) -> None:
    """Transition every ``bracket_legs`` row for *bracket_id* to CANCELLED.

    Symmetric with the fill_collection dissolve path (``fill_collection._dissolve_bracket``):
    once a bracket is DISSOLVED the read-time invariant
    (``BracketRecord._check_dissolved_rule``) requires every leg row to be
    CANCELLED. This iterates ``bracket_legs`` directly rather than deriving
    from the cancelled protective *orders* — order-less EVENT_INVALIDATION /
    advisory legs (``order_id=None``) have no broker order an order-sweep
    could reach, so an orders-only cancel leaves them PENDING_ACTIVATION and
    makes the DISSOLVED bracket unreadable. ALP-731.
    """
    stmt = select(BracketLegRow).where(BracketLegRow.bracket_id == bracket_id)
    for leg_row in (await handle.session.execute(stmt)).scalars():
        leg_row.leg_status = BracketLegStatus.CANCELLED.value


async def _assert_bracket_readable(handle: InvocationHandle, *, bracket_id: str) -> None:
    """Re-materialize the bracket through the read codec as a write-time guard.

    Defense in depth (ALP-731): a DISSOLVED bracket whose legs are not all
    CANCELLED is committable but unreadable — every
    ``get_brackets_for_positions`` loader then raises and one corrupt row
    becomes a system-wide kill switch. Rebuilding the record here runs the
    same ``BracketRecord`` invariants the read path enforces
    (``brackets_codec.rows_to_record`` → ``_check_dissolved_rule``), so a
    state the reader forbids fails loudly at write time instead of committing
    silently. A no-op when the bracket row is absent.
    """
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        return
    leg_stmt = (
        select(BracketLegRow)
        .where(BracketLegRow.bracket_id == bracket_id)
        .order_by(BracketLegRow.leg_index.asc())
    )
    leg_rows = tuple((await handle.session.execute(leg_stmt)).scalars())
    bracket_rows_to_record(bracket_row, leg_rows)


def _protective_roles_for_change_fields(
    *,
    new_stop_level: NewStopLevel | None,
    new_target_level: NewTargetLevel | None,
    new_time_expiration_present: bool,
) -> frozenset[str]:
    """Return the protective-leg roles the change-fields target.

    NewStopLevel → PRICE_STOP; NewTargetLevel → TAKE_PROFIT;
    new_time_expiration → TIME_STOP. Keeps the OMS-state writeback in lockstep
    with the broker mutation in
    :func:`alphamind.decision.portfolio_manager.submit_envelope._adjust_command_context`,
    so a stop-only ADJUST never also marks the take-profit leg CANCELLED.
    """
    roles: set[str] = set()
    if new_stop_level is not None:
        roles.add(OrderRole.PRICE_STOP.value)
    if new_target_level is not None:
        roles.add(OrderRole.TAKE_PROFIT.value)
    if new_time_expiration_present:
        roles.add(OrderRole.TIME_STOP.value)
    return frozenset(roles)


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
    bracket through the codec; the history vocabulary is shared with
    ``brackets_codec``.
    """
    bracket_row = await handle.session.get(BracketRow, bracket_id)
    if bracket_row is None:
        return
    history = _MODIFICATION_HISTORY_ADAPTER.validate_json(bracket_row.modification_history_json)
    new_history = (
        *history,
        BracketLegModification(
            timestamp=timestamp,
            pm_command_id=CommandId(pm_command_id),
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


def _emit_order_submitted(
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
        "order_type": order.order_type.value,
        "quantity": order.quantity,
        "instrument_ticker": _instrument_ticker_for_activity_log(order.instrument_spec),
    }
    if order.direction is not None:
        # MLEG strategy parents carry direction=None — the broker adapter
        # emits per-leg side / position_intent and the envelope-level value
        # has no meaning (ALP-614). Omit the key from the payload entirely
        # rather than emitting null.
        parameters["direction"] = order.direction.value
    if order.price_parameters.limit_price is not None:
        parameters["limit_price"] = order.price_parameters.limit_price
    if order.price_parameters.stop_trigger_price is not None:
        parameters["stop_trigger_price"] = order.price_parameters.stop_trigger_price
    if extra_parameters:
        parameters.update(extra_parameters)
    _emit(
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


def _emit_order_cancelled(
    handle: InvocationHandle,
    *,
    order: OrderRecord,
    position_id: str | None,
    thesis_id: str | None,
    cancel_reason: str,
    timestamp: datetime,
) -> None:
    _emit(
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


def _emit(
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
    append_activity_log_entry(handle, entry)
