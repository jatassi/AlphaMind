"""Multi-leg (mleg) order POST translation (story ALP-382).

Translates canonical OMS commands carrying ``StrategyInstrument`` into
alpaca-py ``OrderClass.MLEG`` requests with up to 4 legs, per-leg
``position_intent``, and ``ratio_qty`` in simplified form (GCD = 1). Submits
via ``TradingClient.submit_order(...)`` wrapped by
``alphamind.execution.broker_adapter.retry.submit_with_retry``.

Per ``broker-adapter.md § Supported instruments`` and ``§ Order submission``:

* Mleg orders carry up to 4 option legs sharing one underlying.
* All legs are options (no equity legs in an mleg per the design).
* Time-in-force is ``DAY`` (the only TIF Alpaca accepts on options).
* Bracket / OCO / OTO are unsupported on mleg — protective management is
  monitor-driven on the underlying equity stream.

This module enforces only Alpaca-level constraints (leg count, single
underlying, all-options, valid ratios). Strategy-type structural validation
(e.g., "iron_condor must have 4 legs") lives upstream in canonical command
validation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from math import gcd
from typing import Any, Literal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId, OccSymbol
from alphamind.commands.command_models import (
    AddCommand,
    CloseCommand,
    OpenCommand,
    StrategyInstrument,
    StrategyLeg,
    StrategyType,
)
from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.retry import (
    SubmissionOutcome,
    Submitted,
    submit_with_retry,
)
from alphamind.execution.oms.command_ids import is_engine_originated, is_pm_originated
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
)
from alphamind.portfolio_state.records.positions import (
    StrategyLeg as PositionStrategyLeg,
)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


PositionIntentLiteral = Literal["buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"]


@dataclass(frozen=True)
class MLEGLegAck:
    """Per-leg child of an mleg parent acknowledgment."""

    occ_symbol: OccSymbol
    side: Literal["buy", "sell"]
    ratio_qty: int
    position_intent: PositionIntentLiteral


@dataclass(frozen=True)
class MLEGSubmission:
    """Alpaca's acknowledgment record for a submitted mleg order."""

    alpaca_order_id: AlpacaOrderId
    client_order_id: ClientOrderId
    status: str
    legs: tuple[MLEGLegAck, ...]
    strategy_type: StrategyType


# ---------------------------------------------------------------------------
# OCC symbol construction
# ---------------------------------------------------------------------------


def _build_occ_symbol(
    underlying: str,
    expiration: date,
    contract_type: OptionContractType,
    strike: float,
) -> str:
    """Construct the 21-character OCC option symbol.

    Format: ``{ROOT:6}{YY:2}{MM:2}{DD:2}{C|P}{STRIKE:8}`` where the strike is
    in thousandths zero-padded to 8 digits. The root is left-justified and
    space-padded to 6 characters.

    Local helper — sibling story 02c (ALP-381) ships ``build_occ_symbol`` as
    the canonical version. Once 02c lands, this can defer to that import.
    """
    root = underlying.upper().ljust(6)
    yymmdd = expiration.strftime("%y%m%d")
    cp = "C" if contract_type == OptionContractType.CALL else "P"
    strike_thousandths = round(strike * 1000)
    return f"{root}{yymmdd}{cp}{strike_thousandths:08d}"


def _parse_expiration(expiration_str: str) -> date:
    """Parse the ISO-8601 ``YYYY-MM-DD`` expiration string the canonical
    ``StrategyLeg.expiration`` carries."""
    return date.fromisoformat(expiration_str)


_CONTRACT_TYPE_ENUM: dict[str, OptionContractType] = {
    "call": OptionContractType.CALL,
    "put": OptionContractType.PUT,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_client_order_id(client_order_id: str) -> None:
    """Reject malformed ``client_order_id`` per ``oms-command-ids.md``.

    Delegates to :func:`oms.command_ids.is_pm_originated` /
    :func:`is_engine_originated` so the broker adapter shares one pattern
    definition with the OMS intake.
    """
    if not client_order_id:
        msg = "client_order_id must be non-empty"
        raise ValueError(msg)
    if not (is_pm_originated(client_order_id) or is_engine_originated(client_order_id)):
        msg = (
            f"client_order_id {client_order_id!r} does not match the canonical "
            f"OMS command-ID pattern (PM-originated 'inv-...' or "
            f"engine-originated 'MON....')"
        )
        raise ValueError(msg)


def _require_strategy_instrument(command: OpenCommand) -> StrategyInstrument:
    """Assert the command carries a ``StrategyInstrument``; the dispatcher
    routes equity / single-leg options elsewhere."""
    instrument = command.instrument
    if not isinstance(instrument, StrategyInstrument):
        msg = (
            f"submit_mleg_* requires StrategyInstrument; got "
            f"{type(instrument).__name__}. Equity → submit_equity_*, "
            f"single-leg option → submit_options_*."
        )
        raise TypeError(msg)
    return instrument


def _validate_alpaca_constraints(legs: Sequence[object], underlying: str) -> None:
    """Enforce Alpaca-level mleg leg-count and underlying-presence constraints.

    Alpaca rejects mleg orders outside [2, 4] legs and any mleg without a
    resolved underlying. The leg-count bound mirrors ``alpaca-py``'s own
    ``OrderRequest.root_validator`` — we surface it ahead of the SDK call so
    callers see a uniform ``ValueError`` regardless of which validator fires
    first. The single-underlying invariant is enforced separately at the
    leg-set level (``_underlying_from_legs``).
    """
    if len(legs) > 4:
        msg = f"mleg supports up to 4 legs; got {len(legs)}"
        raise ValueError(msg)
    if len(legs) < 2:
        msg = f"mleg requires at least 2 legs; got {len(legs)}"
        raise ValueError(msg)
    if not underlying:
        msg = "mleg requires a non-empty underlying"
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# Ratio simplification & per-leg construction
# ---------------------------------------------------------------------------


def _simplify_ratios(ratios: Iterable[int]) -> tuple[int, ...]:
    """Divide each ratio by the gcd of all ratios.

    Alpaca rejects mleg requests whose leg ratios share a common factor > 1
    (per ``broker-adapter.md``: ratios in simplified form, GCD = 1). For
    ``(2, 4, 2, 4)`` → ``(1, 2, 1, 2)``; for ``(1, 1)`` → ``(1, 1)``.
    """
    ratio_list = list(ratios)
    common = ratio_list[0]
    for value in ratio_list[1:]:
        common = gcd(common, value)
    return tuple(value // common for value in ratio_list)


_OPEN_INTENT_FOR_DIRECTION: dict[str, PositionIntent] = {
    "long": PositionIntent.BUY_TO_OPEN,
    "short": PositionIntent.SELL_TO_OPEN,
}

_OPEN_SIDE_FOR_DIRECTION: dict[str, OrderSide] = {
    "long": OrderSide.BUY,
    "short": OrderSide.SELL,
}


def _build_open_legs(legs: Sequence[StrategyLeg], underlying: str) -> list[OptionLegRequest]:
    """Build alpaca-py ``OptionLegRequest`` objects for an OPEN.

    Ratios are simplified to GCD = 1 before being submitted.
    """
    simplified = _simplify_ratios(leg.quantity_ratio for leg in legs)
    requests: list[OptionLegRequest] = []
    for leg, ratio in zip(legs, simplified, strict=True):
        # ALP-462 — strike is ``Price`` (Decimal); cast to float for the OCC
        # symbol builder which still carries the legacy float surface.
        occ = _build_occ_symbol(
            underlying,
            _parse_expiration(leg.expiration),
            _CONTRACT_TYPE_ENUM[leg.contract_type],
            float(leg.strike),
        )
        requests.append(
            OptionLegRequest(
                symbol=occ,
                ratio_qty=ratio,
                side=_OPEN_SIDE_FOR_DIRECTION[leg.direction],
                position_intent=_OPEN_INTENT_FOR_DIRECTION[leg.direction],
            )
        )
    return requests


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------


def _build_request(
    *,
    legs: list[OptionLegRequest],
    qty: float,
    client_order_id: str,
    limit_price: float | None,
) -> MarketOrderRequest | LimitOrderRequest:
    """Construct a ``MarketOrderRequest`` (no limit) or ``LimitOrderRequest``
    (net debit/credit limit) wrapping the legs as an mleg."""
    if limit_price is not None:
        return LimitOrderRequest(
            qty=qty,
            order_class=OrderClass.MLEG,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            legs=legs,
            limit_price=limit_price,
        )
    return MarketOrderRequest(
        qty=qty,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
        legs=legs,
    )


def _legs_to_acks(
    leg_requests: Sequence[OptionLegRequest],
) -> tuple[MLEGLegAck, ...]:
    """Translate alpaca-py request legs into the public ``MLEGLegAck`` tuple.

    Each request leg always carries both ``side`` and ``position_intent``
    because the translator populates them; the ``Optional`` typing on
    ``OptionLegRequest`` is for API symmetry with ``OrderRequest``.
    PositionIntent / OrderSide enum values match our literal alphabet by
    construction.
    """
    return tuple(
        MLEGLegAck(
            occ_symbol=OccSymbol(leg.symbol),
            side=_required_side(leg).value,
            ratio_qty=int(leg.ratio_qty),
            position_intent=_required_intent(leg).value,
        )
        for leg in leg_requests
    )


def _required_side(leg: OptionLegRequest) -> OrderSide:
    if leg.side is None:
        msg = f"OptionLegRequest missing side: {leg!r}"
        raise AssertionError(msg)
    return leg.side


def _required_intent(leg: OptionLegRequest) -> PositionIntent:
    if leg.position_intent is None:
        msg = f"OptionLegRequest missing position_intent: {leg!r}"
        raise AssertionError(msg)
    return leg.position_intent


async def _submit(
    *,
    client: TradingClient,
    request: MarketOrderRequest | LimitOrderRequest,
    execution: ExecutionConfig,
    strategy_type: StrategyType,
    leg_requests: Sequence[OptionLegRequest],
) -> SubmissionOutcome[MLEGSubmission]:
    """Wrap the SDK call in ``submit_with_retry`` and adapt the result.

    The Alpaca call is sync; ``asyncio.to_thread`` keeps the event loop
    responsive while the request is in flight. ``client_order_id`` is
    embedded on ``request`` itself, so it is not threaded again here.
    """

    async def call() -> Any:
        return await asyncio.to_thread(client.submit_order, request)

    outcome = await submit_with_retry(
        call, window_seconds=execution.submission_retry_window_seconds
    )
    if isinstance(outcome, Submitted):
        order = outcome.payload
        submission = MLEGSubmission(
            alpaca_order_id=AlpacaOrderId(str(order.id)),
            client_order_id=ClientOrderId(order.client_order_id),
            status=order.status.value,
            legs=_legs_to_acks(leg_requests),
            strategy_type=strategy_type,
        )
        return Submitted(payload=submission, attempt_count=outcome.attempt_count)
    return outcome


# ---------------------------------------------------------------------------
# Public submission API
# ---------------------------------------------------------------------------


async def submit_mleg_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[MLEGSubmission]:
    """Translate an OPEN command on a strategy and submit as an mleg order."""
    _validate_client_order_id(client_order_id)
    instrument = _require_strategy_instrument(command)
    _validate_alpaca_constraints(instrument.legs, instrument.underlying)

    leg_requests = _build_open_legs(instrument.legs, instrument.underlying)
    request = _build_request(
        legs=leg_requests,
        qty=command.position_size.quantity,
        client_order_id=client_order_id,
        # ALP-462 — Price → float at the Alpaca SDK boundary.
        limit_price=(
            float(command.entry_order.limit_price)
            if command.entry_order.limit_price is not None
            else None
        ),
    )
    return await _submit(
        client=client,
        request=request,
        execution=execution,
        strategy_type=instrument.strategy_type,
        leg_requests=leg_requests,
    )


_INTENT_ENUM_BY_LITERAL: dict[PositionIntentLiteral, PositionIntent] = {
    "buy_to_open": PositionIntent.BUY_TO_OPEN,
    "sell_to_open": PositionIntent.SELL_TO_OPEN,
    "buy_to_close": PositionIntent.BUY_TO_CLOSE,
    "sell_to_close": PositionIntent.SELL_TO_CLOSE,
}

_SIDE_ENUM_BY_LITERAL: dict[Literal["buy", "sell"], OrderSide] = {
    "buy": OrderSide.BUY,
    "sell": OrderSide.SELL,
}


# ---------------------------------------------------------------------------
# The single open→close inversion seam
# ---------------------------------------------------------------------------


# A strategy leg is stored in the position record by its OPEN ``direction``
# (LONG / SHORT = how it was opened). Closing reverses each leg: a LONG-opened
# leg is sold to close; a SHORT-opened leg is bought to close.
_CLOSE_SIDE_FOR_DIRECTION: dict[Direction, Literal["buy", "sell"]] = {
    Direction.LONG: "sell",
    Direction.SHORT: "buy",
}

_CLOSE_INTENT_FOR_DIRECTION: dict[Direction, PositionIntentLiteral] = {
    Direction.LONG: "sell_to_close",
    Direction.SHORT: "buy_to_close",
}

_CLOSE_INTENTS: frozenset[PositionIntentLiteral] = frozenset({"buy_to_close", "sell_to_close"})


def strategy_legs_to_close_acks(
    legs: Sequence[PositionStrategyLeg],
) -> tuple[MLEGLegAck, ...]:
    """Convert a position's open-side strategy legs into close-side ``MLEGLegAck`` legs.

    This is the *single* seam where the open→close inversion happens. Each
    persisted :class:`StrategyLeg` carries the ``direction`` it was opened
    with; closing reverses it — a LONG-opened leg closes ``sell`` /
    ``sell_to_close``, a SHORT-opened leg closes ``buy`` / ``buy_to_close``.
    Every strategy-CLOSE caller (the OMS engine envelope, the bracket-stop
    wiring, the PM submit-envelope dispatcher) routes through this helper so
    the inversion is applied exactly once.

    A leg with ``direction is None`` cannot be reversed and raises
    :class:`ValueError`.

    Each leg's ``ratio_qty`` is ``1``: the strategy unit count rides on the
    request-level ``qty``, so this assumes a 1:1 leg structure (vertical
    spreads, iron condors). Ratio strategies (e.g. 1x2) are not represented.
    """
    acks: list[MLEGLegAck] = []
    for leg in legs:
        direction = leg.direction
        if direction is None:
            msg = f"strategy leg {leg.leg_id!r} has no direction set; cannot build a close-side leg"
            raise ValueError(msg)
        opt = leg.options
        occ = _build_occ_symbol(
            opt.underlying_ticker,
            opt.expiration_date,
            opt.contract_type,
            opt.strike_price,
        )
        acks.append(
            MLEGLegAck(
                occ_symbol=OccSymbol(occ),
                side=_CLOSE_SIDE_FOR_DIRECTION[direction],
                ratio_qty=1,
                position_intent=_CLOSE_INTENT_FOR_DIRECTION[direction],
            )
        )
    return tuple(acks)


def _build_close_legs(close_legs: Sequence[MLEGLegAck]) -> list[OptionLegRequest]:
    """Translate close-side ``MLEGLegAck`` legs into alpaca-py ``OptionLegRequest``s.

    ``close_legs`` already carry close-side ``side`` / ``position_intent`` —
    the open→close inversion happens upstream at
    :func:`strategy_legs_to_close_acks`. This is a straight literal → enum
    translation (mirroring :func:`_build_scaled_open_legs`); it rejects any
    leg whose ``position_intent`` is not a ``*_to_close`` value.
    """
    requests: list[OptionLegRequest] = []
    for leg in close_legs:
        if leg.position_intent not in _CLOSE_INTENTS:
            msg = (
                f"submit_mleg_close: leg has non-close position_intent "
                f"{leg.position_intent!r}; expected buy_to_close or sell_to_close"
            )
            raise ValueError(msg)
        requests.append(
            OptionLegRequest(
                symbol=leg.occ_symbol,
                ratio_qty=leg.ratio_qty,
                side=_SIDE_ENUM_BY_LITERAL[leg.side],
                position_intent=_INTENT_ENUM_BY_LITERAL[leg.position_intent],
            )
        )
    return requests


def _build_scaled_open_legs(open_legs: Sequence[MLEGLegAck], scale: int) -> list[OptionLegRequest]:
    """Build ADD-side leg requests by scaling open ratios then re-simplifying.

    ADD on a strategy adds ``additional_quantity`` of the existing strategy
    units; per-leg ratios scale proportionally and re-simplify to GCD = 1
    before submission. ``position_intent`` keeps the original open intent —
    no inversion, since the ADD is opening more of the same exposure.
    """
    scaled = _simplify_ratios(leg.ratio_qty * scale for leg in open_legs)
    requests: list[OptionLegRequest] = []
    for leg, ratio in zip(open_legs, scaled, strict=True):
        requests.append(
            OptionLegRequest(
                symbol=leg.occ_symbol,
                ratio_qty=ratio,
                side=_SIDE_ENUM_BY_LITERAL[leg.side],
                position_intent=_INTENT_ENUM_BY_LITERAL[leg.position_intent],
            )
        )
    return requests


async def submit_mleg_close(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    close_legs: Sequence[MLEGLegAck],
    strategy_type: StrategyType,
    position_units: float | None = None,
) -> SubmissionOutcome[MLEGSubmission]:
    """Translate a CLOSE on a strategy and submit as a close-side mleg.

    ``close_legs`` carry close-side ``side`` / ``position_intent`` already —
    the open→close inversion happens upstream at the single seam
    :func:`strategy_legs_to_close_acks`. ``close_legs`` and ``strategy_type``
    thread from portfolio state because the canonical ``CloseCommand``
    references the position by ID, not by leg structure. ``position_units``
    is the strategy's open unit count and is required when
    ``command.quantity == "all"`` (mirroring story 02b's ``position_qty``).
    """
    _validate_client_order_id(client_order_id)
    _validate_alpaca_constraints(close_legs, _underlying_from_legs(close_legs))

    leg_requests = _build_close_legs(close_legs)
    qty = _close_qty(command, position_units)
    request = _build_request(
        legs=leg_requests,
        qty=qty,
        client_order_id=client_order_id,
        # ALP-462 — Price → float at the Alpaca SDK boundary.
        limit_price=float(command.limit_price) if command.limit_price is not None else None,
    )
    return await _submit(
        client=client,
        request=request,
        execution=execution,
        strategy_type=strategy_type,
        leg_requests=leg_requests,
    )


async def submit_mleg_add(
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    open_legs: Sequence[MLEGLegAck],
    strategy_type: StrategyType,
) -> SubmissionOutcome[MLEGSubmission]:
    """Translate an ADD on a strategy and submit as a scaled-ratios mleg.

    The translator multiplies each open leg's ratio by the additional unit
    count and re-simplifies before submission; ``position_intent`` matches
    the original open intent (no inversion).
    """
    _validate_client_order_id(client_order_id)
    _validate_alpaca_constraints(open_legs, _underlying_from_legs(open_legs))

    # Pydantic guarantees additional_quantity > 0; mleg additionally requires
    # an integer scale (per-leg ratios are integers — fractional strategy
    # units would not re-simplify cleanly).
    additional_units = int(command.additional_quantity)
    if additional_units != command.additional_quantity:
        msg = (
            f"AddCommand.additional_quantity must be an integer for an mleg "
            f"ADD; got {command.additional_quantity}"
        )
        raise ValueError(msg)
    leg_requests = _build_scaled_open_legs(open_legs, additional_units)
    request = _build_request(
        legs=leg_requests,
        qty=command.additional_quantity,
        client_order_id=client_order_id,
        # ALP-462 — Price → float at the Alpaca SDK boundary.
        limit_price=(
            float(command.entry_order.limit_price)
            if command.entry_order.limit_price is not None
            else None
        ),
    )
    return await _submit(
        client=client,
        request=request,
        execution=execution,
        strategy_type=strategy_type,
        leg_requests=leg_requests,
    )


def _underlying_from_legs(legs: Sequence[MLEGLegAck]) -> str:
    """Recover the underlying root from the OCC symbols' shared prefix.

    Used by close/add validators to confirm the leg set is internally
    consistent (single underlying); deeper than relying on caller state to
    pass it explicitly.
    """
    if not legs:
        return ""
    roots = {leg.occ_symbol[:6].rstrip() for leg in legs}
    if len(roots) > 1:
        msg = f"legs span multiple underlyings: {sorted(roots)}"
        raise ValueError(msg)
    return next(iter(roots))


def _close_qty(command: CloseCommand, position_units: float | None) -> float:
    """Resolve the ``qty`` to send for a strategy close.

    ``CloseCommand.quantity`` of ``"all"`` closes every open unit; the unit
    count is threaded from portfolio state via ``position_units`` (the
    open-legs ack carries the per-strategy ratio, not the count). For numeric
    ``quantity``, that value is used directly.
    """
    if command.quantity == "all":
        if position_units is None:
            msg = (
                "submit_mleg_close requires position_units when "
                'CloseCommand.quantity == "all" '
                "(OMS threads the strategy's open unit count from state)"
            )
            raise ValueError(msg)
        return float(position_units)
    return float(command.quantity)
