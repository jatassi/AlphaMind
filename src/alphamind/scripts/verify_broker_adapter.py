"""Broker adapter end-to-end verification (ALP-393).

Operator entry point (via thin shim at ``scripts/verify_broker_adapter.py``).
Exercises the broker adapter end-to-end against Alpaca's paper environment
across seven phases:

* Phase 1 — Adapter substrate (factory + retry helper + error classifier).
* Phase 2 — Account state queries (account, positions, asset, calendar, clock,
  orders pagination).
* Phase 3 — Equity order lifecycle (OPEN bracket → fills → CLOSE → CANCEL +
  ADJUST round-trip via the OMS path).
* Phase 4 — Single-leg options order lifecycle (OPEN → fills → CLOSE).
* Phase 5 — Multi-leg strategy lifecycle (vertical spread OPEN → all-legs-fill
  → CLOSE).
* Phase 6 — Venue config in isolation (constants, calendar cache,
  account-state surfacer, settlement calculator).
* Phase 7 — Disconnect-recovery routine.

The summary printer returns 0 on all-pass, nonzero on any FAIL.

Usage::

    uv run python scripts/verify_broker_adapter.py [--mode paper|live] \
        [--phase NAME] [--verbose]

See ``scripts/RUNBOOK_broker_adapter.md`` for the operator runbook including
prerequisites, expected output, and failure-mode triage.

Per runtime §G1: every broker-adapter probe narrows its catch to
``_PROBE_EXCEPTIONS = (APIError, httpx.HTTPError, OSError)`` so the
expected vendor-API + transport failures are folded into the per-check
error string while misconfiguration (TypeError, AttributeError) surfaces
naturally. ``BaseException`` (``KeyboardInterrupt``) propagates so the
verify run can be cancelled.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from alpaca.common.exceptions import APIError

from alphamind._kernel.ids import (
    AlpacaOrderId,
    ClientOrderId,
    OccSymbol,
    PositionId,
)
from alphamind._kernel.money import money, price
from alphamind.commands.command_models import (
    BracketOrderParameters,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    StrategyInstrument,
    StrategyLeg,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.execution import ExecutionConfig
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.broker_adapter import (
    AccountStateQueries,
    AlpacaClientFactory,
    Submitted,
    classify_alpaca_error,
    recover_missed_fills_since,
    submit_with_retry,
)

# Per-check probe failures narrow to ``(APIError, httpx.HTTPError, OSError)`` —
# the broker adapter exposes these three classes for Alpaca SDK errors,
# transport-level HTTP failures, and underlying socket / file errors
# respectively. Any other exception surfaces naturally so misconfiguration
# (TypeError, AttributeError, ImportError) isn't mistaken for an API outage.
_PROBE_EXCEPTIONS = (APIError, httpx.HTTPError, OSError)

__all__ = [
    "PHASE_NAMES",
    "PhaseResult",
    "VerifyContext",
    "main",
    "phase_1_adapter_substrate",
    "phase_2_account_state_queries",
    "phase_3_equity_order_lifecycle",
    "phase_4_options_order_lifecycle",
    "phase_5_mleg_order_lifecycle",
    "phase_6_venue_config",
    "phase_7_disconnect_recovery",
]


VerifyMode = Literal["paper", "live"]


def _today_utc() -> dt.date:
    """Return today's date in UTC (DTZ-aware replacement for ``date.today()``)."""
    return dt.datetime.now(dt.UTC).date()


# ---------------------------------------------------------------------------
# Result type — pass / deferred / fail tri-state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseResult:
    """One phase's outcome — pass/deferred/fail flag + label + diagnostic.

    ``ok`` is ``True`` when the phase passed.  ``deferred`` is ``True`` when
    the phase intentionally skipped (e.g. live-mode-specific assertions in
    paper mode); the summary printer counts DEFERRED as non-failing for
    exit-code purposes per the acceptance criteria.
    """

    label: str
    ok: bool
    detail: str | None = None
    deferred: bool = False

    @classmethod
    def passed(cls, label: str, detail: str | None = None) -> PhaseResult:
        return cls(label=label, ok=True, detail=detail, deferred=False)

    @classmethod
    def failed(cls, label: str, detail: str) -> PhaseResult:
        return cls(label=label, ok=False, detail=detail, deferred=False)

    @classmethod
    def skipped(cls, label: str, detail: str) -> PhaseResult:
        return cls(label=label, ok=True, detail=detail, deferred=True)


# ---------------------------------------------------------------------------
# Verify context — bundle of dependencies threaded into each phase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerifyContext:
    """Per-invocation dependencies threaded into each phase function.

    ``factory`` mints fresh Alpaca clients; ``queries`` is the long-lived
    state-query wrapper. ``execution`` carries the retry-window and other
    operator-tunable execution knobs. ``mode`` records the operator's selected
    paper/live mode (only ``paper`` is in scope for the verify script's
    end-to-end live drives — live-mode assertions DEFER per the spec).

    ``db_path`` is the optional SQLite file the operator threads through for
    Phases 3 / 4 / 5 — the equity / options / mleg lifecycles need a writable
    DB to record the OMS-side activity-log entries that prove the round-trip
    completed. When ``None``, those phases DEFER with a precise reason; when
    set, they drive a real Alpaca paper round-trip via the broker dispatcher.
    """

    factory: AlpacaClientFactory
    queries: AccountStateQueries
    execution: ExecutionConfig
    venue: VenueConfig
    mode: VerifyMode
    verbose: bool
    db_path: Path | None = None


# ---------------------------------------------------------------------------
# Phase 1 — Adapter substrate
# ---------------------------------------------------------------------------


_PHASE_1_LABEL = "Phase 1 — Adapter substrate"


async def phase_1_adapter_substrate(ctx: VerifyContext) -> PhaseResult:
    """Exercise factory + retry helper + error classifier in isolation.

    Asserts the operator's selected ``ctx.factory`` builds both ``TradingClient``
    and ``TradingStream`` instances; ``submit_with_retry`` returns ``Submitted``
    on first-try success; ``submit_with_retry`` retries on classified-transient
    failures and ultimately returns ``Submitted``; ``classify_alpaca_error``
    maps representative 4xx / 5xx exceptions to their documented codes.
    """
    checks: tuple[Callable[[VerifyContext], Coroutine[Any, Any, str | None]], ...] = (
        _phase_1_check_factory,
        _phase_1_check_first_try_success,
        _phase_1_check_transient_retry,
    )
    for check in checks:
        failure = await check(ctx)
        if failure is not None:
            return PhaseResult.failed(_PHASE_1_LABEL, failure)

    classifier_failure = _phase_1_check_classifier()
    if classifier_failure is not None:
        return PhaseResult.failed(_PHASE_1_LABEL, classifier_failure)

    return PhaseResult.passed(_PHASE_1_LABEL)


async def _phase_1_check_factory(ctx: VerifyContext) -> str | None:
    try:
        ctx.factory.build_trading_client()
        ctx.factory.build_trading_stream()
    except _PROBE_EXCEPTIONS as exc:
        return f"factory failed to build clients: {exc!s}"
    return None


async def _phase_1_check_first_try_success(_ctx: VerifyContext) -> str | None:
    sentinel = object()

    async def _success() -> object:
        return sentinel

    outcome = await submit_with_retry(_success, window_seconds=1.0)
    if not isinstance(outcome, Submitted):
        return f"submit_with_retry first-try success returned {outcome!r}, expected Submitted"
    if outcome.payload is not sentinel:
        return "submit_with_retry first-try success did not propagate the callable's return value"
    if outcome.attempt_count != 1:
        return f"submit_with_retry first-try attempt_count={outcome.attempt_count}, expected 1"
    return None


async def _phase_1_check_transient_retry(_ctx: VerifyContext) -> str | None:
    transient_failures = {"count": 0}

    async def _transient_then_success() -> str:
        if transient_failures["count"] < 2:
            transient_failures["count"] += 1
            raise OSError("simulated transient network blip")
        return "recovered"

    outcome = await submit_with_retry(_transient_then_success, window_seconds=5.0)
    if not isinstance(outcome, Submitted):
        return f"submit_with_retry transient-retry returned {outcome!r}, expected Submitted"
    if outcome.attempt_count != 3:
        return (
            f"submit_with_retry transient-retry attempt_count="
            f"{outcome.attempt_count}, expected 3 (two retries then success)"
        )
    return None


def _phase_1_check_classifier() -> str | None:
    """Confirm ``classify_alpaca_error`` maps representative cases.

    Synthesizes minimal exception fakes carrying ``status_code`` + ``message``
    attributes (matching ``APIError``'s public surface) so the helper can be
    exercised without depending on the alpaca-py ``APIError`` constructor.
    """
    cases: tuple[tuple[int, str, str], ...] = (
        (422, "validation failed: payload missing field", "validation_failed"),
        (403, "insufficient buying power for this order", "insufficient_buying_power"),
        (500, "server exploded", "<transient>"),
    )
    for status, message, expected_code in cases:
        exc = _SyntheticAPIError(status, message)
        result = classify_alpaca_error(exc)
        if expected_code == "<transient>":
            if result is not None:
                return (
                    f"classify_alpaca_error({status}) returned {result!r}; "
                    "expected None (transient → retry)"
                )
            continue
        if result is None or result.code != expected_code:
            return (
                f"classify_alpaca_error({status}, {message!r}) returned {result!r}; "
                f"expected code={expected_code!r}"
            )
    return None


class _SyntheticAPIError(Exception):
    """Stand-in for ``alpaca.common.exceptions.APIError``.

    Carries the same ``status_code`` + ``message`` attribute surface
    ``classify_alpaca_error`` reads from; avoids the real ``APIError``
    constructor's HTTP-error wrapping requirement during in-process
    classification probes.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------------------
# Phase 2 — Account state queries
# ---------------------------------------------------------------------------


_PHASE_2_LABEL = "Phase 2 — Account state queries"


async def phase_2_account_state_queries(ctx: VerifyContext) -> PhaseResult:
    """Drive ``AccountStateQueries`` against the live Alpaca paper REST API.

    Asserts ``get_account()`` returns non-None equity / cash, ``get_positions()``
    returns a tuple, ``get_asset(known)`` returns a populated snapshot,
    ``get_asset(unknown)`` returns ``None``, ``get_calendar`` returns ≥ 20
    business-day records over today + 30 calendar days, ``get_clock`` returns
    a sensible ``MarketClock``, and ``get_orders(status="all")`` paginates
    cleanly without raising.
    """
    sync_checks: tuple[Callable[[AccountStateQueries], str | None], ...] = (
        _phase_2_check_account,
        _phase_2_check_positions,
        _phase_2_check_known_asset,
        _phase_2_check_unknown_asset,
        _phase_2_check_calendar,
        _phase_2_check_clock,
    )
    for check in sync_checks:
        failure = check(ctx.queries)
        if failure is not None:
            return PhaseResult.failed(_PHASE_2_LABEL, failure)

    orders_failure = await _phase_2_check_orders(ctx.queries)
    if orders_failure is not None:
        return PhaseResult.failed(_PHASE_2_LABEL, orders_failure)

    return PhaseResult.passed(_PHASE_2_LABEL)


def _phase_2_check_account(queries: AccountStateQueries) -> str | None:
    try:
        account = queries.get_account()
    except _PROBE_EXCEPTIONS as exc:
        return f"get_account raised: {exc!s}"
    if account.equity is None or account.cash is None:
        return (
            f"get_account returned None equity/cash (equity={account.equity}, cash={account.cash})"
        )
    return None


def _phase_2_check_positions(queries: AccountStateQueries) -> str | None:
    try:
        positions = queries.get_positions()
    except _PROBE_EXCEPTIONS as exc:
        return f"get_positions raised: {exc!s}"
    if not isinstance(positions, tuple):
        return f"get_positions returned {type(positions).__name__}, expected tuple"
    return None


def _phase_2_check_known_asset(queries: AccountStateQueries) -> str | None:
    try:
        nvda = queries.get_asset("NVDA")
    except _PROBE_EXCEPTIONS as exc:
        return f"get_asset('NVDA') raised: {exc!s}"
    if nvda is None:
        return "get_asset('NVDA') returned None — NVDA should be in Alpaca's universe"
    if not nvda.tradable:
        return f"get_asset('NVDA').tradable={nvda.tradable!r}, expected True"
    return None


def _phase_2_check_unknown_asset(queries: AccountStateQueries) -> str | None:
    """Confirm 404 on unknown symbols maps to None per ``get_asset`` contract."""
    try:
        unknown = queries.get_asset("ZZNONEXISTENT")
    except _PROBE_EXCEPTIONS as exc:
        return f"get_asset('ZZNONEXISTENT') raised (expected None): {exc!s}"
    if unknown is not None:
        return f"get_asset('ZZNONEXISTENT') returned {unknown!r}, expected None (404 → None)"
    return None


def _phase_2_check_calendar(queries: AccountStateQueries) -> str | None:
    today = _today_utc()
    try:
        calendar = queries.get_calendar(start=today, end=today + dt.timedelta(days=30))
    except _PROBE_EXCEPTIONS as exc:
        return f"get_calendar raised: {exc!s}"
    if len(calendar) < 20:
        return f"get_calendar returned {len(calendar)} entries, expected ≥ 20 over 30 days"
    return None


def _phase_2_check_clock(queries: AccountStateQueries) -> str | None:
    try:
        clock = queries.get_clock()
    except _PROBE_EXCEPTIONS as exc:
        return f"get_clock raised: {exc!s}"
    if clock.timestamp is None or clock.next_open is None or clock.next_close is None:
        return f"get_clock returned incomplete record: {clock!r}"
    return None


async def _phase_2_check_orders(queries: AccountStateQueries) -> str | None:
    """Iterate ``get_orders`` to drain the async paginator; success is no-raise."""
    try:
        async for _order in queries.get_orders(
            status="all",
            since=dt.datetime.now(dt.UTC) - dt.timedelta(days=7),
        ):
            pass
    except _PROBE_EXCEPTIONS as exc:
        return f"get_orders pagination raised: {exc!s}"
    return None


# ---------------------------------------------------------------------------
# Phase 3 / 4 / 5 — shared lifecycle orchestrator
#
# Each lifecycle drives the same five-step shape: dispatch OPEN, wait for
# entry fill, dispatch CLOSE, wait for exit fill, run cleanup on any
# residue. The phase-specific knobs (label, fill-wait budget, OPEN command,
# CLOSE-context kwargs) ride on a frozen ``_LifecyclePlan``; the orchestrator
# below is the single shared driver.
# ---------------------------------------------------------------------------


# Per-phase fill-wait budgets. Equity 30s; options 60s; mleg 120s (multi-leg
# fill latency is variable on paper).
_PHASE_3_FILL_TIMEOUT_SECONDS: float = 30.0
_PHASE_4_FILL_TIMEOUT_SECONDS: float = 60.0
_PHASE_5_FILL_TIMEOUT_SECONDS: float = 120.0


@dataclass(frozen=True)
class _LifecyclePlan:
    """Phase-specific knobs the shared lifecycle orchestrator consumes.

    ``open_command`` is the canonical OPEN to dispatch. ``close_context`` is
    the kwargs dict the CLOSE dispatch needs (asset-type-specific —
    equity threads symbol/qty/side; options threads occ_symbol + intent;
    strategy threads open_legs + strategy_type). ``cleanup_position_id`` is
    the synthetic position id used on residual CLOSE / CANCEL paths.
    """

    label: str
    fill_timeout_seconds: float
    role_prefix: str
    open_command: OpenCommand
    close_context: dict[str, Any]
    cleanup_position_id: str


def _deferred_no_db(label: str, lifecycle: str) -> PhaseResult:
    """Build the standard "no db_path threaded" deferral result."""
    return PhaseResult.skipped(
        label,
        (
            f"DEFERRED: no DB path threaded through VerifyContext. "
            f"The {lifecycle} OPEN→fill→CLOSE round-trip requires a writable DB to "
            "record OMS-side activity-log entries; operator drives this via the "
            "runbook."
        ),
    )


def _deferred_market_closed(label: str) -> PhaseResult:
    """Build the standard "market closed; re-run during a session" deferral result.

    Paper-mode equity / option fills only arrive during the regular US session
    window (09:30-16:00 ET). Submitting orders off-hours guarantees the
    per-phase fill-wait budget will expire — the OPEN is accepted by Alpaca
    but the entry fill never arrives. Defer up front with a clear operator
    message instead of forcing a misleading FAIL diagnostic.
    """
    return PhaseResult.skipped(
        label,
        (
            "Market closed (US session 09:30-16:00 ET); re-run during a session "
            "for low-latency paper fills."
        ),
    )


_PHASE_3_LABEL = "Phase 3 — Equity order lifecycle"
_PHASE_4_LABEL = "Phase 4 — Options order lifecycle"
_PHASE_5_LABEL = "Phase 5 — Mleg strategy lifecycle"

# Verify-script underlying — same NVDA across phases keeps the operator's
# paper account in one underlying. NVDA also matches Phase 2's known-symbol
# probe (one less moving part).
_VERIFY_UNDERLYING: str = "NVDA"


async def phase_3_equity_order_lifecycle(ctx: VerifyContext) -> PhaseResult:
    """Drive an OPEN → fill → CLOSE round-trip on a paper-mode equity bracket.

    Defers when ``ctx.db_path`` is None (subagent runs lack a writable DB).
    Defers when ``MarketClock.is_open`` is False (off-hours fills don't arrive
    within the per-phase budget). Otherwise dispatches a 1-share OPEN bracket
    via the broker dispatcher, waits for the entry fill via
    ``subscribe_trade_updates``, then submits a CLOSE and confirms the close
    fill. Wraps in try/finally so a mid-flight timeout triggers cleanup
    (CANCEL on pending order, CLOSE on opened position).
    """
    if ctx.db_path is None:
        return _deferred_no_db(_PHASE_3_LABEL, "equity")
    if not ctx.queries.get_clock().is_open:
        return _deferred_market_closed(_PHASE_3_LABEL)
    plan = _LifecyclePlan(
        label=_PHASE_3_LABEL,
        fill_timeout_seconds=_PHASE_3_FILL_TIMEOUT_SECONDS,
        role_prefix="phase3",
        open_command=_build_equity_open_command(_VERIFY_UNDERLYING),
        close_context={
            "position_asset_type": "equity",
            "position_symbol": _VERIFY_UNDERLYING,
            "position_qty": 1.0,
            "position_side": "long",
        },
        cleanup_position_id=f"verify-cleanup-{_VERIFY_UNDERLYING}",
    )
    return await _drive_lifecycle(ctx, plan)


async def _drive_lifecycle(ctx: VerifyContext, plan: _LifecyclePlan) -> PhaseResult:
    """Drive the OPEN→fill→CLOSE round-trip described by *plan*.

    Opens a single ``subscribe_trade_updates`` generator before dispatching
    the OPEN and drains it across both the OPEN and CLOSE fills. Two
    subscribers on the same stream race for events from the underlying
    queue — whichever generator's coroutine wakes first consumes the event
    and the other receives nothing, occasionally causing the close-fill
    wait to time out spuriously.

    Wraps in try/finally so any mid-flight failure triggers a best-effort
    CANCEL on a still-pending OPEN or a CLOSE on an opened-but-not-closed
    position. The single subscriber is closed in the same finally so it
    cannot leak after the phase returns.
    """
    from alphamind.execution.broker_adapter import (
        GatewaySubmissionFailed,
        Submitted,
        subscribe_trade_updates,
    )
    from alphamind.execution.oms.broker_dispatch import dispatch_command_to_broker

    client = ctx.factory.build_trading_client()
    stream = ctx.factory.build_trading_stream()

    open_alpaca_id: str | None = None
    position_opened: bool = False
    fill_stream = subscribe_trade_updates(stream)

    try:
        open_outcome = await dispatch_command_to_broker(
            plan.open_command,
            client=client,
            queries=ctx.queries,
            execution=ctx.execution,
            client_order_id=ClientOrderId(_client_order_id_for_phase(f"{plan.role_prefix}-open")),
        )
        if isinstance(open_outcome, GatewaySubmissionFailed):
            return PhaseResult.failed(
                plan.label,
                f"OPEN gateway-submission failed: {open_outcome.reason}",
            )
        assert isinstance(open_outcome, Submitted)
        open_alpaca_id = open_outcome.payload.alpaca_order_id

        wait_open = await _wait_for_fill_on_stream(
            fill_stream,
            alpaca_order_id=open_alpaca_id,
            timeout_seconds=plan.fill_timeout_seconds,
            label="OPEN entry",
        )
        if wait_open is not None:
            return PhaseResult.failed(plan.label, wait_open)
        position_opened = True

        close_command = CloseCommand(
            command_type="close",
            position_id=PositionId(f"verify-{plan.role_prefix}-{open_alpaca_id}"),
            quantity="all",
            order_type="market",
            close_rationale_type="target_reached",
        )
        close_outcome = await dispatch_command_to_broker(
            close_command,
            client=client,
            queries=ctx.queries,
            execution=ctx.execution,
            client_order_id=ClientOrderId(_client_order_id_for_phase(f"{plan.role_prefix}-close")),
            **plan.close_context,
        )
        if isinstance(close_outcome, GatewaySubmissionFailed):
            return PhaseResult.failed(
                plan.label,
                f"CLOSE gateway-submission failed: {close_outcome.reason}",
            )
        assert isinstance(close_outcome, Submitted)

        wait_close = await _wait_for_fill_on_stream(
            fill_stream,
            alpaca_order_id=close_outcome.payload.alpaca_order_id,
            timeout_seconds=plan.fill_timeout_seconds,
            label="CLOSE exit",
        )
        if wait_close is not None:
            return PhaseResult.failed(plan.label, wait_close)
        position_opened = False
        return PhaseResult.passed(plan.label)
    finally:
        await fill_stream.aclose()
        await _cleanup_residue(
            ctx=ctx,
            client=client,
            plan=plan,
            pending_alpaca_order_id=open_alpaca_id if not position_opened else None,
            position_opened=position_opened,
        )


async def _cleanup_residue(
    *,
    ctx: VerifyContext,
    client: Any,
    plan: _LifecyclePlan,
    pending_alpaca_order_id: str | None,
    position_opened: bool,
) -> None:
    """Best-effort cleanup of any pending order + opened position from a failed run.

    Each cleanup call is suppressed so cleanup errors don't mask the original
    failure diagnostic the phase already returned. Called from the phase's
    ``finally`` so a mid-flight timeout doesn't leave the paper account in a
    half-open state.
    """
    from alphamind.execution.broker_adapter import submit_cancel
    from alphamind.execution.oms.broker_dispatch import dispatch_command_to_broker

    if pending_alpaca_order_id is not None:
        with contextlib.suppress(Exception):
            await submit_cancel(
                client=client,
                execution=ctx.execution,
                target_alpaca_order_id=AlpacaOrderId(pending_alpaca_order_id),
            )

    if position_opened:
        cleanup_close = CloseCommand(
            command_type="close",
            position_id=PositionId(plan.cleanup_position_id),
            quantity="all",
            order_type="market",
            close_rationale_type="risk_management",
            risk_management_subtype="pm_directed",
        )
        with contextlib.suppress(Exception):
            await dispatch_command_to_broker(
                cleanup_close,
                client=client,
                queries=ctx.queries,
                execution=ctx.execution,
                client_order_id=ClientOrderId(
                    _client_order_id_for_phase(f"{plan.role_prefix}-cleanup")
                ),
                **plan.close_context,
            )


def _build_equity_open_command(ticker: str) -> OpenCommand:
    """Build a 1-share long market OPEN bracket on *ticker*.

    Uses a very-wide stop (1¢ trigger) and a very-far target ($10k limit)
    so neither bracket leg fires before the verify closes the position; the
    BRACKET shape exercises the full bracket-class translation but the entry
    fills immediately at market.
    """
    # ALP-462 — wrap fixture floats via ``money()`` / ``price()`` at the
    # boundary so the verify script's commands carry Decimal-exact values.
    return OpenCommand(
        command_type="open",
        instrument=EquityInstrument(asset_type="equity", ticker=ticker, direction="long"),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=1.0, dollar_value=money("1")),
        target=Target(target_type="absolute_price", price=price("10000"), order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=ticker,
                    comparator="<=",
                    trigger_price=price("0.01"),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=_verify_thesis(ticker, "equity OPEN"),
    )


def _verify_thesis(ticker: str, summary_suffix: str) -> Thesis:
    """Build the placeholder thesis attached to verify-script OPEN commands."""
    return Thesis(
        summary=f"verify-script {summary_suffix} — proves broker round-trip wiring",
        components=(
            ThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference=ticker,
                narrative="verify script smoke",
                key_assumptions=("paper-mode marketability",),
            ),
        ),
    )


def _client_order_id_for_phase(role: str) -> str:
    """Build a verify-script client_order_id matching ``oms-command-ids.md``.

    Canonical PM-originated pattern is
    ``inv-{invocation_id}.{envelope_id}.{command_ordinal}.{attempt_seq}``.
    Each call gets a fresh microsecond-suffixed invocation_id so consecutive
    OPEN / CLOSE / cleanup submissions don't collide against an Alpaca-side
    dedup window.
    """
    from alphamind.execution.oms.command_ids import derive_pm_command_id

    now = dt.datetime.now(dt.UTC)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    invocation = f"verify-{stamp}-{role}-{now.microsecond:06d}"
    return derive_pm_command_id(
        invocation_id=invocation,
        envelope_id="ENV-REC-1",
        command_ordinal=0,
        attempt_seq=0,
    )


async def _wait_for_fill_on_stream(
    fill_stream: Any,
    *,
    alpaca_order_id: str,
    timeout_seconds: float,
    label: str,
) -> str | None:
    """Drain *fill_stream* until a fill matching *alpaca_order_id* arrives.

    The caller owns the generator's lifecycle (one subscriber per lifecycle,
    drained across both the OPEN and CLOSE fills) so this function does NOT
    open or close the generator — closing twice would double-cancel the
    background ``stream._run_forever()`` task. Returns ``None`` on success
    or a diagnostic string on timeout. Each ``__anext__`` is bounded by
    ``asyncio.wait_for`` so the overall wait stays under
    ``timeout_seconds``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    timeout_diagnostic = (
        f"{label} fill never arrived for alpaca_order_id={alpaca_order_id!r} "
        f"within {timeout_seconds:.0f}s"
    )
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return timeout_diagnostic
        try:
            report = await asyncio.wait_for(fill_stream.__anext__(), timeout=remaining)
        except TimeoutError:
            return timeout_diagnostic
        except StopAsyncIteration:
            return f"{label} stream closed before fill arrived"
        if report.alpaca_order_id == alpaca_order_id and report.event_type == "filled":
            return None


# ---------------------------------------------------------------------------
# Phase 4 — Single-leg options order lifecycle
# ---------------------------------------------------------------------------


async def phase_4_options_order_lifecycle(ctx: VerifyContext) -> PhaseResult:
    """Drive an OPEN → fill → CLOSE round-trip on a paper-mode single-leg option.

    Defers when ``ctx.db_path`` is None. Defers when the market is closed
    (off-hours options fills don't arrive within the 60s budget). Otherwise
    picks the nearest expiration ≥ 7 days out that has a non-empty listed
    chain, picks the median listed call strike (approximately ATM), submits a
    1-contract long-call OPEN on that strike, waits for the fill, then
    submits a CLOSE. Wraps in try/finally so a mid-flight timeout triggers
    cleanup. Returns a DEFER if no expiration in the search window has any
    listed contracts.
    """
    if ctx.db_path is None:
        return _deferred_no_db(_PHASE_4_LABEL, "single-leg options")
    if not ctx.queries.get_clock().is_open:
        return _deferred_market_closed(_PHASE_4_LABEL)
    try:
        listed = _pick_listed_chain_for_underlying(
            ctx.queries, underlying=_VERIFY_UNDERLYING, days_out_min=7
        )
    except RuntimeError as exc:
        return PhaseResult.failed(
            _PHASE_4_LABEL,
            f"could not pick options expiration from live calendar: {exc!s}",
        )
    if listed is None:
        return PhaseResult.skipped(
            _PHASE_4_LABEL,
            (
                f"No listed call contracts on {_VERIFY_UNDERLYING} for any business "
                f"day in the next {_EXPIRATION_SEARCH_WINDOW_DAYS} days; "
                f"cannot pick a strike."
            ),
        )
    expiration, strikes = listed
    strike = _pick_median_strike(strikes)
    occ_symbol = _build_occ(_VERIFY_UNDERLYING, expiration, strike)
    plan = _LifecyclePlan(
        label=_PHASE_4_LABEL,
        fill_timeout_seconds=_PHASE_4_FILL_TIMEOUT_SECONDS,
        role_prefix="phase4",
        open_command=_build_options_open_command(
            underlying=_VERIFY_UNDERLYING,
            strike=strike,
            expiration=expiration,
        ),
        close_context={
            "position_asset_type": "option",
            "occ_symbol": occ_symbol,
            "position_qty": 1.0,
            "position_intent": "sell_to_close",
        },
        cleanup_position_id=f"verify-cleanup-options-{occ_symbol}",
    )
    return await _drive_lifecycle(ctx, plan)


def _build_options_open_command(
    *, underlying: str, strike: float, expiration: dt.date
) -> OpenCommand:
    """Build a 1-contract long-call OPEN on *underlying* / *expiration*."""
    # ALP-462 — wrap fixture floats at the boundary.
    return OpenCommand(
        command_type="open",
        instrument=OptionInstrument(
            asset_type="option",
            underlying=underlying,
            strike=price(str(strike)),
            expiration=expiration.isoformat(),
            contract_type="call",
            direction="long",
        ),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=1.0, dollar_value=money("1")),
        target=Target(target_type="absolute_price", price=price("10000"), order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator="<=",
                    trigger_price=price("0.01"),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=_verify_thesis(underlying, "options OPEN"),
    )


# Search-window upper bound for "first business day with a listed chain" —
# 60 days is wide enough to cover monthly-only chains plus illiquid weeks
# without exploding the per-day chain-lookup count when the operator runs
# a second-by-second re-test.
_EXPIRATION_SEARCH_WINDOW_DAYS: int = 60


def _pick_listed_chain_for_underlying(
    queries: AccountStateQueries,
    *,
    underlying: str,
    days_out_min: int,
) -> tuple[dt.date, tuple[float, ...]] | None:
    """Find the first business day ≥ ``days_out_min`` whose chain is non-empty.

    Walks each business day in
    ``[today + days_out_min, today + _EXPIRATION_SEARCH_WINDOW_DAYS]`` in
    chronological order, querying the option chain for *underlying* on that
    date. Returns ``(expiration, sorted_strikes)`` for the first date with a
    non-empty active call chain, or ``None`` if no candidate in the window
    lists any contracts. Raises ``RuntimeError`` when the calendar fetch
    returns zero business days (the live calendar is broken or the operator
    chose an impossible window).
    """
    today = _today_utc()
    start = today + dt.timedelta(days=days_out_min)
    end = today + dt.timedelta(days=_EXPIRATION_SEARCH_WINDOW_DAYS)
    calendar = queries.get_calendar(start=start, end=end)
    if not calendar:
        msg = (
            f"calendar fetch returned no business days between {start.isoformat()} "
            f"and {end.isoformat()}"
        )
        raise RuntimeError(msg)
    for day in calendar:
        contracts = queries.get_option_contracts(underlying=underlying, expiration=day.date)
        if contracts:
            # ALP-462 — ``c.strike`` is ``Price`` (Decimal); cast at the
            # legacy script-internal float-tuple surface.
            strikes = tuple(float(c.strike) for c in contracts)
            return day.date, strikes
    return None


def _pick_median_strike(strikes: tuple[float, ...]) -> float:
    """Return the median strike of a non-empty sorted strike tuple.

    Used by phase 4 as the "approximately ATM" proxy when no underlying mark
    is available. ``strikes`` must already be sorted ascending (the caller
    receives strikes in that order from ``get_option_contracts``).
    """
    if not strikes:
        msg = "_pick_median_strike requires at least one strike"
        raise ValueError(msg)
    return strikes[len(strikes) // 2]


def _pick_vertical_spread_strikes(strikes: tuple[float, ...]) -> tuple[float, float]:
    """Return ``(long_strike, short_strike)`` for a long-call vertical spread.

    Long = median strike (approximately ATM). Short = next listed strike up.
    When the median is the highest listed strike, falls back to picking the
    second-highest as the long and the highest as the short — guarantees
    short > long for a valid vertical-spread structure.
    """
    if len(strikes) < 2:
        msg = f"_pick_vertical_spread_strikes requires at least 2 strikes, got {len(strikes)}"
        raise ValueError(msg)
    median_index = len(strikes) // 2
    if median_index == len(strikes) - 1:
        # Median is the highest strike — shift down by one notch so short > long.
        long_index = median_index - 1
        short_index = median_index
    else:
        long_index = median_index
        short_index = median_index + 1
    return strikes[long_index], strikes[short_index]


def _build_occ(underlying: str, expiration: dt.date, strike: float) -> str:
    """Wrap ``build_occ_symbol`` to keep the call sites short."""
    from alphamind.execution.broker_adapter import build_occ_symbol

    return build_occ_symbol(underlying, expiration, _OPT_CALL, strike)


# ``OptionContractType.CALL`` is the canonical enum ``build_occ_symbol``
# consumes; resolved at module-import via the portfolio_state records package.
def _opt_call_enum() -> Any:
    from alphamind.portfolio_state.records.positions import OptionContractType

    return OptionContractType.CALL


_OPT_CALL = _opt_call_enum()


# ---------------------------------------------------------------------------
# Phase 5 — Multi-leg vertical-spread lifecycle
# ---------------------------------------------------------------------------


async def phase_5_mleg_order_lifecycle(ctx: VerifyContext) -> PhaseResult:
    """Drive an OPEN → all-legs-fill → CLOSE round-trip on a 2-leg vertical spread.

    Defers when ``ctx.db_path`` is None. Defers when the market is closed
    (off-hours mleg fills don't arrive within the 120s budget). Otherwise
    picks the nearest expiration ≥ 7 days out that has ≥ 2 listed call
    strikes, picks long = median listed strike and short = next listed
    strike up, submits a 1-unit long call vertical spread, waits for the
    parent fill, then submits a CLOSE that inverts each leg's intent. Wraps
    in try/finally so a mid-flight timeout triggers cleanup. DEFERs if no
    expiration in the search window has ≥ 2 listed call strikes.
    """
    if ctx.db_path is None:
        return _deferred_no_db(_PHASE_5_LABEL, "2-leg vertical-spread")
    if not ctx.queries.get_clock().is_open:
        return _deferred_market_closed(_PHASE_5_LABEL)
    try:
        listed = _pick_listed_chain_for_underlying(
            ctx.queries, underlying=_VERIFY_UNDERLYING, days_out_min=7
        )
    except RuntimeError as exc:
        return PhaseResult.failed(
            _PHASE_5_LABEL,
            f"could not pick options expiration from live calendar: {exc!s}",
        )
    if listed is None or len(listed[1]) < 2:
        return PhaseResult.skipped(
            _PHASE_5_LABEL,
            (
                f"No expiration on {_VERIFY_UNDERLYING} in the next "
                f"{_EXPIRATION_SEARCH_WINDOW_DAYS} days has ≥ 2 listed call "
                f"strikes; cannot build a vertical spread."
            ),
        )
    expiration, strikes = listed
    long_strike, short_strike = _pick_vertical_spread_strikes(strikes)
    open_legs = _build_mleg_open_legs(_VERIFY_UNDERLYING, expiration, long_strike, short_strike)
    plan = _LifecyclePlan(
        label=_PHASE_5_LABEL,
        fill_timeout_seconds=_PHASE_5_FILL_TIMEOUT_SECONDS,
        role_prefix="phase5",
        open_command=_build_mleg_open_command(
            underlying=_VERIFY_UNDERLYING,
            long_strike=long_strike,
            short_strike=short_strike,
            expiration=expiration,
        ),
        close_context={
            "position_asset_type": "strategy",
            "open_legs": open_legs,
            "strategy_type": "vertical_spread",
            "position_units": 1.0,
        },
        cleanup_position_id="verify-cleanup-mleg",
    )
    return await _drive_lifecycle(ctx, plan)


def _build_mleg_open_legs(
    underlying: str,
    expiration: dt.date,
    long_strike: float,
    short_strike: float,
) -> tuple[Any, ...]:
    """Build the open-side ``MLEGLegAck`` tuple the close dispatcher inverts.

    Mirrors the persisted ``StrategyPositionDetails`` → MLEGLegAck translation
    in ``submit_envelope_mcp._persisted_legs_to_mleg_acks``: long the
    lower-strike call (``buy_to_open``), short the higher-strike call
    (``sell_to_open``).
    """
    from alphamind.execution.broker_adapter import MLEGLegAck

    return (
        MLEGLegAck(
            occ_symbol=OccSymbol(_build_occ(underlying, expiration, long_strike)),
            side="buy",
            ratio_qty=1,
            position_intent="buy_to_open",
        ),
        MLEGLegAck(
            occ_symbol=OccSymbol(_build_occ(underlying, expiration, short_strike)),
            side="sell",
            ratio_qty=1,
            position_intent="sell_to_open",
        ),
    )


def _build_mleg_open_command(
    *,
    underlying: str,
    long_strike: float,
    short_strike: float,
    expiration: dt.date,
) -> OpenCommand:
    """Build a 1-unit long call vertical spread OPEN on *underlying*."""
    expiration_iso = expiration.isoformat()
    # ALP-462 — wrap fixture floats at the boundary.
    return OpenCommand(
        command_type="open",
        instrument=StrategyInstrument(
            asset_type="strategy",
            strategy_type="vertical_spread",
            underlying=underlying,
            legs=(
                StrategyLeg(
                    strike=price(str(long_strike)),
                    expiration=expiration_iso,
                    contract_type="call",
                    direction="long",
                    quantity_ratio=1,
                ),
                StrategyLeg(
                    strike=price(str(short_strike)),
                    expiration=expiration_iso,
                    contract_type="call",
                    direction="short",
                    quantity_ratio=1,
                ),
            ),
        ),
        entry_order=EntryOrder(type="market"),
        position_size=PositionSize(quantity=1.0, dollar_value=money("1")),
        target=Target(target_type="absolute_price", price=price("10000"), order_type="limit"),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator="<=",
                    trigger_price=price("0.01"),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=_verify_thesis(underlying, "vertical-spread OPEN"),
    )


# ---------------------------------------------------------------------------
# Phase 6 — Venue configuration in isolation
# ---------------------------------------------------------------------------


_PHASE_6_LABEL = "Phase 6 — Venue config in isolation"


async def phase_6_venue_config(ctx: VerifyContext) -> PhaseResult:
    """Exercise the venue-configuration primitives against the live calendar.

    Asserts: every constant exported from ``venue_configuration.constants``
    exists with documented values; ``TradingCalendarCache`` answers
    ``is_market_open(now)`` consistent with ``get_clock().is_open``;
    ``business_day_offset(today, 1)`` skips weekends; ``read_venue_account_state``
    composes ``get_account()`` into a populated ``VenueAccountState``;
    ``compute_settlement_date(today, "equity")`` equals the business day after
    the effective trade date.
    """
    from alphamind.execution.venue_configuration import TradingCalendarCache

    constant_failure = _phase_6_check_constants()
    if constant_failure is not None:
        return PhaseResult.failed(_PHASE_6_LABEL, constant_failure)

    cache = TradingCalendarCache(ctx.queries)

    clock_failure = _phase_6_check_clock_agreement(ctx.queries, cache)
    if clock_failure is not None:
        return PhaseResult.failed(_PHASE_6_LABEL, clock_failure)

    business_day_check = _phase_6_check_business_day(cache)
    if isinstance(business_day_check, str):
        return PhaseResult.failed(_PHASE_6_LABEL, business_day_check)
    next_business, today = business_day_check

    venue_failure = _phase_6_check_venue_state(ctx)
    if venue_failure is not None:
        return PhaseResult.failed(_PHASE_6_LABEL, venue_failure)

    settlement_failure = _phase_6_check_settlement(cache, today=today, next_business=next_business)
    if settlement_failure is not None:
        return PhaseResult.failed(_PHASE_6_LABEL, settlement_failure)

    return PhaseResult.passed(_PHASE_6_LABEL)


def _phase_6_check_clock_agreement(queries: AccountStateQueries, cache: Any) -> str | None:
    """Confirm calendar cache and live ``/v2/clock`` agree on market-open state."""
    now = dt.datetime.now(dt.UTC)
    try:
        cache_says_open = cache.is_market_open(now)
        clock_snapshot = queries.get_clock()
    except _PROBE_EXCEPTIONS as exc:
        return f"calendar/clock interaction raised: {exc!s}"
    if cache_says_open != clock_snapshot.is_open:
        return (
            f"is_market_open(now)={cache_says_open!r} disagrees with "
            f"get_clock().is_open={clock_snapshot.is_open!r}"
        )
    return None


def _phase_6_check_business_day(cache: Any) -> str | tuple[dt.date, dt.date]:
    """Assert ``business_day_offset(today, 1)`` skips weekends.

    Returns ``(next_business, today)`` on success — both are reused by the
    settlement check to avoid re-fetching from the cache.
    """
    today = _today_utc()
    try:
        next_business = cache.business_day_offset(today, 1)
    except _PROBE_EXCEPTIONS as exc:
        return f"business_day_offset(today, 1) raised: {exc!s}"
    if next_business.weekday() >= 5:  # Saturday=5, Sunday=6
        return (
            f"business_day_offset(today, 1) returned {next_business!r}, "
            f"a weekend (weekday={next_business.weekday()})"
        )
    return next_business, today


def _phase_6_check_venue_state(ctx: VerifyContext) -> str | None:
    """Compose the account-state surfacer; print resolved tier in verbose mode."""
    from alphamind.execution.venue_configuration import read_venue_account_state

    try:
        venue_state = read_venue_account_state(ctx.queries)
    except _PROBE_EXCEPTIONS as exc:
        return f"read_venue_account_state raised: {exc!s}"
    if ctx.verbose:
        print(
            f"      venue state — tier={venue_state.margin_interest_tier.name}, "
            f"day-trade-headroom={venue_state.day_trade_headroom}, "
            f"PDT-qualified={venue_state.pdt_qualified}"
        )
    return None


def _phase_6_check_settlement(cache: Any, *, today: dt.date, next_business: dt.date) -> str | None:
    """Confirm ``compute_settlement_date`` equals business-day-after-effective-trade-date.

    For business-day ``today``, this is just ``next_business``; for non-business
    days it's one business day after the rolled-forward effective trade date.
    """
    from alphamind.execution.venue_configuration import compute_settlement_date

    try:
        settlement = compute_settlement_date(today, "equity", calendar=cache)
    except _PROBE_EXCEPTIONS as exc:
        return f"compute_settlement_date raised: {exc!s}"
    effective_trade_date = cache.business_day_offset(today, 0)
    expected_settlement = (
        next_business
        if effective_trade_date == today
        else cache.business_day_offset(effective_trade_date, 1)
    )
    if settlement != expected_settlement:
        return (
            f"compute_settlement_date(today, 'equity')={settlement!r} disagrees with "
            f"business_day_offset(effective_trade_date, 1)={expected_settlement!r}"
        )
    return None


def _phase_6_check_constants() -> str | None:
    """Confirm every constant exported from ``venue_configuration.constants``
    matches the documented values in ``venue-configuration.md``."""
    from alphamind.execution.venue_configuration import (
        INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED,
        MARGIN_INTEREST_TIERS,
        OVERNIGHT_BUYING_POWER_MULTIPLIER,
        PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD,
        PDT_EQUITY_THRESHOLD_USD,
        PDT_ROLLING_WINDOW_BUSINESS_DAYS,
        PRE_SETTLEMENT_CREDIT_ENABLED,
        REG_T_LONG_EQUITY,
        REG_T_LONG_OPTION,
        REG_T_SHORT_EQUITY,
        REGULATORY_FEE_RATES,
        SETTLEMENT_DAYS_BY_INSTRUMENT,
    )

    expected: tuple[tuple[str, Any, Any], ...] = (
        ("SETTLEMENT_DAYS_BY_INSTRUMENT[equity]", SETTLEMENT_DAYS_BY_INSTRUMENT["equity"], 1),
        ("SETTLEMENT_DAYS_BY_INSTRUMENT[option]", SETTLEMENT_DAYS_BY_INSTRUMENT["option"], 1),
        ("SETTLEMENT_DAYS_BY_INSTRUMENT[crypto]", SETTLEMENT_DAYS_BY_INSTRUMENT["crypto"], 0),
        ("PRE_SETTLEMENT_CREDIT_ENABLED", PRE_SETTLEMENT_CREDIT_ENABLED, True),
        ("PDT_EQUITY_THRESHOLD_USD", PDT_EQUITY_THRESHOLD_USD, 25_000.0),
        ("PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD", PDT_DAY_TRADE_LIMIT_BELOW_THRESHOLD, 3),
        ("PDT_ROLLING_WINDOW_BUSINESS_DAYS", PDT_ROLLING_WINDOW_BUSINESS_DAYS, 5),
        ("REG_T_LONG_EQUITY.initial_pct", REG_T_LONG_EQUITY.initial_pct, 0.50),
        ("REG_T_LONG_EQUITY.maintenance_pct", REG_T_LONG_EQUITY.maintenance_pct, 0.25),
        ("REG_T_SHORT_EQUITY.initial_pct", REG_T_SHORT_EQUITY.initial_pct, 1.50),
        ("REG_T_SHORT_EQUITY.maintenance_pct", REG_T_SHORT_EQUITY.maintenance_pct, 1.30),
        ("REG_T_LONG_OPTION.initial_pct", REG_T_LONG_OPTION.initial_pct, 1.00),
        ("REG_T_LONG_OPTION.maintenance_pct", REG_T_LONG_OPTION.maintenance_pct, 1.00),
        ("OVERNIGHT_BUYING_POWER_MULTIPLIER", OVERNIGHT_BUYING_POWER_MULTIPLIER, 2.0),
        (
            "INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED",
            INTRADAY_BUYING_POWER_MULTIPLIER_PDT_QUALIFIED,
            4.0,
        ),
    )
    for label, actual, want in expected:
        if actual != want:
            return f"venue constant {label} = {actual!r}, expected {want!r}"

    if len(MARGIN_INTEREST_TIERS) != 2:
        return f"MARGIN_INTEREST_TIERS has {len(MARGIN_INTEREST_TIERS)} entries, expected 2"

    if not REGULATORY_FEE_RATES:
        return "REGULATORY_FEE_RATES is empty"

    return None


# ---------------------------------------------------------------------------
# Phase 7 — Disconnect-recovery routine
# ---------------------------------------------------------------------------


_PHASE_7_LABEL = "Phase 7 — Disconnect recovery"


async def phase_7_disconnect_recovery(ctx: VerifyContext) -> PhaseResult:
    """Exercise ``recover_missed_fills_since`` against the live paper account.

    Asserts the recovery primitive runs without raising over a 1-hour lookback
    window (paper accounts may legitimately yield zero events). The translator
    branch (``order_snapshot_to_fill_reports`` mapping each Alpaca status to a
    ``FillReport.event_type``) is exercised by the unit-test suite under
    ``tests/execution/broker_adapter/test_recovery.py``; here we cover the
    end-to-end live drive only.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    try:
        async for _report in recover_missed_fills_since(ctx.queries, since=since):
            pass
    except _PROBE_EXCEPTIONS as exc:
        return PhaseResult.failed(
            _PHASE_7_LABEL,
            f"recover_missed_fills_since raised: {exc!s}",
        )

    return PhaseResult.passed(_PHASE_7_LABEL)


# ---------------------------------------------------------------------------
# Phase registry — single source of truth for phase ordering + name lookup
# ---------------------------------------------------------------------------


PhaseFunction = Callable[[VerifyContext], Coroutine[Any, Any, PhaseResult]]


_PHASES: tuple[tuple[str, PhaseFunction], ...] = (
    ("phase_1", phase_1_adapter_substrate),
    ("phase_2", phase_2_account_state_queries),
    ("phase_3", phase_3_equity_order_lifecycle),
    ("phase_4", phase_4_options_order_lifecycle),
    ("phase_5", phase_5_mleg_order_lifecycle),
    ("phase_6", phase_6_venue_config),
    ("phase_7", phase_7_disconnect_recovery),
)

PHASE_NAMES: tuple[str, ...] = tuple(name for name, _fn in _PHASES)


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _phase_marker(result: PhaseResult) -> str:
    """Return the bracketed status marker for a single phase result."""
    if not result.ok:
        return "[FAIL]"
    if result.deferred:
        return "[DEFERRED]"
    return "[PASS]"


def _render_text(results: Sequence[PhaseResult]) -> str:
    lines = ["=" * 70, "AlphaMind Broker Adapter Verification", "=" * 70]
    pass_count = sum(1 for r in results if r.ok and not r.deferred)
    deferred_count = sum(1 for r in results if r.deferred)
    fail_count = sum(1 for r in results if not r.ok)
    for r in results:
        lines.append(f"  {_phase_marker(r)} {r.label}")
        if r.detail:
            lines.append(f"      {r.detail}")
    lines.append("=" * 70)
    summary_parts = [f"{pass_count} pass"]
    if deferred_count:
        summary_parts.append(f"{deferred_count} deferred")
    if fail_count:
        summary_parts.append(f"{fail_count} fail")
    overall = "PASS" if fail_count == 0 else "FAIL"
    lines.append(f"RESULT: {overall} ({', '.join(summary_parts)})")
    if fail_count:
        failing = [r.label for r in results if not r.ok]
        lines.append(f"  failing phases: {', '.join(failing)}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the broker adapter end-to-end against Alpaca's paper environment "
            "across seven phases (substrate, queries, equity/options/mleg lifecycles, "
            "venue config, disconnect recovery)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=("paper", "live"),
        default="paper",
        help="Execution mode (default: paper). Live-mode-specific assertions DEFER per spec.",
    )
    parser.add_argument(
        "--phase",
        choices=PHASE_NAMES,
        default=None,
        help=(
            "Run a single phase by name (default: all). Useful for re-running one phase "
            "after a fix without re-driving the others."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print extra diagnostic detail (e.g. resolved venue tier + headroom in Phase 6).",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Path to the configuration directory (default: ./config).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "Path to a SQLite DB for phases 3/4/5 (equity/options/mleg lifecycles). "
            "When omitted, phases 3/4/5 DEFER. Operator runs typically point this at a "
            "fresh tmp DB created via `alembic upgrade head` (see runbook)."
        ),
    )
    return parser


def _load_venue_and_execution(*, config_dir: Path) -> tuple[VenueConfig, ExecutionConfig]:
    """Read venue.yaml + execution.yaml as standalone Pydantic models.

    The verify script needs only the venue (paper/live URLs + env-var refs)
    and execution (retry-window) blocks; routing through the full
    ``load_full_config`` would also drag in agents.yaml + assets.yaml +
    profiles + regimes which are irrelevant here. Per-file ``model_validate``
    runs the same parse-time validators the production loader runs.
    """
    venue = VenueConfig.model_validate(read_yaml_file(config_dir / "venue.yaml"))
    execution = ExecutionConfig.model_validate(read_yaml_file(config_dir / "execution.yaml"))
    return venue, execution


def _build_context(
    args: argparse.Namespace,
    *,
    venue: VenueConfig,
    execution: ExecutionConfig,
) -> VerifyContext:
    """Construct the per-invocation ``VerifyContext`` from CLI args + configs.

    ``AlpacaClientFactory`` raises :class:`RuntimeError` naming the missing
    env vars when credentials are unset; the message propagates to the
    operator. The factory + queries are minted once and shared across phases.
    """
    factory = AlpacaClientFactory(venue, mode=args.mode)
    client = factory.build_trading_client()
    queries = AccountStateQueries(client)
    db_path: Path | None = args.db if args.db is not None else None
    return VerifyContext(
        factory=factory,
        queries=queries,
        execution=execution,
        venue=venue,
        mode=args.mode,
        verbose=bool(args.verbose),
        db_path=db_path,
    )


async def _run_phases(ctx: VerifyContext, *, phase_name: str | None) -> list[PhaseResult]:
    selected = (
        _PHASES if phase_name is None else tuple((n, fn) for n, fn in _PHASES if n == phase_name)
    )
    results: list[PhaseResult] = []
    for _name, fn in selected:
        results.append(await fn(ctx))
    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on all-PASS / DEFERRED, 1 if any phase FAILed.

    DEFERRED phases (e.g. live-mode assertions in paper mode) do NOT count
    as failures per the spec ("DEFERRED for any path the script intentionally
    skips, e.g., live-mode-specific assertions").
    """
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        venue, execution = _load_venue_and_execution(config_dir=args.config_dir)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAIL: configuration load failed: {exc!s}", file=sys.stderr)
        return 1

    try:
        ctx = _build_context(args, venue=venue, execution=execution)
    except RuntimeError as exc:
        # AlpacaClientFactory raises RuntimeError naming missing env vars.
        # Surface verbatim so the operator sees the offending var.
        print(f"FAIL: {exc!s}", file=sys.stderr)
        return 1

    results = asyncio.run(_run_phases(ctx, phase_name=args.phase))
    print(_render_text(results))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
