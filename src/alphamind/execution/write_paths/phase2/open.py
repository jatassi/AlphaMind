"""OPEN command writeback (capital reservation, position, thesis, bracket)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    ThesisId,
    make_symbol,
)
from alphamind._kernel.money import price
from alphamind.commands.command_models import (
    BracketOrderType,
    EquityInstrument,
    EventLeg,
    InvalidationLeg,
    OpenCommand,
    OptionInstrument,
    PriceLeg,
    StrategyInstrument,
    Target,
    Thesis,
    TimeLeg,
)
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.broker_adapter.order_options import (
    derive_capital_floor_client_order_id,
    floor_price_per_contract,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.oms.command_ids import parse_pm_command_id, synthesize_id_suffix
from alphamind.execution.write_paths.phase2._shared import (
    _OMS_COMPONENT_TYPE_TO_PERSISTED,
    _build_entry_order_from_command,
    _build_pending_order,
    _close_order_direction_for_position,
    _emit,
    _emit_capital_reserved,
    _emit_order_submitted,
    _id_suffix,
    _instrument_spec_for_position,
    _instrument_ticker_key,
    _order_reserved_notional,
    _reserve_capital,
)
from alphamind.portfolio_state.events.activity_log import (
    EventType,
    ThesisCreatedDetail,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EnforcementBinding,
    EventTrigger,
    InstrumentSpec,
    OptionsInstrumentSpec,
    OrderClass,
    OrderDirection,
    OrderRecord,
    OrderRole,
    OrderType,
    PLAnchorSpec,
    PriceParameters,
    PriceTrigger,
    TimeTrigger,
    TriggerSignal,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionDetailsPayload,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
    position_direction,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisNature,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import Greeks
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

_BRACKET_ORDER_TYPE_TO_PERSISTED: dict[BracketOrderType, OrderType] = {
    "market": OrderType.STOP,  # stop-with-market-on-trigger
    "limit": OrderType.STOP_LIMIT,
    "stop": OrderType.STOP,
    "stop_limit": OrderType.STOP_LIMIT,
}


async def _writeback_open(
    handle: InvocationHandle,
    *,
    command: OpenCommand,
    result: SubmissionResult,
    submitted_alpaca_order_id: str | None = None,
    submitted_leg_alpaca_order_ids: Mapping[str, str] | None = None,
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
    * capital reservation amount ← the entry order's notional
      (``_order_reserved_notional`` = ``limit_price * quantity``; ``money(0)``
      for a market entry), NOT ``command.position_size.dollar_value`` (ALP-741)

    When ``submitted_alpaca_order_id`` is supplied (broker-routing coordinated
    swap, story 03e / ALP-390), the persisted entry order carries the broker's
    real Alpaca order id.

    ``submitted_leg_alpaca_order_ids`` (ALP-746) maps a native bracket / OTO's
    protective role (``"take_profit"`` / ``"stop_loss"``) to the broker's real
    child id, captured at submission off ``Order.legs``. The take-profit id
    stamps the TAKE_PROFIT order and the stop-loss id stamps the first
    PRICE_STOP order, so a later protective fill / OCO sibling-cancel resolves
    to the local leg row. Legs with no broker counterpart — TIME_STOP and the
    advisory EVENT legs (and any PRICE_STOP beyond the one Alpaca brackets,
    which submits a single stop child) — carry NO broker id (``alpaca_order_id``
    NULL, ALP-847): they are monitor-enforced Intent, not Broker-Owned Fact.
    """
    leg_ids = submitted_leg_alpaca_order_ids or {}
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
    # Derive the canonical instrument_spec + close-side direction once. For a
    # strategy position both the entry envelope and each protective-leg
    # envelope persist as MLEG orders with the strategy spec and direction
    # None (ALP-614); equity / single-leg options paths take the equity-spec
    # branch and carry a meaningful BUY/SELL.
    entry_instrument_spec = _instrument_spec_for_position(position)
    pos_direction = position_direction(position)
    close_order_direction = _close_order_direction_for_position(position)
    thesis = _build_active_thesis(
        thesis_id=ids["thesis_id"],
        position_id=ids["position_id"],
        thesis=command.thesis,
        timestamp=timestamp,
    )
    # ALP-856 / FS4 — an options OPEN submits an always-on broker-enforced capital
    # floor alongside the entry. The floor is a tracked broker order with its OWN
    # durable OrderRow, keyed by the floor's deterministic ``client_order_id``
    # (precommitted PENDING_SUBMIT with ``alpaca_order_id`` NULL, exactly like the
    # entry). The floor bracket leg's ``order_id`` points at the floor OMS order_id —
    # NOT the broker Alpaca id — so the DEFERRABLE FK to ``orders.order_id`` is
    # satisfied. The broker id rides back on ``leg_alpaca_order_ids['capital_floor']``
    # and is backfilled onto the floor OrderRow after submit. Equity / strategy
    # OPENs carry no floor.
    floor_price = _capital_floor_price(command)
    floor_order, floor_order_id = _capital_floor_order_for_open(
        command=command,
        command_id=result.command_id,
        ids=ids,
        floor_price=floor_price,
        capital_floor_alpaca_order_id=leg_ids.get("capital_floor"),
        timestamp=timestamp,
    )
    bracket = _build_pending_bracket(
        bracket_id=ids["bracket_id"],
        position_id=ids["position_id"],
        entry_order_id=ids["entry_order_id"],
        target=command.target,
        target_order_id=target_order_id,
        invalidation_leg_orders=tuple(invalidation_leg_orders),
        instrument=command.instrument,
        entry_window_deadline=(
            command.entry_window.deadline if command.entry_window is not None else None
        ),
        capital_floor_order_id=floor_order_id,
        capital_floor_price=floor_price,
    )
    entry_order = _build_entry_order_from_command(
        order_id=ids["entry_order_id"],
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        ticker=ticker,
        entry_order=command.entry_order,
        quantity=command.position_size.quantity,
        direction=pos_direction,
        instrument_spec=entry_instrument_spec,
        pm_command_id=result.command_id,
        timestamp=timestamp,
        role=OrderRole.ENTRY,
        alpaca_order_id_override=submitted_alpaca_order_id,
    )
    target_order, invalidation_orders = _build_protective_orders(
        context=_ProtectiveOrderContext(
            position_id=ids["position_id"],
            bracket_id=ids["bracket_id"],
            thesis_id=ids["thesis_id"],
            quantity=command.position_size.quantity,
            order_direction=close_order_direction,
            instrument_spec=entry_instrument_spec,
            pm_command_id=result.command_id,
            timestamp=timestamp,
        ),
        target=command.target,
        target_order_id=target_order_id,
        invalidation_leg_orders=invalidation_leg_orders,
        leg_alpaca_order_ids=leg_ids,
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
    # ALP-856 — the capital floor's durable OrderRow (options OPEN only). Its
    # ``order_id`` is what the floor bracket leg references, so it must land in the
    # same transaction the bracket legs do (the DEFERRABLE FK is checked at COMMIT).
    if floor_order is not None:
        handle.session.add(order_record_to_row(floor_order))
    await handle.session.flush()

    # Reserve the entry order's notional (``limit_price * quantity``), NOT the
    # PM-command ``dollar_value`` (ALP-741). The reservation must use the same
    # basis the reprice / cancel / fill release paths use, or a repriced or
    # cancelled entry over-/under-releases against ``dollar_value`` and drives
    # ``reserved_capital_usd`` away from the true sum of live pending-entry
    # notionals — negative, in the worst case, which crashes every subsequent
    # decision-pipeline invocation. A market entry carries no price, so its
    # notional is ``money(0)``: a marketable order reserves nothing and is
    # filled immediately (its consideration flows through Phase 1).
    reserved_amount = _order_reserved_notional(entry_order)

    _emit_order_submitted(
        handle,
        order=entry_order,
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        timestamp=timestamp,
        pm_command_id=result.command_id,
    )
    _emit(
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
    if reserved_amount > 0:
        await _reserve_capital(handle, amount_usd=reserved_amount)
        _emit_capital_reserved(
            handle,
            order_id=ids["entry_order_id"],
            position_id=ids["position_id"],
            thesis_id=ids["thesis_id"],
            amount_usd=reserved_amount,
            timestamp=timestamp,
        )


# ---------------------------------------------------------------------------
# OPEN-only helpers
# ---------------------------------------------------------------------------


_CONTRACT_TYPE_FROM_WIRE: dict[str, OptionContractType] = {
    "call": OptionContractType.CALL,
    "put": OptionContractType.PUT,
}
_DIRECTION_FROM_WIRE: dict[str, Direction] = {
    "long": Direction.LONG,
    "short": Direction.SHORT,
}
# ALP-852 / ADR-0003 — wire ``Thesis.nature`` tag → persisted ``ThesisNature``.
_THESIS_NATURE_FROM_WIRE: dict[str, ThesisNature] = {
    "directional": ThesisNature.DIRECTIONAL,
    "non_directional": ThesisNature.NON_DIRECTIONAL,
}
# ALP-852 — wire ``PriceLeg.trigger_signal`` → persisted ``TriggerSignal``. The
# continuous monitor reads this off the thesis-invalidation leg to select the
# trigger evaluator (underlying vs option-price / net-mark).
_TRIGGER_SIGNAL_FROM_WIRE: dict[str, TriggerSignal] = {
    "underlying_price": TriggerSignal.UNDERLYING_PRICE,
    "option_price": TriggerSignal.OPTION_PRICE,
    "net_mark": TriggerSignal.NET_MARK,
}


def _direction_from_instrument(
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> Direction | None:
    """Position-level ``Direction`` for an OPEN command's instrument.

    For an equity or single-leg option the return is the instrument's own
    ``direction``. For a :class:`StrategyInstrument` the return is ``None`` — a
    multi-leg strategy has no position-level direction; strategy consumers
    derive directional sign from the legs (each :class:`StrategyLeg` carries
    its own ``direction``). The resulting value feeds ``PositionRecord.direction``
    directly, whose validator ties ``None`` to a strategy payload. See
    ``docs/design/05-execution-layer/position-model.md`` § Strategy position.
    """
    if isinstance(instrument, StrategyInstrument):
        return None
    return _DIRECTION_FROM_WIRE[instrument.direction]


def _new_open_ids(ticker: str, *, command_id: str) -> dict[str, str]:
    """Mint the OPEN's local-graph ids from the PM OPEN ``command_id``.

    Position / bracket / order ids are suffix-derived (``synthesize_id_suffix``
    strips any broker-carried link, so they stay byte-identical to the pre-link
    derivation). The ``thesis_id`` is NOT re-constructed here — it is read back
    out of the broker-carried link by parsing the command id, so the embedded
    thesis is the single source of truth for the OPEN thesis identity (ALP-844,
    A2). ``parse_pm_command_id`` raises ``ValueError`` for a non-PM / thesis-less
    id; for a real OPEN that is the correct fail-loud behavior — every
    AlphaMind OPEN carries a parseable PM thesis-bearing command id.
    """
    suffix = _id_suffix(command_id)
    return {
        "position_id": f"POS-{ticker}-{suffix}",
        "thesis_id": parse_pm_command_id(command_id).thesis_id,
        "bracket_id": f"BRK-{ticker}-{suffix}",
        "entry_order_id": f"ORD-{ticker}-entry-{suffix}",
        "stop_leg_order_id": f"ORD-{ticker}-stop-{suffix}",
    }


@dataclass(frozen=True)
class _ProtectiveOrderContext:
    """Shared identifiers + sizing for a bracket's protective-leg orders.

    Bundled so :func:`_build_protective_orders` threads one context object
    rather than re-listing every id / sizing field per builder call.
    """

    position_id: str
    bracket_id: str
    thesis_id: str
    quantity: float
    order_direction: OrderDirection | None
    instrument_spec: InstrumentSpec
    pm_command_id: str
    timestamp: datetime


def _build_protective_orders(
    *,
    context: _ProtectiveOrderContext,
    target: Target,
    target_order_id: str,
    invalidation_leg_orders: Sequence[tuple[InvalidationLeg, str | None]],
    leg_alpaca_order_ids: Mapping[str, str],
) -> tuple[OrderRecord, list[OrderRecord]]:
    """Build the TAKE_PROFIT order + one order per price/time invalidation leg.

    Native bracket / OTO protective children captured at submission
    (``leg_alpaca_order_ids``, ALP-746) stamp their real broker ids: the
    ``take_profit`` id onto the TAKE_PROFIT order, the ``stop_loss`` id onto the
    first PriceLeg's PRICE_STOP order (Alpaca's native bracket carries exactly
    one stop child, mapped from the first PriceLeg by
    ``order_equity._bracket_params``). A TimeLeg (TIME_STOP), the advisory
    EVENT legs, and any PRICE_STOP beyond the first have no broker counterpart
    and carry NO broker id (``alpaca_order_id`` NULL, ALP-847) — monitor-enforced
    Intent.
    """
    c = context
    target_order = _build_take_profit_order(
        order_id=target_order_id,
        position_id=c.position_id,
        bracket_id=c.bracket_id,
        thesis_id=c.thesis_id,
        target=target,
        quantity=c.quantity,
        order_direction=c.order_direction,
        instrument_spec=c.instrument_spec,
        pm_command_id=c.pm_command_id,
        timestamp=c.timestamp,
        alpaca_order_id_override=leg_alpaca_order_ids.get("take_profit"),
    )
    stop_loss_override = leg_alpaca_order_ids.get("stop_loss")
    invalidation_orders: list[OrderRecord] = []
    for wire_leg, leg_order_id in invalidation_leg_orders:
        if leg_order_id is None or isinstance(wire_leg, EventLeg):
            continue
        if isinstance(wire_leg, PriceLeg) and stop_loss_override is not None:
            leg_override: str | None = stop_loss_override
            stop_loss_override = None
        else:
            leg_override = None
        invalidation_orders.append(
            _build_invalidation_leg_order(
                order_id=leg_order_id,
                position_id=c.position_id,
                bracket_id=c.bracket_id,
                thesis_id=c.thesis_id,
                wire_leg=wire_leg,
                quantity=c.quantity,
                order_direction=c.order_direction,
                instrument_spec=c.instrument_spec,
                pm_command_id=c.pm_command_id,
                timestamp=c.timestamp,
                alpaca_order_id_override=leg_override,
            )
        )
    return target_order, invalidation_orders


def _build_take_profit_order(  # noqa: PLR0913 — distinct identifiers + sizing must thread through.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    target: Target,
    quantity: float,
    order_direction: OrderDirection | None,
    instrument_spec: InstrumentSpec,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build the persisted take-profit order from a canonical Target.

    For a strategy position ``order_direction`` is ``None`` and
    ``instrument_spec`` is the parent :class:`StrategyInstrumentSpec`; the
    leg is persisted as the MLEG envelope that exits the strategy (ALP-614).

    ``alpaca_order_id_override`` (ALP-746) carries the native bracket / OTO's
    take-profit child id captured at submission; ``None`` means NO broker id
    (``alpaca_order_id`` NULL, ALP-847 — no broker counterpart, e.g. a strategy
    MLEG exit's monitor-enforced take-profit).
    """
    if target.order_type == "market":
        order_type = OrderType.MARKET
        price_parameters = PriceParameters()
    else:
        order_type = OrderType.LIMIT
        price_parameters = PriceParameters(limit_price=target.price)
    order_class = OrderClass.MLEG if order_direction is None else OrderClass.OTO
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.TAKE_PROFIT,
        order_class=order_class,
        direction=order_direction,
        order_type=order_type,
        price_parameters=price_parameters,
        quantity=quantity,
        instrument_spec=instrument_spec,
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _build_invalidation_leg_order(  # noqa: PLR0913 — leg construction threads ids + ticker + sizing.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str | None,
    wire_leg: PriceLeg | TimeLeg,
    quantity: float,
    order_direction: OrderDirection | None,
    instrument_spec: InstrumentSpec,
    pm_command_id: str,
    timestamp: datetime,
    alpaca_order_id_override: str | None = None,
) -> OrderRecord:
    """Build the persisted protective-leg order for a price/time invalidation leg.

    For a strategy position ``order_direction`` is ``None`` and
    ``instrument_spec`` is the parent :class:`StrategyInstrumentSpec`; the
    leg is persisted as the MLEG envelope that exits the strategy (ALP-614).

    ``alpaca_order_id_override`` (ALP-746) carries the native bracket's stop
    child id for the PRICE_STOP leg; a TIME_STOP (TimeLeg) has no broker
    counterpart and is always called with ``None`` → NO broker id
    (``alpaca_order_id`` NULL, ALP-847 — monitor-enforced Intent).
    """
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
        # TimeLeg: broker emits market on the deadline. Persisted as MARKET
        # with no price parameters; the BracketLeg's TimeTrigger carries the
        # deadline for the engine's time-stop monitor.
        price_parameters = PriceParameters()
        persisted_order_type = OrderType.MARKET
        role = OrderRole.TIME_STOP
    order_class = OrderClass.MLEG if order_direction is None else OrderClass.OTO
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
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        alpaca_order_id_override=alpaca_order_id_override,
    )


def _zeroed_option_greeks() -> OptionGreeks:
    """A fully-zeroed :class:`OptionGreeks` — the skeleton placeholder.

    Per-leg and strategy-level greeks are zeroed at OPEN time and refreshed by
    the continuous monitor (architecture.md § 4d), exactly as for single-leg
    options. The validation-metadata strategy greeks are a per-leg *average*,
    not a net, so they must not seed ``strategy_greeks`` (parent ALP-588
    § Surfacing conditions).
    """
    return OptionGreeks(delta=0.0, gamma=0.0, theta=0.0, vega=0.0)


def _build_strategy_skeleton(
    *,
    instrument: StrategyInstrument,
    position_id: str,
) -> StrategyPositionDetails:
    """Build a zeroed :class:`StrategyPositionDetails` for a strategy OPEN.

    One record-form :class:`StrategyLeg` per wire :class:`StrategyLeg`, each
    carrying an :class:`OptionsPositionDetails` skeleton at
    ``contract_count=0.0`` / ``premium_paid_per_contract=0.0`` — the record
    reflects state, not intent, mirroring the single-option branch. Payoff
    metrics (``net_premium_usd``, ``max_profit_usd``, ``max_loss_usd``,
    ``breakeven_levels``) and ``strategy_greeks`` are all skeleton zeros; the
    Phase 1 entry-fill handler recomputes them from the filled legs.

    Leg ids are deterministic — ``{position_id}-leg-{idx}`` — and each wire
    leg's ``direction`` is carried straight through. The strategy branch does
    not consume ``validation_greeks`` / ``validation_iv``.
    """
    zeroed_greeks = _zeroed_option_greeks()
    legs: list[StrategyLeg] = []
    for idx, wire_leg in enumerate(instrument.legs):
        legs.append(
            StrategyLeg(
                leg_id=f"{position_id}-leg-{idx}",
                options=OptionsPositionDetails(
                    underlying_ticker=make_symbol(instrument.underlying),
                    # ALP-462 — Price → float at the legacy OptionsPositionDetails surface.
                    strike_price=float(wire_leg.strike),
                    expiration_date=date.fromisoformat(wire_leg.expiration),
                    contract_type=_CONTRACT_TYPE_FROM_WIRE[wire_leg.contract_type],
                    contract_count=0.0,
                    contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
                    premium_paid_per_contract=0.0,
                    greeks=zeroed_greeks,
                ),
                direction=_DIRECTION_FROM_WIRE[wire_leg.direction],
            )
        )
    return StrategyPositionDetails(
        strategy_type_label=instrument.strategy_type,
        legs=tuple(legs),
        net_premium_usd=0.0,
        max_profit_usd=0.0,
        max_loss_usd=0.0,
        breakeven_levels=(),
        strategy_greeks=zeroed_greeks,
    )


def _build_pending_position(
    *,
    position_id: str,
    thesis_id: str,
    bracket_id: str,
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
    direction: Direction | None,
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

    :class:`StrategyInstrument` lands a :class:`StrategyPositionDetails`
    skeleton (see :func:`_build_strategy_skeleton`) — one zeroed leg per wire
    leg, zeroed payoff metrics, zeroed greeks. The strategy branch does not
    read ``validation_greeks`` / ``validation_iv``; the validation metadata's
    strategy greeks are a per-leg average, not a net (parent ALP-588
    § Surfacing conditions), so they must not seed ``strategy_greeks``. The
    Phase 1 entry-fill handler recomputes the payoff metrics from the filled legs.
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
        details: PositionDetailsPayload = OptionsPositionDetails(
            underlying_ticker=make_symbol(instrument.underlying),
            # ALP-462 — Price → float at the legacy OptionsPositionDetails surface.
            strike_price=float(instrument.strike),
            expiration_date=date.fromisoformat(instrument.expiration),
            contract_type=_CONTRACT_TYPE_FROM_WIRE[instrument.contract_type],
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
            ticker=make_symbol(instrument.ticker),
            share_count=0.0,
            average_cost_basis_per_share=0.0,
            borrow_rate_pct=0.0 if short_fields_present else None,
            accrued_borrow_cost_usd=0.0 if short_fields_present else None,
            locate_status=LocateStatus.LOCATED if short_fields_present else None,
            margin_held_usd=0.0 if short_fields_present else None,
        )
    else:
        details = _build_strategy_skeleton(
            instrument=instrument,
            position_id=position_id,
        )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id),
        bracket_id=BracketId(bracket_id),
        status=PositionStatus.PENDING,
        direction=direction,
        entry_timestamp=None,
        details=details,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _build_active_thesis(
    *,
    thesis_id: str,
    position_id: str,
    thesis: Thesis,
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

    # Per-type running index so ``thesis_components.component_id`` (a PK) stays
    # unique when a wire ``Thesis`` carries >1 component of the same type.
    # ``ThesisRecord._check_mandatory_coverage`` requires ≥1 of each
    # {entry, target, invalidation} rationale but does not cap the count, so
    # multi-component-per-type is a first-class shape (ALP-699).
    per_type_index: dict[ThesisComponentType, int] = {}

    persisted_components: list[ThesisComponent] = []
    for wc in wire_components:
        component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[wc.component_type]
        idx = per_type_index.get(component_type, 0)
        per_type_index[component_type] = idx + 1
        persisted_components.append(
            ThesisComponent(
                component_id=f"{thesis_id}-{component_type.value.lower()}-{idx:02d}",
                thesis_id=ThesisId(thesis_id),
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
    # injects placeholder components for any missing required type. Backfill
    # reuses ``per_type_index`` rather than a hardcoded ``-00`` so the
    # uniqueness invariant defends both code paths and stays decoupled from the
    # wire loop's starting index — backfill only ever fires for absent types
    # today, but the shared counter removes the implicit coupling.
    required_wire_types = (
        "entry_rationale",
        "target_rationale",
        "invalidation_rationale",
    )
    for required in required_wire_types:
        if required in seen_types:
            continue
        component_type = _OMS_COMPONENT_TYPE_TO_PERSISTED[required]
        idx = per_type_index.get(component_type, 0)
        per_type_index[component_type] = idx + 1
        persisted_components.append(
            ThesisComponent(
                component_id=f"{thesis_id}-{component_type.value.lower()}-{idx:02d}",
                thesis_id=ThesisId(thesis_id),
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
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary=summary,
        nature=_THESIS_NATURE_FROM_WIRE[thesis.nature],
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
    enforcement_binding: EnforcementBinding,
) -> BracketLeg:
    """Translate a wire-format invalidation leg to a persisted :class:`BracketLeg`.

    Per parent decision (G), wire-format and persisted leg shapes remain
    distinct — this helper bridges them at the writeback. Price legs use
    :class:`PriceTrigger` against the underlying; time legs use
    :class:`TimeTrigger` with the deadline; event legs use
    :class:`EventTrigger` (advisory only — no broker order).

    ``enforcement_binding`` (ADR-0003 / ALP-847) is the typed broker-vs-monitor
    binding the caller computes from instrument type + leg position. A TIME /
    EVENT leg has no broker counterpart, so the caller always passes
    ``MONITOR_ENFORCED`` for those; a PRICE_STOP is broker-enforced only when it
    is the first equity stop the native bracket carries.
    """
    if isinstance(wire_leg, PriceLeg):
        cmp = wire_leg.condition.comparator
        # PriceTrigger.direction: LTE for stop-on-decline (most common LONG
        # stop), GTE for stop-on-rise (most common SHORT stop / LONG target).
        direction: Literal["LTE", "GTE"] = "LTE" if cmp in ("<=", "<") else "GTE"
        return BracketLeg(
            leg_id=leg_id,
            leg_type=BracketLegType.PRICE_STOP,
            order_id=OrderId(leg_order_id) if leg_order_id is not None else None,
            trigger=PriceTrigger(
                underlying_ticker=make_symbol(wire_leg.condition.underlying_trigger or ticker),
                # ALP-462 — Price → float at the legacy PriceTrigger surface.
                threshold_usd=float(wire_leg.condition.trigger_price),
                direction=direction,
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            enforcement_binding=enforcement_binding,
            status=BracketLegStatus.PENDING_ACTIVATION,
            # ALP-852 — carry the thesis-invalidation signal onto the leg so the
            # continuous monitor selects the trigger evaluator by thesis nature
            # (underlying vs option-price / net-mark). 02d's command validator
            # already pins this consistent with the thesis nature.
            trigger_signal=_TRIGGER_SIGNAL_FROM_WIRE[wire_leg.trigger_signal],
        )
    if isinstance(wire_leg, TimeLeg):
        return BracketLeg(
            leg_id=leg_id,
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId(leg_order_id) if leg_order_id is not None else None,
            trigger=TimeTrigger(deadline=wire_leg.condition.deadline),
            enforcement=BracketLegEnforcement.MECHANICAL,
            enforcement_binding=enforcement_binding,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
    # EventLeg — soft, no broker order.
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.EVENT_INVALIDATION,
        order_id=None,
        trigger=EventTrigger(description=wire_leg.condition.event_description),
        enforcement=BracketLegEnforcement.ADVISORY,
        enforcement_binding=enforcement_binding,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _target_to_bracket_leg(
    *,
    leg_id: str,
    target: Target,
    target_order_id: str,
    ticker: str,
    direction: Direction,
    enforcement_binding: EnforcementBinding,
) -> BracketLeg:
    """Translate the canonical :class:`Target` to a persisted TAKE_PROFIT leg.

    Long take-profit fires on price >= threshold (GTE); short on price <= (LTE).
    ``enforcement_binding`` (ALP-847) is BROKER_ENFORCED for an equity native
    bracket (the take-profit child Alpaca carries) and MONITOR_ENFORCED for an
    options / strategy position (no native bracket).
    """
    # ALP-462 — Price → float at the legacy PriceTrigger surface.
    threshold_usd = float(target.price) if target.price is not None else 0.01
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(target_order_id),
        trigger=PriceTrigger(
            underlying_ticker=make_symbol(ticker),
            threshold_usd=threshold_usd,
            direction="GTE" if direction == Direction.LONG else "LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        enforcement_binding=enforcement_binding,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _strategy_target_to_bracket_leg(
    *,
    leg_id: str,
    target: Target,
    target_order_id: str,
    ticker: str,
) -> BracketLeg:
    """Translate a strategy's :class:`Target` to a P/L-anchored TAKE_PROFIT leg.

    A multi-leg strategy take-profit references the strategy's *net P/L*, not a
    single-sided underlying-price threshold (parent ALP-588 decision F) — so
    the hard-coded-LONG ``PriceTrigger`` direction of :func:`_target_to_bracket_leg`
    is not applied here. The leg carries a :class:`PLAnchorSpec` (``spec_type="target"``)
    whose ``pct`` is the strategy profit fraction to capture; the strategy
    net-P/L evaluator (``bracket_stops/triggers.py`` :func:`evaluate_strategy_pl_target_trigger`)
    scores against ``pct * max_profit_usd`` and reads the strategy's cost basis
    straight off ``StrategyPositionDetails.net_premium_usd`` — so the anchor
    needs no ``actual_entry_price``.

    The wire ``Target.pl_percentage`` is a whole-number percentage (per the
    analyst output schema, "80 for +80%"); it is divided to the fraction the
    :class:`PLAnchorSpec` carries. The leg still carries the structurally
    required :class:`PriceTrigger` (the ``BracketLeg`` validator pairs a
    ``TAKE_PROFIT`` leg with a price trigger), but its direction is inert for
    a strategy — the ``pl_anchor`` drives firing.
    """
    # The OpenCommand validator (_validate_strategy_target_type) rejects a
    # non-pl_percentage strategy target at the command boundary (ALP-611), so a
    # validated strategy OPEN never reaches this guard — it is a defense-in-depth
    # backstop for direct / unvalidated construction.
    if target.target_type != "pl_percentage" or target.pl_percentage is None:
        msg = (
            f"strategy take-profit requires target_type='pl_percentage' with "
            f"pl_percentage set; got target_type={target.target_type!r}"
        )
        raise ValueError(msg)
    pct = target.pl_percentage / 100.0
    # ALP-462 — Price → float at the legacy PriceTrigger surface. ``Target``'s
    # validator requires ``price`` for every ``target_type``; it is the planned
    # price-equivalent of the P/L target.
    assert target.price is not None
    planned_price = float(target.price)
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(target_order_id),
        trigger=PriceTrigger(
            underlying_ticker=make_symbol(ticker),
            threshold_usd=planned_price,
            direction="GTE",  # inert for a strategy — pl_anchor drives firing
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        # ALP-847 — a strategy has no native bracket (Alpaca does not support
        # complex order classes on options), so the take-profit is always
        # monitor-enforced armed Intent.
        enforcement_binding=EnforcementBinding.MONITOR_ENFORCED,
        status=BracketLegStatus.PENDING_ACTIVATION,
        pl_anchor=PLAnchorSpec(
            spec_type="target",
            pct=pct,
            planned_entry_price=planned_price,
        ),
    )


def _capital_floor_price(command: OpenCommand) -> float | None:
    """The PnL-denominated capital-floor price per contract for an options OPEN.

    ``None`` for an equity OPEN (no floor) or a strategy OPEN (the single-leg
    floor is not submitted for multi-leg positions at this story). Otherwise
    delegates the arithmetic to the shared
    :func:`broker_adapter.order_options.floor_price_per_contract`, so the
    writeback's recorded floor price is byte-identical to the broker submission's
    (ALP-856 / CU1 — one formula, not two mirrored copies).
    """
    if not isinstance(command.instrument, OptionInstrument):
        return None
    floor = command.capital_protection_floor
    if floor is None:
        return None
    return floor_price_per_contract(
        dollar_value=float(command.position_size.dollar_value),
        max_loss=float(floor.max_loss),
        quantity=command.position_size.quantity,
    )


def _capital_floor_order_id(command_id: str) -> str:
    """The floor's durable OMS ``order_id``, derived from its real client_order_id.

    The capital floor is a tracked broker order with its own durable
    projection-cache ``OrderRow`` (ALP-856 / FS4), exactly as the entry is tracked
    through the ALP-836 atomic precommit/backfill machinery. Its OMS ``order_id``
    derives from the floor's deterministic ``client_order_id``
    (:func:`derive_capital_floor_client_order_id`) — NOT a synthetic ``alp-`` id
    (ADR-0003 invariant 5). Because :func:`synthesize_id_suffix` strips the
    broker-carried link and hashes the base id, and the floor's
    ``command_ordinal`` is shifted off the entry's, the floor's suffix differs
    from the entry's, so the two OMS order ids never collide.
    """
    floor_cid = derive_capital_floor_client_order_id(command_id)
    return f"ORD-FLOOR-{synthesize_id_suffix(floor_cid)}"


def _capital_floor_order_for_open(
    *,
    command: OpenCommand,
    command_id: str,
    ids: Mapping[str, str],
    floor_price: float | None,
    capital_floor_alpaca_order_id: str | None,
    timestamp: datetime,
) -> tuple[OrderRecord | None, str | None]:
    """Build the floor's durable OrderRow + its OMS order_id for an options OPEN.

    Returns ``(None, None)`` for an equity / strategy OPEN (no floor). For an
    options OPEN, mints the floor OMS order_id (the FK target the floor bracket leg
    references — derived from ``command_id``, the authoritative ``result.command_id``
    the precommit/backfill path also keys off) and the floor :class:`OrderRecord` the
    writeback persists, so the floor is a tracked broker order (ALP-856 / FS4) rather
    than an orphan.
    """
    if not isinstance(command.instrument, OptionInstrument) or floor_price is None:
        return None, None
    floor_order_id = _capital_floor_order_id(command_id)
    floor_order = _build_capital_floor_order(
        order_id=floor_order_id,
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        instrument=command.instrument,
        quantity=command.position_size.quantity,
        capital_floor_price=floor_price,
        capital_floor_alpaca_order_id=capital_floor_alpaca_order_id,
        pm_command_id=command_id,
        timestamp=timestamp,
    )
    return floor_order, floor_order_id


def _build_capital_floor_order(  # noqa: PLR0913 — distinct id / position / bracket / pricing threaded through, matching the other phase2 _build_* builders.
    *,
    order_id: str,
    position_id: str,
    bracket_id: str,
    thesis_id: str,
    instrument: OptionInstrument,
    quantity: float,
    capital_floor_price: float | None,
    capital_floor_alpaca_order_id: str | None,
    pm_command_id: str,
    timestamp: datetime,
) -> OrderRecord:
    """Build the options OPEN's durable capital-floor :class:`OrderRecord` (ALP-856).

    The floor is a tracked broker order, so it gets its own PENDING OrderRow keyed
    (at precommit) by the floor's deterministic ``client_order_id`` — the same
    ALP-836 atomic precommit/backfill machinery the entry rides. The floor *closes*
    the position (its broker side reverses the entry: a long floor SELLs on a
    decline → LTE / SELL, a short floor BUYs on a rise → GTE / BUY), and is a resting
    ``stop_limit`` at the PnL-denominated ``capital_floor_price``. ``alpaca_order_id``
    is NULL at pre-commit and backfilled from the broker submission's
    ``leg_alpaca_order_ids['capital_floor']`` after dispatch.
    """
    floor_direction = OrderDirection.SELL if instrument.direction == "long" else OrderDirection.BUY
    if capital_floor_price is not None:
        # The resting ``stop_limit`` collapses its stop_trigger + limit to the one
        # PnL-denominated floor price (mirrors ``order_options.submit_options_capital_floor``).
        floor_price = price(capital_floor_price)
        price_parameters = PriceParameters(
            limit_price=floor_price,
            stop_trigger_price=floor_price,
        )
    else:
        price_parameters = PriceParameters()
    return _build_pending_order(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.PRICE_STOP,
        order_class=OrderClass.SIMPLE,
        direction=floor_direction,
        order_type=OrderType.STOP_LIMIT,
        price_parameters=price_parameters,
        quantity=quantity,
        instrument_spec=OptionsInstrumentSpec(
            underlying=make_symbol(instrument.underlying),
            strike=float(instrument.strike),
            expiration=date.fromisoformat(instrument.expiration),
            contract_type=_CONTRACT_TYPE_FROM_WIRE[instrument.contract_type],
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        ),
        pm_command_id=pm_command_id,
        thesis_id=thesis_id,
        timestamp=timestamp,
        alpaca_order_id_override=capital_floor_alpaca_order_id,
    )


def _capital_floor_bracket_leg(
    *,
    bracket_id: str,
    ticker: str,
    capital_floor_order_id: str | None,
    capital_floor_price: float | None,
    direction: str | None,
) -> BracketLeg | None:
    """Build the options OPEN's broker-enforced capital-floor leg (ALP-856).

    ``None`` when no floor was submitted (equity / strategy OPEN — the floor is
    absent). Otherwise a BROKER_ENFORCED PRICE_STOP leg whose ``order_id`` is the
    floor's durable OMS ``order_id`` (the precommitted :class:`OrderRecord`), NOT
    the broker Alpaca id — so the DEFERRABLE FK to ``orders.order_id`` is satisfied
    (the merged design stamped the broker id here, which references no ``orders``
    row → ``FOREIGN KEY constraint failed`` under production ``foreign_keys=ON``).
    The closer's cancel-on-monitor-fire resolves this ``order_id`` → the floor
    OrderRow's ``alpaca_order_id`` → ``submitter.cancel_floor``. The monitor never
    fires it (``_is_active_eligible_leg`` drops broker-enforced legs), so the
    :class:`PriceTrigger` is the floor's recorded resting level, not a
    monitor-evaluated condition. A long floor closes by SELLing on a decline (LTE);
    a short floor BUYs on a rise (GTE).
    """
    if capital_floor_order_id is None and capital_floor_price is None:
        return None
    # A positive structural threshold keeps the PriceTrigger valid even if the
    # floor price is unavailable.
    threshold = capital_floor_price if capital_floor_price is not None else 0.01
    return BracketLeg(
        leg_id=f"{bracket_id}-leg-floor",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=(OrderId(capital_floor_order_id) if capital_floor_order_id is not None else None),
        trigger=PriceTrigger(
            underlying_ticker=make_symbol(ticker),
            threshold_usd=threshold,
            direction="GTE" if direction == "short" else "LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        enforcement_binding=EnforcementBinding.BROKER_ENFORCED,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )


def _build_pending_bracket(  # noqa: PLR0913 — the OPEN bracket threads its id graph + sizing + the optional floor id.
    *,
    bracket_id: str,
    position_id: str,
    entry_order_id: str,
    target: Target,
    target_order_id: str,
    invalidation_leg_orders: tuple[tuple[InvalidationLeg, str | None], ...],
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
    entry_window_deadline: datetime | None,
    capital_floor_order_id: str | None = None,
    capital_floor_price: float | None = None,
) -> BracketRecord:
    """Build a PENDING_ENTRY bracket.

    Take-profit leg comes from ``target``; one leg per ``invalidation_leg``
    entry. The bracket record carries no creation timestamp; per-leg
    submission timestamps live on the broker orders.

    ``entry_window_deadline`` is the analyst's patient-entry deadline threaded
    from ``command.entry_window`` (ALP-737); ``None`` when the recommendation
    carried no window. The continuous monitor auto-cancels a still-
    ``PENDING_ENTRY`` entry once ``now() > entry_window_deadline`` per the
    ``BracketRecord`` lifecycle contract (``orders.py``).

    For a :class:`StrategyInstrument`, the take-profit leg is P/L-anchored
    (see :func:`_strategy_target_to_bracket_leg`) — it references the
    strategy's net P/L, not a single-sided underlying-price threshold. For
    equity / single-leg options the take-profit keeps its plain
    underlying-price :class:`PriceTrigger` (see :func:`_target_to_bracket_leg`).

    Enforcement binding (ADR-0003 / ALP-847): only an **equity** OPEN has a
    native Alpaca bracket, which carries exactly one take-profit child + one
    stop child — so the take-profit and the FIRST price-stop are
    broker-enforced. A secondary equity price-stop, every time/event leg, and
    every options / strategy leg (no native bracket on options) are
    monitor-enforced armed Intent.
    """
    ticker = _instrument_ticker_key(instrument)
    is_equity = isinstance(instrument, EquityInstrument)
    if isinstance(instrument, StrategyInstrument):
        target_leg = _strategy_target_to_bracket_leg(
            leg_id=f"{bracket_id}-leg-target",
            target=target,
            target_order_id=target_order_id,
            ticker=ticker,
        )
    else:
        target_leg = _target_to_bracket_leg(
            leg_id=f"{bracket_id}-leg-target",
            target=target,
            target_order_id=target_order_id,
            ticker=ticker,
            direction=Direction.LONG,
            # Equity native bracket carries the take-profit child; options have
            # no native bracket → monitor-enforced.
            enforcement_binding=(
                EnforcementBinding.BROKER_ENFORCED
                if is_equity
                else EnforcementBinding.MONITOR_ENFORCED
            ),
        )
    invalidation_legs: list[BracketLeg] = []
    # The native equity bracket carries exactly one stop child — the first
    # PriceLeg (matching ``order_equity._bracket_params`` and the ALP-746 leg-id
    # capture). It is broker-enforced; all subsequent stops and all time legs are
    # monitor-enforced.
    first_equity_stop_taken = False
    for idx, (wire_leg, leg_order_id) in enumerate(invalidation_leg_orders):
        if is_equity and isinstance(wire_leg, PriceLeg) and not first_equity_stop_taken:
            first_equity_stop_taken = True
            leg_binding = EnforcementBinding.BROKER_ENFORCED
        else:
            leg_binding = EnforcementBinding.MONITOR_ENFORCED
        invalidation_legs.append(
            _wire_leg_to_bracket_leg(
                leg_id=f"{bracket_id}-leg-inv{idx}",
                wire_leg=wire_leg,
                leg_order_id=leg_order_id,
                ticker=ticker,
                enforcement_binding=leg_binding,
            )
        )
    # ALP-856 — an options OPEN carries an always-on broker-enforced capital
    # floor (a resting GTC ``stop_limit`` submitted at dispatch). It is recorded
    # as a dedicated BROKER_ENFORCED PRICE_STOP leg whose ``order_id`` is the
    # floor's durable OMS ``order_id`` (the precommitted floor OrderRow) so the
    # DEFERRABLE FK to ``orders.order_id`` is satisfied; cancel-on-monitor-fire
    # then resolves that order_id → the floor OrderRow's ``alpaca_order_id`` and
    # cancels the resting floor by it. Equity / strategy OPENs never carry one (no
    # single-leg options floor is submitted for them), so no leg is added.
    floor_leg = _capital_floor_bracket_leg(
        bracket_id=bracket_id,
        ticker=ticker,
        capital_floor_order_id=capital_floor_order_id,
        capital_floor_price=capital_floor_price,
        direction=(instrument.direction if isinstance(instrument, OptionInstrument) else None),
    )
    floor_legs = (floor_leg,) if floor_leg is not None else ()
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(entry_order_id),
        protective_legs=(target_leg, *invalidation_legs, *floor_legs),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=entry_window_deadline,
    )
