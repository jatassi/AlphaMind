"""Shape tests for ``BreachLoopResult`` and ``RuleEvaluation`` (story 03b)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.guardrail_enforcement import Phase1EnforcementResult
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.risk_guardrails.breach_behavior import (
    DrawdownSample,
    HaltState,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)


def _active_risk_parameters() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
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


def test_rule_evaluation_is_frozen_and_carries_required_fields() -> None:
    evaluation = RuleEvaluation(
        rule_id="net_long_pct",
        current_value=72.0,
        limit_value=80.0,
        overage=-8.0,
        zone=RiskZone.WARNING,
        classification=None,
    )
    assert evaluation.rule_id == "net_long_pct"
    assert evaluation.current_value == 72.0
    assert evaluation.limit_value == 80.0
    assert evaluation.overage == -8.0
    assert evaluation.zone is RiskZone.WARNING
    assert evaluation.classification is None
    with pytest.raises(FrozenInstanceError):
        evaluation.rule_id = "other"  # type: ignore[misc]


def test_rule_evaluation_carries_classification_when_breaching() -> None:
    evaluation = RuleEvaluation(
        rule_id="position_max_loss_equity_pct",
        current_value=100.0,
        limit_value=95.0,
        overage=5.0,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
    )
    assert evaluation.classification is BreachResponse.immediate_engine


def test_breach_loop_result_is_frozen_and_immutable() -> None:
    phase1_result = Phase1EnforcementResult(
        active_risk_parameters=_active_risk_parameters(),
        drawdown_tier=None,
    )
    sample = DrawdownSample(
        sampled_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        intraday_drawdown_pct=2.5,
    )
    result = BreachLoopResult(
        as_of=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        phase1_result=phase1_result,
        rule_evaluations=(),
        halt_state=None,
        immediate_action_breaches=(),
        drawdown_velocity_sample=sample,
    )
    assert result.rule_evaluations == ()
    assert result.immediate_action_breaches == ()
    assert result.halt_state is None
    assert result.drawdown_velocity_sample is sample
    with pytest.raises(FrozenInstanceError):
        result.as_of = datetime(2026, 1, 1, tzinfo=UTC)  # type: ignore[misc]


def test_breach_loop_result_carries_halt_state_and_breaches() -> None:
    phase1_result = Phase1EnforcementResult(
        active_risk_parameters=_active_risk_parameters(),
        drawdown_tier=None,
    )
    halt = HaltState(
        daily_halt_active=True,
        cumulative_full_halt_active=False,
        daily_drawdown_pct=5.0,
        daily_drawdown_limit_pct=5.0,
    )
    breach = RuleEvaluation(
        rule_id="position_max_loss_equity_pct",
        current_value=100.0,
        limit_value=95.0,
        overage=5.0,
        zone=RiskZone.BLOCKED,
        classification=BreachResponse.immediate_engine,
    )
    sample = DrawdownSample(
        sampled_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        intraday_drawdown_pct=5.0,
    )
    result = BreachLoopResult(
        as_of=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        phase1_result=phase1_result,
        rule_evaluations=(breach,),
        halt_state=halt,
        immediate_action_breaches=(breach,),
        drawdown_velocity_sample=sample,
    )
    assert result.halt_state is halt
    assert result.immediate_action_breaches == (breach,)
    assert result.rule_evaluations == (breach,)
