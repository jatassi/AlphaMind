"""Tests for ``run_breach_loop`` (story 03b / ALP-437 + ALP-831).

The loop is a run-forever coroutine; tests drive it through a fake
``SupervisedLoop`` factory that iterates a fixed number of ticks, making the
body execute deterministically and cooperatively.  The watchdog-binding test
(ALP-831 AC 5) uses a real ``MonitorSupervisor`` with an injected sleep shim so
the production ``supervised_loop`` path is exercised end-to-end.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, cast

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.guardrails import BreachResponse, ProgressiveTier
from alphamind.execution.continuous_monitor.breach_loop import (
    BreachLoopHealthSignal,
    BreachLoopResult,
    RuleEvaluation,
    run_breach_loop,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor, SupervisedLoop
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
    UnderlyingQuote,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventType,
)
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.risk_guardrails.breach_behavior import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    EscalationZones as LibraryEscalationZones,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FeatureFlagsView,
    LibraryConfig,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    AssetType,
    Direction,
    ExistingPosition,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput
from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(
    cadence_s: int = 60,
    *,
    failure_threshold: int = 3,
    underlying_price_max_age_seconds: float = 900.0,
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=cadence_s,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        subscription_refresh_seconds=30,
        max_reconnect_attempts=3,
        supervisor_shutdown_timeout_seconds=5,
        breach_loop_consecutive_failure_alert_threshold=failure_threshold,
        underlying_price_max_age_seconds=underlying_price_max_age_seconds,
    )


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T143000Z-abcdef01",
        started_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        mode="paper",
    )


def _active_risk_parameters(daily_limit: float = 5.0) -> ActiveRiskParameterSet:
    """Single-rule parameter set with a daily-drawdown entry."""
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="daily_drawdown_pct",
                value=daily_limit,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=daily_limit,
            ),
            ActiveRiskParameterEntry(
                rule_id="net_long_pct",
                rule_label="net_long_pct",
                value=80.0,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=80.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="position_max_loss_equity_pct",
                rule_label="position_max_loss_equity_pct",
                value=2.0,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=2.0,
            ),
            ActiveRiskParameterEntry(
                rule_id="sector_concentration_pct",
                rule_label="sector_concentration_pct",
                value=25.0,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=25.0,
            ),
        ),
        active_overlays=(),
    )


def _regime_output() -> RegimeAdaptationOutput:
    from alphamind.config.models.regimes import Regime

    state = RegimeAdaptationState(
        as_of="2026-05-11T14:30:00Z",
        invocation_id="mon-1",
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="normal",
        distillation_vix_level=0.0,
        regime_skip_emergency=False,
    )
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=_active_risk_parameters(),
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=state,
        audit_log_entries=(),
    )


def _drawdown_state(intraday_pct: float = 0.0, cumulative_pct: float = 0.0) -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=cumulative_pct,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=intraday_pct,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _library_snapshot(*, net_long_pct: float = 50.0, sector_pct: float = 10.0) -> LibrarySnapshot:
    return LibrarySnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=20_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": sector_pct}),
        net_long_pct=net_long_pct,
        net_short_pct=0.0,
        gross_pct=net_long_pct,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )


def _library_config(active_parameters: ActiveRiskParameterSet) -> LibraryConfig:
    """Translate the parameter set into a ``LibraryConfig`` (test helper).

    Production wiring composes this from the resolver; the tests are concerned
    with the loop's invariant that it builds a ``LibraryConfig`` from the
    Phase-1 result and threads it into the evaluator.
    """
    effective_limits = {entry.rule_id: entry.value for entry in active_parameters.entries}
    zones = {
        rule_id: LibraryEscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
        for rule_id in effective_limits
    }
    return LibraryConfig(
        effective_limits=MappingProxyType(effective_limits),
        escalation_zones=MappingProxyType(zones),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech",),
        active_regime="normal",
        active_profile="standard",
        conservative_buffer_pct=10.0,
    )


def _breach_response_lookup() -> Mapping[str, BreachResponse]:
    return MappingProxyType(
        {
            "position_max_loss_equity_pct": BreachResponse.immediate_engine,
            "position_max_loss_options_pct": BreachResponse.immediate_engine,
            "daily_drawdown_pct": BreachResponse.immediate_engine,
            "cumulative_drawdown_pct": BreachResponse.immediate_engine,
            "single_short_max_pct": BreachResponse.immediate_engine,
            "net_long_pct": BreachResponse.deferred_to_pm,
            "sector_concentration_pct": BreachResponse.deferred_to_pm,
        }
    )


class _FixtureIvProvider:
    """Trivial ``IvProvider`` for tests; the breach loop never invokes it when
    proposals are empty (current-state-only evaluation)."""

    def lookup_iv(self, **_: Any) -> Any:  # pragma: no cover - never called
        raise AssertionError("iv_provider.lookup_iv should not be called with empty proposals")


@dataclass
class _StubMarketHours:
    """Fake :class:`MarketHoursClock`."""

    open_flag: bool = True
    queries: list[datetime] = field(default_factory=list)

    def is_market_open(self, at: datetime) -> bool:
        self.queries.append(at)
        return self.open_flag


@dataclass
class _CallableRecord:
    """Records every call to an async callback for assertion."""

    calls: list[tuple[Any, ...]] = field(default_factory=list)


class _StubRepository:
    """In-memory ``PortfolioStateRepository`` subset (drawdown only)."""

    def __init__(
        self,
        *,
        drawdown_states: list[DrawdownState],
        active_risk_parameters: ActiveRiskParameterSet,
    ) -> None:
        self._drawdowns = drawdown_states
        self._index = 0
        self._active_risk_parameters = active_risk_parameters

    def get_drawdown_state(self) -> DrawdownState:
        idx = min(self._index, len(self._drawdowns) - 1)
        state = self._drawdowns[idx]
        self._index += 1
        return state

    def get_open_positions(self) -> tuple[Any, ...]:
        return ()

    def get_active_risk_parameters(self) -> ActiveRiskParameterSet:
        return self._active_risk_parameters


def _progressive_tiers() -> tuple[ProgressiveTier, ...]:
    return (
        ProgressiveTier(trigger_pct=8.0, max_position_size_pct=3.0, max_gross_pct=80.0),
        ProgressiveTier(trigger_pct=10.0, max_position_size_pct=2.0, max_gross_pct=60.0),
        ProgressiveTier(trigger_pct=12.0, full_halt=True),
    )


def _cache() -> UnderlyingPriceCache:
    return UnderlyingPriceCache()


def _populated_cache() -> UnderlyingPriceCache:
    cache = UnderlyingPriceCache()
    quote = UnderlyingQuote(
        ticker=Symbol("AAPL"), price=150.0, as_of=datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
    )
    asyncio.run(cache.update(quote))
    return cache


# ---------------------------------------------------------------------------
# SupervisedLoop test helpers
# ---------------------------------------------------------------------------


def _make_counted_loop(n_ticks: int) -> SupervisedLoop:
    """Return a SupervisedLoop factory that iterates exactly *n_ticks* times.

    Stand-in for ``MonitorSupervisor.supervised_loop``; exercises the loop
    body a bounded number of times then raises CancelledError so the test
    completes.  The cadence→watchdog-bound mapping is tested at the supervisor
    level in ``test_supervisor.py``.
    """

    async def _loop() -> AsyncIterator[None]:
        for _ in range(n_ticks):
            yield
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    return _loop


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_in_normal_zone_does_not_invoke_immediate_breach_callback() -> None:
    """No breaches → ``on_immediate_breach`` never fires; ``on_emergency_input`` does."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state(intraday_pct=0.0)],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate = _CallableRecord()
    emergency = _CallableRecord()
    activity_log = _CallableRecord()

    async def _immediate(result: BreachLoopResult, evaluation: RuleEvaluation) -> None:
        immediate.calls.append((result, evaluation))

    async def _emergency(result: BreachLoopResult) -> None:
        emergency.calls.append((result,))

    async def _sink(entries: Iterable[ActivityLogEntry]) -> None:
        activity_log.calls.append((tuple(entries),))

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    market_hours = _StubMarketHours(open_flag=True)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=market_hours,
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_make_counted_loop(1),
        )

    assert immediate.calls == []
    assert len(emergency.calls) == 1
    assert isinstance(emergency.calls[0][0], BreachLoopResult)


@pytest.mark.asyncio
async def test_immediate_action_hard_block_invokes_callback() -> None:
    """A snapshot whose net_long_pct breaches → BLOCKED with deferred classification,
    while position_max_loss_equity_pct breaches → BLOCKED with immediate classification."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate = _CallableRecord()
    emergency = _CallableRecord()
    activity_log = _CallableRecord()

    async def _immediate(result: BreachLoopResult, evaluation: RuleEvaluation) -> None:
        immediate.calls.append((result, evaluation))

    async def _emergency(result: BreachLoopResult) -> None:
        emergency.calls.append((result,))

    async def _sink(entries: Iterable[ActivityLogEntry]) -> None:
        activity_log.calls.append((tuple(entries),))

    # Snapshot pinned so position_max_size_pct=5.0 limit at 2.0; current 5.0 → > 250%.
    breach_snapshot = LibrarySnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=20_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 10.0}),
        net_long_pct=50.0,
        net_short_pct=0.0,
        gross_pct=50.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        # position_max_size_pct > limit (2.0) → BLOCKED on immediate-classification rule.
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )

    async def _snapshot() -> LibrarySnapshot:
        return breach_snapshot

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # Override the LibraryConfig factory so position_max_size_pct has limit 2.0 and
    # zones tight enough that 5.0 trips hard_block.
    def _custom_library_config(active_parameters: ActiveRiskParameterSet) -> LibraryConfig:
        effective_limits = {entry.rule_id: entry.value for entry in active_parameters.entries}
        effective_limits["position_max_size_pct"] = 2.0
        zones = {
            rule_id: LibraryEscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
            for rule_id in effective_limits
        }
        return LibraryConfig(
            effective_limits=MappingProxyType(effective_limits),
            escalation_zones=MappingProxyType(zones),
            feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
            active_sectors=("tech",),
            active_regime="normal",
            active_profile="standard",
            conservative_buffer_pct=10.0,
        )

    lookup = MappingProxyType(
        {
            **_breach_response_lookup(),
            "position_max_size_pct": BreachResponse.deferred_to_pm,
        }
    )
    # We want an immediate-action rule that breaches. Synthesise via
    # ``position_max_loss_equity_pct`` — there's no direct snapshot read for
    # this rule in the library (it's a per-position rule). Use
    # ``single_short_max_pct`` instead: it reads from the snapshot directly.
    breach_snapshot2 = breach_snapshot.__class__(
        portfolio_value_usd=breach_snapshot.portfolio_value_usd,
        cash_usd=breach_snapshot.cash_usd,
        reserved_for_pending_orders_usd=breach_snapshot.reserved_for_pending_orders_usd,
        sector_exposure_pct=breach_snapshot.sector_exposure_pct,
        net_long_pct=breach_snapshot.net_long_pct,
        net_short_pct=10.0,
        gross_pct=60.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=10.0,
        single_short_max_pct=8.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )

    async def _snapshot2() -> LibrarySnapshot:
        return breach_snapshot2

    # single_short_max_pct value = 5.0; current = 8.0 → 160% → BLOCKED.
    def _config_for_short(active_parameters: ActiveRiskParameterSet) -> LibraryConfig:
        effective_limits = {entry.rule_id: entry.value for entry in active_parameters.entries}
        effective_limits["single_short_max_pct"] = 5.0
        effective_limits["total_short_pct"] = 15.0
        effective_limits["position_max_size_pct"] = 10.0
        zones = {
            rule_id: LibraryEscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
            for rule_id in effective_limits
        }
        return LibraryConfig(
            effective_limits=MappingProxyType(effective_limits),
            escalation_zones=MappingProxyType(zones),
            feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
            active_sectors=("tech",),
            active_regime="normal",
            active_profile="standard",
            conservative_buffer_pct=10.0,
        )

    market_hours = _StubMarketHours(open_flag=True)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot2,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_config_for_short,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=lookup,
            market_hours=market_hours,
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_make_counted_loop(1),
        )

    # `single_short_max_pct` is an immediate-action rule per the registry; ensure
    # the callback fired exactly once with that rule.
    assert len(immediate.calls) == 1
    _result, evaluation = immediate.calls[0]
    assert evaluation.rule_id == "single_short_max_pct"
    assert evaluation.zone is RiskZone.BLOCKED
    assert evaluation.classification is BreachResponse.immediate_engine

    # Emergency input always fires regardless of breach state.
    assert len(emergency.calls) == 1


@pytest.mark.asyncio
async def test_deferred_breach_does_not_invoke_immediate_callback() -> None:
    """A deferred-classification rule in BLOCKED → recorded in evaluations,
    but ``on_immediate_breach`` is NOT invoked."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate = _CallableRecord()
    emergency = _CallableRecord()

    async def _immediate(result: BreachLoopResult, evaluation: RuleEvaluation) -> None:
        immediate.calls.append((result, evaluation))

    async def _emergency(result: BreachLoopResult) -> None:
        emergency.calls.append((result,))

    async def _sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    # Snapshot: sector_exposure_pct['tech'] = 30%, limit 25% → 120% → BLOCKED.
    breach_snapshot = LibrarySnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=20_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 30.0}),
        net_long_pct=50.0,
        net_short_pct=0.0,
        gross_pct=50.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=2.0,
        existing_positions=MappingProxyType({}),
    )

    async def _snapshot() -> LibrarySnapshot:
        return breach_snapshot

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_make_counted_loop(1),
        )

    assert immediate.calls == []
    assert len(emergency.calls) == 1
    result = emergency.calls[0][0]
    # The sector_concentration_<tech> entry must be in evaluations, BLOCKED.
    sector_entry = next(
        (e for e in result.rule_evaluations if e.rule_id == "sector_concentration_tech"), None
    )
    assert sector_entry is not None
    assert sector_entry.zone is RiskZone.BLOCKED
    assert sector_entry.classification is BreachResponse.deferred_to_pm


@pytest.mark.asyncio
async def test_market_closed_pauses_evaluation() -> None:
    """Outside market hours → no evaluation, no callbacks, no events."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate = _CallableRecord()
    emergency = _CallableRecord()
    snapshot_calls = {"n": 0}

    async def _immediate(result: BreachLoopResult, evaluation: RuleEvaluation) -> None:
        immediate.calls.append((result, evaluation))

    async def _emergency(result: BreachLoopResult) -> None:
        emergency.calls.append((result,))

    async def _sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    async def _snapshot() -> LibrarySnapshot:
        snapshot_calls["n"] += 1
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    market_hours = _StubMarketHours(open_flag=False)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=market_hours,
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_make_counted_loop(2),
        )

    assert immediate.calls == []
    assert emergency.calls == []
    assert snapshot_calls["n"] == 0


@pytest.mark.asyncio
async def test_loop_beats_watchdog_via_supervised_loop() -> None:
    """ALP-831 AC 5: breach_loop is bound at its cadence and beats on every tick —
    including closed-market ticks — via the production supervised_loop seam.

    This test drives the loop through a REAL MonitorSupervisor with an injected
    sleep shim.  The supervisor.beat()/register_watch() path is exercised by
    supervised_loop itself; we assert (a) the watchdog entry for 'breach_loop'
    has a positive last_beat after two iterations, and (b) the loop completed
    both iterations despite the market being closed (i.e. beats on closed ticks).
    """
    sleep_calls: list[float] = []
    call_count = {"n": 0}
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        call_count["n"] += 1
        # Cancel after 2 iterations so the loop is bounded.
        if call_count["n"] >= 2:
            await real_sleep(0)
            raise asyncio.CancelledError
        await real_sleep(0)

    import time

    monotonic_calls: list[float] = []
    _base = time.monotonic()

    def _fake_monotonic() -> float:
        t = _base + len(monotonic_calls) * 0.1
        monotonic_calls.append(t)
        return t

    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    async def _immediate(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
        return None

    async def _emergency(_result: BreachLoopResult) -> None:
        return None

    async def _sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    config = _config(cadence_s=30)
    session = _session()

    supervisor = MonitorSupervisor(
        session=session,
        config=config,
        sleep=_fake_sleep,
        monotonic=_fake_monotonic,
    )

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            session,
            config,
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=False),  # closed → beats still fire
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=lambda: supervisor.supervised_loop(
                "breach_loop", float(config.breach_evaluation_cadence_seconds)
            ),
        )

    # The watchdog entry for breach_loop must exist and have been beaten.
    entry = supervisor._watch.get("breach_loop")
    assert entry is not None, "breach_loop was not registered in the watchdog"
    assert entry.last_beat is not None, "breach_loop never beat the watchdog"
    # The bound must be cadence * multiplier (30 * 10.0 = 300s).
    assert entry.bound_seconds == pytest.approx(300.0)
    # The loop must have slept at the cadence (proves pacing is driven by supervised_loop).
    assert sleep_calls, "supervised_loop never slept — beat + pacing not driven"
    assert sleep_calls[0] == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_halt_onset_emits_halt_activated_then_persists_silent() -> None:
    """Drawdown crossing the daily limit triggers HALT_ACTIVATED on tick 1;
    persistence at the limit on tick 2 emits no event."""
    # Two ticks: tick 1 → daily drawdown 5.0 (==limit 5.0) → halt active.
    # Tick 2 → still 5.0 → no event.
    repo = _StubRepository(
        drawdown_states=[
            _drawdown_state(intraday_pct=5.0),
            _drawdown_state(intraday_pct=5.0),
        ],
        active_risk_parameters=_active_risk_parameters(daily_limit=5.0),
    )

    activity_log = _CallableRecord()

    async def _sink(entries: Iterable[ActivityLogEntry]) -> None:
        activity_log.calls.append((tuple(entries),))

    async def _immediate(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
        return None

    async def _emergency(_result: BreachLoopResult) -> None:
        return None

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_make_counted_loop(2),
        )

    # Tick 1 should emit one HALT_ACTIVATED; tick 2 should emit zero entries.
    entries_per_tick = [calls[0] for calls in activity_log.calls]
    # The sink is called every tick (even when entries are empty) so we can
    # observe both: tick 1 → 1 entry; tick 2 → 0 entries.
    halt_activated_tick = [e for e in entries_per_tick[0]]
    assert len(halt_activated_tick) == 1
    assert halt_activated_tick[0].event_type is EventType.HALT_ACTIVATED
    # ``mon-alp-`` prefix groups breach-loop halt entries within the
    # continuous-monitor ``mon-`` vocabulary.
    assert halt_activated_tick[0].entry_id.startswith("mon-alp-")
    persistence_entries = [e for e in entries_per_tick[1]]
    assert persistence_entries == []


@pytest.mark.asyncio
async def test_cumulative_tier3_halt_onset_and_lift() -> None:
    """Cumulative drawdown crossing the tier-3 threshold triggers HALT_ACTIVATED
    (halt_type=cumulative_drawdown_tier3); a subsequent recovery emits HALT_LIFTED."""
    tiers = _progressive_tiers()
    repo = _StubRepository(
        drawdown_states=[
            _drawdown_state(intraday_pct=0.0, cumulative_pct=13.0),  # tier3 → halt
            _drawdown_state(intraday_pct=0.0, cumulative_pct=5.0),  # below → lift
        ],
        active_risk_parameters=_active_risk_parameters(daily_limit=5.0),
    )

    activity_log = _CallableRecord()

    async def _sink(entries: Iterable[ActivityLogEntry]) -> None:
        activity_log.calls.append((tuple(entries),))

    async def _immediate(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
        return None

    async def _emergency(_result: BreachLoopResult) -> None:
        return None

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=tiers,
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_make_counted_loop(2),
        )

    # Tick 1 must include HALT_ACTIVATED with halt_type cumulative_drawdown_tier3.
    tick_1 = activity_log.calls[0][0]
    assert any(
        e.event_type is EventType.HALT_ACTIVATED
        and e.detail.halt_type == "cumulative_drawdown_tier3"
        for e in tick_1
    )
    # Tick 2 must include HALT_LIFTED with halt_type cumulative_drawdown_tier3.
    tick_2 = activity_log.calls[1][0]
    assert any(
        e.event_type is EventType.HALT_LIFTED and e.detail.halt_type == "cumulative_drawdown_tier3"
        for e in tick_2
    )


@pytest.mark.asyncio
async def test_cancellation_propagates_cleanly() -> None:
    """``asyncio.CancelledError`` raised externally exits the loop cleanly."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )

    async def _immediate(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
        return None

    async def _emergency(_result: BreachLoopResult) -> None:
        return None

    async def _sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # Use a loop that never cancels on its own so we can test external cancel.
    async def _infinite_loop() -> AsyncIterator[None]:
        while True:
            yield
            await asyncio.sleep(3600)

    async def _go() -> None:
        await run_breach_loop(
            _session(),
            _config(cadence_s=3600),  # long cadence so the loop blocks on sleep
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            loop=_infinite_loop,
        )

    task = asyncio.create_task(_go())
    # Yield so the loop runs one full tick then blocks on the long sleep.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_determinism_identical_inputs_produce_identical_result() -> None:
    """Same inputs on consecutive ticks → identical ``BreachLoopResult.rule_evaluations``."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state(), _drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    emergency = _CallableRecord()

    async def _immediate(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
        return None

    async def _emergency(result: BreachLoopResult) -> None:
        emergency.calls.append((result,))

    async def _sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # Pin ``now`` so both ticks share an as_of.
    fixed = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=_sink,
            on_immediate_breach=_immediate,
            on_emergency_input=_emergency,
            now=lambda: fixed,
            loop=_make_counted_loop(2),
        )

    first = emergency.calls[0][0]
    second = emergency.calls[1][0]
    assert first.rule_evaluations == second.rule_evaluations


# ---------------------------------------------------------------------------
# Sustained-failure escalation (ALP-732 Gap 2)
# ---------------------------------------------------------------------------


def _noop_callbacks() -> tuple[
    Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]],
    Callable[[BreachLoopResult], Awaitable[None]],
    Callable[[Iterable[ActivityLogEntry]], Awaitable[None]],
]:
    async def _immediate(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
        return None

    async def _emergency(_result: BreachLoopResult) -> None:
        return None

    async def _sink(_entries: Iterable[ActivityLogEntry]) -> None:
        return None

    return _immediate, _emergency, _sink


@pytest.mark.asyncio
async def test_breach_loop_sustained_failure_alert_fires_then_clears() -> None:
    """N consecutive failed ticks fire one degraded signal; the first success clears it.

    Threshold 2; the snapshot provider raises on ticks 1-3 then succeeds on
    tick 4. Expected health signals: degraded (on the 2nd failure, fired once)
    then recovered (on the 4th tick's success)."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    snapshot_calls = {"n": 0}

    async def _snapshot() -> LibrarySnapshot:
        snapshot_calls["n"] += 1
        if snapshot_calls["n"] <= 3:
            raise RuntimeError("snapshot assembly failed (simulated)")
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=2),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            loop=_make_counted_loop(4),
        )

    signals = [c[0] for c in health.calls]
    # Exactly one degraded then one recovered — the alert fires once on
    # crossing the threshold and does not re-fire on every subsequent failure.
    assert [s.degraded for s in signals] == [True, False]
    degraded = signals[0]
    assert degraded.consecutive_failures == 2
    assert degraded.last_error is not None
    assert "snapshot assembly failed" in degraded.last_error
    recovered = signals[1]
    assert recovered.degraded is False
    assert recovered.consecutive_failures == 3


@pytest.mark.asyncio
async def test_breach_loop_below_threshold_failures_do_not_fire_alert() -> None:
    """Failures that never reach the threshold raise no health signal."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    snapshot_calls = {"n": 0}

    async def _snapshot() -> LibrarySnapshot:
        snapshot_calls["n"] += 1
        # Tick 1 fails, ticks 2-3 succeed — never 3 in a row at threshold 3.
        if snapshot_calls["n"] == 1:
            raise RuntimeError("transient blip")
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=3),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            loop=_make_counted_loop(3),
        )

    assert health.calls == []


@pytest.mark.asyncio
async def test_breach_loop_faulty_health_sink_does_not_kill_loop() -> None:
    """A health sink that raises must not crash the loop — resilience is the point."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()

    def _on_health(_signal: BreachLoopHealthSignal) -> None:
        raise RuntimeError("alert sink is down")

    async def _snapshot() -> LibrarySnapshot:
        raise RuntimeError("snapshot assembly failed (simulated)")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # The loop must keep ticking (and the fake loop iterator still runs) despite
    # the sink raising on every degraded emit.
    iterations = {"n": 0}

    async def _counting_loop() -> AsyncIterator[None]:
        for _ in range(3):
            iterations["n"] += 1
            yield
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            loop=_counting_loop,
        )

    assert iterations["n"] == 3


# ---------------------------------------------------------------------------
# ALP-770 — per-position freshness gate + stale-price escalation
# ---------------------------------------------------------------------------


def _library_snapshot_with_position(ticker: str) -> LibrarySnapshot:
    """Snapshot with one open equity position on *ticker* so the freshness gate
    detects it as an expected ticker."""
    ep = ExistingPosition(
        position_id=f"pos-{ticker}",
        underlying=ticker,
        sector="tech",
        direction=Direction.LONG,
        asset_type=AssetType.EQUITY,
        notional_usd=10_000.0,
        delta_adjusted_exposure_usd=10_000.0,
        current_greeks=None,
        daily_borrow_cost_usd=None,
        reserves_capital_usd=0.0,
    )
    return LibrarySnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=90_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType({"tech": 10.0}),
        net_long_pct=10.0,
        net_short_pct=0.0,
        gross_pct=10.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=10.0,
        existing_positions=MappingProxyType({ep.position_id: ep}),
    )


class _SwappableCache:
    """Proxy cache that rotates through a list of real caches across ticks.

    Exposes ``read_all``, ``get_all``, ``global_staleness``, and ``update``
    so the breach-loop can call all its cache methods.  The ``_i`` index is
    advanced by the test's loop iterator after each yield.
    """

    def __init__(self, delegates: list[UnderlyingPriceCache]) -> None:
        self._delegates = delegates
        self._i = 0

    def read_all(self, tickers: Any, *, as_of: datetime, max_age_seconds: float) -> Any:
        return self._delegates[self._i].read_all(
            tickers, as_of=as_of, max_age_seconds=max_age_seconds
        )

    def get_all(self) -> Any:
        return self._delegates[self._i].get_all()

    def global_staleness(
        self, *, as_of: datetime, max_age_seconds: float, expected_tickers: Any
    ) -> Any:
        return self._delegates[self._i].global_staleness(
            as_of=as_of,
            max_age_seconds=max_age_seconds,
            expected_tickers=expected_tickers,
        )

    async def update(self, _quote: UnderlyingQuote) -> None:
        return None


def _stale_cache(ticker: str, *, price: float, age_seconds: float) -> UnderlyingPriceCache:
    """Cache with one quote whose as_of is ``age_seconds`` before the tick time used
    in these tests (2026-05-11T14:30:00Z)."""
    tick_time = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
    quote_time = tick_time - timedelta(seconds=age_seconds)
    cache = UnderlyingPriceCache()
    # Bypass async update — safe here as there is no concurrent access in test setup.
    cache._quotes[ticker] = UnderlyingQuote(ticker=Symbol(ticker), price=price, as_of=quote_time)
    return cache


@pytest.mark.asyncio
async def test_stale_price_escalates_to_degraded_after_n_cycles() -> None:
    """A stale price for an open position fires DEGRADED after N consecutive cycles.

    threshold=1; IBM quote is 2h old (7200s >> 60s underlying_price_max_age_seconds);
    the first successful tick returns had_stale=True → stale_consecutive_count=1
    >= threshold → DEGRADED emitted exactly once.
    """
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot_with_position("IBM")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # IBM price is 2 hours old; underlying_price_max_age_seconds=60 → stale
    cache = _stale_cache("IBM", price=200.0, age_seconds=7200.0)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1, underlying_price_max_age_seconds=60.0),
            repository=cast(PortfolioStateRepository, repo),
            cache=cache,
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            now=lambda: datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            loop=_make_counted_loop(1),
        )

    # One DEGRADED signal fired after 1 stale cycle (threshold=1).
    # (May also include a global-stale signal since only stale/missing entries.)
    degraded_signals = [c[0] for c in health.calls if c[0].degraded]
    stale_degraded = [
        s for s in degraded_signals if s.last_error == "stale/missing underlying price"
    ]
    # Exactly one per-ticker DEGRADED (fires-once debounce, ALP-770); the global-stale
    # signal co-fires on this all-stale tick under a distinct last_error and is excluded.
    assert len(stale_degraded) == 1
    signal = stale_degraded[0]
    assert signal.degraded is True
    assert signal.consecutive_failures == 1
    assert signal.last_error is not None


@pytest.mark.asyncio
async def test_missing_price_escalates_to_degraded_after_n_cycles() -> None:
    """An open position with no cache entry at all fires DEGRADED (treated as missing).

    threshold=1; IBM has an open position but is absent from the cache.
    """
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot_with_position("IBM")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1, underlying_price_max_age_seconds=60.0),
            repository=cast(PortfolioStateRepository, repo),
            cache=_cache(),  # empty — IBM absent
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            now=lambda: datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            loop=_make_counted_loop(1),
        )

    degraded_calls = [c[0] for c in health.calls if c[0].degraded]
    stale_degraded = [s for s in degraded_calls if s.last_error == "stale/missing underlying price"]
    # Exactly one per-ticker DEGRADED for the missing-as-stale escalation (fires-once
    # debounce, ALP-770); the global-stale signal co-fires under a distinct last_error.
    assert len(stale_degraded) == 1


@pytest.mark.asyncio
async def test_stale_price_escalation_recovers_when_fresh() -> None:
    """After a stale-price DEGRADED escalation, a fresh price emits RECOVERED.

    threshold=1; tick 1 has a stale IBM price → DEGRADED; tick 2 has a fresh
    IBM price → RECOVERED; tick 3 is also fresh → no further signal.
    """
    repo = _StubRepository(
        drawdown_states=[_drawdown_state(), _drawdown_state(), _drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    tick_time = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot_with_position("IBM")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # Tick 1: stale (2 h old quote); ticks 2+: fresh (5 s old).
    stale_c = _stale_cache("IBM", price=200.0, age_seconds=7200.0)
    fresh_c = _stale_cache("IBM", price=200.0, age_seconds=5.0)
    swappable: Any = _SwappableCache([stale_c, fresh_c, fresh_c])

    # Use a loop that advances the swappable cache index after each tick.
    async def _counted_swap_loop() -> AsyncIterator[None]:
        for _ in range(3):
            yield
            swappable._i = min(swappable._i + 1, 2)
            await asyncio.sleep(0)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1, underlying_price_max_age_seconds=60.0),
            repository=cast(PortfolioStateRepository, repo),
            cache=cast(Any, swappable),
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            now=lambda: tick_time,
            loop=_counted_swap_loop,
        )

    # Tick 1 stale → DEGRADED; tick 2 fresh → RECOVERED; tick 3 fresh → none.
    stale_error = "stale/missing underlying price"
    degraded = [c[0] for c in health.calls if c[0].degraded and c[0].last_error == stale_error]
    recovered = [c[0] for c in health.calls if not c[0].degraded]
    assert len(degraded) == 1  # DEGRADED on tick 1
    assert len(recovered) == 1  # RECOVERED on tick 2


@pytest.mark.asyncio
async def test_fresh_price_no_escalation() -> None:
    """A fresh price for an open position never triggers the stale-price DEGRADED path."""
    repo = _StubRepository(
        drawdown_states=[_drawdown_state(), _drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot_with_position("IBM")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # IBM quote is 5 s old; underlying_price_max_age_seconds=60 → fresh
    cache = _stale_cache("IBM", price=200.0, age_seconds=5.0)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1, underlying_price_max_age_seconds=60.0),
            repository=cast(PortfolioStateRepository, repo),
            cache=cache,
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            now=lambda: datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            loop=_make_counted_loop(2),
        )

    # No stale/missing health signals: price is fresh the whole time.
    stale_signals = [
        c[0] for c in health.calls if c[0].last_error == "stale/missing underlying price"
    ]
    assert stale_signals == []


# ---------------------------------------------------------------------------
# ALP-831 — new ACs: config-driven threshold, global-stale emitter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_reads_underlying_price_max_age_from_config() -> None:
    """ALP-831 AC 2: the loop reads config.underlying_price_max_age_seconds.

    A quote 70s old is stale at threshold=60s and fresh at threshold=900s.
    The config fixture controls which branch fires without any local default
    parameter — there is no local max_price_age_seconds to pass.
    """
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health_60 = _CallableRecord()
    health_900 = _CallableRecord()

    def _on_health_60(signal: BreachLoopHealthSignal) -> None:
        health_60.calls.append((signal,))

    def _on_health_900(signal: BreachLoopHealthSignal) -> None:
        health_900.calls.append((signal,))

    async def _snapshot() -> LibrarySnapshot:
        return _library_snapshot_with_position("AAPL")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    # AAPL quote is 70 s old.
    tick_time = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
    cache = _stale_cache("AAPL", price=180.0, age_seconds=70.0)

    # --- With threshold 60s: 70s > 60s → stale → DEGRADED after 1 cycle ---
    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1, underlying_price_max_age_seconds=60.0),
            repository=cast(PortfolioStateRepository, repo),
            cache=cache,
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health_60,
            now=lambda: tick_time,
            loop=_make_counted_loop(1),
        )

    stale_signals_60 = [
        c[0] for c in health_60.calls if c[0].last_error == "stale/missing underlying price"
    ]
    assert len(stale_signals_60) == 1, "expected DEGRADED at 60s threshold"

    # --- With threshold 900s: 70s < 900s → fresh → no stale escalation ---
    repo2 = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=1, underlying_price_max_age_seconds=900.0),
            repository=cast(PortfolioStateRepository, repo2),
            cache=cache,
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health_900,
            now=lambda: tick_time,
            loop=_make_counted_loop(1),
        )

    stale_signals_900 = [
        c[0] for c in health_900.calls if c[0].last_error == "stale/missing underlying price"
    ]
    assert stale_signals_900 == [], "expected no DEGRADED at 900s threshold for 70s old quote"


@pytest.mark.asyncio
async def test_global_stale_emits_distinct_last_error() -> None:
    """ALP-831 AC 4: when all expected tickers are stale/missing, the emitted
    health signal carries last_error == 'underlying price feed globally stale —
    writer wedged', separable from the per-ticker-lag escalation string.
    """
    repo = _StubRepository(
        drawdown_states=[_drawdown_state()],
        active_risk_parameters=_active_risk_parameters(),
    )
    immediate, emergency, sink = _noop_callbacks()
    health = _CallableRecord()

    def _on_health(signal: BreachLoopHealthSignal) -> None:
        health.calls.append((signal,))

    async def _snapshot() -> LibrarySnapshot:
        # One open position on IBM.
        return _library_snapshot_with_position("IBM")

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    tick_time = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
    # IBM quote is 2 h old (stale); cache otherwise empty → whole feed cold.
    cache = _stale_cache("IBM", price=200.0, age_seconds=7200.0)

    with pytest.raises(asyncio.CancelledError):
        await run_breach_loop(
            _session(),
            _config(failure_threshold=5, underlying_price_max_age_seconds=60.0),
            repository=cast(PortfolioStateRepository, repo),
            cache=cache,
            snapshot_provider=_snapshot,
            regime_provider=_regime,
            progressive_tiers=_progressive_tiers(),
            library_config_factory=_library_config,
            iv_provider=_FixtureIvProvider(),
            risk_free_rate=0.045,
            breach_response_lookup=_breach_response_lookup(),
            market_hours=_StubMarketHours(open_flag=True),
            activity_log_sink=sink,
            on_immediate_breach=immediate,
            on_emergency_input=emergency,
            on_health_signal=_on_health,
            now=lambda: tick_time,
            loop=_make_counted_loop(1),
        )

    # The global-stale signal must appear with the distinct error string.
    global_stale_signals = [
        c[0]
        for c in health.calls
        if c[0].last_error == "underlying price feed globally stale — writer wedged"
    ]
    assert len(global_stale_signals) >= 1
    assert global_stale_signals[0].degraded is True
