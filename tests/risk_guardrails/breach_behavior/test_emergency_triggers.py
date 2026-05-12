"""Tests for the breach-behavior emergency-invocation triggers (story 05c)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from alphamind._kernel.regime import RiskZone
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    DrawdownSample,
    EmergencyContext,
    EmergencyTrigger,
    MarginCallEvent,
    RegimeLabel,
    evaluate_daily_drawdown_velocity,
    evaluate_emergency_invocation,
    evaluate_margin_call,
    evaluate_multi_rule_breach,
    evaluate_regime_jump,
)

_T0 = datetime(2026, 4, 29, 14, 0, tzinfo=UTC)


def _shipped_config() -> BreachBehaviorConfig:
    """Return a BreachBehaviorConfig with shipped knob values."""
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


def _budget_entry(
    *,
    rule_id: str,
    zone: RiskZone,
    current_value: float = 80.0,
    limit_value: float = 100.0,
) -> RiskBudgetEntry:
    headroom = limit_value - current_value
    headroom_pct = max(0.0, min(100.0, 100.0 * headroom / limit_value))
    return RiskBudgetEntry(
        rule_id=rule_id,
        rule_label=rule_id,
        current_value=current_value,
        limit_value=limit_value,
        headroom=headroom,
        headroom_pct_of_limit=headroom_pct,
        zone=zone,
        unit="pct",
        cumulative_invocation_impact_value=0.0,
    )


# ---------------------------------------------------------------------------
# Regime jump
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prior", "current"),
    [
        (RegimeLabel.LOW_VOL, RegimeLabel.NORMAL),
        (RegimeLabel.NORMAL, RegimeLabel.ELEVATED),
        (RegimeLabel.ELEVATED, RegimeLabel.CRISIS),
    ],
)
def test_adjacent_tightening_does_not_fire(prior: RegimeLabel, current: RegimeLabel) -> None:
    trigger, detail = evaluate_regime_jump(
        prior_regime_label=prior,
        current_regime_label=current,
    )
    assert trigger is None
    assert detail is None


@pytest.mark.parametrize(
    ("prior", "current"),
    [
        (RegimeLabel.LOW_VOL, RegimeLabel.ELEVATED),
        (RegimeLabel.NORMAL, RegimeLabel.CRISIS),
    ],
)
def test_skip_one_level_tightening_fires(prior: RegimeLabel, current: RegimeLabel) -> None:
    trigger, detail = evaluate_regime_jump(
        prior_regime_label=prior,
        current_regime_label=current,
    )
    assert trigger is EmergencyTrigger.REGIME_JUMP
    assert detail is not None
    assert prior.value in detail
    assert current.value in detail


def test_skip_multiple_levels_tightening_fires() -> None:
    trigger, detail = evaluate_regime_jump(
        prior_regime_label=RegimeLabel.LOW_VOL,
        current_regime_label=RegimeLabel.CRISIS,
    )
    assert trigger is EmergencyTrigger.REGIME_JUMP
    assert detail is not None
    assert RegimeLabel.LOW_VOL.value in detail
    assert RegimeLabel.CRISIS.value in detail


@pytest.mark.parametrize(
    ("prior", "current"),
    [
        (RegimeLabel.NORMAL, RegimeLabel.LOW_VOL),
        (RegimeLabel.ELEVATED, RegimeLabel.NORMAL),
        (RegimeLabel.ELEVATED, RegimeLabel.LOW_VOL),
        (RegimeLabel.CRISIS, RegimeLabel.ELEVATED),
        (RegimeLabel.CRISIS, RegimeLabel.NORMAL),
        (RegimeLabel.CRISIS, RegimeLabel.LOW_VOL),
    ],
)
def test_loosening_does_not_fire(prior: RegimeLabel, current: RegimeLabel) -> None:
    trigger, detail = evaluate_regime_jump(
        prior_regime_label=prior,
        current_regime_label=current,
    )
    assert trigger is None
    assert detail is None


@pytest.mark.parametrize("regime", list(RegimeLabel))
def test_same_regime_does_not_fire(regime: RegimeLabel) -> None:
    trigger, detail = evaluate_regime_jump(
        prior_regime_label=regime,
        current_regime_label=regime,
    )
    assert trigger is None
    assert detail is None


# ---------------------------------------------------------------------------
# Multi-rule breach
# ---------------------------------------------------------------------------


_DEFERRED_RULES: tuple[str, ...] = (
    "sector_concentration",
    "net_long",
    "net_short",
    "gross_exposure",
    "options_delta",
    "total_short",
)


def test_multi_rule_breach_below_threshold_does_not_fire() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.WARNING),
            _budget_entry(rule_id="gross_exposure", zone=RiskZone.NORMAL),
        ),
    )
    trigger, detail = evaluate_multi_rule_breach(
        risk_budget=budget,
        config=_shipped_config(),
        rules_with_deferred_response=_DEFERRED_RULES,
    )
    assert trigger is None
    assert detail is None


def test_multi_rule_breach_at_threshold_fires() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="gross_exposure", zone=RiskZone.WARNING),
        ),
    )
    trigger, detail = evaluate_multi_rule_breach(
        risk_budget=budget,
        config=_shipped_config(),
        rules_with_deferred_response=_DEFERRED_RULES,
    )
    assert trigger is EmergencyTrigger.MULTI_RULE_BREACH
    assert detail is not None
    assert "sector_concentration" in detail
    assert "net_long" in detail
    assert "net_short" in detail


def test_multi_rule_breach_above_threshold_lists_all() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="gross_exposure", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="options_delta", zone=RiskZone.BLOCKED),
        ),
    )
    trigger, detail = evaluate_multi_rule_breach(
        risk_budget=budget,
        config=_shipped_config(),
        rules_with_deferred_response=_DEFERRED_RULES,
    )
    assert trigger is EmergencyTrigger.MULTI_RULE_BREACH
    assert detail is not None
    for rid in (
        "sector_concentration",
        "net_long",
        "net_short",
        "gross_exposure",
        "options_delta",
    ):
        assert rid in detail


def test_multi_rule_breach_excludes_non_deferred_rules() -> None:
    """A BLOCKED rule that is not in rules_with_deferred_response does not contribute."""
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            # immediate_engine classification — not in deferred list
            _budget_entry(rule_id="position_max_loss_equity_pct", zone=RiskZone.BLOCKED),
        ),
    )
    trigger, detail = evaluate_multi_rule_breach(
        risk_budget=budget,
        config=_shipped_config(),
        rules_with_deferred_response=_DEFERRED_RULES,
    )
    assert trigger is None
    assert detail is None


def test_multi_rule_breach_custom_threshold_two_fires() -> None:
    config = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=2,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
        ),
    )
    trigger, detail = evaluate_multi_rule_breach(
        risk_budget=budget,
        config=config,
        rules_with_deferred_response=_DEFERRED_RULES,
    )
    assert trigger is EmergencyTrigger.MULTI_RULE_BREACH
    assert detail is not None


# ---------------------------------------------------------------------------
# Daily drawdown velocity
# ---------------------------------------------------------------------------


def _sample(*, minutes_before_t0: float, intraday_drawdown_pct: float) -> DrawdownSample:
    return DrawdownSample(
        sampled_at=_T0 - timedelta(minutes=minutes_before_t0),
        intraday_drawdown_pct=intraday_drawdown_pct,
    )


def test_drawdown_velocity_crossing_within_window_fires() -> None:
    history = (
        _sample(minutes_before_t0=25, intraday_drawdown_pct=0.4),
        _sample(minutes_before_t0=20, intraday_drawdown_pct=0.6),
        _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
    )
    trigger, detail = evaluate_daily_drawdown_velocity(
        drawdown_history=history,
        daily_drawdown_limit_pct=2.5,
        config=_shipped_config(),
    )
    assert trigger is EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY
    assert detail is not None
    assert "1.5" in detail
    assert "20" in detail


def test_drawdown_velocity_crossing_outside_window_does_not_fire() -> None:
    history = (
        _sample(minutes_before_t0=45, intraday_drawdown_pct=0.4),
        _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
    )
    trigger, detail = evaluate_daily_drawdown_velocity(
        drawdown_history=history,
        daily_drawdown_limit_pct=2.5,
        config=_shipped_config(),
    )
    assert trigger is None
    assert detail is None


def test_drawdown_velocity_no_crossing_does_not_fire() -> None:
    history = (
        _sample(minutes_before_t0=25, intraday_drawdown_pct=0.4),
        _sample(minutes_before_t0=10, intraday_drawdown_pct=0.7),
        _sample(minutes_before_t0=0, intraday_drawdown_pct=1.0),
    )
    trigger, detail = evaluate_daily_drawdown_velocity(
        drawdown_history=history,
        daily_drawdown_limit_pct=2.5,
        config=_shipped_config(),
    )
    assert trigger is None
    assert detail is None


def test_drawdown_velocity_steady_above_threshold_does_not_fire() -> None:
    """All samples already above threshold → no before-cross → no fire."""
    history = (
        _sample(minutes_before_t0=25, intraday_drawdown_pct=1.6),
        _sample(minutes_before_t0=10, intraday_drawdown_pct=1.7),
        _sample(minutes_before_t0=0, intraday_drawdown_pct=1.8),
    )
    trigger, detail = evaluate_daily_drawdown_velocity(
        drawdown_history=history,
        daily_drawdown_limit_pct=2.5,
        config=_shipped_config(),
    )
    assert trigger is None
    assert detail is None


def test_drawdown_velocity_custom_window_smaller_does_not_fire() -> None:
    config = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=15,
        drawdown_velocity_threshold_pct_of_daily_limit=60.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )
    history = (
        _sample(minutes_before_t0=25, intraday_drawdown_pct=0.4),
        _sample(minutes_before_t0=20, intraday_drawdown_pct=0.6),
        _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
    )
    trigger, detail = evaluate_daily_drawdown_velocity(
        drawdown_history=history,
        daily_drawdown_limit_pct=2.5,
        config=config,
    )
    assert trigger is None
    assert detail is None


def test_drawdown_velocity_custom_threshold_higher() -> None:
    """With threshold 80% of 2.5 = 2.0, a crossing of 2.0 fires."""
    config = BreachBehaviorConfig(
        forced_reduction_short_trim_target_pct_of_limit=95.0,
        forced_reduction_total_short_immediate_threshold_pct_of_limit=110.0,
        drawdown_velocity_window_minutes=30,
        drawdown_velocity_threshold_pct_of_daily_limit=80.0,
        multi_rule_breach_simultaneous_deferred_rules_count=3,
        cascade_max_steps=8,
        delta_buffer_secondary_check_buffer_factor=1.0,
        emergency_invocation_cooldown_minutes=30,
    )
    history = (
        _sample(minutes_before_t0=20, intraday_drawdown_pct=1.5),
        _sample(minutes_before_t0=0, intraday_drawdown_pct=2.1),
    )
    trigger, detail = evaluate_daily_drawdown_velocity(
        drawdown_history=history,
        daily_drawdown_limit_pct=2.5,
        config=config,
    )
    assert trigger is EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY
    assert detail is not None


def test_drawdown_sample_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DrawdownSample(
            sampled_at=datetime(2026, 4, 29, 14, 0),  # noqa: DTZ001
            intraday_drawdown_pct=1.0,
        )


# ---------------------------------------------------------------------------
# Margin call
# ---------------------------------------------------------------------------


def test_margin_call_event_present_fires() -> None:
    event = MarginCallEvent(
        issued_at=_T0,
        additional_margin_required_usd=50_000.0,
    )
    trigger, detail = evaluate_margin_call(margin_call_event=event)
    assert trigger is EmergencyTrigger.MARGIN_CALL
    assert detail is not None
    assert "50000" in detail.replace(",", "") or "50,000" in detail


def test_margin_call_event_absent_does_not_fire() -> None:
    trigger, detail = evaluate_margin_call(margin_call_event=None)
    assert trigger is None
    assert detail is None


def test_margin_call_event_rejects_zero_amount() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        MarginCallEvent(
            issued_at=_T0,
            additional_margin_required_usd=0.0,
        )


def test_margin_call_event_rejects_negative_amount() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        MarginCallEvent(
            issued_at=_T0,
            additional_margin_required_usd=-1.0,
        )


def test_margin_call_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        MarginCallEvent(
            issued_at=datetime(2026, 4, 29, 14, 0),  # noqa: DTZ001
            additional_margin_required_usd=50_000.0,
        )


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def _empty_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _quiet_composer_kwargs() -> dict[str, Any]:
    """Composer kwargs that fire NO trigger (used to add one trigger at a time)."""
    return {
        "now": _T0,
        "last_invocation_started_at": _T0 - timedelta(minutes=65),
        "last_emergency_triggered_at": None,
        "cooldown_minutes": 30,
        "normal_cadence_minutes": 120.0,
        "prior_regime_label": RegimeLabel.NORMAL,
        "current_regime_label": RegimeLabel.NORMAL,
        "risk_budget": _empty_budget(),
        "rules_with_deferred_response": _DEFERRED_RULES,
        "drawdown_history": (),
        "daily_drawdown_limit_pct": 2.5,
        "margin_call_event": None,
        "config": _shipped_config(),
    }


def test_composer_no_trigger_returns_none() -> None:
    result = evaluate_emergency_invocation(**_quiet_composer_kwargs())
    assert result is None


def test_composer_regime_jump_no_cooldown_fires() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.REGIME_JUMP


def test_composer_margin_call_no_cooldown_fires() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "margin_call_event": MarginCallEvent(
            issued_at=_T0,
            additional_margin_required_usd=10_000.0,
        ),
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.MARGIN_CALL


def test_composer_drawdown_velocity_no_cooldown_fires() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "drawdown_history": (
            _sample(minutes_before_t0=20, intraday_drawdown_pct=0.6),
            _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
        ),
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY


def test_composer_multi_rule_breach_no_cooldown_fires() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.BLOCKED),
        ),
    )
    kwargs = _quiet_composer_kwargs() | {"risk_budget": budget}
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.MULTI_RULE_BREACH


# ---------------------------------------------------------------------------
# Composer — cooldown
# ---------------------------------------------------------------------------


def test_composer_cooldown_active_suppresses_regime_jump() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "last_emergency_triggered_at": _T0 - timedelta(minutes=15),
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert result is None


def test_composer_cooldown_elapsed_allows_regime_jump() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "last_emergency_triggered_at": _T0 - timedelta(minutes=35),
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.REGIME_JUMP


def test_composer_cooldown_active_allows_margin_call() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "last_emergency_triggered_at": _T0 - timedelta(minutes=5),
        "margin_call_event": MarginCallEvent(
            issued_at=_T0,
            additional_margin_required_usd=10_000.0,
        ),
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.MARGIN_CALL


def test_composer_cooldown_active_suppresses_multi_rule_breach() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.BLOCKED),
        ),
    )
    kwargs = _quiet_composer_kwargs() | {
        "last_emergency_triggered_at": _T0 - timedelta(minutes=15),
        "risk_budget": budget,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert result is None


def test_composer_cooldown_active_suppresses_drawdown_velocity() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "last_emergency_triggered_at": _T0 - timedelta(minutes=15),
        "drawdown_history": (
            _sample(minutes_before_t0=20, intraday_drawdown_pct=0.6),
            _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
        ),
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert result is None


# ---------------------------------------------------------------------------
# Composer — priority
# ---------------------------------------------------------------------------


def test_composer_priority_margin_call_wins_over_regime_jump() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
        "margin_call_event": MarginCallEvent(
            issued_at=_T0,
            additional_margin_required_usd=10_000.0,
        ),
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.MARGIN_CALL


def test_composer_priority_regime_jump_wins_over_velocity_and_multi_rule() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.BLOCKED),
        ),
    )
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
        "drawdown_history": (
            _sample(minutes_before_t0=20, intraday_drawdown_pct=0.6),
            _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
        ),
        "risk_budget": budget,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.REGIME_JUMP


def test_composer_priority_velocity_wins_over_multi_rule() -> None:
    budget = RiskBudgetConsumption(
        entries=(
            _budget_entry(rule_id="sector_concentration", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_long", zone=RiskZone.BLOCKED),
            _budget_entry(rule_id="net_short", zone=RiskZone.BLOCKED),
        ),
    )
    kwargs = _quiet_composer_kwargs() | {
        "drawdown_history": (
            _sample(minutes_before_t0=20, intraday_drawdown_pct=0.6),
            _sample(minutes_before_t0=0, intraday_drawdown_pct=1.6),
        ),
        "risk_budget": budget,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.DAILY_DRAWDOWN_VELOCITY


# ---------------------------------------------------------------------------
# Composer — context fields
# ---------------------------------------------------------------------------


def test_composer_minutes_since_last_invocation_computed_correctly() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "last_invocation_started_at": _T0 - timedelta(minutes=65),
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.minutes_since_last_invocation == pytest.approx(65.0)


def test_composer_normal_cadence_minutes_passes_through() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "normal_cadence_minutes": 120.0,
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.normal_cadence_minutes == pytest.approx(120.0)


def test_composer_trigger_detail_matches_firing_evaluator() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger_detail == "Regime jump: LOW_VOL -> CRISIS"


# ---------------------------------------------------------------------------
# Composer — validation
# ---------------------------------------------------------------------------


def test_composer_rejects_naive_now() -> None:
    kwargs = _quiet_composer_kwargs() | {"now": datetime(2026, 4, 29, 14, 0)}  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_emergency_invocation(**kwargs)


def test_composer_rejects_now_equal_to_last_invocation_started_at() -> None:
    kwargs = _quiet_composer_kwargs() | {"last_invocation_started_at": _T0}
    with pytest.raises(ValueError, match="now must be after last_invocation_started_at"):
        evaluate_emergency_invocation(**kwargs)


def test_composer_rejects_now_before_last_invocation_started_at() -> None:
    kwargs = _quiet_composer_kwargs() | {"last_invocation_started_at": _T0 + timedelta(minutes=1)}
    with pytest.raises(ValueError, match="now must be after last_invocation_started_at"):
        evaluate_emergency_invocation(**kwargs)


# ---------------------------------------------------------------------------
# Composer — frozen output and determinism
# ---------------------------------------------------------------------------


def test_composer_returned_context_is_frozen() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    with pytest.raises(ValidationError):
        result.trigger_detail = "tampered"


def test_composer_is_deterministic() -> None:
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    results = [evaluate_emergency_invocation(**kwargs) for _ in range(100)]
    first = results[0]
    assert all(r == first for r in results)


# ---------------------------------------------------------------------------
# Worked example — A10 reproduction
# ---------------------------------------------------------------------------


def test_a10_low_vol_to_crisis_regime_jump_reproduction() -> None:
    """Worked example A10: low-vol → crisis fires REGIME_JUMP."""
    kwargs = _quiet_composer_kwargs() | {
        "prior_regime_label": RegimeLabel.LOW_VOL,
        "current_regime_label": RegimeLabel.CRISIS,
    }
    result = evaluate_emergency_invocation(**kwargs)
    assert isinstance(result, EmergencyContext)
    assert result.trigger is EmergencyTrigger.REGIME_JUMP
    assert result.trigger_detail == "Regime jump: LOW_VOL -> CRISIS"
    assert result.normal_cadence_minutes == pytest.approx(120.0)
    assert result.minutes_since_last_invocation == pytest.approx(65.0)
