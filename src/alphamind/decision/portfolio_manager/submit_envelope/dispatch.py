"""Broker-routing helpers for the ``submit_envelope`` package — ALP-464.

Hosts :func:`_route_through_broker` (per-command dispatch loop), per-command
context resolvers, and :class:`_AbandonedCommandEntry`. The
:class:`alphamind.commands.protocols.BrokerDispatch` Protocol is composition-
root injected (ALP-458); ``broker_dispatch=None`` lazy-loads the concrete
``dispatch_command_to_broker``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import select as _select

from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId, PositionId, make_occ_symbol
from alphamind._kernel.money import price
from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    EquityInstrument,
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
from alphamind.persistence.retry import run_with_sqlite_busy_retry
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionsPositionDetails,
    PositionRecord,
    StrategyPositionDetails,
    position_direction,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as _position_row_to_record,
)

if TYPE_CHECKING:
    from alpaca.trading.client import TradingClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from alphamind.config.models.execution import ExecutionConfig
    from alphamind.execution.broker_adapter import AccountStateQueries, QuoteSource
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

# ALP-747 — rejection code surfaced on a command whose bracket geometry is
# materially incoherent vs the live touch at dispatch (a stale-anchor mispricing
# that straddled its own stale reference and so cleared the analyst-side guard).
_STALE_ANCHOR_REJECTION_CODE = "stale_anchor_vs_live_quote"

logger = logging.getLogger(__name__)


async def _stale_anchor_rejection_reason(
    command: OMSCommand, *, quote_source: QuoteSource
) -> str | None:
    """Live-quote coherence backstop (ALP-747): fetch the live touch and return a
    rejection reason if *command*'s bracket geometry is materially incoherent vs
    it, else ``None``.

    Returns ``None`` when the command is out of scope (only an equity OPEN whose
    entry fills at the live quote is checked — see
    :func:`fills_at_live_quote_equity`) or when no live quote is available. The
    fail-open on a missing / erroring quote is deliberate: the broker's own
    bracket validation remains the final backstop, and a flaky quote feed must
    never block an otherwise-valid submission.
    """
    # Imported from the broker_adapter package root (not the ``entry_pricing``
    # submodule) so the edge matches the composition-root carve-out the sibling
    # lazy execution imports already use (see ``.importlinter``).
    from alphamind.execution.broker_adapter import (
        fills_at_live_quote_equity,
        live_bracket_incoherence_reason,
    )

    if not fills_at_live_quote_equity(command):
        return None
    assert isinstance(command, OpenCommand)
    assert isinstance(command.instrument, EquityInstrument)
    ticker = command.instrument.ticker
    try:
        quote = await quote_source.latest_quote(ticker)
    except Exception:
        # Warranted broad except (ALP-747): a misbehaving QuoteSource must never
        # abort an otherwise-valid submission — fail open and let broker
        # validation backstop, mirroring entry_pricing._rewrite_one (ALP-738).
        logger.warning(
            "broker_dispatch: live-coherence quote fetch failed for %s; skipping the "
            "stale-anchor check (broker validation remains the backstop)",
            ticker,
            exc_info=True,
        )
        return None
    if quote is None:
        logger.warning(
            "broker_dispatch: no live quote for %s; skipping the stale-anchor coherence "
            "check (broker validation remains the backstop)",
            ticker,
        )
        return None
    return live_bracket_incoherence_reason(command, quote=quote)


async def _route_through_broker(
    *,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    client: TradingClient,
    queries: AccountStateQueries,
    execution_config: ExecutionConfig,
    invocation_handle: Any | None = None,
    broker_dispatch: BrokerDispatch | None = None,
    quote_source: QuoteSource | None = None,
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

    ALP-747 — before dispatching an accepted equity OPEN whose entry fills at the
    live quote, the bracket geometry is checked against a fresh broker quote
    (``quote_source``). A bracket sized against a stale reference price (the ORCL
    daily-vs-intraday mispricing) straddles its stale anchor self-consistently —
    so the analyst-side ALP-742 guard passes it — but is materially off the live
    touch; it flips to rejected (``_STALE_ANCHOR_REJECTION_CODE``) with an
    :class:`_AbandonedCommandEntry` and is never sent. The check fails open: a
    missing/erroring quote leaves the command to the broker's own validation.
    Skipped entirely when ``quote_source`` is absent (the fixture-only path).
    """
    # Lazy import — atomic ships an alpaca-py-adjacent dependency we don't want
    # loaded for the fixture-only path; only the handle-present path reaches it.
    from alphamind.execution.write_paths.phase2.atomic import session_factory_from_handle

    dispatch: BrokerDispatch
    if broker_dispatch is not None:
        dispatch = broker_dispatch
    else:
        from alphamind.execution.oms.broker_dispatch import dispatch_command_to_broker

        # The concrete dispatcher's explicit per-asset kwargs duck-type the
        # Protocol's ``**context: Any`` shape; the cast is structurally safe
        # at the Protocol seam — exactly the swap-point we want to accept.
        dispatch = cast(BrokerDispatch, dispatch_command_to_broker)

    # ALP-836 — atomicity-first persistence. When an invocation_handle is present
    # (the production broker-active path), each command's durable ``orders`` row is
    # committed BEFORE its broker dispatch (pre-commit), then backfilled with the
    # real broker id after (or torn down on rejection), each in its own retryable
    # transaction on a fresh session derived from the handle's async engine. When
    # absent (the fixture-only broker path), dispatch stays a no-persistence no-op,
    # exactly as before.
    session_factory = (
        session_factory_from_handle(invocation_handle) if invocation_handle is not None else None
    )
    invocation_id = invocation_handle.invocation_id if invocation_handle is not None else None
    ctx = _BrokerRouteCtx(
        client=client,
        queries=queries,
        execution_config=execution_config,
        invocation_handle=invocation_handle,
        dispatch=dispatch,
        quote_source=quote_source,
        session_factory=session_factory,
        invocation_id=invocation_id,
    )

    updated: list[SubmissionResult] = []
    dispatches: list[BrokerDispatchResult | None] = []
    abandoned: list[_AbandonedCommandEntry] = []

    for result, command in zip(submission_results, envelope.commands, strict=True):
        if result.status != "accepted":
            updated.append(result)
            dispatches.append(None)
            continue
        new_result, dispatch_entry, abandoned_entry = await _route_one_command(
            result=result, command=command, ctx=ctx
        )
        updated.append(new_result)
        dispatches.append(dispatch_entry)
        if abandoned_entry is not None:
            abandoned.append(abandoned_entry)

    return tuple(updated), tuple(dispatches), tuple(abandoned)


@dataclass(frozen=True)
class _BrokerRouteCtx:
    """Per-invocation broker-routing context threaded to :func:`_route_one_command`."""

    client: TradingClient
    queries: AccountStateQueries
    execution_config: ExecutionConfig
    invocation_handle: Any | None
    dispatch: BrokerDispatch
    quote_source: QuoteSource | None
    session_factory: async_sessionmaker[AsyncSession] | None
    invocation_id: str | None


async def _route_one_command(
    *,
    result: SubmissionResult,
    command: OMSCommand,
    ctx: _BrokerRouteCtx,
) -> tuple[SubmissionResult, BrokerDispatchResult | None, _AbandonedCommandEntry | None]:
    """Route one accepted command: stale-check → (A) pre-commit → (B) dispatch →
    (C) backfill / (F) teardown. Returns ``(updated_result, dispatch_entry,
    abandoned_entry)``.

    Atomicity (ALP-836) is active when ``ctx.session_factory`` is present: the
    durable ``orders`` row is committed before dispatch and a command whose
    pre-commit cannot land is aborted WITHOUT dispatching (no broker order without
    a durable row). CANCEL has no pre-commit (it creates no row); its writeback
    runs after a successful broker cancel. The per-command persistence is handled
    by the self-guarding ``_precommit_if_atomic`` / ``_abandon_if_atomic`` /
    ``_finalize_dispatch_if_persisting`` helpers (no-ops on the fixture path).
    """
    # ALP-747 stale-anchor coherence backstop — rejected before any pre-commit.
    if ctx.quote_source is not None:
        stale_reason = await _stale_anchor_rejection_reason(command, quote_source=ctx.quote_source)
        if stale_reason is not None:
            logger.warning(
                "broker_dispatch: rejecting %s before submission — %s",
                result.command_id,
                stale_reason,
            )
            return (
                _to_rejection(result, code=_STALE_ANCHOR_REJECTION_CODE, reason=stale_reason),
                None,
                _abandoned(result, command, stale_reason, 0),
            )

    # Resolve the broker-dispatch context BEFORE the pre-commit mutates state.
    # For an ADJUST this reads the ORIGINAL protective leg's real
    # ``alpaca_order_id`` (the broker REPLACE target) while it is still PENDING —
    # the pre-commit cancels that leg and inserts a synthetic-id replacement, so
    # resolving context afterwards would target the placeholder id (ALP-836).
    # OPEN needs no context (returns ``{}``); CLOSE/ADD/CANCEL read rows the
    # pre-commit does not mutate. A resolution failure (missing position/leg)
    # propagates before any pre-commit, leaving no half-mutated graph.
    context_kwargs = await _dispatcher_context_for(command, invocation_handle=ctx.invocation_handle)

    # ALP-847 — a CANCEL/ADJUST whose target leg is monitor-enforced (no broker
    # order → ``target_alpaca_order_id`` resolves to None) is a LOCAL Intent
    # state change. It never reaches the broker dispatch (no synthetic id is ever
    # sent to Alpaca — the ALP-837 path is unrepresentable). Run only the local
    # writeback and return accepted.
    if isinstance(command, CancelCommand | AdjustCommand) and (
        context_kwargs.get("target_alpaca_order_id") is None
    ):
        return await _route_monitor_enforced_local(ctx, command=command, result=result)

    return await _dispatch_to_broker(
        ctx, command=command, result=result, context_kwargs=context_kwargs
    )


async def _dispatch_to_broker(
    ctx: _BrokerRouteCtx,
    *,
    command: OMSCommand,
    result: SubmissionResult,
    context_kwargs: dict[str, Any],
) -> tuple[SubmissionResult, BrokerDispatchResult | None, _AbandonedCommandEntry | None]:
    """(A) pre-commit → (B) dispatch → (C) backfill / (F) teardown for one command.

    The broker-routing body of :func:`_route_one_command`, reached once the
    stale-anchor backstop and the ALP-847 monitor-enforced-local branch have both
    passed. ``context_kwargs`` is the resolved per-command dispatcher context.
    """
    from alphamind.execution.broker_adapter import GatewaySubmissionFailed, Submitted
    from alphamind.execution.broker_adapter.errors import classify_alpaca_error
    from alphamind.execution.broker_adapter.order_options import PermanentRejectionError

    # (A) Pre-commit the durable order row before dispatch. If it cannot land, the
    # command aborts without dispatching — never a broker order without a row.
    try:
        await _precommit_if_atomic(ctx, command=command, result=result)
    except Exception:
        logger.exception(
            "broker_dispatch: pre-commit failed for %s; aborting WITHOUT dispatch "
            "(no broker order without a durable row)",
            result.command_id,
        )
        reason = "precommit_failed: durable order row could not be committed pre-dispatch"
        return (
            _to_rejection(result, code="precommit_failed", reason=reason),
            None,
            _abandoned(result, command, reason, 0),
        )

    # (B) Dispatch to the broker.
    try:
        outcome = await ctx.dispatch(
            command,
            client=ctx.client,
            queries=ctx.queries,
            execution=ctx.execution_config,
            client_order_id=ClientOrderId(result.command_id),
            **context_kwargs,
        )
    except PermanentRejectionError as exc:
        await _abandon_if_atomic(
            ctx, command=command, result=result, reason=f"permanent_rejection: {exc.rejection.code}"
        )
        return _to_rejection(result, code=exc.rejection.code, reason=str(exc)), None, None
    except Exception as exc:
        # Translation seam per runtime §G1: equity/mleg translators re-raise raw
        # alpaca-py APIError on permanent failure; classify here for a uniform
        # rejection shape. The wide catch is warranted — the translator's
        # exception hierarchy is not contracted — and any non-broker exception is
        # re-raised so config bugs surface unchanged. ``BaseException``
        # (``CancelledError``) propagates so cancellation is never re-classified.
        rejection = classify_alpaca_error(exc)
        if rejection is None:
            raise
        await _abandon_if_atomic(
            ctx, command=command, result=result, reason=f"alpaca_rejection: {rejection.code}"
        )
        return (
            _to_rejection(
                result,
                code=rejection.code,
                reason=(
                    f"Alpaca rejected: code={rejection.code}, "
                    f"http_status={rejection.http_status}, "
                    f"message={rejection.alpaca_message!r}"
                ),
            ),
            None,
            None,
        )
    if isinstance(outcome, GatewaySubmissionFailed):
        reason = (
            f"gateway_submission_failed: {outcome.reason} "
            f"(last_error={outcome.last_error_class}, attempts={outcome.attempt_count})"
        )
        await _abandon_if_atomic(ctx, command=command, result=result, reason=reason)
        return (
            _to_rejection(result, code="broker_gateway_failure", reason=reason),
            None,
            _abandoned(result, command, reason, outcome.attempt_count),
        )

    assert isinstance(outcome, Submitted)
    # (C) Backfill the real broker id + flip PENDING_SUBMIT → PENDING (order
    # commands); for CANCEL, persist the cancel writeback now that the broker
    # confirmed the cancel.
    await _finalize_dispatch_if_persisting(
        ctx, command=command, result=result, dispatch_result=outcome.payload
    )
    # Swap the validator's synthetic order_id for the broker's real one so the
    # submission_log carries the broker-grade id.
    return _with_real_order_id(result, outcome.payload.alpaca_order_id), outcome.payload, None


async def _precommit_if_atomic(
    ctx: _BrokerRouteCtx, *, command: OMSCommand, result: SubmissionResult
) -> None:
    """(A) Pre-commit the durable order row in its own retryable transaction.

    No-op when persistence is off (fixture path) or for CANCEL (no order row).
    Raises on a pre-commit that cannot land after retries — the caller aborts the
    command WITHOUT dispatching.
    """
    if (
        ctx.session_factory is None
        or ctx.invocation_id is None
        or isinstance(command, CancelCommand)
    ):
        return
    from alphamind.execution.write_paths.phase2.atomic import precommit_command

    await run_with_sqlite_busy_retry(
        partial(
            precommit_command,
            ctx.session_factory,
            invocation_id=ctx.invocation_id,
            command=command,
            result=result,
        )
    )


async def _abandon_if_atomic(
    ctx: _BrokerRouteCtx, *, command: OMSCommand, result: SubmissionResult, reason: str
) -> None:
    """(F) Tear down a pre-committed command whose broker dispatch was rejected.

    No-op when persistence is off or for CANCEL (no pre-committed row exists).
    """
    if (
        ctx.session_factory is None
        or ctx.invocation_id is None
        or isinstance(command, CancelCommand)
    ):
        return
    from alphamind.execution.write_paths.phase2.atomic import abandon_command

    await abandon_command(
        ctx.session_factory,
        invocation_id=ctx.invocation_id,
        command=command,
        result=result,
        reason=reason,
    )


async def _finalize_dispatch_if_persisting(
    ctx: _BrokerRouteCtx,
    *,
    command: OMSCommand,
    result: SubmissionResult,
    dispatch_result: BrokerDispatchResult,
) -> None:
    """(C) After a successful dispatch: backfill the real broker id (order
    commands) or persist the cancel writeback (CANCEL). No-op when persistence is
    off (fixture path).
    """
    if ctx.session_factory is None or ctx.invocation_id is None:
        return
    from alphamind.execution.write_paths.phase2.atomic import (
        backfill_command_broker_ids,
        persist_cancel_writeback,
    )

    if isinstance(command, CancelCommand):
        await persist_cancel_writeback(
            ctx.session_factory, invocation_id=ctx.invocation_id, command=command
        )
        return
    await run_with_sqlite_busy_retry(
        partial(
            backfill_command_broker_ids,
            ctx.session_factory,
            command=command,
            result=result,
            dispatch_result=dispatch_result,
        )
    )


async def _route_monitor_enforced_local(
    ctx: _BrokerRouteCtx,
    *,
    command: CancelCommand | AdjustCommand,
    result: SubmissionResult,
) -> tuple[SubmissionResult, BrokerDispatchResult | None, _AbandonedCommandEntry | None]:
    """Apply a monitor-enforced leg's CANCEL/ADJUST as a LOCAL Intent change (ALP-847).

    A monitor-enforced leg has no broker order, so its CANCEL/ADJUST never goes to
    the broker — there is no synthetic id to send (the ALP-837 path is
    unrepresentable). The local writeback alone realizes the change:

    * CANCEL → ``persist_cancel_writeback`` marks the leg's order CANCELLED and
      releases nothing (a protective leg reserves no capital).
    * ADJUST → ``precommit_command`` runs the ADJUST writeback, which cancels the
      old leg and inserts a fresh monitor-enforced replacement (``alpaca_order_id``
      None — no broker dispatch follows, so the precommit IS the final state).

    No-op writeback on the fixture path (no ``session_factory``); the result is
    still ``accepted`` so the submission log reflects the local action. Returns no
    dispatch entry and no abandoned entry.
    """
    if ctx.session_factory is not None and ctx.invocation_id is not None:
        from alphamind.execution.write_paths.phase2.atomic import (
            persist_cancel_writeback,
            precommit_command,
        )

        if isinstance(command, CancelCommand):
            await run_with_sqlite_busy_retry(
                partial(
                    persist_cancel_writeback,
                    ctx.session_factory,
                    invocation_id=ctx.invocation_id,
                    command=command,
                )
            )
        else:
            await run_with_sqlite_busy_retry(
                partial(
                    precommit_command,
                    ctx.session_factory,
                    invocation_id=ctx.invocation_id,
                    command=command,
                    result=result,
                )
            )
    logger.info(
        "broker_dispatch: %s on a monitor-enforced leg applied locally (no broker call) — %s",
        command.command_type.upper(),
        result.command_id,
    )
    return result, None, None


def _abandoned(
    result: SubmissionResult, command: OMSCommand, reason: str, attempt_count: int
) -> _AbandonedCommandEntry:
    return _AbandonedCommandEntry(
        command_id=result.command_id,
        command_type=_COMMAND_TYPE_TO_LABEL[command.command_type],
        failure_reason=reason,
        retry_attempt_count=attempt_count,
    )


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
            f"submit_envelope broker-routing for {command.command_type!r} commands "
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
        direction = _equity_or_options_direction(position)
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_qty": position.details.share_count,
            "position_side": "long" if direction is Direction.LONG else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        # OptionsPositionDetails stores contract fields, not the OCC symbol;
        # derive the OCC at the dispatcher boundary.
        from alphamind.execution.broker_adapter.order_options import build_occ_symbol

        direction = _equity_or_options_direction(position)
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
            "position_intent": ("sell_to_close" if direction is Direction.LONG else "buy_to_close"),
        }
    if isinstance(position.details, StrategyPositionDetails):
        # StrategyPositionDetails carries legs and a strategy_type_label; the
        # broker translator needs the typed StrategyType, so we coerce here.
        # The CLOSE threads *close-side* legs — the single open→close
        # inversion seam reverses each leg (LONG → sell_to_close, SHORT →
        # buy_to_close) and preserves the direction-is-None → ValueError guard.
        from alphamind.execution.broker_adapter import strategy_legs_to_close_acks

        return {
            "position_asset_type": "strategy",
            "close_legs": strategy_legs_to_close_acks(position.details.legs),
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
        direction = _equity_or_options_direction(position)
        return {
            "position_asset_type": "equity",
            "position_symbol": position.details.ticker,
            "position_side": "long" if direction is Direction.LONG else "short",
        }
    if isinstance(position.details, OptionsPositionDetails):
        # Reconstruct the OptionInstrument from the persisted contract fields.
        # ALP-462: ``strike`` is ``Price`` here while ``strike_price`` is still
        # float on legacy ``OptionsPositionDetails``; ``price()`` wraps at the
        # boundary so downstream consumers see Decimal-exact values.
        direction = _equity_or_options_direction(position)
        side: Literal["long", "short"] = "long" if direction is Direction.LONG else "short"
        instrument = OptionInstrument(
            asset_type="option",
            underlying=position.details.underlying_ticker,
            strike=price(str(position.details.strike_price)),
            expiration=position.details.expiration_date.isoformat(),
            contract_type=("call" if position.details.contract_type.value == "CALL" else "put"),
            direction=side,
        )
        return {
            "position_asset_type": "option",
            "position_option_instrument": instrument,
            "position_side": side,
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


def _equity_or_options_direction(position: PositionRecord) -> Direction:
    """Return the position-level direction for an equity / single-leg options record.

    The equity / options dispatch builders are reached only inside ``isinstance``
    narrowings, where :func:`position_direction` always yields a concrete
    :class:`Direction` (a strategy — the one ``None`` case — routes to its own
    branch). The non-``None`` assertion makes that instrument-narrowing
    invariant explicit.
    """
    direction = position_direction(position)
    assert direction is not None
    return direction


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
                occ_symbol=make_occ_symbol(occ),
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
    "_stale_anchor_rejection_reason",
    "_to_rejection",
    "_with_real_order_id",
]
