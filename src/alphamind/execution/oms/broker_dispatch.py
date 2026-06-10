"""Canonical command → broker-adapter dispatch helper — story 03e (ALP-390).

Dispatches each canonical :class:`OMSCommand` variant to the right
``submit_*`` function in
:mod:`alphamind.execution.broker_adapter` and folds the result into a unified
:class:`BrokerDispatchResult` shape the OMS persists onto the
:class:`OrderRecord` produced by the command-execution write path. This module is the
broker-routing landing site for the engine-stub
(:mod:`alphamind.decision.portfolio_manager.submit_envelope` /
:mod:`alphamind.execution.oms.submit_engine_envelope`) per parent
ALP-121 decision (C).

Routing table — dispatched on (command_type, instrument.asset_type):

* OPEN equity   → :func:`submit_equity_open`
* OPEN options  → :func:`submit_options_open`
* OPEN strategy → :func:`submit_mleg_open`
* ADD equity    → :func:`submit_equity_add`
* ADD options   → :func:`submit_options_add`
* ADD strategy  → :func:`submit_mleg_add`
* CLOSE equity  → :func:`submit_equity_close`
* CLOSE options → :func:`submit_options_close`
* CLOSE strategy→ :func:`submit_mleg_close`
* ADJUST        → :func:`submit_replace`  (against the threaded ``target_alpaca_order_id``)
* CANCEL        → :func:`submit_cancel`   (against the threaded ``target_alpaca_order_id``)

ADD / CLOSE commands carry only a ``position_id`` — the dispatcher's caller
threads the position's instrument identity (symbol / OCC symbol /
:class:`OptionInstrument` / :class:`MLEGLegAck` legs / strategy_type) and
side / quantity from portfolio state.

ADJUST / CANCEL target an existing alpaca order by id; the caller threads
``target_alpaca_order_id`` from the order's record.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId
from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    EquityInstrument,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    StrategyInstrument,
    StrategyType,
)
from alphamind.config.models.execution import ExecutionConfig
from alphamind.execution.broker_adapter import (
    AccountStateQueries,
    EquitySubmission,
    GatewaySubmissionFailed,
    MLEGLegAck,
    MLEGSubmission,
    OptionsSubmission,
    SubmissionOutcome,
    Submitted,
    derive_capital_floor_client_order_id,
    submit_cancel,
    submit_equity_add,
    submit_equity_close,
    submit_equity_open,
    submit_mleg_add,
    submit_mleg_close,
    submit_mleg_open,
    submit_options_add,
    submit_options_capital_floor,
    submit_options_close,
    submit_options_open,
    submit_replace,
)
from alphamind.execution.broker_adapter.errors import (
    PermanentRejection,
    classify_alpaca_error,
)
from alphamind.execution.broker_adapter.order_modify import (
    AssetClass as ReplaceAssetClass,
)
from alphamind.execution.broker_adapter.order_modify import (
    OrderClass as ReplaceOrderClass,
)
from alphamind.execution.broker_adapter.order_modify import (
    ReplaceFields,
)
from alphamind.execution.broker_adapter.order_options import (
    PermanentRejectionError,
)
from alphamind.execution.broker_adapter.queries import PositionSnapshot
from alphamind.execution.broker_adapter.retry import bounded_broker_call

__all__ = [
    "BrokerDispatchResult",
    "PayloadKind",
    "dispatch_command_to_broker",
]

logger = logging.getLogger(__name__)


PayloadKind = Literal["equity", "options", "mleg"]


@dataclass(frozen=True)
class BrokerDispatchResult:
    """Unified result type the OMS persists onto an :class:`OrderRecord`.

    Carries the broker's real ``alpaca_order_id`` plus the underlying typed
    submission payload so callers (the broker-routing coordinated swap, story 03e)
    can persist the real broker id and surface the typed ack on the activity
    log.

    For ADJUST submissions, ``alpaca_order_id`` carries the NEW (replacement)
    Alpaca order id — Alpaca's PATCH-as-cancel-and-replace semantics replace
    the original. For CANCEL submissions, ``alpaca_order_id`` carries the
    canceled order's id (the cancellation has no fresh order id).

    ``leg_alpaca_order_ids`` maps a native equity bracket / OTO's protective
    role (``"take_profit"`` / ``"stop_loss"``) to the broker's real child id,
    captured at submission (ALP-746). The command-execution OPEN writeback consumes it to
    stamp each protective-leg ``orders`` row with its real ``alpaca_order_id``.
    Empty for every non-equity-OPEN submission (options / mleg / replace /
    cancel and equity ADD / CLOSE carry no broker-side protective child).
    """

    alpaca_order_id: AlpacaOrderId
    client_order_id: ClientOrderId
    status: str
    order_class: str
    payload_kind: PayloadKind
    raw_submission: Any
    leg_alpaca_order_ids: Mapping[str, AlpacaOrderId] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


async def dispatch_command_to_broker(  # noqa: PLR0913 — caller threads every per-command parameter once.
    command: OMSCommand,
    *,
    client: TradingClient,
    queries: AccountStateQueries,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    # Threaded from portfolio state at the call site for CLOSE / ADD on options:
    position_symbol: str | None = None,
    position_qty: float | None = None,
    position_side: Literal["long", "short"] | None = None,
    position_asset_type: Literal["equity", "option", "strategy"] | None = None,
    occ_symbol: str | None = None,
    position_intent: Literal["buy_to_close", "sell_to_close"] | None = None,
    position_option_instrument: OptionInstrument | None = None,
    # ADD strategy threads the position's open-side legs (ADD opens more of
    # the same exposure); CLOSE strategy threads close-side legs (the
    # open→close inversion happened upstream at the single seam).
    open_legs: Sequence[MLEGLegAck] | None = None,
    close_legs: Sequence[MLEGLegAck] | None = None,
    strategy_type: StrategyType | None = None,
    position_units: float | None = None,
    # ALP-937 — broker-enforced protective-leg ids the decision side resolved
    # from the position's bracket; an equity CLOSE cancels these (freeing the
    # held_for_orders shares) before the SIMPLE close sell. Empty / None for a
    # position with no broker-enforced legs and for every non-equity CLOSE.
    close_protective_leg_alpaca_order_ids: Sequence[AlpacaOrderId] | None = None,
    # Threaded from order record for ADJUST / CANCEL:
    target_alpaca_order_id: AlpacaOrderId | None = None,
    target_asset_class: ReplaceAssetClass | None = None,
    target_order_class: ReplaceOrderClass | None = None,
    fields: ReplaceFields | None = None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    """Route *command* to the right broker submission function.

    See module docstring for the routing table. The caller threads
    portfolio-state context for CLOSE / ADD (no embedded instrument on the
    canonical command) and order-record context for ADJUST / CANCEL.

    The caller (broker-routing coordinated swap, story 03e) supplies one
    canonical ``AccountStateQueries`` bundle per invocation; the dispatcher
    consults it on the equity CLOSE path, whose ALP-943 drift guard re-checks
    the live broker position before any order reaches the wire.
    """
    if isinstance(command, OpenCommand):
        return await _dispatch_open(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
        )
    if isinstance(command, AddCommand):
        return await _dispatch_add(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            position_asset_type=position_asset_type,
            position_symbol=position_symbol,
            position_side=position_side,
            position_option_instrument=position_option_instrument,
            open_legs=open_legs,
            strategy_type=strategy_type,
        )
    if isinstance(command, CloseCommand):
        return await _dispatch_close(
            command,
            client=client,
            queries=queries,
            execution=execution,
            client_order_id=client_order_id,
            position_asset_type=position_asset_type,
            position_symbol=position_symbol,
            position_qty=position_qty,
            position_side=position_side,
            occ_symbol=occ_symbol,
            position_intent=position_intent,
            close_legs=close_legs,
            strategy_type=strategy_type,
            position_units=position_units,
            close_protective_leg_alpaca_order_ids=close_protective_leg_alpaca_order_ids,
        )
    # PATCH (ADJUST) and DELETE (CANCEL) don't carry a fresh client_order_id —
    # they target an existing Alpaca order by its id.
    del client_order_id  # documented unused for ADJUST/CANCEL.
    if isinstance(command, AdjustCommand):
        return await _dispatch_adjust(
            command,
            client=client,
            execution=execution,
            target_alpaca_order_id=target_alpaca_order_id,
            target_asset_class=target_asset_class,
            target_order_class=target_order_class,
            fields=fields,
        )
    if isinstance(command, CancelCommand):
        return await _dispatch_cancel(
            client=client,
            execution=execution,
            target_alpaca_order_id=target_alpaca_order_id,
        )
    msg = f"unsupported OMS command variant: {type(command).__name__}"
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------
# Per-command-type dispatchers
# ---------------------------------------------------------------------------


async def _dispatch_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
) -> SubmissionOutcome[BrokerDispatchResult]:
    instrument = command.instrument
    if isinstance(instrument, EquityInstrument):
        outcome = await submit_equity_open(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
        )
        return _wrap_equity(outcome)
    if isinstance(instrument, OptionInstrument):
        return await _dispatch_options_open(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
        )
    if isinstance(instrument, StrategyInstrument):
        outcome_m = await submit_mleg_open(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
        )
        return _wrap_mleg(outcome_m)
    msg = f"OpenCommand carries unsupported instrument: {type(instrument).__name__}"
    raise NotImplementedError(msg)


async def _dispatch_options_open(
    command: OpenCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
) -> SubmissionOutcome[BrokerDispatchResult]:
    """Submit an options entry AND its always-on broker-enforced capital floor (ALP-856).

    Invariant 4 (ADR-0003): every open options position carries a resting
    broker-enforced exit. After the single-leg entry submits, this places the
    PM-authored, PnL-denominated GTC ``stop_limit`` capital floor (``02d`` level,
    derived by :func:`submit_options_capital_floor`) under its own
    ``client_order_id`` (derived from the entry's so its fill self-attributes).
    The floor's real broker ``alpaca_order_id`` rides back on the dispatch
    result's ``leg_alpaca_order_ids`` under ``"capital_floor"`` so the OPEN
    writeback stamps it onto the broker-enforced floor leg, and
    cancel-on-monitor-fire can cancel the resting floor by id.

    The entry submits first: if it fails (gateway exhaustion → the caller
    abandons the durable pre-commit), no floor is attempted. Once the entry is
    LIVE, the floor is mandatory — a mandatory floor means NO unprotected
    options position — so ANY floor-submit failure (``GatewaySubmissionFailed``
    or a permanently-rejecting ``PermanentRejectionError``) FIRST retracts the
    live entry via :func:`submit_cancel` (so it cannot rest broker-unprotected,
    FL1/ALP-856), THEN surfaces the failure as the dispatch outcome. The caller
    still tears down the LOCAL graph; this cancel closes the broker side that the
    local-graph abandon never touches.
    """
    entry_outcome = await submit_options_open(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
    )
    if isinstance(entry_outcome, GatewaySubmissionFailed):
        return entry_outcome
    entry_alpaca_order_id = entry_outcome.payload.alpaca_order_id
    try:
        floor_outcome = await submit_options_capital_floor(
            command,
            client=client,
            execution=execution,
            client_order_id=derive_capital_floor_client_order_id(client_order_id),
        )
    except PermanentRejectionError:
        # The floor was permanently rejected (e.g. options level not approved)
        # AFTER the entry went live. Retract the live entry first so it cannot
        # rest unprotected, then re-raise so the caller also abandons local state.
        await _cancel_live_entry(
            client=client, execution=execution, entry_alpaca_order_id=entry_alpaca_order_id
        )
        raise
    if isinstance(floor_outcome, GatewaySubmissionFailed):
        # The floor exhausted its retry window after the entry went live; retract
        # the live entry first, then surface the gateway failure as the outcome.
        await _cancel_live_entry(
            client=client, execution=execution, entry_alpaca_order_id=entry_alpaca_order_id
        )
        return floor_outcome
    return _wrap_options(
        entry_outcome,
        leg_alpaca_order_ids={"capital_floor": floor_outcome.payload.alpaca_order_id},
    )


async def _cancel_live_entry(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    entry_alpaca_order_id: AlpacaOrderId,
) -> None:
    """Retract a live options entry whose mandatory floor failed to submit (FL1).

    The floor is mandatory: a floor-submit failure must leave NO unprotected
    options position, so the live entry is cancelled at the broker before the
    floor failure is surfaced. The cancel is best-effort — a
    ``GatewaySubmissionFailed`` or ``PermanentRejectionError`` from the cancel
    itself propagates so the boundary logs it (an already-terminal / unknown
    entry is the broker's to reconcile), and the original floor failure is the
    caller's teardown signal regardless.
    """
    await submit_cancel(
        client=client,
        execution=execution,
        target_alpaca_order_id=entry_alpaca_order_id,
    )


async def _dispatch_add(  # noqa: PLR0913 — ADD threads every per-asset-type parameter the broker translator requires.
    command: AddCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    position_asset_type: Literal["equity", "option", "strategy"] | None,
    position_symbol: str | None,
    position_side: Literal["long", "short"] | None,
    position_option_instrument: OptionInstrument | None,
    open_legs: Sequence[MLEGLegAck] | None,
    strategy_type: StrategyType | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    asset_type = _require_asset_type(position_asset_type, command_kind="ADD")
    if asset_type == "equity":
        if position_symbol is None:
            raise _missing("position_symbol", command_kind="ADD equity")
        if position_side is None:
            raise _missing("position_side", command_kind="ADD equity")
        outcome = await submit_equity_add(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            symbol=position_symbol,
            side=_entry_side(position_side),
        )
        return _wrap_equity(outcome)
    if asset_type == "option":
        if position_option_instrument is None:
            raise _missing("position_option_instrument", command_kind="ADD options")
        if position_side is None:
            raise _missing("position_side", command_kind="ADD options")
        outcome_o = await submit_options_add(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            instrument=position_option_instrument,
            direction=position_side,
        )
        return _wrap_options(outcome_o)
    # strategy
    if open_legs is None:
        raise _missing("open_legs", command_kind="ADD strategy")
    if strategy_type is None:
        raise _missing("strategy_type", command_kind="ADD strategy")
    outcome_m = await submit_mleg_add(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
        open_legs=open_legs,
        strategy_type=strategy_type,
    )
    return _wrap_mleg(outcome_m)


async def _dispatch_close(  # noqa: PLR0913 — close threads every per-asset-type parameter the broker translator requires.
    command: CloseCommand,
    *,
    client: TradingClient,
    queries: AccountStateQueries,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    position_asset_type: Literal["equity", "option", "strategy"] | None,
    position_symbol: str | None,
    position_qty: float | None,
    position_side: Literal["long", "short"] | None,
    occ_symbol: str | None,
    position_intent: Literal["buy_to_close", "sell_to_close"] | None,
    close_legs: Sequence[MLEGLegAck] | None,
    strategy_type: StrategyType | None,
    position_units: float | None,
    close_protective_leg_alpaca_order_ids: Sequence[AlpacaOrderId] | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    asset_type = _require_asset_type(position_asset_type, command_kind="CLOSE")
    if asset_type == "equity":
        return await _close_equity(
            command,
            client=client,
            queries=queries,
            execution=execution,
            client_order_id=client_order_id,
            position_symbol=position_symbol,
            position_qty=position_qty,
            position_side=position_side,
            close_protective_leg_alpaca_order_ids=close_protective_leg_alpaca_order_ids,
        )
    if asset_type == "option":
        return await _close_option(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            occ_symbol=occ_symbol,
            position_qty=position_qty,
            position_intent=position_intent,
        )
    return await _close_strategy(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
        close_legs=close_legs,
        strategy_type=strategy_type,
        position_units=position_units,
    )


async def _close_equity(  # noqa: PLR0913 — close threads every broker-translator parameter plus the drift-guard query surface.
    command: CloseCommand,
    *,
    client: TradingClient,
    queries: AccountStateQueries,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    position_symbol: str | None,
    position_qty: float | None,
    position_side: Literal["long", "short"] | None,
    close_protective_leg_alpaca_order_ids: Sequence[AlpacaOrderId] | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if position_symbol is None:
        raise _missing("position_symbol", command_kind="CLOSE equity")
    if position_qty is None:
        raise _missing("position_qty", command_kind="CLOSE equity")
    if position_side is None:
        raise _missing("position_side", command_kind="CLOSE equity")
    symbol = position_symbol

    # ALP-943 — execution-time drift guard. The local positions projection is
    # frozen between fill-collection phases, while the monitor flattens
    # positions between them by design (re-protection stops, ALP-938), so the
    # projected symbol/qty/side may be stale by the time this dispatch runs.
    # Re-check the live broker position BEFORE any leg-cancel RPC: a flat or
    # side-flipped position rejects (a "close" would OPEN a new position —
    # the 2026-06-09 -4 MRVL naked short); a shrunken one clamps the quantity.
    requested_qty = position_qty if command.quantity == "all" else float(command.quantity)
    live = await bounded_broker_call(lambda: queries.get_open_position(symbol))
    qty = _checked_close_quantity(
        live,
        position_symbol=symbol,
        position_side=position_side,
        requested_qty=requested_qty,
    )

    # ALP-937 — cancel the native bracket's broker-enforced protective legs
    # BEFORE the close sell. The resting OCO legs reserve 100% of the position's
    # shares (``held_for_orders``), so without this the SIMPLE sell sees
    # ``available: 0`` and Alpaca rejects it (``other_permanent`` / 403). Each
    # cancel is best-effort for an already-CANCELED / unknown leg, but a leg the
    # broker reports FILLED aborts the close (ALP-943 — the position exited).
    # Monitor-enforced legs (no broker id) are never threaded here.
    protection_torn_down = await _cancel_protective_legs(
        client=client,
        execution=execution,
        leg_alpaca_order_ids=close_protective_leg_alpaca_order_ids,
    )

    # Submit the close sell. If it fails AFTER live protection was torn down the
    # position is broker-naked, so raise the (F) operator alert before letting the
    # failure propagate to the normal rejection / ``command_abandoned`` path. The
    # ``Exception`` catch (not ``BaseException``) lets ``CancelledError`` propagate
    # un-alerted; a raised broker error is the permanent rejection the caller then
    # classifies.
    try:
        outcome = await submit_equity_close(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            symbol=symbol,
            qty=qty,
            position_side=position_side,
        )
    except Exception:
        if protection_torn_down:
            _alert_close_rejected_after_cancel(command, position_symbol=symbol)
        raise
    if protection_torn_down and isinstance(outcome, GatewaySubmissionFailed):
        _alert_close_rejected_after_cancel(command, position_symbol=symbol)
    return _wrap_equity(outcome)


# Float-dust tolerance for the live-vs-requested close-quantity comparison: a
# live qty within 1e-9 of the requested qty is the same position, not drift.
_CLOSE_QTY_EPSILON = 1e-9


def _position_state_drift(message: str) -> PermanentRejectionError:
    """Build the ALP-943 local drift rejection (``http_status=0`` — no HTTP exchange)."""
    return PermanentRejectionError(
        PermanentRejection(
            code="position_state_drift",
            http_status=0,
            alpaca_message=message,
        )
    )


def _checked_close_quantity(
    live: PositionSnapshot | None,
    *,
    position_symbol: str,
    position_side: Literal["long", "short"],
    requested_qty: float,
) -> float:
    """Validate the projected equity CLOSE against the live broker position (ALP-943).

    Returns the final close quantity. A flat broker (no live position) or a live
    side contradicting the projection raises the ``position_state_drift``
    rejection — submitting the "close" would open a NEW position in the opposite
    direction (with ``short_selling_enabled`` Alpaca executes a sell on a flat
    position as ``sell_to_open``). A live absolute quantity below the requested
    quantity clamps the close to what actually exists at the broker.
    """
    if live is None or live.qty == 0:
        raise _position_state_drift(
            f"equity CLOSE drift guard: {position_symbol} expected {position_side} "
            f"{requested_qty}, live broker position is flat"
        )
    live_side: Literal["long", "short"] = "long" if live.qty > 0 else "short"
    if live_side != position_side:
        raise _position_state_drift(
            f"equity CLOSE drift guard: {position_symbol} expected {position_side} "
            f"{requested_qty}, live broker position is {live_side} qty {live.qty}"
        )
    live_abs = abs(live.qty)
    if live_abs < requested_qty - _CLOSE_QTY_EPSILON:
        logger.warning(
            "broker_dispatch: equity CLOSE of %s clamped from %s to live broker qty %s "
            "(position shrank between snapshot and dispatch)",
            position_symbol,
            requested_qty,
            live_abs,
        )
        return live_abs
    return requested_qty


async def _cancel_protective_legs(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    leg_alpaca_order_ids: Sequence[AlpacaOrderId] | None,
) -> bool:
    """Cancel each broker-enforced protective leg ahead of a CLOSE.

    Returns whether at least one cancel was CONFIRMED accepted by the broker —
    i.e. live protection was actively torn down (the ALP-937 (F) naked-position
    signal). Two cases that do NOT count, because neither establishes that we
    removed live protection:

    * **Permanently-rejected cancel** — ``submit_cancel`` re-raises the raw
      alpaca-py ``APIError`` on a permanent 404 (not-found) / 422
      (already-terminal), NOT a ``PermanentRejectionError`` (it wraps the equity
      ``_is_transient`` classifier that re-raises permanent errors). The leg is
      already terminal — but WHICH terminal state matters (ALP-943): a FILLED
      leg is broker-confirmed proof the position exited, so the close aborts via
      :func:`_resolve_rejected_leg_state` rather than selling into a flat
      position; a canceled / expired / unknown leg is benign and the close
      proceeds. A genuinely non-broker exception re-raises so a real fault is
      never masked.
    * **Gateway-failed cancel** — unconfirmed (the leg may still rest and hold
      shares); the close sell itself surfaces the real problem if so.
    """
    leg_ids = tuple(leg_alpaca_order_ids or ())
    protection_torn_down = False
    for leg_alpaca_order_id in leg_ids:
        try:
            cancel_outcome = await submit_cancel(
                client=client,
                execution=execution,
                target_alpaca_order_id=leg_alpaca_order_id,
            )
        except Exception as exc:
            rejection = classify_alpaca_error(exc)
            if rejection is None:
                raise
            await _resolve_rejected_leg_state(
                client=client,
                leg_alpaca_order_id=leg_alpaca_order_id,
                rejection_code=rejection.code,
            )
            continue
        if isinstance(cancel_outcome, GatewaySubmissionFailed):
            logger.warning(
                "broker_dispatch: protective leg %s cancel exhausted the retry window "
                "(%s); the close sell may still be rejected if the leg holds shares",
                leg_alpaca_order_id,
                cancel_outcome.reason,
            )
            continue
        protection_torn_down = True
    return protection_torn_down


# Broker order statuses proving a protective leg EXECUTED — the protected
# position (or part of it) exited, so a close built from the projection must
# not proceed (ALP-943).
_LEG_EXECUTED_STATUSES = frozenset({"filled", "partially_filled"})


async def _resolve_rejected_leg_state(
    *,
    client: TradingClient,
    leg_alpaca_order_id: AlpacaOrderId,
    rejection_code: str,
) -> None:
    """Resolve a permanently-rejected leg cancel into proceed-or-abort (ALP-943).

    The cancel rejection alone is ambiguous: the leg may be canceled / expired
    (benign — the OCO sibling fired or the leg lapsed; its shares are free) or
    FILLED (the protective exit executed — the position is gone). Reading the
    leg's actual broker state via ``get_order_by_id`` disambiguates without
    parsing rejection message text. A filled / partially-filled leg raises the
    ``position_state_drift`` rejection so the close never sells into the exited
    position; every other resolved state — and an unresolvable leg (404 or a
    failing lookup) — proceeds as before, with the close sell itself surfacing
    any held-shares problem.
    """
    try:
        order = await bounded_broker_call(lambda: client.get_order_by_id(leg_alpaca_order_id))
    except Exception:
        logger.info(
            "broker_dispatch: protective leg %s permanently rejected at cancel (%s) and "
            "its state could not be resolved — assuming already terminal; proceeding "
            "with the close",
            leg_alpaca_order_id,
            rejection_code,
        )
        return
    status = getattr(getattr(order, "status", None), "value", getattr(order, "status", None))
    if status in _LEG_EXECUTED_STATUSES:
        raise _position_state_drift(
            f"equity CLOSE drift guard: protective leg {leg_alpaca_order_id} is "
            f"{status} at the broker — the protective exit executed, so the "
            "position this CLOSE targets no longer exists as projected"
        )
    logger.info(
        "broker_dispatch: protective leg %s permanently rejected at cancel (%s); broker "
        "reports it %s — benign already-terminal, proceeding with the close",
        leg_alpaca_order_id,
        rejection_code,
        status,
    )


def _alert_close_rejected_after_cancel(command: CloseCommand, *, position_symbol: str) -> None:
    """Emit the ALP-937 (F) broker-naked-position operator alert."""
    logger.critical(
        "ALP-937 NAKED POSITION: equity CLOSE of %s (position_id=%s) was rejected AFTER its "
        "protective bracket legs were cancelled — the position is broker-unprotected until the "
        "PM re-evaluates next invocation. The continuous-monitor position-level max-loss "
        "guardrail is the active backstop.",
        position_symbol,
        command.position_id,
    )


async def _close_option(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    occ_symbol: str | None,
    position_qty: float | None,
    position_intent: Literal["buy_to_close", "sell_to_close"] | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if occ_symbol is None:
        raise _missing("occ_symbol", command_kind="CLOSE options")
    if position_intent is None:
        raise _missing("position_intent", command_kind="CLOSE options")
    if position_qty is None:
        raise _missing("position_qty", command_kind="CLOSE options")
    outcome = await submit_options_close(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
        occ_symbol=occ_symbol,
        position_qty=position_qty,
        position_intent=position_intent,
    )
    return _wrap_options(outcome)


async def _close_strategy(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    close_legs: Sequence[MLEGLegAck] | None,
    strategy_type: StrategyType | None,
    position_units: float | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if close_legs is None:
        raise _missing("close_legs", command_kind="CLOSE strategy")
    if strategy_type is None:
        raise _missing("strategy_type", command_kind="CLOSE strategy")
    outcome = await submit_mleg_close(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
        close_legs=close_legs,
        strategy_type=strategy_type,
        position_units=position_units,
    )
    return _wrap_mleg(outcome)


async def _dispatch_adjust(
    command: AdjustCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    target_alpaca_order_id: AlpacaOrderId | None,
    target_asset_class: ReplaceAssetClass | None,
    target_order_class: ReplaceOrderClass | None,
    fields: ReplaceFields | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if target_alpaca_order_id is None:
        raise _missing("target_alpaca_order_id", command_kind="ADJUST")
    if target_asset_class is None:
        raise _missing("target_asset_class", command_kind="ADJUST")
    if target_order_class is None:
        raise _missing("target_order_class", command_kind="ADJUST")
    replace_fields = fields if fields is not None else _adjust_to_replace_fields(command)
    outcome = await submit_replace(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId(target_alpaca_order_id),
        target_asset_class=target_asset_class,
        target_order_class=target_order_class,
        fields=replace_fields,
    )
    return _wrap_replace(outcome, asset_class=target_asset_class)


async def _dispatch_cancel(
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    target_alpaca_order_id: AlpacaOrderId | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if target_alpaca_order_id is None:
        raise _missing("target_alpaca_order_id", command_kind="CANCEL")
    outcome = await submit_cancel(
        client=client,
        execution=execution,
        target_alpaca_order_id=AlpacaOrderId(target_alpaca_order_id),
    )
    return _wrap_cancel(outcome, target_alpaca_order_id=target_alpaca_order_id)


# ---------------------------------------------------------------------------
# Result wrappers
# ---------------------------------------------------------------------------


def _wrap_equity(
    outcome: SubmissionOutcome[EquitySubmission],
) -> SubmissionOutcome[BrokerDispatchResult]:
    if isinstance(outcome, GatewaySubmissionFailed):
        return outcome
    sub = outcome.payload
    return Submitted(
        payload=BrokerDispatchResult(
            alpaca_order_id=sub.alpaca_order_id,
            client_order_id=sub.client_order_id,
            status=sub.status,
            order_class=sub.order_class,
            payload_kind="equity",
            raw_submission=sub,
            # Native bracket / OTO protective children captured at submission
            # (ALP-746); empty for SIMPLE equity ADD / CLOSE.
            leg_alpaca_order_ids={ack.role: ack.alpaca_order_id for ack in sub.leg_acks},
        ),
        attempt_count=outcome.attempt_count,
    )


def _wrap_options(
    outcome: SubmissionOutcome[OptionsSubmission],
    *,
    leg_alpaca_order_ids: Mapping[str, AlpacaOrderId] | None = None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if isinstance(outcome, GatewaySubmissionFailed):
        return outcome
    sub = outcome.payload
    return Submitted(
        payload=BrokerDispatchResult(
            alpaca_order_id=sub.alpaca_order_id,
            client_order_id=sub.client_order_id,
            status=sub.status,
            order_class=sub.order_class,
            payload_kind="options",
            raw_submission=sub,
            # ALP-856 — the resting broker-enforced capital floor's id, carried
            # under ``"capital_floor"`` for an options OPEN; empty for options
            # ADD / CLOSE (no floor submitted on those).
            leg_alpaca_order_ids=leg_alpaca_order_ids or {},
        ),
        attempt_count=outcome.attempt_count,
    )


def _wrap_mleg(
    outcome: SubmissionOutcome[MLEGSubmission],
) -> SubmissionOutcome[BrokerDispatchResult]:
    if isinstance(outcome, GatewaySubmissionFailed):
        return outcome
    sub = outcome.payload
    return Submitted(
        payload=BrokerDispatchResult(
            alpaca_order_id=sub.alpaca_order_id,
            client_order_id=sub.client_order_id,
            status=sub.status,
            order_class="mleg",
            payload_kind="mleg",
            raw_submission=sub,
        ),
        attempt_count=outcome.attempt_count,
    )


def _wrap_replace(
    outcome: SubmissionOutcome[Any],
    *,
    asset_class: ReplaceAssetClass,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if isinstance(outcome, GatewaySubmissionFailed):
        return outcome
    ack = outcome.payload
    payload_kind: PayloadKind = (
        "mleg"
        if asset_class == "us_option_strategy"
        else "options"
        if asset_class == "us_option"
        else "equity"
    )
    return Submitted(
        payload=BrokerDispatchResult(
            alpaca_order_id=ack.new_alpaca_order_id,
            client_order_id=ack.client_order_id,
            status=ack.status,
            # Replace acks don't surface order_class; record the asset_class for ledger queries.
            order_class=asset_class,
            payload_kind=payload_kind,
            raw_submission=ack,
        ),
        attempt_count=outcome.attempt_count,
    )


def _wrap_cancel(
    outcome: SubmissionOutcome[Any],
    *,
    target_alpaca_order_id: AlpacaOrderId,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if isinstance(outcome, GatewaySubmissionFailed):
        return outcome
    ack = outcome.payload
    return Submitted(
        payload=BrokerDispatchResult(
            alpaca_order_id=target_alpaca_order_id,
            # Cancel has no fresh client_order_id; surface the target id for log queries.
            client_order_id=ClientOrderId(target_alpaca_order_id),
            status="canceled" if ack.accepted else "cancel_pending",
            order_class="cancel",
            payload_kind="equity",  # cancel is asset-agnostic; payload_kind unused downstream.
            raw_submission=ack,
        ),
        attempt_count=outcome.attempt_count,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry_side(direction: Literal["long", "short"]) -> OrderSide:
    return OrderSide.BUY if direction == "long" else OrderSide.SELL


def _missing(name: str, *, command_kind: str) -> ValueError:
    """Build the missing-kwarg :class:`ValueError` the dispatcher raises.

    The dispatcher's signature carries every per-asset-type kwarg as
    ``Optional`` because Python doesn't have asset-type-discriminated keyword
    arguments; per-branch ``if foo is None: raise _missing(...)`` checks
    pinpoint the missing context for the caller while letting mypy narrow
    Literal types through the inline ``is None`` test.
    """
    return ValueError(f"dispatch_command_to_broker {command_kind!s} requires {name!r}")


def _require_asset_type(
    asset_type: Literal["equity", "option", "strategy"] | None,
    *,
    command_kind: str,
) -> Literal["equity", "option", "strategy"]:
    if asset_type is None:
        raise _missing("position_asset_type", command_kind=command_kind)
    return asset_type


def _adjust_to_replace_fields(command: AdjustCommand) -> ReplaceFields:
    """Translate an :class:`AdjustCommand`'s change-fields into broker-level
    :class:`ReplaceFields`.

    Maps stop / target / time changes to the intersection of OMS-modifiable
    and Alpaca-PATCHable fields. Thesis-only or event-only adjustments produce
    no broker-level fields — caller should not dispatch those through the
    broker (the OMS still persists the thesis update via command execution).
    """
    if command.new_stop_level is not None:
        stop = command.new_stop_level
        return ReplaceFields(
            limit_price=stop.limit_price,
            stop_price=stop.trigger_price,
        )
    if command.new_target_level is not None:
        tgt = command.new_target_level
        return ReplaceFields(
            limit_price=tgt.price if tgt.order_type == "limit" and tgt.price else None,
        )
    # time-only / event-only / thesis-only — no broker fields.
    return ReplaceFields()
