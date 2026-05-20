"""OPEN command writeback (capital reservation, position, thesis, bracket)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
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
    TimeLeg,
)
from alphamind.commands.submission_results import SubmissionResult
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.write_paths.phase2._shared import (
    _OMS_COMPONENT_TYPE_TO_PERSISTED,
    _build_entry_order_from_command,
    _build_pending_order,
    _emit,
    _emit_capital_reserved,
    _emit_order_submitted,
    _id_suffix,
    _instrument_ticker_key,
    _order_direction_for_close,
    _order_position_direction,
    _price_to_float,
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
    EventTrigger,
    OrderClass,
    OrderRecord,
    OrderRole,
    OrderType,
    PLAnchorSpec,
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
    PositionDetailsPayload,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
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

    When ``submitted_alpaca_order_id`` is supplied (broker-routing coordinated
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
    # Order builders require a non-None Direction; derive it once from the
    # pending position (strategy positions yield the inert Direction.LONG
    # placeholder — see _order_position_direction).
    order_direction = _order_position_direction(position)
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
        instrument=command.instrument,
    )
    entry_order = _build_entry_order_from_command(
        order_id=ids["entry_order_id"],
        position_id=ids["position_id"],
        bracket_id=ids["bracket_id"],
        thesis_id=ids["thesis_id"],
        ticker=ticker,
        entry_order=command.entry_order,
        quantity=command.position_size.quantity,
        direction=order_direction,
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
        direction=order_direction,
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
                direction=order_direction,
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
    _emit_capital_reserved(
        handle,
        order_id=ids["entry_order_id"],
        position_id=ids["position_id"],
        thesis_id=ids["thesis_id"],
        amount_usd=command.position_size.dollar_value,
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
    suffix = _id_suffix(command_id)
    return {
        "position_id": f"POS-{ticker}-{suffix}",
        "thesis_id": f"THE-{ticker}-{suffix}",
        "bracket_id": f"BRK-{ticker}-{suffix}",
        "entry_order_id": f"ORD-{ticker}-entry-{suffix}",
        "stop_leg_order_id": f"ORD-{ticker}-stop-{suffix}",
    }


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
        price_parameters = PriceParameters(limit_price=_price_to_float(target.price))
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
        trigger_price = _price_to_float(wire_leg.condition.trigger_price)
        if persisted_order_type == OrderType.STOP_LIMIT:
            price_parameters = PriceParameters(
                limit_price=_price_to_float(wire_leg.order_parameters.limit_price),
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
                    underlying_ticker=Symbol(instrument.underlying),
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
            underlying_ticker=Symbol(instrument.underlying),
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
            ticker=Symbol(instrument.ticker),
            share_count=0.0,
            average_cost_basis_per_share=0.0,
            borrow_rate_pct=0.0 if short_fields_present else None,
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
            order_id=OrderId(leg_order_id) if leg_order_id is not None else None,
            trigger=PriceTrigger(
                underlying_ticker=Symbol(wire_leg.condition.underlying_trigger or ticker),
                # ALP-462 — Price → float at the legacy PriceTrigger surface.
                threshold_usd=float(wire_leg.condition.trigger_price),
                direction=direction,
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.PENDING_ACTIVATION,
        )
    if isinstance(wire_leg, TimeLeg):
        return BracketLeg(
            leg_id=leg_id,
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=OrderId(leg_order_id) if leg_order_id is not None else None,
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
    # ALP-462 — Price → float at the legacy PriceTrigger surface.
    threshold_usd = float(target.price) if target.price is not None else 0.01
    return BracketLeg(
        leg_id=leg_id,
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(target_order_id),
        trigger=PriceTrigger(
            underlying_ticker=Symbol(ticker),
            threshold_usd=threshold_usd,
            direction="GTE" if direction == Direction.LONG else "LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
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
            underlying_ticker=Symbol(ticker),
            threshold_usd=planned_price,
            direction="GTE",  # inert for a strategy — pl_anchor drives firing
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
        pl_anchor=PLAnchorSpec(
            spec_type="target",
            pct=pct,
            planned_entry_price=planned_price,
        ),
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
    instrument: EquityInstrument | OptionInstrument | StrategyInstrument,
) -> BracketRecord:
    """Build a PENDING_ENTRY bracket.

    Take-profit leg comes from ``target``; one leg per ``invalidation_leg``
    entry. The bracket record carries no creation timestamp; per-leg
    submission timestamps live on the broker orders.

    For a :class:`StrategyInstrument`, the take-profit leg is P/L-anchored
    (see :func:`_strategy_target_to_bracket_leg`) — it references the
    strategy's net P/L, not a single-sided underlying-price threshold. For
    equity / single-leg options the take-profit keeps its plain
    underlying-price :class:`PriceTrigger` (see :func:`_target_to_bracket_leg`).
    """
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
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(entry_order_id),
        protective_legs=(target_leg, *invalidation_legs),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )
