"""Shared fixtures for cascade-dispatch tests.

Library Protocol stubs (``ScriptedLibrary``, ``StubRuleProjection``, etc.)
re-export from ``tests.risk_guardrails.breach_behavior.fixtures`` — the
canonical home for cascade-orchestration test fakes. The local additions are
the recorders (``RecordingSubmit``, ``RecordingDeferralSink``), the PositionView
builder (``equity_view``), and the breach-loop builders that the dispatcher
tests consume.
"""

from __future__ import annotations

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
from tests.risk_guardrails.breach_behavior.fixtures import (
    ScriptedLibrary,
    StubLibraryConfig,
    StubLibraryOutput,
    StubMarketInputs,
    StubPortfolioState,
    StubRuleProjection,
    make_active_risk_parameters,
)

__all__ = [
    "NOW",
    "RecordingDeferralSink",
    "RecordingSubmit",
    "ScriptedLibrary",
    "StubLibraryConfig",
    "StubLibraryOutput",
    "StubMarketInputs",
    "StubPortfolioState",
    "StubRuleProjection",
    "equity_view",
    "make_breach_config",
    "make_breach_loop_result",
]

NOW = datetime(2026, 5, 17, 14, 30, 0, tzinfo=UTC)


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
    """Build an equity ``PositionView`` with cost-derived market value.

    ``current_market_value_usd`` is derived as ``cost_basis * share_count +
    unrealized_pnl_usd``; tests that need a specific market value should
    choose ``unrealized_pnl_usd`` accordingly. Short positions automatically
    receive locate / borrow-rate / margin defaults.
    """
    short_fields: dict[str, Any] = (
        {
            "borrow_rate_pct": 0.5,
            "accrued_borrow_cost_usd": 0.0,
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


def make_breach_loop_result(*, evaluations: tuple[RuleEvaluation, ...]) -> BreachLoopResult:
    immediate = tuple(e for e in evaluations if e.classification is BreachResponse.immediate_engine)
    phase1_result = Phase1EnforcementResult(
        active_risk_parameters=make_active_risk_parameters(
            regime=RegimeLabel.ELEVATED,
            rule_values={},
        ),
        drawdown_tier=DrawdownTier.CONSTRAINED,
    )
    return BreachLoopResult(
        as_of=NOW,
        phase1_result=phase1_result,
        rule_evaluations=evaluations,
        halt_state=None,
        immediate_action_breaches=immediate,
        drawdown_velocity_sample=DrawdownSample(
            sampled_at=NOW,
            intraday_drawdown_pct=1.0,
        ),
    )
