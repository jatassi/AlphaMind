"""Single-leg options OMS-command translation (story 02c / ALP-381).

Translates canonical OMS commands carrying ``OptionInstrument`` into
``alpaca-py`` order requests targeting OCC option symbols and submits them via
``TradingClient.submit_order(...)`` wrapped by
:func:`alphamind.execution.broker_adapter.retry.submit_with_retry`.

Per ``broker-adapter.md § Order types``, ``§ Time-in-force``, ``§ Order
classes``: options on Alpaca are day-only, simple-class only — the translator
hard-codes ``time_in_force=DAY`` and ``order_class=SIMPLE`` regardless of the
command's invalidation-leg shape. The continuous monitor manages protective
legs by watching the underlying equity stream
(``orders-and-brackets.md § Options price-based stops: trigger on the
underlying``); the broker submission is always SIMPLE.

Permanent rejections defined in ``broker-adapter.md § Options-specific
rejections`` (``options_level_not_approved``, ``contract_expired``,
``underlying_halted``, ``asset_not_tradable``) are surfaced to the caller as
:class:`PermanentRejectionError`. The OMS broker-routing coordinated swap
(story 03e) translates these into synchronous OMS rejections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal, cast

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.models import Order
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    OrderRequest,
    StopLimitOrderRequest,
)

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId, OccSymbol, make_occ_symbol
from alphamind._kernel.money import Price
from alphamind.commands.command_models import (
    AddCommand,
    CloseCommand,
    EntryOrder,
    OpenCommand,
    OptionInstrument,
)
from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter.errors import (
    PermanentRejection,
    classify_alpaca_error,
)
from alphamind.execution.broker_adapter.retry import (
    GatewaySubmissionFailed,
    SubmissionOutcome,
    Submitted,
    bounded_broker_call,
    submit_with_retry,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.oms.command_ids import (
    derive_pm_command_id,
    is_engine_originated,
    is_pm_originated,
    parse_pm_command_id,
)
from alphamind.portfolio_state.records.positions import OptionContractType

# The capital floor is a second broker order submitted alongside the options
# entry on the same OPEN, so it needs its OWN ``client_order_id`` distinct from
# the entry's (Alpaca rejects a duplicate). The floor id is the entry's
# PM-originated id re-derived with the ``command_ordinal`` shifted by this
# reserved offset: it stays pattern-valid, carries the SAME thesis + invocation
# FK (so a floor fill self-attributes, ALP-844), and cannot collide with a real
# sibling command ordinal (no envelope carries this many commands).
_CAPITAL_FLOOR_ORDINAL_OFFSET = 1_000_000


@dataclass(frozen=True)
class OptionsSubmission:
    """Alpaca's acknowledgment record for a submitted single-leg options order."""

    alpaca_order_id: AlpacaOrderId
    client_order_id: ClientOrderId
    occ_symbol: OccSymbol
    status: str
    order_class: str  # always "simple" for options


class PermanentRejectionError(Exception):
    """A submission was rejected by Alpaca for a non-retriable reason.

    Carries the typed :class:`PermanentRejection` so the caller (broker-routing
    coordinated swap, story 03e) can translate it into a synchronous OMS
    rejection per ``broker-adapter.md § Order submission``.
    """

    def __init__(self, rejection: PermanentRejection) -> None:
        super().__init__(
            f"Alpaca rejected: code={rejection.code}, "
            f"http_status={rejection.http_status}, "
            f"message={rejection.alpaca_message!r}"
        )
        self.rejection = rejection


# ---------------------------------------------------------------------------
# OCC symbol construction
# ---------------------------------------------------------------------------


def build_occ_symbol(
    underlying: str,
    expiration: date,
    contract_type: OptionContractType,
    strike: float,
) -> str:
    """Construct the 21-character OCC option symbol per the standard.

    Format: ``{ROOT:6}{YY:2}{MM:2}{DD:2}{C|P}{STRIKE:8}`` — root is left-aligned
    and space-padded to six characters; date is six digits ``YYMMDD``;
    contract type is ``C`` for CALL or ``P`` for PUT; strike is the price in
    thousandths of a dollar, zero-padded to eight digits (e.g., ``800.0`` →
    ``00800000`` and ``12.50`` → ``00012500``).

    Share-class tickers (e.g., ``BRK.B``, ``BF.B``) drop the dot in the OCC
    root field per OCC convention — ``BRK.B`` → ``BRKB``.

    Examples:
        >>> build_occ_symbol("NVDA", date(2024, 3, 15), OptionContractType.CALL, 800.0)
        'NVDA  240315C00800000'
    """
    root = underlying.upper().replace(".", "").ljust(6)
    yymmdd = expiration.strftime("%y%m%d")
    cp = "C" if contract_type is OptionContractType.CALL else "P"
    # ``round`` to avoid binary float drift on values like 12.50
    # (12500.000000001 -> 12500 in thousandths).
    strike_thousandths = round(strike * 1000)
    strike_field = f"{strike_thousandths:08d}"
    return f"{root}{yymmdd}{cp}{strike_field}"


# ---------------------------------------------------------------------------
# Submission API
# ---------------------------------------------------------------------------


async def submit_options_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[OptionsSubmission]:
    """Translate an OPEN command targeting an OptionInstrument and submit.

    Always SIMPLE class with ``time_in_force=DAY`` per Alpaca's options
    constraints. The continuous monitor manages protective legs against the
    underlying equity stream — bracket structure on the OMS command is
    intentionally not threaded onto the broker request.
    """
    instrument = _require_option_instrument(command.instrument, command_kind="open")
    occ_symbol = _occ_from_instrument(instrument)
    side = OrderSide.BUY if instrument.direction == "long" else OrderSide.SELL
    request = _build_request(
        order=command.entry_order,
        symbol=occ_symbol,
        side=side,
        qty=command.position_size.quantity,
        client_order_id=client_order_id,
    )
    return await _submit(client, request, execution, occ_symbol=occ_symbol)


async def submit_options_add(
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    instrument: OptionInstrument,
    direction: Literal["long", "short"],
) -> SubmissionOutcome[OptionsSubmission]:
    """Translate an ADD command on an existing options position and submit.

    The canonical ADD command does not carry an ``Instrument`` — only a
    ``position_id`` plus the additional sizing and entry order. The caller
    (submit_envelope wrapper, story 03e) threads the position's instrument
    and direction from portfolio state. Always SIMPLE class with
    ``time_in_force=DAY``.
    """
    occ_symbol = _occ_from_instrument(instrument)
    side = OrderSide.BUY if direction == "long" else OrderSide.SELL
    request = _build_request(
        order=command.entry_order,
        symbol=occ_symbol,
        side=side,
        qty=command.additional_quantity,
        client_order_id=client_order_id,
    )
    return await _submit(client, request, execution, occ_symbol=occ_symbol)


async def submit_options_capital_floor(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
) -> SubmissionOutcome[OptionsSubmission]:
    """Submit the always-on broker-enforced capital-protection floor (W3c / ALP-856).

    On an options OPEN, places a single-leg **GTC** ``stop_limit`` that closes the
    position once its loss reaches the PM-authored, PnL-denominated
    ``capital_protection_floor.max_loss`` (story 02d). The floor is *wedge-survivable*
    — it rests at the broker and fires even with the monitor stopped (invariant 4 /
    ADR-0003); ``stop_limit`` (not ``stop_market``) bounds bad fills, accepting
    possible non-fill in a true gap (caught by the monitor + guardrail).

    The floor closes the position, so its side is the reverse of the entry: a long
    (``BUY``-to-open) floor SELLs, a short floor BUYs. The trigger price is
    PnL-denominated, derived from the floor's ``max_loss`` against the planned entry
    premium — no new numeric threshold lives in code (the level is PM judgment per
    OPEN). The ``stop_trigger`` and ``limit`` collapse to that one floor price: a
    ``stop_limit`` resting exactly at the floor, fully PM-authored.
    """
    instrument = _require_option_instrument(command.instrument, command_kind="open")
    floor = command.capital_protection_floor
    if floor is None:
        msg = (
            "submit_options_capital_floor requires command.capital_protection_floor; "
            "an options OPEN always carries one (ALP-848)"
        )
        raise ValueError(msg)
    occ_symbol = _occ_from_instrument(instrument)
    # The floor *closes* the position, so its side reverses the entry.
    side = OrderSide.SELL if instrument.direction == "long" else OrderSide.BUY
    qty = command.position_size.quantity
    floor_price = _floor_price_per_contract(
        dollar_value=float(command.position_size.dollar_value),
        max_loss=float(floor.max_loss),
        quantity=qty,
    )
    _validate_client_order_id(client_order_id)
    request = StopLimitOrderRequest(
        symbol=occ_symbol,
        side=side,
        qty=qty,
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.SIMPLE,
        client_order_id=client_order_id,
        stop_price=floor_price,
        limit_price=floor_price,
    )
    return await _submit(client, request, execution, occ_symbol=occ_symbol)


def derive_capital_floor_client_order_id(entry_client_order_id: str) -> str:
    """Derive the floor's ``client_order_id`` from the options entry's (ALP-856).

    The capital floor is a second broker order submitted on the same options OPEN
    as the entry, so it needs its own ``client_order_id`` — Alpaca rejects a
    duplicate. The floor id is the entry's PM-originated id re-derived with the
    ``command_ordinal`` shifted by :data:`_CAPITAL_FLOOR_ORDINAL_OFFSET`: it stays
    pattern-valid, carries the SAME thesis + invocation FK so a floor fill
    self-attributes (ALP-844), and cannot collide with a real sibling command
    ordinal. An OPEN is always PM-originated (the engine emits only CLOSE), so a
    non-PM id is a programming error and raises ``ValueError`` via the parser.
    """
    components = parse_pm_command_id(entry_client_order_id)
    return derive_pm_command_id(
        invocation_id=components.invocation_id,
        envelope_id=components.envelope_id,
        command_ordinal=components.command_ordinal + _CAPITAL_FLOOR_ORDINAL_OFFSET,
        attempt_seq=components.attempt_seq,
        thesis_id=components.thesis_id,
    )


async def submit_options_close(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: str,
    occ_symbol: str,
    position_qty: float,
    position_intent: Literal["buy_to_close", "sell_to_close"],
) -> SubmissionOutcome[OptionsSubmission]:
    """Translate a CLOSE command on an options position and submit.

    The canonical CloseCommand carries only ``position_id``, so the caller
    threads ``occ_symbol`` and ``position_intent`` from portfolio state.
    ``position_intent`` is the *closing* intent (``buy_to_close`` or
    ``sell_to_close``); the broker side is the matching ``BUY`` or ``SELL``.

    Atomic stop (``order_type="stop"``) is not in the canonical CloseCommand
    enum (``market | limit``), so its absence is enforced by the type system
    upstream — the switch on ``order_type`` here just maps the two valid
    cases.
    """
    qty = position_qty if command.quantity == "all" else float(command.quantity)
    side = OrderSide.BUY if position_intent == "buy_to_close" else OrderSide.SELL
    request = _build_close_request(
        order_type=command.order_type,
        symbol=occ_symbol,
        side=side,
        qty=qty,
        limit_price=command.limit_price,
        client_order_id=client_order_id,
    )
    return await _submit(client, request, execution, occ_symbol=occ_symbol)


# ---------------------------------------------------------------------------
# Request construction helpers
# ---------------------------------------------------------------------------


def _build_request(
    *,
    order: EntryOrder,
    symbol: str,
    side: OrderSide,
    qty: float,
    client_order_id: str,
) -> OrderRequest:
    """Map an ``EntryOrder`` to the matching alpaca-py request type."""
    _validate_client_order_id(client_order_id)
    common: dict[str, object] = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "time_in_force": TimeInForce.DAY,
        "order_class": OrderClass.SIMPLE,
        "client_order_id": client_order_id,
    }
    if order.type == "market":
        return MarketOrderRequest(**common)
    if order.type == "limit":
        if order.limit_price is None:
            msg = "EntryOrder type=limit requires limit_price"
            raise ValueError(msg)
        # ALP-462 — Price → float at the Alpaca SDK boundary.
        return LimitOrderRequest(**common, limit_price=float(order.limit_price))
    if order.type == "stop_limit":
        if order.limit_price is None or order.stop_price is None:
            msg = "EntryOrder type=stop_limit requires both limit_price and stop_price"
            raise ValueError(msg)
        return StopLimitOrderRequest(
            **common,
            limit_price=float(order.limit_price),
            stop_price=float(order.stop_price),
        )
    # Unreachable — EntryOrderType is an exhaustive Literal.
    msg = f"unsupported EntryOrder.type for options: {order.type!r}"
    raise ValueError(msg)


def _build_close_request(
    *,
    order_type: Literal["market", "limit"],
    symbol: str,
    side: OrderSide,
    qty: float,
    limit_price: Price | None,
    client_order_id: str,
) -> OrderRequest:
    _validate_client_order_id(client_order_id)
    common: dict[str, object] = {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "time_in_force": TimeInForce.DAY,
        "order_class": OrderClass.SIMPLE,
        "client_order_id": client_order_id,
    }
    if order_type == "market":
        return MarketOrderRequest(**common)
    if order_type == "limit":
        if limit_price is None:
            msg = "CloseCommand order_type=limit requires limit_price"
            raise ValueError(msg)
        # Price → float at the Alpaca SDK boundary.
        return LimitOrderRequest(**common, limit_price=float(limit_price))
    msg = f"unsupported CloseCommand.order_type for options: {order_type!r}"
    raise ValueError(msg)


def _floor_price_per_contract(*, dollar_value: float, max_loss: float, quantity: float) -> float:
    """Translate the PnL-denominated floor (``max_loss`` USD) to a per-contract price.

    The PM authors the floor in PnL terms (story 02d); the broker order needs a
    per-contract trigger price. The planned entry premium per contract is
    ``dollar_value / (quantity * multiplier)`` (the position's planned dollars
    spread over its contracts), and the position has lost ``max_loss`` when the
    contract price has dropped by ``max_loss / (quantity * multiplier)``:

        floor_price = (dollar_value - max_loss) / (quantity * multiplier)

    Both terms are PM-authored OPEN fields — no numeric threshold is introduced
    here. A floor whose ``max_loss`` meets-or-exceeds the planned outlay implies a
    non-positive trigger price, which Alpaca would reject; the caller's schema
    validation (02d) keeps ``max_loss`` below the capital at risk, so the
    derived price stays positive.
    """
    contracts = quantity * LISTED_OPTION_CONTRACT_MULTIPLIER
    return (dollar_value - max_loss) / contracts


def _validate_client_order_id(client_order_id: str) -> None:
    """Refuse empty or non-canonical IDs before any SDK call.

    Delegates structural validation to ``oms.command_ids`` so the broker
    adapter shares one pattern definition with the OMS intake.
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


def _require_option_instrument(instrument: object, *, command_kind: str) -> OptionInstrument:
    """Assert the dispatched command carries an OptionInstrument.

    Routing equity / options / strategy is the dispatcher's job (story 03e);
    the options translator's precondition is that the caller routed correctly.
    A wrong-asset instrument is a programming error — surface as ``TypeError``.
    """
    if not isinstance(instrument, OptionInstrument):
        msg = (
            f"submit_options_{command_kind} requires OptionInstrument; "
            f"got {type(instrument).__name__}"
        )
        raise TypeError(msg)
    return instrument


def _occ_from_instrument(instrument: OptionInstrument) -> str:
    expiration = _parse_expiration(instrument.expiration)
    contract_type = (
        OptionContractType.CALL if instrument.contract_type == "call" else OptionContractType.PUT
    )
    # ALP-462 — strike is ``Price`` (Decimal); cast to float for the OCC
    # symbol builder which expects the legacy float surface.
    return build_occ_symbol(
        instrument.underlying,
        expiration,
        contract_type,
        float(instrument.strike),
    )


def _parse_expiration(expiration: str) -> date:
    """Parse the OMS schema's ``date`` format (``YYYY-MM-DD``)."""
    try:
        return date.fromisoformat(expiration)
    except ValueError as exc:
        msg = (
            f"OptionInstrument.expiration {expiration!r} is not a valid "
            f"YYYY-MM-DD date per the OMS command schema"
        )
        raise ValueError(msg) from exc


# ---------------------------------------------------------------------------
# Submission with retry
# ---------------------------------------------------------------------------


async def _submit(
    client: TradingClient,
    request: OrderRequest,
    execution: ExecutionConfig,
    *,
    occ_symbol: str,
) -> SubmissionOutcome[OptionsSubmission]:
    """Submit ``request`` via the SDK with bounded retry, mapping permanent
    rejections to :class:`PermanentRejectionError`.

    Wrap-and-classify pattern: the inner call lets the SDK exception escape
    unchanged so the default ``is_transient`` classifier (which inspects HTTP
    status on ``APIError``) decides retry. Permanent rejections re-raise from
    the helper, and we catch them here to translate into
    :class:`PermanentRejectionError` carrying the typed
    :class:`PermanentRejection`.
    """

    async def _do_submit() -> Order:
        # ``submit_order`` is typed as ``Order | dict`` in alpaca-py; the dict
        # branch only fires in raw-data mode, which the adapter never enables
        # (``AlpacaClientFactory.build_trading_client`` does not pass
        # ``raw_data=True``). Cast at the boundary keeps the rest of the
        # function strictly typed. ``bounded_broker_call`` time-bounds the
        # offloaded sync call so a hung socket cannot park the caller (e.g. the
        # monitor's fire-close) for the full client-factory socket timeout.
        return cast(Order, await bounded_broker_call(lambda: client.submit_order(request)))

    try:
        outcome = await submit_with_retry(
            _do_submit,
            window_seconds=execution.submission_retry_window_seconds,
        )
    except Exception as exc:
        # Translation seam per runtime §G1: the alpaca-py SDK raises a single
        # ``APIError`` for both transient and permanent failures plus separate
        # ``httpx`` exceptions for network errors. Catch ``Exception`` and let
        # ``classify_alpaca_error`` decide; non-classifiable exceptions
        # re-raise unchanged. ``BaseException`` (``CancelledError``,
        # ``KeyboardInterrupt``, ``SystemExit``) propagates so external
        # interruptions are never re-classified as broker rejections.
        rejection = classify_alpaca_error(exc)
        if rejection is not None:
            raise PermanentRejectionError(rejection) from exc
        raise

    if isinstance(outcome, GatewaySubmissionFailed):
        return outcome
    order = outcome.payload
    return Submitted(
        payload=OptionsSubmission(
            alpaca_order_id=AlpacaOrderId(str(order.id)),
            client_order_id=ClientOrderId(order.client_order_id),
            occ_symbol=make_occ_symbol(occ_symbol),
            status=order.status.value,
            order_class=order.order_class.value,
        ),
        attempt_count=outcome.attempt_count,
    )
