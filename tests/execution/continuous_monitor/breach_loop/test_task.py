"""Tests for ``run_breach_loop`` (story 03b / ALP-437).

The loop is a run-forever coroutine; tests run it under a fake ``now``
sequence + a fake ``asyncio.sleep`` shim so the body executes deterministically
and cooperatively yields after each tick. We drive at most a handful of ticks
per test and cancel the task to verify ``CancelledError`` exits cleanly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.guardrails import BreachResponse, ProgressiveTier
from alphamind.execution.continuous_monitor.breach_loop import (
    BreachLoopResult,
    RuleEvaluation,
    run_breach_loop,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
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
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput
from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _config(cadence_s: int = 60) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=cadence_s,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        subscription_refresh_seconds=30,
        max_reconnect_attempts=3,
        supervisor_shutdown_timeout_seconds=5,
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
# Driver: run the loop for N ticks then cancel
# ---------------------------------------------------------------------------


async def _drive_loop(
    coro_factory: Callable[[], Awaitable[None]],
    *,
    ticks: int,
    tick_sentinels: list[int],
    sleep_calls: list[float],
    monkeypatch: pytest.MonkeyPatch,
    target_module: str,
) -> None:
    """Run *coro_factory* under a patched sleep that lets us cap iterations."""
    real_sleep = asyncio.sleep
    call_count = {"n": 0}

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        call_count["n"] += 1
        if call_count["n"] >= ticks:
            # Yield to give the loop a chance to read post-sleep, but ultimately
            # we cancel via the gather mechanism below.
            await real_sleep(0)
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(f"{target_module}.asyncio.sleep", _fake_sleep)
    tick_sentinels.clear()
    with pytest.raises(asyncio.CancelledError):
        await coro_factory()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_in_normal_zone_does_not_invoke_immediate_breach_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    sleep_calls: list[float] = []
    snapshot_provider_calls = {"n": 0}

    async def _snapshot() -> LibrarySnapshot:
        snapshot_provider_calls["n"] += 1
        return _library_snapshot()

    async def _regime() -> RegimeAdaptationOutput:
        return _regime_output()

    market_hours = _StubMarketHours(open_flag=True)

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=1,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
    )

    assert immediate.calls == []
    assert len(emergency.calls) == 1
    assert isinstance(emergency.calls[0][0], BreachLoopResult)
    assert sleep_calls == [60.0]


@pytest.mark.asyncio
async def test_immediate_action_hard_block_invokes_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    sleep_calls: list[float] = []
    market_hours = _StubMarketHours(open_flag=True)

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=1,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
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
async def test_deferred_breach_does_not_invoke_immediate_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    sleep_calls: list[float] = []

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=1,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
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
async def test_market_closed_pauses_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
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
    sleep_calls: list[float] = []

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=2,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
    )

    assert immediate.calls == []
    assert emergency.calls == []
    assert snapshot_calls["n"] == 0


@pytest.mark.asyncio
async def test_halt_onset_emits_halt_activated_then_persists_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    sleep_calls: list[float] = []

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=2,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
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
async def test_cumulative_tier3_halt_onset_and_lift(monkeypatch: pytest.MonkeyPatch) -> None:
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

    sleep_calls: list[float] = []

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=2,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
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
        )

    task = asyncio.create_task(_go())
    # Yield so the loop runs one full tick then blocks on the long sleep.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_determinism_identical_inputs_produce_identical_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    sleep_calls: list[float] = []

    async def _go() -> None:
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
        )

    await _drive_loop(
        _go,
        ticks=2,
        tick_sentinels=[],
        sleep_calls=sleep_calls,
        monkeypatch=monkeypatch,
        target_module="alphamind.execution.continuous_monitor.breach_loop.task",
    )

    first = emergency.calls[0][0]
    second = emergency.calls[1][0]
    assert first.rule_evaluations == second.rule_evaluations
