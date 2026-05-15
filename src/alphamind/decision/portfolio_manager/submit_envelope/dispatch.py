"""Broker-routing helpers for the ``submit_envelope`` package — ALP-464.

Hosts :func:`_route_through_broker` (per-command dispatch loop), per-command
context resolvers, and :class:`_AbandonedCommandEntry`. The
:class:`alphamind.commands.protocols.BrokerDispatch` Protocol is composition-
root injected (ALP-458); ``broker_dispatch=None`` lazy-loads the concrete
``dispatch_command_to_broker``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import select as _select

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId, OccSymbol, PositionId
from alphamind._kernel.money import price
from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    OMSCommand,
    OpenCommand,
    OptionInstrument,
    StrategyType,
)
from alphamind.commands.pm_envelope import PMEnvelope
from alphamind.commands.protocols import BrokerDispatch
from alphamind.decision.portfolio_manager.submit_envelope.types import (
    RejectionPayload,
    SubmissionResult,
    _BreachedRule,
)
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as _position_row_to_record,
)

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient

    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.execution.broker_adapter.order_modify import (
        AssetClass as ReplaceAssetClass,
    )
    from alphamind.execution.broker_adapter.order_modify import (
        OrderClass as ReplaceOrderClass,
    )
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult


@dataclass(frozen=True)
class _AbandonedCommandEntry:
    """Per-command broker-failure marker — drives the ``command_abandoned`` emit.

    Phase 2 writeback is skipped for an abandoned command (no order rows
    persisted) but a single ``command_abandoned`` activity-log entry is emitted
    so post-hoc forensics can reconstruct why the command never reached the
    broker.
    """

    command_id: str
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]
    failure_reason: str
    retry_attempt_count: int


_COMMAND_TYPE_TO_LABEL: dict[str, Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]] = {
    "open": "OPEN",
    "close": "CLOSE",
    "adjust": "ADJUST",
    "cancel": "CANCEL",
    "add": "ADD",
}


async def _route_through_broker(
    *,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    client: TradingClient,
    queries: AccountStateQueries,
    execution_config: ExecutionConfig,
    invocation_id: str,
    invocation_handle: Any | None = None,
    broker_dispatch: BrokerDispatch | None = None,
) -> tuple[
    tuple[SubmissionResult, ...],
    tuple[BrokerDispatchResult | None, ...],
    tuple[_AbandonedCommandEntry, ...],
]:
    """Dispatch each accepted command through the broker adapter.

    Returns ``(updated_submission_results, dispatch_results, abandoned_entries)``.
    Validator-rejected results pass through unchanged with ``None`` dispatch entries.
    Validator-accepted results carry the broker's ack on ``Submitted``; flip to
    rejected on ``GatewaySubmissionFailed`` (appending an
    :class:`_AbandonedCommandEntry`) or ``PermanentRejectionError`` (surfacing
    the broker's ``PermanentRejection.code`` as the rule).
    """
    # Lazy imports — broker_adapter ships an alpaca-py dependency we don't
    # want loaded for the fixture-only path.
    from alphamind.execution.broker_adapter import GatewaySubmissionFailed, Submitted
    from alphamind.execution.broker_adapter.errors import classify_alpaca_error
    from alphamind.execution.broker_adapter.order_options import PermanentRejectionError

    dispatch: BrokerDispatch
    if broker_dispatch is not None:
        dispatch = broker_dispatch
    else:
        from alphamind.execution.oms.broker_dispatch import dispatch_command_to_broker

        # The concrete dispatcher's explicit per-asset kwargs duck-type the
        # Protocol's ``**context: Any`` shape; the cast is structurally safe
        # at the Protocol seam — exactly the swap-point we want to accept.
        dispatch = cast(BrokerDispatch, dispatch_command_to_broker)

    updated: list[SubmissionResult] = []
    dispatches: list[BrokerDispatchResult | None] = []
    abandoned: list[_AbandonedCommandEntry] = []

    for result, command in zip(submission_results, envelope.commands, strict=True):
        if result.status != "accepted":
            updated.append(result)
            dispatches.append(None)
            continue
        try:
            context_kwargs = await _dispatcher_context_for(
                command, invocation_handle=invocation_handle
            )
            outcome = await dispatch(
                command,
                client=client,
                queries=queries,
                execution=execution_config,
                client_order_id=ClientOrderId(result.command_id),
                **context_kwargs,
            )
        except PermanentRejectionError as exc:
            # Single-leg options translator wraps permanent rejections in this
            # typed exception; surface the broker code in gateway_reason.
            updated.append(_to_rejection(result, code=exc.rejection.code, reason=str(exc)))
            dispatches.append(None)
            continue
        except Exception as exc:
            # Translation seam per runtime §G1: equity/mleg translators
            # re-raise raw alpaca-py APIError on permanent failure; classify
            # here for a uniform rejection shape. The wide catch is warranted
            # because the translator's exception hierarchy is not contracted;
            # ``classify_alpaca_error`` decides on a per-instance basis and we
            # re-raise any non-broker exception so config bugs surface
            # unchanged. ``BaseException`` (``CancelledError``) propagates so
            # cooperative cancellation is never re-classified.
            rejection = classify_alpaca_error(exc)
            if rejection is None:
                raise
            updated.append(
                _to_rejection(
                    result,
                    code=rejection.code,
                    reason=(
                        f"Alpaca rejected: code={rejection.code}, "
                        f"http_status={rejection.http_status}, "
                        f"message={rejection.alpaca_message!r}"
                    ),
                )
            )
            dispatches.append(None)
            continue
        if isinstance(outcome, GatewaySubmissionFailed):
            reason = (
                f"gateway_submission_failed: {outcome.reason} "
                f"(last_error={outcome.last_error_class}, attempts={outcome.attempt_count})"
            )
            updated.append(_to_rejection(result, code="broker_gateway_failure", reason=reason))
            dispatches.append(None)
            abandoned.append(
                _AbandonedCommandEntry(
                    command_id=result.command_id,
                    command_type=_COMMAND_TYPE_TO_LABEL[command.command_type],
                    failure_reason=reason,
                    retry_attempt_count=outcome.attempt_count,
                )
            )
            continue
        assert isinstance(outcome, Submitted)
        # Swap the validator's synthetic order_id for the broker's real
        # alpaca_order_id so the submission_log carries the broker-grade id.
        updated.append(_with_real_order_id(result, outcome.payload.alpaca_order_id))
        dispatches.append(outcome.payload)

    # invocation_id retained on the signature for future provenance threading.
    del invocation_id
    return tuple(updated), tuple(dispatches), tuple(abandoned)


async def _dispatcher_context_for(
    command: OMSCommand, *, invocation_handle: Any | None
) -> dict[str, Any]:
    """Resolve the per-command kwargs the dispatcher needs.

    OPEN carries the instrument inline (no extra context). CLOSE/ADD/ADJUST
    read portfolio state from the persisted position record under
    *invocation_handle*'s session. CANCEL resolves the OMS order_id to the
    broker's alpaca_order_id via the persisted order row.
    """
    if isinstance(command, OpenCommand):
        return {}
    if invocation_handle is None:
        msg = (
            f"engine-stub broker-routing for {command.command_type!r} commands "
            "requires invocation_handle to resolve portfolio state from the "
            "persisted position record."
        )
        raise ValueError(msg)
    if isinstance(command, CloseCommand):
        return await _close_command_context(command, invocation_handle=invocation_handle)
    if isinstance(command, AddCommand):
        return await _add_command_context(command, invocation_handle=invocation_handle)
    if isinstance(command, AdjustCommand):
        return await _adjust_command_context(command, invocation_handle=invocation_handle)
    if isinstance(command, CancelCommand):
        return await _cancel_command_context(command, invocation_handle=invocation_handle)
    msg = f"unsupported OMS command variant for broker routing: {type(command).__name__}"
    raise NotImplementedError(msg)


async def _close_command_context(
    command: CloseCommand, *, invocation_handle: Any
) -> dict[str, Any]:
    """Resolve CLOSE context — equity: symbol/qty/side; option: OCC/intent;
    strategy: legs/strategy_type."""
    position = await _read_position(command.position_id, invocation_handle=invocation_handle)
    if isinstance(position.details, EquityPositionDetails):
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_qty": position.details.share_count,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        # OptionsPositionDetails stores contract fields, not the OCC symbol;
        # derive the OCC at the dispatcher boundary.
        from alphamind.execution.broker_adapter.order_options import build_occ_symbol

        occ = build_occ_symbol(
            position.details.underlying_ticker,
            position.details.expiration_date,
            position.details.contract_type,
            position.details.strike_price,
        )
        return {
            "position_asset_type": "option",
            "occ_symbol": occ,
            "position_qty": position.details.contract_count,
            "position_intent": (
                "sell_to_close" if position.direction.value == "LONG" else "buy_to_close"
            ),
        }
    if isinstance(position.details, StrategyPositionDetails):
        # StrategyPositionDetails carries legs and a strategy_type_label; the
        # broker translator needs the typed StrategyType, so we coerce here.
        legs = _persisted_legs_to_mleg_acks(position.details.legs)
        return {
            "position_asset_type": "strategy",
            "open_legs": legs,
            "strategy_type": cast(StrategyType, position.details.strategy_type_label),
            "position_units": None,
        }
    msg = f"CLOSE references position with unsupported details: {type(position.details).__name__}"
    raise NotImplementedError(msg)


async def _add_command_context(command: AddCommand, *, invocation_handle: Any) -> dict[str, Any]:
    """Resolve ADD context — equity: symbol/side; option: OptionInstrument;
    strategy: legs/strategy_type."""
    position = await _read_position(command.position_id, invocation_handle=invocation_handle)
    if isinstance(position.details, EquityPositionDetails):
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        # Reconstruct the OptionInstrument from the persisted contract fields.
        # ALP-462: ``strike`` is ``Price`` here while ``strike_price`` is still
        # float on legacy ``OptionsPositionDetails``; ``price()`` wraps at the
        # boundary so downstream consumers see Decimal-exact values.
        instrument = OptionInstrument(
            asset_type="option",
            underlying=position.details.underlying_ticker,
            strike=price(str(position.details.strike_price)),
            expiration=position.details.expiration_date.isoformat(),
            contract_type=("call" if position.details.contract_type.value == "CALL" else "put"),
            direction="long" if position.direction.value == "LONG" else "short",
        )
        return {
            "position_asset_type": "option",
            "position_option_instrument": instrument,
            "position_side": "long" if position.direction.value == "LONG" else "short",
        }
    if isinstance(position.details, StrategyPositionDetails):
        legs = _persisted_legs_to_mleg_acks(position.details.legs)
        return {
            "position_asset_type": "strategy",
            "open_legs": legs,
            "strategy_type": cast(StrategyType, position.details.strategy_type_label),
        }
    msg = f"ADD references position with unsupported details: {type(position.details).__name__}"
    raise NotImplementedError(msg)


async def _adjust_command_context(
    command: AdjustCommand, *, invocation_handle: Any
) -> dict[str, Any]:
    """Resolve ADJUST context — reads the bracket-side protective order being
    modified and surfaces its ``alpaca_order_id`` + ``asset_class`` +
    ``order_class``. Leg-target follows the change-field (stop→PRICE_STOP,
    target→TAKE_PROFIT, time→TIME_STOP) to stay in lockstep with
    :func:`_writeback_adjust`.
    """
    position = await _read_position(command.position_id, invocation_handle=invocation_handle)

    if position.bracket_id is None:
        msg = f"ADJUST references position {command.position_id!r} with no bracket"
        raise ValueError(msg)

    target_roles = _adjust_target_roles(command)
    if not target_roles:
        # Pure thesis/event-update ADJUST — surfaced as a structural error so
        # the dispatcher can't silently no-op.
        msg = (
            f"ADJUST against position {command.position_id!r} carries no "
            "protective change-fields (stop / target / time); no broker leg to replace"
        )
        raise ValueError(msg)

    stmt = _select(OrderRow).where(
        OrderRow.bracket_id == position.bracket_id, OrderRow.status == "PENDING"
    )
    rows = (await invocation_handle.session.execute(stmt)).scalars().all()
    target = next((r for r in rows if r.order_role in target_roles), None)
    if target is None:
        msg = (
            f"ADJUST against bracket {position.bracket_id!r} found no "
            f"PENDING protective leg matching roles {sorted(target_roles)} to replace"
        )
        raise ValueError(msg)

    # Strategy→us_option_strategy/mleg; option→us_option/simple;
    # equity→us_equity/simple (Alpaca treats bracket children as simple on
    # cancel-and-replace, with the parent staying linked).
    if isinstance(position.details, StrategyPositionDetails):
        target_asset_class: ReplaceAssetClass = "us_option_strategy"
        target_order_class: ReplaceOrderClass = "mleg"
    elif isinstance(position.details, OptionsPositionDetails):
        target_asset_class = "us_option"
        target_order_class = "simple"
    elif isinstance(position.details, EquityPositionDetails):
        target_asset_class = "us_equity"
        target_order_class = "simple"
    else:
        msg = (
            f"ADJUST against position {command.position_id!r} carries unsupported "
            f"details type {type(position.details).__name__}"
        )
        raise NotImplementedError(msg)

    return {
        "target_alpaca_order_id": target.alpaca_order_id,
        "target_asset_class": target_asset_class,
        "target_order_class": target_order_class,
    }


def _adjust_target_roles(command: AdjustCommand) -> frozenset[str]:
    """Return the protective-leg role(s) the ADJUST targets.

    Mirrors :func:`_protective_roles_for_change_fields`. ``frozenset()`` for
    thesis/event-only updates (no broker mutation needed).
    """
    roles: set[str] = set()
    if command.new_stop_level is not None:
        roles.add("PRICE_STOP")
    if command.new_target_level is not None:
        roles.add("TAKE_PROFIT")
    if command.new_time_expiration is not None:
        roles.add("TIME_STOP")
    return frozenset(roles)


async def _cancel_command_context(
    command: CancelCommand, *, invocation_handle: Any
) -> dict[str, Any]:
    """Resolve CANCEL context — reads target order by OMS id, returns
    ``alpaca_order_id``."""
    order_row = await invocation_handle.session.get(OrderRow, command.order_id)
    if order_row is None:
        msg = f"CANCEL references missing order_id={command.order_id!r}"
        raise ValueError(msg)
    return {"target_alpaca_order_id": order_row.alpaca_order_id}


async def _read_position(position_id: PositionId, *, invocation_handle: Any) -> Any:
    pos_row = await invocation_handle.session.get(PositionRow, position_id)
    if pos_row is None:
        msg = f"command references missing position_id={position_id!r}"
        raise ValueError(msg)
    return _position_row_to_record(pos_row)


def _persisted_legs_to_mleg_acks(legs: Any) -> tuple[Any, ...]:
    """Translate persisted strategy legs to ``MLEGLegAck`` tuples (ratio=1)."""
    from alphamind.execution.broker_adapter import MLEGLegAck
    from alphamind.execution.broker_adapter.order_options import build_occ_symbol

    acks: list[Any] = []
    for leg in legs:
        opt = leg.options
        occ = build_occ_symbol(
            opt.underlying_ticker, opt.expiration_date, opt.contract_type, opt.strike_price
        )
        leg_direction = leg.direction
        if leg_direction is None:
            msg = f"persisted strategy leg {leg.leg_id!r} has no direction set"
            raise ValueError(msg)
        side: Literal["buy", "sell"] = "buy" if leg_direction.value == "LONG" else "sell"
        intent: Literal["buy_to_open", "sell_to_open"] = (
            "buy_to_open" if side == "buy" else "sell_to_open"
        )
        acks.append(
            MLEGLegAck(
                occ_symbol=OccSymbol(occ),
                side=side,
                ratio_qty=1,
                position_intent=intent,
            )
        )
    return tuple(acks)


def _to_rejection(result: SubmissionResult, *, code: str, reason: str) -> SubmissionResult:
    """Flip an accepted result to a broker-rejected one. ``code`` →
    ``gateway_reason`` (ALP-390); ``reason`` → ``suggested_modification``."""
    return SubmissionResult(
        command_ordinal=result.command_ordinal,
        status="rejected",
        command_id=result.command_id,
        rejection_payload=RejectionPayload(
            rules_breached=(
                _BreachedRule(
                    rule=code,
                    current=0.0,
                    limit=0.0,
                    overage=0.0,
                    unit="ok",
                ),
            ),
            suggested_modification=reason,
            gateway_reason=code,
        ),
    )


def _with_real_order_id(
    result: SubmissionResult, alpaca_order_id: AlpacaOrderId
) -> SubmissionResult:
    """Return a copy of *result* whose acknowledgment ``order_id`` is the broker's id."""
    if result.acknowledgment is None:
        return result
    new_ack = result.acknowledgment.model_copy(update={"order_id": alpaca_order_id})
    return SubmissionResult(
        command_ordinal=result.command_ordinal,
        status=result.status,
        command_id=result.command_id,
        acknowledgment=new_ack,
        rejection_payload=result.rejection_payload,
    )


__all__ = [
    "_COMMAND_TYPE_TO_LABEL",
    "_AbandonedCommandEntry",
    "_add_command_context",
    "_adjust_command_context",
    "_adjust_target_roles",
    "_cancel_command_context",
    "_close_command_context",
    "_dispatcher_context_for",
    "_persisted_legs_to_mleg_acks",
    "_read_position",
    "_route_through_broker",
    "_to_rejection",
    "_with_real_order_id",
]
