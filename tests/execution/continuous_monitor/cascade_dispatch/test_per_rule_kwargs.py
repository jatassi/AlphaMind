"""Tests for the per-rule kwargs providers factory.

The factory returns one :data:`PerRuleKwargsProvider` per ``immediate_engine``
rule declared in ``config/guardrails.yaml``. Each provider extracts the
selector's keyword arguments from the per-tick ``BreachDispatchContext`` and
the firing ``RuleEvaluation`` so the cascade dispatcher can route immediate
breaches through ``selector_for(rule_id)`` without any caller-specific glue.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import alphamind.decision.portfolio_manager.models  # noqa: F401  # break OMS ↔ PM import cycle
from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import RegimeTransitionState
from alphamind.commands.engine_envelope import EngineEnvelope as OmsEngineEnvelope
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.guardrails import BreachResponse, GuardrailsConfig
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    SubmissionResult,
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
from alphamind.execution.continuous_monitor.cascade_dispatch.per_rule_kwargs import (
    build_per_rule_kwargs_providers,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.selectors import (
    RULE_SELECTOR_DISPATCH,
)
from alphamind.execution.guardrail_enforcement.orchestrator import (
    Phase1EnforcementResult,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    DrawdownSample,
    DrawdownTier,
    PositionLiquidity,
    PositionRiskReward,
    RegimeLabel,
    RiskZone,
)

_NOW = datetime(2026, 5, 17, 14, 30, 0, tzinfo=UTC)
_SESSION_ID = "monsession-x"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GUARDRAILS_YAML = _REPO_ROOT / "config" / "guardrails.yaml"


# ---------------------------------------------------------------------------
# PositionView builders — minimal fixtures wired through the canonical record
# constructor so the dispatcher consumes the same shape it sees in production.
# ---------------------------------------------------------------------------


def _equity_view(
    *,
    position_id: str,
    ticker: str = "NVDA",
    direction: Direction = Direction.LONG,
    cost_basis: float = 150.0,
    share_count: float = 10.0,
    unrealized_pnl_usd: float = -52.5,
    position_weight_pct: float = 10.0,
) -> PositionView:
    short_fields: dict[str, Any] = (
        {
            "borrow_rate_pct": 0.5,
            "locate_status": LocateStatus.LOCATED,
            "margin_held_usd": 750.0,
        }
        if direction is Direction.SHORT
        else {}
    )
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=cost_basis,
        **short_fields,
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
                fill_price=price(cost_basis),
                fill_quantity=share_count,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    sign = 1.0 if direction == Direction.LONG else -1.0
    cost = cost_basis * share_count
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(cost + unrealized_pnl_usd),
        unrealized_pnl_usd=signed_money(unrealized_pnl_usd),
        unrealized_pnl_pct=unrealized_pnl_usd / cost * 100.0,
        position_weight_pct=position_weight_pct * sign,
        position_age_hours=2.0,
        notional_exposure_usd=money(cost),
        delta_adjusted_exposure_usd=signed_money(cost * sign),
        distance_to_target_usd=signed_money(10.0),
        distance_to_stop_usd=signed_money(5.0),
        risk_reward_at_current=2.0,
    )


def _options_view(
    *,
    position_id: str,
    underlying: str = "SPY",
    unrealized_pnl_usd: float = -80.0,
    cost_per_contract: float = 200.0,
    contract_count: int = 2,
    position_weight_pct: float = 5.0,
) -> PositionView:
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(underlying),
        contract_type=OptionContractType.CALL,
        strike_price=450.0,
        expiration_date=datetime(2026, 6, 19, tzinfo=UTC).date(),
        contract_count=contract_count,
        contract_multiplier=100.0,
        premium_paid_per_contract=cost_per_contract,
        greeks=OptionGreeks(delta=0.5, gamma=0.01, theta=-0.05, vega=0.10),
    )
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(f"THE-{position_id}"),
        bracket_id=BracketId(f"BRK-{position_id}"),
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW,
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(cost_per_contract),
                fill_quantity=contract_count,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    cost = cost_per_contract * contract_count
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(cost + unrealized_pnl_usd),
        unrealized_pnl_usd=signed_money(unrealized_pnl_usd),
        unrealized_pnl_pct=unrealized_pnl_usd / cost * 100.0,
        position_weight_pct=position_weight_pct,
        position_age_hours=4.0,
        notional_exposure_usd=money(cost),
        delta_adjusted_exposure_usd=signed_money(cost * 0.5),
        distance_to_target_usd=signed_money(20.0),
        distance_to_stop_usd=signed_money(10.0),
        risk_reward_at_current=2.0,
    )


# ---------------------------------------------------------------------------
# BreachDispatchContext + Library + dispatcher stubs
# ---------------------------------------------------------------------------


def _make_breach_config() -> BreachBehaviorConfig:
    return BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=5,
        drawdown_velocity_threshold_pct_of_daily_limit=50.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=5,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )


@dataclass(frozen=True)
class _StubLibraryOutput:
    per_rule: tuple[Any, ...]


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
class _ScriptedLibrary:
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
        self.calls.append({"state": state, "proposals": tuple(proposals)})
        idx = min(len(self.calls) - 1, len(self.outputs) - 1)
        return self.outputs[idx]


@dataclass
class _RecordingSubmit:
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


def _make_context(positions: tuple[PositionView, ...]) -> BreachDispatchContext:
    liquidity = tuple(
        PositionLiquidity(position_id=p.position_id, adv_to_position_size_ratio=10.0)
        for p in positions
    )
    risk_reward = tuple(
        PositionRiskReward(position_id=p.position_id, risk_reward_ratio=2.0) for p in positions
    )
    library = _ScriptedLibrary(
        outputs=[
            _StubLibraryOutput(per_rule=()),
            _StubLibraryOutput(per_rule=()),
        ]
    )
    return BreachDispatchContext(
        open_positions=positions,
        liquidity=liquidity,
        risk_reward_metric=risk_reward,
        library_snapshot=_StubPortfolioState(),
        library_config=_StubLibraryConfig(
            effective_limits={
                "position_max_loss_equity_pct": 1.0,
                "position_max_loss_options_pct": 1.0,
                "daily_drawdown_pct": 1.0,
                "cumulative_drawdown_pct": 1.0,
                "single_short_max_pct": 1.0,
            }
        ),
        market_inputs=_StubMarketInputs(),
        evaluate_proposals=library,
        portfolio_value_usd=100_000.0,
        active_regime=RegimeLabel.ELEVATED,
        progressive_tiers=(),
        breach_classification={},
    )


def _make_phase1_result() -> Phase1EnforcementResult:
    return Phase1EnforcementResult(
        active_risk_parameters=ActiveRiskParameterSet(
            regime_label=RegimeLabel.ELEVATED,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=(),
            active_overlays=(),
        ),
        drawdown_tier=DrawdownTier.CONSTRAINED,
    )


def _make_breach_loop_result(rule: RuleEvaluation) -> BreachLoopResult:
    return BreachLoopResult(
        as_of=_NOW,
        phase1_result=_make_phase1_result(),
        rule_evaluations=(rule,),
        halt_state=None,
        immediate_action_breaches=(rule,),
        drawdown_velocity_sample=DrawdownSample(
            sampled_at=_NOW,
            intraday_drawdown_pct=1.0,
        ),
    )


def _evaluation(*, rule_id: str, current: float, limit: float) -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=rule_id,
        current_value=current,
        limit_value=limit,
        overage=abs(current - limit),
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
    )


# ---------------------------------------------------------------------------
# Factory shape — keys cover every immediate_engine rule in guardrails.yaml.
# ---------------------------------------------------------------------------


def test_factory_returns_provider_per_immediate_engine_rule_in_guardrails() -> None:
    """Every ``immediate_engine`` rule in ``guardrails.yaml`` has a registered provider."""
    guardrails = GuardrailsConfig.model_validate(read_yaml_file(_GUARDRAILS_YAML))
    expected = {
        rule.id
        for rule in guardrails.rules
        if rule.breach_response is BreachResponse.immediate_engine
    }
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    assert set(providers.keys()) == expected


def test_factory_keys_match_dispatch_table_exactly() -> None:
    """The provider set and the dispatch-table key set are equal.

    Renaming one without the other (the structural class of bug this PR
    fixes) is what this symmetric check catches in CI.
    """
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    assert set(providers.keys()) == set(RULE_SELECTOR_DISPATCH.keys())


# ---------------------------------------------------------------------------
# Per-provider kwargs shape
# ---------------------------------------------------------------------------


def test_drawdown_provider_returns_open_positions_and_liquidity() -> None:
    """Drawdown providers feed ``select_for_drawdown_breach(open_positions, liquidity)``."""
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    positions = (_equity_view(position_id="POS-1", unrealized_pnl_usd=-300.0),)
    context = _make_context(positions)

    for rule_id in ("daily_drawdown_pct", "cumulative_drawdown_pct"):
        provider = providers[rule_id]
        rule = _evaluation(rule_id=rule_id, current=-6.0, limit=-5.0)
        kwargs = provider(rule, context)
        assert kwargs == {"open_positions": positions, "liquidity": context.liquidity}


def test_position_max_loss_equity_provider_picks_breaching_equity() -> None:
    """``position_max_loss_equity_pct`` provider selects the worst equity over the limit.

    Limits are positive-magnitude percentages (``3.0`` for a 3% max loss);
    ``unrealized_pnl_pct`` is signed.
    """
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    breaching = _equity_view(position_id="POS-1", unrealized_pnl_usd=-60.0)  # -4.0% on $1500 cost
    healthy = _equity_view(position_id="POS-2", unrealized_pnl_usd=-15.0)  # -1.0%
    context = _make_context((breaching, healthy))

    rule = _evaluation(rule_id="position_max_loss_equity_pct", current=4.0, limit=3.0)
    kwargs = providers["position_max_loss_equity_pct"](rule, context)

    assert kwargs["breaching_position_id"] == "POS-1"
    assert kwargs["open_positions"] == (breaching, healthy)
    assert kwargs["loss_pct"] == 4.0
    assert kwargs["limit_pct"] == 3.0


def test_position_max_loss_options_provider_picks_breaching_option() -> None:
    """``position_max_loss_options_pct`` ignores equities and picks the worst option."""
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    equity_loser = _equity_view(position_id="POS-EQ-1", unrealized_pnl_usd=-200.0)
    breaching_option = _options_view(position_id="POS-OPT-1", unrealized_pnl_usd=-80.0)
    context = _make_context((equity_loser, breaching_option))

    rule = _evaluation(rule_id="position_max_loss_options_pct", current=20.0, limit=15.0)
    kwargs = providers["position_max_loss_options_pct"](rule, context)

    assert kwargs["breaching_position_id"] == "POS-OPT-1"


def test_position_max_loss_provider_raises_when_no_position_breaches() -> None:
    """The provider raises a structural error if no in-class position is below the limit."""
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    positions = (_equity_view(position_id="POS-1", unrealized_pnl_usd=-15.0),)  # -1.0%
    context = _make_context(positions)

    rule = _evaluation(rule_id="position_max_loss_equity_pct", current=3.5, limit=3.0)
    with pytest.raises(ValueError, match="position_max_loss_equity_pct"):
        providers["position_max_loss_equity_pct"](rule, context)


def test_single_short_max_provider_passes_breaching_short_limit_and_config() -> None:
    """``single_short_max_pct`` routes the breaching short + limit + config to the selector."""
    breach_config = _make_breach_config()
    providers = build_per_rule_kwargs_providers(breach_behavior_config=breach_config)
    long_eq = _equity_view(position_id="POS-LONG-1", position_weight_pct=2.0)
    big_short = _equity_view(
        position_id="POS-SHORT-BIG",
        direction=Direction.SHORT,
        position_weight_pct=4.0,
    )
    small_short = _equity_view(
        position_id="POS-SHORT-SMALL",
        direction=Direction.SHORT,
        position_weight_pct=2.5,
    )
    context = _make_context((long_eq, big_short, small_short))

    rule = _evaluation(rule_id="single_short_max_pct", current=4.0, limit=3.0)
    kwargs = providers["single_short_max_pct"](rule, context)

    assert kwargs["breaching_position_id"] == "POS-SHORT-BIG"
    assert kwargs["open_positions"] == (long_eq, big_short, small_short)
    assert kwargs["single_short_max_pct_of_portfolio"] == 3.0
    assert kwargs["config"] is breach_config


def test_single_short_max_provider_raises_when_no_short_breaches() -> None:
    """The provider raises when no short exceeds the per-position cap."""
    providers = build_per_rule_kwargs_providers(breach_behavior_config=_make_breach_config())
    small_short = _equity_view(
        position_id="POS-SHORT-1",
        direction=Direction.SHORT,
        position_weight_pct=2.0,
    )
    context = _make_context((small_short,))

    rule = _evaluation(rule_id="single_short_max_pct", current=2.0, limit=3.0)
    with pytest.raises(ValueError, match="single_short_max_pct"):
        providers["single_short_max_pct"](rule, context)


# ---------------------------------------------------------------------------
# Integration — dispatcher constructed with this map handles one immediate
# breach per registered rule without raising ``no per-rule kwargs provider``.
# ---------------------------------------------------------------------------


async def test_dispatcher_with_built_providers_handles_one_breach_per_registered_rule() -> None:
    """The dispatcher built with ``build_per_rule_kwargs_providers`` handles one
    immediate breach per registered rule_id end-to-end, submitting exactly one
    envelope per scenario whose ``position_id`` matches the breaching position
    the provider selected.

    Without the envelope assertion this test would pass even if every provider
    built nonsensical kwargs, as long as nothing raised — so each scenario
    pins the submitted envelope's ``position_id`` to the only breaching
    position in its context.
    """
    breach_config = _make_breach_config()
    providers = build_per_rule_kwargs_providers(breach_behavior_config=breach_config)
    equity_max_loss = _equity_view(position_id="POS-EQ", unrealized_pnl_usd=-60.0)
    options_max_loss = _options_view(position_id="POS-OPT", unrealized_pnl_usd=-80.0)
    daily_loser = _equity_view(position_id="POS-DD", unrealized_pnl_usd=-100.0)
    cumulative_loser = _equity_view(position_id="POS-CD", unrealized_pnl_usd=-100.0)
    oversized_short = _equity_view(
        position_id="POS-SH", direction=Direction.SHORT, position_weight_pct=4.0
    )
    scenarios: dict[str, tuple[tuple[PositionView, ...], tuple[float, float], str]] = {
        "position_max_loss_equity_pct": ((equity_max_loss,), (4.0, 3.0), "POS-EQ"),
        "position_max_loss_options_pct": ((options_max_loss,), (20.0, 15.0), "POS-OPT"),
        "daily_drawdown_pct": ((daily_loser,), (-6.0, -5.0), "POS-DD"),
        "cumulative_drawdown_pct": ((cumulative_loser,), (-9.0, -8.0), "POS-CD"),
        "single_short_max_pct": ((oversized_short,), (4.0, 3.0), "POS-SH"),
    }
    assert set(scenarios.keys()) == set(providers.keys())
    for rule_id, (positions, (current, limit), expected_position_id) in scenarios.items():
        context = _make_context(positions)
        rule = _evaluation(rule_id=rule_id, current=current, limit=limit)
        submit = _RecordingSubmit()
        deferral_sink = _RecordingDeferralSink()

        def _context_provider(ctx: BreachDispatchContext = context) -> BreachDispatchContext:
            return ctx

        dispatcher = CascadeDispatcher(
            monitor_session_id=_SESSION_ID,
            breach_config=breach_config,
            trigger_ids=TriggerIdGenerator(session_id=_SESSION_ID),
            context_provider=_context_provider,
            submit_envelope=submit,
            deferral_sink=deferral_sink,
            per_rule_kwargs_providers=providers,
            now=lambda: _NOW,
        )
        result = _make_breach_loop_result(rule)
        await dispatcher.handle_immediate_breach(result, rule)

        assert deferral_sink.calls == [], (
            f"{rule_id}: dispatcher unexpectedly deferred to PM "
            f"({deferral_sink.calls[0].reason if deferral_sink.calls else ''})"
        )
        assert len(submit.calls) == 1, f"{rule_id}: expected 1 envelope, got {len(submit.calls)}"
        assert submit.calls[0].commands[0].position_id == expected_position_id
