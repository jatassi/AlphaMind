"""Canonical command → broker-adapter dispatch helper — story 03e (ALP-390).

Dispatches each canonical :class:`OMSCommand` variant to the right
``submit_*`` function in
:mod:`alphamind.execution.broker_adapter` and folds the result into a unified
:class:`BrokerDispatchResult` shape the OMS persists onto the
:class:`OrderRecord` produced by the Phase 2 write path. Replaces the
synthetic-acknowledgment behavior of the engine-stub
(:mod:`alphamind.decision.portfolio_manager.submit_envelope` /
:mod:`alphamind.execution.oms.submit_engine_envelope`) — per parent
ALP-121 decision (C), broker routing was deferred and lands here.

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

from collections.abc import Sequence
from dataclasses import dataclass
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
    submit_cancel,
    submit_equity_add,
    submit_equity_close,
    submit_equity_open,
    submit_mleg_add,
    submit_mleg_close,
    submit_mleg_open,
    submit_options_add,
    submit_options_close,
    submit_options_open,
    submit_replace,
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

__all__ = [
    "BrokerDispatchResult",
    "PayloadKind",
    "dispatch_command_to_broker",
]


PayloadKind = Literal["equity", "options", "mleg"]


@dataclass(frozen=True)
class BrokerDispatchResult:
    """Unified result type the OMS persists onto an :class:`OrderRecord`.

    Carries the broker's real ``alpaca_order_id`` plus the underlying typed
    submission payload so callers (the engine-stub coordinated swap, story 03e)
    can persist the real broker id and surface the typed ack on the activity
    log.

    For ADJUST submissions, ``alpaca_order_id`` carries the NEW (replacement)
    Alpaca order id — Alpaca's PATCH-as-cancel-and-replace semantics replace
    the original. For CANCEL submissions, ``alpaca_order_id`` carries the
    canceled order's id (the cancellation has no fresh order id).
    """

    alpaca_order_id: AlpacaOrderId
    client_order_id: ClientOrderId
    status: str
    order_class: str
    payload_kind: PayloadKind
    raw_submission: Any


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
    open_legs: Sequence[MLEGLegAck] | None = None,
    strategy_type: StrategyType | None = None,
    position_units: float | None = None,
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

    The ``queries`` parameter is part of the runner-facing signature so the
    caller (engine-stub coordinated swap, story 03e) supplies one canonical
    ``AccountStateQueries`` bundle per invocation; the dispatcher does not
    consult it directly at this story.
    """
    del queries  # accepted for runner-signature parity; consulted by callers/wiring downstream.

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
            execution=execution,
            client_order_id=client_order_id,
            position_asset_type=position_asset_type,
            position_symbol=position_symbol,
            position_qty=position_qty,
            position_side=position_side,
            occ_symbol=occ_symbol,
            position_intent=position_intent,
            open_legs=open_legs,
            strategy_type=strategy_type,
            position_units=position_units,
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
        outcome_o = await submit_options_open(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
        )
        return _wrap_options(outcome_o)
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
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    position_asset_type: Literal["equity", "option", "strategy"] | None,
    position_symbol: str | None,
    position_qty: float | None,
    position_side: Literal["long", "short"] | None,
    occ_symbol: str | None,
    position_intent: Literal["buy_to_close", "sell_to_close"] | None,
    open_legs: Sequence[MLEGLegAck] | None,
    strategy_type: StrategyType | None,
    position_units: float | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    asset_type = _require_asset_type(position_asset_type, command_kind="CLOSE")
    if asset_type == "equity":
        return await _close_equity(
            command,
            client=client,
            execution=execution,
            client_order_id=client_order_id,
            position_symbol=position_symbol,
            position_qty=position_qty,
            position_side=position_side,
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
        open_legs=open_legs,
        strategy_type=strategy_type,
        position_units=position_units,
    )


async def _close_equity(
    command: CloseCommand,
    *,
    client: TradingClient,
    execution: ExecutionConfig,
    client_order_id: ClientOrderId,
    position_symbol: str | None,
    position_qty: float | None,
    position_side: Literal["long", "short"] | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if position_symbol is None:
        raise _missing("position_symbol", command_kind="CLOSE equity")
    if position_qty is None:
        raise _missing("position_qty", command_kind="CLOSE equity")
    if position_side is None:
        raise _missing("position_side", command_kind="CLOSE equity")
    outcome = await submit_equity_close(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
        symbol=position_symbol,
        position_qty=position_qty,
        position_side=position_side,
    )
    return _wrap_equity(outcome)


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
    open_legs: Sequence[MLEGLegAck] | None,
    strategy_type: StrategyType | None,
    position_units: float | None,
) -> SubmissionOutcome[BrokerDispatchResult]:
    if open_legs is None:
        raise _missing("open_legs", command_kind="CLOSE strategy")
    if strategy_type is None:
        raise _missing("strategy_type", command_kind="CLOSE strategy")
    outcome = await submit_mleg_close(
        command,
        client=client,
        execution=execution,
        client_order_id=client_order_id,
        open_legs=open_legs,
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
        ),
        attempt_count=outcome.attempt_count,
    )


def _wrap_options(
    outcome: SubmissionOutcome[OptionsSubmission],
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
    broker (the OMS still persists the thesis update via Phase 2).
    """
    # ALP-462 — Price → float at the Alpaca SDK boundary (ReplaceFields still float).
    if command.new_stop_level is not None:
        stop = command.new_stop_level
        return ReplaceFields(
            limit_price=float(stop.limit_price) if stop.limit_price is not None else None,
            stop_price=float(stop.trigger_price),
        )
    if command.new_target_level is not None:
        tgt = command.new_target_level
        return ReplaceFields(
            limit_price=float(tgt.price) if tgt.order_type == "limit" and tgt.price else None,
        )
    # time-only / event-only / thesis-only — no broker fields.
    return ReplaceFields()
