"""Shared fixtures for cascade-dispatch tests.

Centralizes the stubs, recorders, and builders used by ``test_dispatcher.py``
and ``test_per_rule_kwargs.py``. ``equity_view`` reconciles the two callers'
PositionView builders: cost-derived market value plus short-equity defaults
(``locate_status``, ``borrow_rate_pct``, ``margin_held_usd``). ``ScriptedLibrary``
keeps the broader ``(state, proposals, config, market, delta_buffer_factor)``
recording shape from ``test_dispatcher.py`` so dispatcher tests retain their
call-shape assertions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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
from alphamind.config.models.guardrails import BreachResponse
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    SubmissionResult,
)
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.cascade_dispatch.dispatcher import (
    DeferralEvent,
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
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    DrawdownSample,
    DrawdownTier,
    RegimeLabel,
)

NOW = datetime(2026, 5, 17, 14, 30, 0, tzinfo=UTC)


@dataclass(frozen=True)
class StubLibraryOutput:
    per_rule: tuple[Any, ...]


@dataclass(frozen=True)
class StubLibraryConfig:
    effective_limits: dict[str, float]


@dataclass(frozen=True)
class StubPortfolioState:
    label: str = "default"


@dataclass(frozen=True)
class StubMarketInputs:
    label: str = "default"


@dataclass
class ScriptedLibrary:
    """``evaluate_proposals`` stub — returns ``outputs[i]`` on the i-th call."""

    outputs: list[StubLibraryOutput]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        state: Any,
        proposals: Sequence[Any],
        config: Any,
        market: Any,
        delta_buffer_factor: float = 1.0,
    ) -> StubLibraryOutput:
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


@dataclass
class RecordingSubmit:
    """Stub for the OMS submit-engine-envelope callable."""

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
class RecordingDeferralSink:
    """Stub for the deferral-event callable the dispatcher emits on deferred_to_pm."""

    calls: list[DeferralEvent] = field(default_factory=list)

    async def __call__(self, event: DeferralEvent) -> None:
        self.calls.append(event)


def equity_view(
    *,
    position_id: str,
    ticker: str = "NVDA",
    direction: Direction = Direction.LONG,
    cost_basis: float = 150.0,
    share_count: float = 10.0,
    unrealized_pnl_usd: float = -52.5,
    position_weight_pct: float = 10.0,
) -> PositionView:
    """Build a PositionView for an equity position. Short defaults compose cleanly."""
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
        entry_timestamp=NOW,
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=NOW,
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


def make_breach_config() -> BreachBehaviorConfig:
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


def make_phase1_result() -> Phase1EnforcementResult:
    """Build a minimal Phase1EnforcementResult — fields most tests don't read."""
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


def make_breach_loop_result(*, evaluations: tuple[RuleEvaluation, ...]) -> BreachLoopResult:
    immediate = tuple(e for e in evaluations if e.classification is BreachResponse.immediate_engine)
    return BreachLoopResult(
        as_of=NOW,
        phase1_result=make_phase1_result(),
        rule_evaluations=evaluations,
        halt_state=None,
        immediate_action_breaches=immediate,
        drawdown_velocity_sample=DrawdownSample(
            sampled_at=NOW,
            intraday_drawdown_pct=1.0,
        ),
    )
