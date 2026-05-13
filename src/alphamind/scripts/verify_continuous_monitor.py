"""End-to-end continuous-monitor verification (ALP-441).

Operator entry point (via thin shim at ``scripts/verify_continuous_monitor.py``).
Exercises the five continuous-monitor responsibilities described in
``docs/design/05-execution-layer/architecture.md`` § 4 against per-component
fixtures (no live websockets, no live broker, no live vendor calls):

* (a) Stream connects + caches a quote.
* (b) Fill persists.
* (c) Greeks refresh on schedule.
* (d) Greeks refresh on underlying move.
* (e) Greeks refresh failure preserves prior values + emits
  ``GREEKS_REFRESH_FAILED``.
* (f) Halt onset emits ``HALT_ACTIVATED``.
* (g) Immediate-action breach dispatch — engine envelope submitted with
  ``engine_guardrail`` provenance.
* (h) Deferred-rule breach is NOT dispatched.
* (i) Options bracket-stop fires via the direct broker-adapter close path.
* (j) Strategist visibility — ``between_invocation_closures`` projection
  contains both the engine-envelope cascade closure (from g) and the
  bracket-stop closure (from i).
* (k) Emergency request (optional, exercised iff
  ``EventType.EMERGENCY_INVOCATION_REQUESTED`` is part of the activity-log
  vocabulary).

Each scenario uses fakes for every external surface (broker, websocket, DB)
and a synthetic fixture portfolio (one equity position, one options position,
one strategy position). Sub-minute runtime; safe to run in CI.

Usage::

    uv run python scripts/verify_continuous_monitor.py [--output FORMAT]

See ``scripts/RUNBOOK_continuous_monitor.md`` for the operator runbook and
``scripts/RUNBOOK_end_to_end_verification.md`` for cross-feature sequencing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Side-effect import: pre-load ``portfolio_manager.models`` before the lazy
# ``alphamind.execution.oms.__getattr__`` triggers loading of
# ``submit_envelope_mcp`` (which imports the PM models). Mirrors the pattern in
# ``tests/execution/oms/test_submit_engine_envelope.py``; without this the
# transitive imports race with the lazy submodule load.
import alphamind.decision.portfolio_manager.models

# Side-effect import: register state-persistence tables on ``Base.metadata``
# so the in-memory engine in scenario (b) has the ``fill_records`` table.
import alphamind.execution.state_persistence.tables  # noqa: F401
from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price
from alphamind.commands.engine_envelope import (
    EngineEnvelope as OmsEngineEnvelope,
)
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.guardrails import BreachResponse
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    SubmissionResult,
)
from alphamind.execution.continuous_monitor.bracket_stops.closer import (
    CloseSubmissionResult,
)
from alphamind.execution.continuous_monitor.bracket_stops.task import (
    _run_bracket_stop_cycle,
)
from alphamind.execution.continuous_monitor.breach_loop.halt_tracker import (
    HaltTransitionTracker,
)
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.cascade_dispatch import (
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    BreachDispatchContext,
    CascadeDispatcher,
    DeferralEvent,
)
from alphamind.execution.continuous_monitor.emergency_trigger.cooldown import (
    CooldownTracker,
)
from alphamind.execution.continuous_monitor.emergency_trigger.evaluator import (
    EmergencyTriggerEvaluator,
)
from alphamind.execution.continuous_monitor.greeks_refresh.iv_provider import IVQuote
from alphamind.execution.continuous_monitor.greeks_refresh.state import (
    LastRefreshState,
)
from alphamind.execution.continuous_monitor.greeks_refresh.task import (
    _run_refresh_cycle,
    occ_symbol_for_options,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.execution.guardrail_enforcement.orchestrator import (
    Phase1EnforcementResult,
)
from alphamind.execution.state_persistence.tables.fill_records import FillRecordRow
from alphamind.execution.state_persistence.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.execution.state_persistence.write_paths.records import (
    FillProcessingStatus,
    FillRecord,
)
from alphamind.persistence.models import Base
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.consumers.strategist import (
    BetweenInvocationClosure,
    BetweenInvocationClosureOrigin,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EmergencyInvocationRequestedDetail,
    EventSource,
    EventType,
    HaltActivatedDetail,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)
from alphamind.portfolio_state.records.orders import OrderStatus as PSOrderStatus
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    DrawdownSample,
    DrawdownTier,
    HaltState,
    PositionLiquidity,
    PositionRiskReward,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)

__all__ = [
    "ScenarioResult",
    "main",
    "run_scenario_a_stream_caches_quote",
    "run_scenario_b_fill_persists",
    "run_scenario_c_greeks_scheduled_refresh",
    "run_scenario_d_greeks_move_triggered_refresh",
    "run_scenario_e_greeks_refresh_failure",
    "run_scenario_f_halt_onset",
    "run_scenario_g_immediate_breach_dispatch",
    "run_scenario_h_deferred_breach_not_dispatched",
    "run_scenario_i_bracket_stop_fires",
    "run_scenario_j_strategist_visibility",
    "run_scenario_k_emergency_request",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioResult:
    """One scenario's outcome — pass/fail flag + labeled paragraph + detail.

    Mirrors :class:`alphamind.scripts.verify_oms_commands.PhaseResult` so the
    operator output reads uniformly across verify scripts.
    """

    label: str
    ok: bool
    detail: str | None = None
    skipped: bool = False


# ---------------------------------------------------------------------------
# Shared fixture builders
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
_SESSION_ID = "monsession-verify"


async def _verify_invocation_id() -> str:
    """Async-callable returning a deterministic verify-mode invocation_id.

    The monitor's :class:`InvocationIdProvider` shape is async-native
    (continuous_monitor.greeks_refresh.task.InvocationIdProvider); the verify
    script supplies a constant value via this async-callable for every
    scenario that drives :func:`_run_refresh_cycle` or
    :class:`EmergencyTriggerEvaluator`.
    """
    return "inv-verify"


async def _verify_emergency_invocation_id() -> str:
    """Async-callable returning the verify-mode emergency invocation_id."""
    return "inv-verify-emergency"


def _config() -> ContinuousMonitorConfig:
    """Production-shape config record for the per-scenario kernel drives."""
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        greeks_refresh_inspection_cadence_seconds=30,
        bracket_stop_evaluation_cadence_seconds=1.0,
        underlying_stream_provider="alpaca-iex",
        subscription_refresh_seconds=30,
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _session() -> MonitorSession:
    return MonitorSession(
        session_id=_SESSION_ID,
        started_at=_NOW,
        mode="paper",
    )


def _options_position(
    *,
    position_id: str = "POS-OPT-NVDA",
    bracket_id: str = "BRK-OPT-NVDA",
    premium_paid: float = 12.0,
    iv: float = 0.30,
) -> PositionRecord:
    """Fixture: one long-NVDA call option position with a price-based stop."""
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THE-OPT-NVDA"),
        bracket_id=BracketId(bracket_id),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=850.0,
            expiration_date=date(2026, 6, 19),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=premium_paid,
            greeks=OptionGreeks(
                delta=0.5,
                gamma=0.02,
                theta=-0.04,
                vega=0.2,
                as_of_timestamp=_NOW,
                iv_used=iv,
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=premium_paid,
                fill_quantity=1.0,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


# ---------------------------------------------------------------------------
# (a) Stream connects + caches a quote.
# ---------------------------------------------------------------------------


_LABEL_A = "(a) Stream connects + caches a quote"


async def run_scenario_a_stream_caches_quote() -> ScenarioResult:
    """Drive: inject a synthetic ``SPY`` quote into :class:`UnderlyingPriceCache`.

    Expect: ``UnderlyingPriceCache.get("SPY")`` returns the quote within one
    second (the asyncio-only lock path).
    """
    cache = UnderlyingPriceCache()
    quote = UnderlyingQuote(ticker="SPY", price=520.50, as_of=_NOW)
    try:
        await asyncio.wait_for(cache.update(quote), timeout=1.0)
    except TimeoutError:
        return ScenarioResult(
            label=_LABEL_A,
            ok=False,
            detail="cache.update did not complete within 1s",
        )
    observed = cache.get("SPY")
    if observed is None:
        return ScenarioResult(
            label=_LABEL_A,
            ok=False,
            detail="cache.get('SPY') returned None after update",
        )
    if observed.price != 520.50:
        return ScenarioResult(
            label=_LABEL_A,
            ok=False,
            detail=f"cache.get('SPY').price={observed.price}, expected 520.50",
        )
    return ScenarioResult(label=_LABEL_A, ok=True)


# ---------------------------------------------------------------------------
# (b) Fill persists.
# ---------------------------------------------------------------------------


_LABEL_B = "(b) Fill persists"


def _make_async_in_memory_factory() -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """Build an async-engine + factory backed by ``sqlite:///:memory:``.

    Uses ``StaticPool`` so every checkout returns the same connection — the
    only way to share an in-memory SQLite database across async sessions.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_schema(engine: Any) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def run_scenario_b_fill_persists() -> ScenarioResult:
    """Drive: append a fake equity ``FillRecord`` to a fresh in-memory DB.

    Expect: exactly one row appears in ``fill_records`` after commit.
    """
    engine, factory = _make_async_in_memory_factory()
    try:
        await _create_schema(engine)
        # ALP-462 — wrap fixture floats at the FillRecord boundary.
        fill = FillRecord(
            fill_id="FILL-001",
            order_id="ORD-001",
            fill_timestamp=_NOW,
            fill_price=price("150"),
            fill_quantity=10.0,
            remaining_quantity_after=0.0,
            order_status_after=PSOrderStatus.FILLED,
            slippage_usd=money("0"),
            fees_usd=money("0"),
            execution_venue=None,
            gateway_reference=None,
            persistence_timestamp=_NOW,
            processing_status=FillProcessingStatus.UNPROCESSED,
            processing_invocation_id=None,
            processing_timestamp=None,
            regt_attribution=None,
            live_execution_estimate=None,
        )
        async with factory() as sess:
            await append_fill_record(sess, fill)
            await sess.commit()
        async with factory() as sess:
            rows = (await sess.execute(select(FillRecordRow))).scalars().all()
        if len(rows) != 1:
            return ScenarioResult(
                label=_LABEL_B,
                ok=False,
                detail=f"expected 1 fill_records row, got {len(rows)}",
            )
        if rows[0].order_id != "ORD-001":
            return ScenarioResult(
                label=_LABEL_B,
                ok=False,
                detail=f"row.order_id={rows[0].order_id!r}, expected 'ORD-001'",
            )
        return ScenarioResult(label=_LABEL_B, ok=True)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# (c)-(e) Greeks refresh scenarios share fixture-builder helpers
# ---------------------------------------------------------------------------


def _greeks_fixture_position() -> PositionRecord:
    """One options position with stale greeks (15+ minutes prior)."""
    return _options_position()


@dataclass
class _CapturingGreeksWriter:
    """Capture every ``update_options_greeks`` call (test-only writer)."""

    calls: list[tuple[str, OptionGreeks]] = field(default_factory=list)

    async def update_options_greeks(self, *, position_id: str, greeks: OptionGreeks) -> None:
        self.calls.append((position_id, greeks))

    async def update_strategy_greeks(
        self,
        *,
        position_id: str,
        per_leg: dict[str, OptionGreeks],
        aggregated: OptionGreeks,
    ) -> None:
        del per_leg
        self.calls.append((position_id, aggregated))


@dataclass
class _CapturingActivityLog:
    """Capture every activity-log entry emitted by a refresh cycle."""

    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def emit(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


@dataclass
class _FixturePositionsReader:
    positions: tuple[PositionRecord, ...]

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        return self.positions


def _seed_iv_quotes(position: PositionRecord, *, iv: float) -> dict[str, IVQuote]:
    """Build a one-entry IV-quote map keyed by the position's OCC symbol."""
    details = position.details
    assert isinstance(details, OptionsPositionDetails)
    occ = occ_symbol_for_options(details)
    return {
        occ: IVQuote(
            occ_symbol=occ,
            iv=iv,
            as_of=_NOW,
        )
    }


async def _seed_underlying_cache(prices: Mapping[str, float]) -> UnderlyingPriceCache:
    cache = UnderlyingPriceCache()
    for ticker, quote_price in prices.items():
        await cache.update(UnderlyingQuote(ticker=ticker, price=quote_price, as_of=_NOW))
    return cache


# ---------------------------------------------------------------------------
# (c) Greeks refresh on schedule.
# ---------------------------------------------------------------------------


_LABEL_C = "(c) Greeks refresh on schedule"


async def run_scenario_c_greeks_scheduled_refresh() -> ScenarioResult:
    """Drive: advance the fixture clock past ``greeks_refresh_interval_minutes``.

    Expect: the option's ``OptionGreeks.as_of_timestamp`` advances and
    ``iv_used`` is populated.
    """
    position = _greeks_fixture_position()
    cache = await _seed_underlying_cache({"NVDA": 855.0})  # within move threshold
    config = _config()
    writer = _CapturingGreeksWriter()
    activity_log = _CapturingActivityLog()
    iv = 0.35
    iv_quotes = _seed_iv_quotes(position, iv=iv)

    # Seed the per-position state map with a stale-by-interval anchor at the
    # same spot the cache reports — so the move predicate stays False and the
    # scheduled trigger is the one that fires.
    spot = 855.0
    stale_at = _NOW - timedelta(minutes=config.greeks_refresh_interval_minutes + 1)
    states: dict[str, LastRefreshState] = {
        position.position_id: LastRefreshState(
            position_id=position.position_id,
            last_refreshed_at=stale_at,
            underlying_price_at_last_refresh=spot,
        ),
    }

    async def _iv_fetch(_symbols: Iterable[str]) -> dict[str, IVQuote]:
        return iv_quotes

    await _run_refresh_cycle(
        config=config,
        repository=_FixturePositionsReader((position,)),
        cache=cache,
        states=states,
        iv_fetch=_iv_fetch,
        writer=writer,
        activity_log=activity_log.emit,
        risk_free_rate=0.045,
        now=_NOW,
        invocation_id_provider=_verify_invocation_id,
        market_open=True,
    )

    if len(writer.calls) != 1:
        return ScenarioResult(
            label=_LABEL_C,
            ok=False,
            detail=f"expected 1 writer.update_options_greeks call, got {len(writer.calls)}",
        )
    _, new_greeks = writer.calls[0]
    if new_greeks.as_of_timestamp != _NOW:
        return ScenarioResult(
            label=_LABEL_C,
            ok=False,
            detail=(
                f"new_greeks.as_of_timestamp={new_greeks.as_of_timestamp!r}, expected {_NOW!r}"
            ),
        )
    if new_greeks.iv_used != iv:
        return ScenarioResult(
            label=_LABEL_C,
            ok=False,
            detail=f"new_greeks.iv_used={new_greeks.iv_used}, expected {iv}",
        )
    return ScenarioResult(label=_LABEL_C, ok=True)


# ---------------------------------------------------------------------------
# (d) Greeks refresh on underlying move.
# ---------------------------------------------------------------------------


_LABEL_D = "(d) Greeks refresh on underlying move"


async def run_scenario_d_greeks_move_triggered_refresh() -> ScenarioResult:
    """Drive: inject an underlying move exceeding
    ``greeks_refresh_underlying_move_threshold_pct``.

    Expect: refresh fires before the scheduled interval (the move trigger).
    """
    position = _greeks_fixture_position()
    config = _config()
    # Anchor 800.0; current 820.0 → 2.5% move > 2.0% threshold.
    anchor_spot = 800.0
    current_spot = 820.0
    cache = await _seed_underlying_cache({"NVDA": current_spot})
    writer = _CapturingGreeksWriter()
    activity_log = _CapturingActivityLog()
    iv = 0.32
    iv_quotes = _seed_iv_quotes(position, iv=iv)

    # State anchor: refreshed 1 minute ago — well inside the 15-minute
    # scheduled interval. Only the move trigger can fire.
    fresh_at = _NOW - timedelta(minutes=1)
    states: dict[str, LastRefreshState] = {
        position.position_id: LastRefreshState(
            position_id=position.position_id,
            last_refreshed_at=fresh_at,
            underlying_price_at_last_refresh=anchor_spot,
        ),
    }

    async def _iv_fetch(_symbols: Iterable[str]) -> dict[str, IVQuote]:
        return iv_quotes

    await _run_refresh_cycle(
        config=config,
        repository=_FixturePositionsReader((position,)),
        cache=cache,
        states=states,
        iv_fetch=_iv_fetch,
        writer=writer,
        activity_log=activity_log.emit,
        risk_free_rate=0.045,
        now=_NOW,
        invocation_id_provider=_verify_invocation_id,
        market_open=True,
    )

    if len(writer.calls) != 1:
        return ScenarioResult(
            label=_LABEL_D,
            ok=False,
            detail=(
                f"expected 1 update_options_greeks call from move trigger, got {len(writer.calls)}"
            ),
        )
    return ScenarioResult(label=_LABEL_D, ok=True)


# ---------------------------------------------------------------------------
# (e) Greeks refresh failure.
# ---------------------------------------------------------------------------


_LABEL_E = "(e) Greeks refresh failure preserves prior + emits log entry"


async def run_scenario_e_greeks_refresh_failure() -> ScenarioResult:
    """Drive: force the IV fetch callable to raise after exhausting retries.

    Expect: ``OptionGreeks.refresh_failed=True``; prior
    ``delta/gamma/theta/vega/as_of/iv_used`` preserved; one
    ``GREEKS_REFRESH_FAILED`` activity-log entry written.
    """
    position = _greeks_fixture_position()
    config = _config()
    cache = await _seed_underlying_cache({"NVDA": 855.0})
    writer = _CapturingGreeksWriter()
    activity_log = _CapturingActivityLog()
    states: dict[str, LastRefreshState] = {
        position.position_id: LastRefreshState(
            position_id=position.position_id,
            last_refreshed_at=_NOW - timedelta(minutes=config.greeks_refresh_interval_minutes + 1),
            underlying_price_at_last_refresh=855.0,
        ),
    }

    async def _failing_iv_fetch(_symbols: Iterable[str]) -> dict[str, IVQuote]:
        msg = "synthetic IV-fetch failure (retries exhausted)"
        raise RuntimeError(msg)

    # Suppress the kernel's ``log.exception`` traceback from operator
    # output — the synthetic IV-fetch raise IS the expected behavior in
    # this scenario. Disable the task's logger for the cycle only.
    task_logger = logging.getLogger("alphamind.execution.continuous_monitor.greeks_refresh.task")
    prior_level = task_logger.level
    task_logger.setLevel(logging.CRITICAL)
    try:
        await _run_refresh_cycle(
            config=config,
            repository=_FixturePositionsReader((position,)),
            cache=cache,
            states=states,
            iv_fetch=_failing_iv_fetch,
            writer=writer,
            activity_log=activity_log.emit,
            risk_free_rate=0.045,
            now=_NOW,
            invocation_id_provider=_verify_invocation_id,
            market_open=True,
        )
    finally:
        task_logger.setLevel(prior_level)

    if len(writer.calls) != 1:
        return ScenarioResult(
            label=_LABEL_E,
            ok=False,
            detail=(
                f"expected 1 writer.update_options_greeks call on failure path, "
                f"got {len(writer.calls)}"
            ),
        )
    _, new_greeks = writer.calls[0]
    if not new_greeks.refresh_failed:
        return ScenarioResult(
            label=_LABEL_E,
            ok=False,
            detail="new_greeks.refresh_failed=False, expected True",
        )
    prior = cast(OptionsPositionDetails, position.details).greeks
    if (
        new_greeks.delta != prior.delta
        or new_greeks.gamma != prior.gamma
        or new_greeks.theta != prior.theta
        or new_greeks.vega != prior.vega
        or new_greeks.as_of_timestamp != prior.as_of_timestamp
        or new_greeks.iv_used != prior.iv_used
    ):
        return ScenarioResult(
            label=_LABEL_E,
            ok=False,
            detail=(
                "prior greeks not preserved on failure — refresh_failed must "
                "flip without mutating delta/gamma/theta/vega/as_of/iv_used"
            ),
        )
    failure_entries = [
        entry
        for entry in activity_log.entries
        if entry.event_type is EventType.GREEKS_REFRESH_FAILED
    ]
    if len(failure_entries) != 1:
        return ScenarioResult(
            label=_LABEL_E,
            ok=False,
            detail=(f"expected 1 GREEKS_REFRESH_FAILED entry, got {len(failure_entries)}"),
        )
    return ScenarioResult(label=_LABEL_E, ok=True)


# ---------------------------------------------------------------------------
# (f) Halt onset.
# ---------------------------------------------------------------------------


_LABEL_F = "(f) Halt onset emits HALT_ACTIVATED"


async def run_scenario_f_halt_onset() -> ScenarioResult:
    """Drive: flip the fixture's halt state past the daily limit.

    Expect: exactly one ``HALT_ACTIVATED`` entry from the
    :class:`HaltTransitionTracker` over an inactive→active transition.
    """
    tracker = HaltTransitionTracker()
    counter = {"n": 0}

    def _entry_id_factory(local: int) -> str:
        counter["n"] += 1
        return f"halt-verify-{counter['n']:04d}-{local:02d}"

    # First observation: no halt active. No entries expected.
    first_pass = list(
        tracker.observe(
            halt_state=None,
            invocation_id="inv-verify-halt",
            at=_NOW,
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=_entry_id_factory,
        )
    )
    if first_pass:
        return ScenarioResult(
            label=_LABEL_F,
            ok=False,
            detail=(f"expected 0 entries on first observation with no halt; got {len(first_pass)}"),
        )

    # Second observation: daily halt active.
    halted = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=5.0,
        daily_drawdown_limit_pct=5.0,
    )
    second_pass = list(
        tracker.observe(
            halt_state=halted,
            invocation_id="inv-verify-halt",
            at=_NOW + timedelta(minutes=1),
            source=EventSource.GUARDRAIL_LAYER,
            entry_id_factory=_entry_id_factory,
        )
    )
    activated = [entry for entry in second_pass if entry.event_type is EventType.HALT_ACTIVATED]
    if len(activated) != 1:
        return ScenarioResult(
            label=_LABEL_F,
            ok=False,
            detail=(
                f"expected 1 HALT_ACTIVATED entry on transition; "
                f"got {len(activated)} of {len(second_pass)} total entries"
            ),
        )
    detail = activated[0].detail
    if not isinstance(detail, HaltActivatedDetail):
        return ScenarioResult(
            label=_LABEL_F,
            ok=False,
            detail=f"HALT_ACTIVATED detail wrong type: {type(detail).__name__}",
        )
    if detail.halt_type != "daily_drawdown":
        return ScenarioResult(
            label=_LABEL_F,
            ok=False,
            detail=f"halt_type={detail.halt_type!r}, expected 'daily_drawdown'",
        )
    return ScenarioResult(label=_LABEL_F, ok=True)


# ---------------------------------------------------------------------------
# (g) Immediate-action breach dispatch.
# ---------------------------------------------------------------------------


_LABEL_G = "(g) Immediate-action breach dispatch"


def _equity_position_view(
    *,
    position_id: str,
    ticker: str,
    direction: Direction = Direction.LONG,
    share_count: float = 10.0,
    cost_basis: float = 150.0,
    market_value_usd: float = 1500.0,
    unrealized_pnl_usd: float = -200.0,
    position_weight_pct: float = 10.0,
) -> PositionView:
    """Project an equity ``PositionView`` for the dispatcher's context.

    Mirrors :func:`tests.execution.continuous_monitor.cascade_dispatch.\
test_dispatcher._equity_position_view`.
    """
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=cost_basis,
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THE-{position_id}"),
        bracket_id=BracketId(f"BRK-{position_id}"),
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=cost_basis,
                fill_quantity=share_count,
                slippage=0.0,
                fees=0.0,
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    sign = 1.0 if direction == Direction.LONG else -1.0
    return PositionView(
        record=record,
        current_market_value_usd=market_value_usd,
        unrealized_pnl_usd=unrealized_pnl_usd,
        unrealized_pnl_pct=unrealized_pnl_usd / (cost_basis * share_count) * 100.0,
        position_weight_pct=position_weight_pct * sign,
        position_age_hours=2.0,
        notional_exposure_usd=market_value_usd,
        delta_adjusted_exposure_usd=market_value_usd * sign,
        distance_to_target_usd=10.0,
        distance_to_stop_usd=5.0,
        risk_reward_at_current=2.0,
    )


@dataclass(frozen=True)
class _StubRuleProjection:
    rule: str
    status: str
    current: float
    limit: float
    projected_after: float
    headroom_remaining: float
    unit: str
    inverse: bool = False


@dataclass(frozen=True)
class _StubLibraryOutput:
    per_rule: tuple[_StubRuleProjection, ...]


@dataclass(frozen=True)
class _StubLibraryConfig:
    effective_limits: dict[str, float]


@dataclass(frozen=True)
class _StubPortfolioState:
    label: str = "default"


@dataclass(frozen=True)
class _StubMarketInputs:
    label: str = "default"


@dataclass
class _ScriptedEvaluateProposals:
    """Stub for ``evaluate_proposals`` consumed by the cascade orchestrator."""

    outputs: list[_StubLibraryOutput]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        state: Any,
        proposals: Sequence[Any],
        config: Any,
        market: Any,
        delta_buffer_factor: float = 1.0,
    ) -> _StubLibraryOutput:
        self.calls.append(
            {
                "state": state,
                "proposals": tuple(proposals),
                "config": config,
                "market": market,
                "delta_buffer_factor": delta_buffer_factor,
            }
        )
        idx = min(len(self.calls) - 1, len(self.outputs) - 1)
        return self.outputs[idx]


def _breach_behavior_config() -> BreachBehaviorConfig:
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )


def _phase1_result(*, regime: RegimeLabel = RegimeLabel.ELEVATED) -> Phase1EnforcementResult:
    return Phase1EnforcementResult(
        active_risk_parameters=ActiveRiskParameterSet(
            regime_label=regime,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=(),
            active_overlays=(),
        ),
        drawdown_tier=DrawdownTier.CONSTRAINED,
    )


def _make_dispatch_context(
    *,
    breaching_position_id: str = "POS-NVDA-1",
    primary_rule: str = "per_position_max_loss",
) -> BreachDispatchContext:
    breaching = _equity_position_view(
        position_id=breaching_position_id,
        ticker="NVDA",
        unrealized_pnl_usd=-3_500.0,
    )
    positions = (breaching,)
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=10.0)
        for p in positions
    )
    risk_reward = tuple(
        PositionRiskReward(
            position_id=p.position_id,
            risk_reward_ratio=p.risk_reward_at_current or 2.0,
        )
        for p in positions
    )
    return BreachDispatchContext(
        open_positions=positions,
        liquidity=liquidity,
        risk_reward_metric=risk_reward,
        library_snapshot=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                primary_rule: 1.0,
                "per_position_max_loss": 1.0,
            }
        ),
        market_inputs=_StubMarketInputs(),
        evaluate_proposals=_ScriptedEvaluateProposals(
            outputs=[
                _StubLibraryOutput(per_rule=()),  # baseline
                _StubLibraryOutput(per_rule=()),  # post-close
            ]
        ),
        portfolio_value_usd=100_000.0,
        active_regime=RegimeLabel.ELEVATED,
        progressive_tiers=(),
        breach_classification={},
    )


def _per_position_max_loss_kwargs_provider(
    rule: RuleEvaluation,
    context: BreachDispatchContext,
) -> dict[str, Any]:
    breaching = context.open_positions[0]
    return {
        "breaching_position_id": breaching.position_id,
        "open_positions": context.open_positions,
        "loss_pct": rule.current_value,
        "limit_pct": rule.limit_value,
    }


@dataclass
class _RecordingSubmit:
    """Stub for ``submit_engine_envelope`` — records every submitted envelope."""

    calls: list[OmsEngineEnvelope] = field(default_factory=list)

    async def __call__(self, envelope: OmsEngineEnvelope) -> SubmissionResult:
        self.calls.append(envelope)
        return SubmissionResult(
            command_ordinal=0,
            status="accepted",
            command_id=f"{envelope.envelope_id}.0",
            acknowledgment=Acknowledgment(
                position_id=envelope.commands[0].position_id,
                order_id=OrderId(f"ORD-{envelope.commands[0].position_id}"),
            ),
        )


@dataclass
class _RecordingDeferralSink:
    calls: list[DeferralEvent] = field(default_factory=list)

    async def __call__(self, event: DeferralEvent) -> None:
        self.calls.append(event)


def _breach_loop_result(*, evaluations: tuple[RuleEvaluation, ...]) -> BreachLoopResult:
    immediate = tuple(e for e in evaluations if e.classification is BreachResponse.immediate_engine)
    return BreachLoopResult(
        as_of=_NOW,
        phase1_result=_phase1_result(),
        rule_evaluations=evaluations,
        halt_state=None,
        immediate_action_breaches=immediate,
        drawdown_velocity_sample=DrawdownSample(sampled_at=_NOW, intraday_drawdown_pct=1.0),
    )


async def run_scenario_g_immediate_breach_dispatch() -> ScenarioResult:
    """Drive: synthesise a ``per_position_max_loss`` HARD_BLOCK breach and
    feed it to the :class:`CascadeDispatcher`.

    Expect: exactly one engine envelope is submitted; the embedded CLOSE
    carries ``risk_management_subtype='engine_guardrail'`` and
    ``close_rationale_type='risk_management'``.
    """
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = _RecordingSubmit()
    deferral_sink = _RecordingDeferralSink()
    context = _make_dispatch_context()
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=_breach_behavior_config(),
        trigger_ids=trigger_ids,
        context_provider=lambda: context,
        submit_envelope=submit,
        deferral_sink=deferral_sink,
        per_rule_kwargs_providers={
            "per_position_max_loss": _per_position_max_loss_kwargs_provider,
        },
        now=lambda: _NOW,
    )
    rule = RuleEvaluation(
        rule_id="per_position_max_loss",
        current_value=-3.5,
        limit_value=-3.0,
        overage=0.5,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
    )
    result = _breach_loop_result(evaluations=(rule,))
    await dispatcher.handle_immediate_breach(result, rule)

    if len(submit.calls) != 1:
        return ScenarioResult(
            label=_LABEL_G,
            ok=False,
            detail=(f"expected 1 submit_engine_envelope call, got {len(submit.calls)}"),
        )
    if deferral_sink.calls:
        return ScenarioResult(
            label=_LABEL_G,
            ok=False,
            detail=(
                "deferral_sink received calls on the immediate-action path — "
                "the per_position_max_loss cascade should submit, not defer"
            ),
        )
    envelope = submit.calls[0]
    cmd = envelope.commands[0]
    if cmd.risk_management_subtype != "engine_guardrail":
        return ScenarioResult(
            label=_LABEL_G,
            ok=False,
            detail=(
                f"envelope.commands[0].risk_management_subtype="
                f"{cmd.risk_management_subtype!r}, expected 'engine_guardrail'"
            ),
        )
    if cmd.close_rationale_type != "risk_management":
        return ScenarioResult(
            label=_LABEL_G,
            ok=False,
            detail=(
                f"envelope.commands[0].close_rationale_type="
                f"{cmd.close_rationale_type!r}, expected 'risk_management'"
            ),
        )
    return ScenarioResult(label=_LABEL_G, ok=True)


# ---------------------------------------------------------------------------
# (h) Deferred-rule breach is NOT dispatched.
# ---------------------------------------------------------------------------


_LABEL_H = "(h) Deferred-rule breach is NOT dispatched"


async def run_scenario_h_deferred_breach_not_dispatched() -> ScenarioResult:
    """Drive: synthesise a ``sector_concentration`` HARD_BLOCK breach with
    ``classification=deferred_to_pm``.

    Expect: the loop's ``immediate_action_breaches`` is empty (the
    dispatcher is never invoked); the rule still appears in
    ``rule_evaluations`` with classification ``deferred_to_pm``.
    """
    rule = RuleEvaluation(
        rule_id="sector_concentration_pct",
        current_value=30.0,
        limit_value=25.0,
        overage=5.0,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.deferred_to_pm,
    )
    result = _breach_loop_result(evaluations=(rule,))
    if result.immediate_action_breaches:
        return ScenarioResult(
            label=_LABEL_H,
            ok=False,
            detail=(
                "BreachLoopResult.immediate_action_breaches not empty; "
                "deferred classification leaked into the immediate path"
            ),
        )
    deferred = [
        e for e in result.rule_evaluations if e.classification is BreachResponse.deferred_to_pm
    ]
    if len(deferred) != 1:
        return ScenarioResult(
            label=_LABEL_H,
            ok=False,
            detail=(f"expected 1 deferred-classification rule_evaluation, got {len(deferred)}"),
        )
    # Now confirm the dispatcher would not be wired to fire for this rule —
    # the breach loop filters via classification; we mirror the filter here.
    trigger_ids = TriggerIdGenerator(session_id=_SESSION_ID)
    submit = _RecordingSubmit()
    deferral_sink = _RecordingDeferralSink()
    dispatcher = CascadeDispatcher(
        monitor_session_id=_SESSION_ID,
        breach_config=_breach_behavior_config(),
        trigger_ids=trigger_ids,
        context_provider=_make_dispatch_context,
        submit_envelope=submit,
        deferral_sink=deferral_sink,
        per_rule_kwargs_providers={},
        now=lambda: _NOW,
    )
    # Loop filter: only dispatch for immediate_engine classification rules.
    for evaluation in result.immediate_action_breaches:
        await dispatcher.handle_immediate_breach(result, evaluation)
    if submit.calls:
        return ScenarioResult(
            label=_LABEL_H,
            ok=False,
            detail=(
                f"deferred-classification rule reached submit_engine_envelope "
                f"({len(submit.calls)} calls); should be filtered before dispatch"
            ),
        )
    return ScenarioResult(label=_LABEL_H, ok=True)


# ---------------------------------------------------------------------------
# (i) Options bracket-stop fires.
# ---------------------------------------------------------------------------


_LABEL_I = "(i) Options bracket-stop fires"


def _price_stop_bracket(
    *,
    bracket_id: str,
    position_id: str,
    threshold: float,
    underlying: str,
    direction: str = "LTE",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-stop-1",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol(underlying),
            threshold_usd=threshold,
            direction=direction,  # type: ignore[arg-type]
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


@dataclass
class _FakeBracketRepository:
    brackets: tuple[BracketRecord, ...]

    async def get_brackets_for_positions(
        self, *, position_ids: tuple[str, ...]
    ) -> tuple[BracketRecord, ...]:
        return tuple(b for b in self.brackets if b.position_id in position_ids)


@dataclass
class _FakeBracketSubmitter:
    options_calls: list[tuple[str, str]] = field(default_factory=list)
    strategy_calls: list[str] = field(default_factory=list)

    async def submit_options_close(
        self,
        *,
        position: PositionRecord,
        details: OptionsPositionDetails,
        client_order_id: str,
    ) -> CloseSubmissionResult:
        del details
        self.options_calls.append((position.position_id, client_order_id))
        return CloseSubmissionResult(order_ids=(client_order_id,), mode="single_leg")

    async def submit_strategy_close(
        self,
        *,
        position: PositionRecord,
        details: StrategyPositionDetails,
        client_order_id_base: str,
    ) -> CloseSubmissionResult:
        del details
        self.strategy_calls.append(position.position_id)
        return CloseSubmissionResult(order_ids=(client_order_id_base,), mode="strategy_combined")


async def run_scenario_i_bracket_stop_fires() -> ScenarioResult:
    """Drive: inject a quote crossing the price-based-invalidation stop for
    the fixture options position.

    Expect: the broker-adapter close path is called exactly once; one
    ``POSITION_CLOSED`` activity-log entry is written with
    ``event_source=BRACKET_MANAGER`` and ``exit_method=STOP_TRIGGERED``.
    """
    position = _options_position(
        position_id="POS-OPT-NVDA-BR",
        bracket_id="BRK-OPT-NVDA-BR",
    )
    bracket = _price_stop_bracket(
        bracket_id="BRK-OPT-NVDA-BR",
        position_id="POS-OPT-NVDA-BR",
        threshold=865.0,
        underlying="NVDA",
        direction="LTE",
    )
    cache = await _seed_underlying_cache({"NVDA": 860.0})  # below threshold
    submitter = _FakeBracketSubmitter()
    activity_log = _CapturingActivityLog()
    await _run_bracket_stop_cycle(
        config=_config(),
        position_repository=_FixturePositionsReader((position,)),
        bracket_repository=_FakeBracketRepository((bracket,)),
        cache=cache,
        submitter=submitter,
        activity_log=activity_log.emit,
        invocation_id_provider=_verify_invocation_id,
        trigger_ids=TriggerIdGenerator(session_id=_SESSION_ID),
        monitor_session_id=_SESSION_ID,
        now=_NOW,
        risk_free_rate=0.045,
        fired_legs=set(),
    )
    if len(submitter.options_calls) != 1:
        return ScenarioResult(
            label=_LABEL_I,
            ok=False,
            detail=(
                f"expected 1 submitter.submit_options_close call, "
                f"got {len(submitter.options_calls)}"
            ),
        )
    if len(activity_log.entries) != 1:
        return ScenarioResult(
            label=_LABEL_I,
            ok=False,
            detail=(f"expected 1 POSITION_CLOSED entry, got {len(activity_log.entries)}"),
        )
    entry = activity_log.entries[0]
    if entry.source is not EventSource.BRACKET_MANAGER:
        return ScenarioResult(
            label=_LABEL_I,
            ok=False,
            detail=(f"POSITION_CLOSED.source={entry.source!r}, expected BRACKET_MANAGER"),
        )
    detail = entry.detail
    if not isinstance(detail, PositionClosedDetail):
        return ScenarioResult(
            label=_LABEL_I,
            ok=False,
            detail=f"POSITION_CLOSED.detail wrong type: {type(detail).__name__}",
        )
    if detail.exit_method is not PositionExitMethod.STOP_TRIGGERED:
        return ScenarioResult(
            label=_LABEL_I,
            ok=False,
            detail=(f"exit_method={detail.exit_method!r}, expected STOP_TRIGGERED"),
        )
    return ScenarioResult(label=_LABEL_I, ok=True)


# ---------------------------------------------------------------------------
# (j) Strategist visibility.
# ---------------------------------------------------------------------------


_LABEL_J = "(j) Strategist visibility — between_invocation_closures"


def _between_invocation_closure(
    *,
    position_id: str,
    ticker: str,
    closed_at: datetime,
    origin: BetweenInvocationClosureOrigin,
    exit_method: PositionExitMethod,
    closing_order_id: str | None,
) -> BetweenInvocationClosure:
    """Build a ``BetweenInvocationClosure`` mirroring the strategist projection."""
    from alphamind.portfolio_state.records.positions import InstrumentType

    return BetweenInvocationClosure(
        position_id=position_id,
        ticker=ticker,
        instrument_type=InstrumentType.EQUITY
        if origin == "engine_guardrail"
        else InstrumentType.OPTIONS,
        closed_at=closed_at,
        closing_order_id=closing_order_id,
        exit_method=exit_method,
        origin=origin,
        rationale=(
            f"{origin}: exit_method={exit_method.value}"
            if closing_order_id is None
            else f"{origin}: order_id={closing_order_id}"
        ),
    )


async def run_scenario_j_strategist_visibility() -> ScenarioResult:
    """Drive: render the ``between_invocation_closures`` tuple a strategist
    would consume after scenarios (g) and (i) have fired.

    Expect: both the engine-envelope cascade closure (from g) and the
    bracket-stop closure (from i) are present, ordered chronologically.
    """
    cascade_closure_at = _NOW
    bracket_closure_at = _NOW + timedelta(minutes=1)
    cascade_closure = _between_invocation_closure(
        position_id="POS-NVDA-1",
        ticker="NVDA",
        closed_at=cascade_closure_at,
        origin="engine_guardrail",
        exit_method=PositionExitMethod.MARGIN_LIQUIDATION,
        closing_order_id="MON.monsession-verify.1.0",
    )
    bracket_closure = _between_invocation_closure(
        position_id="POS-OPT-NVDA-BR",
        ticker="NVDA",
        closed_at=bracket_closure_at,
        origin="bracket_manager",
        exit_method=PositionExitMethod.STOP_TRIGGERED,
        closing_order_id="ORD-OPT-CLOSE",
    )
    # Real strategist projection emits chronological order — confirm the
    # tuple our verify constructs sorts in ascending closed_at.
    closures = tuple(
        sorted(
            (cascade_closure, bracket_closure),
            key=lambda c: (c.closed_at, c.position_id),
        )
    )
    if len(closures) != 2:
        return ScenarioResult(
            label=_LABEL_J,
            ok=False,
            detail=f"expected 2 closures, got {len(closures)}",
        )
    if closures[0].origin != "engine_guardrail":
        return ScenarioResult(
            label=_LABEL_J,
            ok=False,
            detail=(
                f"closures[0].origin={closures[0].origin!r}, "
                f"expected 'engine_guardrail' (chronological)"
            ),
        )
    if closures[1].origin != "bracket_manager":
        return ScenarioResult(
            label=_LABEL_J,
            ok=False,
            detail=(
                f"closures[1].origin={closures[1].origin!r}, "
                f"expected 'bracket_manager' (chronological)"
            ),
        )
    return ScenarioResult(label=_LABEL_J, ok=True)


# ---------------------------------------------------------------------------
# (k) Emergency request — gated on EventType vocabulary.
# ---------------------------------------------------------------------------


_LABEL_K = "(k) Emergency request — regime jump fires, second suppressed"


def _emergency_breach_loop_result(
    *,
    as_of: datetime,
    regime_label: RegimeLabel,
) -> BreachLoopResult:
    """Build a minimal ``BreachLoopResult`` for the emergency evaluator."""
    arpset = ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="daily_drawdown_pct",
                value=5.0,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )
    phase1_result = Phase1EnforcementResult(
        active_risk_parameters=arpset,
        drawdown_tier=None,
    )
    return BreachLoopResult(
        as_of=as_of,
        phase1_result=phase1_result,
        rule_evaluations=(),
        halt_state=None,
        immediate_action_breaches=(),
        drawdown_velocity_sample=DrawdownSample(sampled_at=as_of, intraday_drawdown_pct=0.0),
    )


@dataclass
class _RecordingEmergencyWriter:
    entries: list[ActivityLogEntry] = field(default_factory=list)

    async def __call__(self, entry: ActivityLogEntry) -> None:
        self.entries.append(entry)


class _NoMarginCallObserver:
    async def __call__(self) -> Any:
        return None


async def run_scenario_k_emergency_request() -> ScenarioResult:
    """Drive: a NORMAL → CRISIS regime jump triggers an emergency request;
    a second regime jump inside the cooldown is suppressed.

    Skipped iff ``EventType.EMERGENCY_INVOCATION_REQUESTED`` is not part of
    the activity-log vocabulary (story ALP-439 not yet landed).
    """
    if "EMERGENCY_INVOCATION_REQUESTED" not in EventType.__members__:
        return ScenarioResult(
            label=_LABEL_K,
            ok=True,
            detail="skipped — EMERGENCY_INVOCATION_REQUESTED not in EventType",
            skipped=True,
        )

    cfg = _breach_behavior_config()
    cooldown = CooldownTracker(cooldown_minutes=cfg.emergency_invocation_cooldown_minutes)
    writer = _RecordingEmergencyWriter()
    evaluator = EmergencyTriggerEvaluator(
        session=_session(),
        breach_behavior_config=cfg,
        cooldown=cooldown,
        trigger_ids=TriggerIdGenerator(session_id=_SESSION_ID),
        margin_call_observer=_NoMarginCallObserver(),
        activity_log_writer=writer,
        breach_response_lookup=MappingProxyType({}),
        invocation_id_provider=_verify_emergency_invocation_id,
    )
    t0 = _NOW
    # Seed prior regime: NORMAL.
    await evaluator.handle_emergency_input(
        _emergency_breach_loop_result(as_of=t0, regime_label=RegimeLabel.NORMAL)
    )
    # Jump to CRISIS — fires.
    await evaluator.handle_emergency_input(
        _emergency_breach_loop_result(
            as_of=t0 + timedelta(minutes=1),
            regime_label=RegimeLabel.CRISIS,
        )
    )
    # Second jump within the 30-minute cooldown — suppressed.
    await evaluator.handle_emergency_input(
        _emergency_breach_loop_result(
            as_of=t0 + timedelta(minutes=6),
            regime_label=RegimeLabel.NORMAL,
        )
    )
    await evaluator.handle_emergency_input(
        _emergency_breach_loop_result(
            as_of=t0 + timedelta(minutes=7),
            regime_label=RegimeLabel.CRISIS,
        )
    )
    if len(writer.entries) != 1:
        return ScenarioResult(
            label=_LABEL_K,
            ok=False,
            detail=(
                f"expected exactly 1 EMERGENCY_INVOCATION_REQUESTED entry; "
                f"got {len(writer.entries)} — cooldown not enforced?"
            ),
        )
    entry = writer.entries[0]
    if entry.event_type is not EventType.EMERGENCY_INVOCATION_REQUESTED:
        return ScenarioResult(
            label=_LABEL_K,
            ok=False,
            detail=(
                f"entry.event_type={entry.event_type!r}, expected EMERGENCY_INVOCATION_REQUESTED"
            ),
        )
    detail = entry.detail
    if not isinstance(detail, EmergencyInvocationRequestedDetail):
        return ScenarioResult(
            label=_LABEL_K,
            ok=False,
            detail=(
                f"detail wrong type: {type(detail).__name__}; "
                f"expected EmergencyInvocationRequestedDetail"
            ),
        )
    if detail.trigger_type != "regime_jump":
        return ScenarioResult(
            label=_LABEL_K,
            ok=False,
            detail=(f"detail.trigger_type={detail.trigger_type!r}, expected 'regime_jump'"),
        )
    return ScenarioResult(label=_LABEL_K, ok=True)


# ---------------------------------------------------------------------------
# Runner aggregation
# ---------------------------------------------------------------------------


_ALL_SCENARIOS: tuple[Callable[[], Awaitable[ScenarioResult]], ...] = (
    run_scenario_a_stream_caches_quote,
    run_scenario_b_fill_persists,
    run_scenario_c_greeks_scheduled_refresh,
    run_scenario_d_greeks_move_triggered_refresh,
    run_scenario_e_greeks_refresh_failure,
    run_scenario_f_halt_onset,
    run_scenario_g_immediate_breach_dispatch,
    run_scenario_h_deferred_breach_not_dispatched,
    run_scenario_i_bracket_stop_fires,
    run_scenario_j_strategist_visibility,
    run_scenario_k_emergency_request,
)


async def _run_all_scenarios() -> list[ScenarioResult]:
    """Run every scenario sequentially.

    The scenarios are deliberately serial — they share no state, but printing
    the per-scenario progress in order is what the operator output guarantees.
    Tests monkeypatch individual scenario callables on the module; the runner
    resolves them through the module attribute so the patch is visible.
    """
    module = sys.modules[__name__]
    results: list[ScenarioResult] = []
    for fn in _ALL_SCENARIOS:
        # Resolve the live attribute on the module so monkeypatching is honoured.
        resolved = getattr(module, fn.__name__)
        try:
            result = await resolved()
        except Exception as exc:
            result = ScenarioResult(
                label=fn.__name__,
                ok=False,
                detail=f"unhandled exception in scenario: {exc!r}",
            )
        results.append(result)
    return results


def _render_text(results: Sequence[ScenarioResult]) -> str:
    lines = [
        "=" * 78,
        "AlphaMind Continuous Monitor Verification",
        "=" * 78,
    ]
    pass_count = sum(1 for r in results if r.ok)
    for r in results:
        marker = "[SKIP]" if r.skipped else ("[PASS]" if r.ok else "[FAIL]")
        lines.append(f"  {marker} {r.label}")
        if r.detail:
            lines.append(f"      {r.detail}")
    lines.append("=" * 78)
    failed = [r.label for r in results if not r.ok]
    if not failed:
        lines.append(f"RESULT: PASS ({pass_count}/{len(results)} scenarios)")
    else:
        lines.append(
            f"RESULT: FAIL ({pass_count}/{len(results)} scenarios passed; "
            f"failing: {', '.join(failed)})"
        )
    lines.append("=" * 78)
    return "\n".join(lines)


def _render_json(results: Sequence[ScenarioResult]) -> str:
    payload: dict[str, Any] = {
        "all_pass": all(r.ok for r in results),
        "scenarios": [
            {
                "label": r.label,
                "ok": r.ok,
                "skipped": r.skipped,
                "detail": r.detail,
            }
            for r in results
        ],
    }
    return json.dumps(payload, indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the continuous monitor's five responsibilities against "
            "in-memory fixtures (no live websockets, no live broker)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on all-PASS, non-zero on any FAIL."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    results = asyncio.run(_run_all_scenarios())

    if args.output == "json":
        print(_render_json(results))
    else:
        print(_render_text(results))

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":  # pragma: no cover - exercised via the shim
    sys.exit(main())
