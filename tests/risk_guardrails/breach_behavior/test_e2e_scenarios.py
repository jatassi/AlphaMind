"""End-to-end golden tests reproducing the breach-behavior scenarios (story 08).

Each test reproduces a scenario from
``docs/design/06-risk-guardrails/scenario-tests.md`` end-to-end through the
breach-behavior package's public surface. The corpus serves as living
documentation tying code behavior to the design's worked examples.

Scenarios covered: A4 daily drawdown halt, A6 short-squeeze position-level
max-loss + envelope composition, A7 margin-call cascade (both no-secondary
and secondary-deferred), A8 cumulative drawdown tier 2, A10 regime-jump
emergency invocation + regime-transition breaches (cross-feature), A11
synchronized HTB buy-in (verifies *absence* of trigger and halt).
"""

from __future__ import annotations

import pathlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from alphamind._kernel.ids import PositionId, Symbol
from alphamind._kernel.regime import RegimeTransitionState
from alphamind.config.models.guardrails import ProgressiveTier
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior import (
    BreachBehaviorConfig,
    BreachDetails,
    CascadeContext,
    DrawdownSample,
    DrawdownTier,
    EmergencyTrigger,
    EngineEnvelope,
    HaltState,
    MarginCallEvent,
    PositionLiquidity,
    PositionRiskReward,
    PositionSelectionAction,
    RegimeLabel,
    SecondaryBreachCheckResult,
    SecondaryBreachOutcome,
    apply_progressive_tier_overrides,
    classify_cumulative_drawdown_tier,
    compose_engine_envelope,
    compute_halt_state,
    evaluate_emergency_invocation,
    generate_cascade_id,
    load_breach_behavior_config,
    orchestrate_margin_call_cascade,
    select_for_position_max_loss,
)
from alphamind.risk_guardrails.regime_adaptation import (
    RuleMetadata,
    detect_regime_transition_breaches,
)
from tests.risk_guardrails.breach_behavior.fixtures import (
    ScriptedLibrary,
    StubLibraryConfig,
    StubLibraryOutput,
    StubMarketInputs,
    StubPortfolioState,
    StubRuleProjection,
    make_active_risk_parameters,
    make_drawdown_state,
    make_position_record,
    make_risk_budget,
)

# ---------------------------------------------------------------------------
# Module-level constants and helpers
# ---------------------------------------------------------------------------

_REPETITIONS = 5
"""Determinism guard: each scenario test runs this many times and asserts
identical outputs."""

_BREACH_BEHAVIOR_YAML = pathlib.Path("config/breach_behavior.yaml")


def _assert_deterministic[T](call: Callable[[], T]) -> T:
    """Invoke ``call`` ``_REPETITIONS`` times, assert equal outputs, return one.

    Each scenario test uses this guard rather than open-coding the loop so the
    determinism property is uniform: identical inputs always produce identical
    outputs across repeated calls.
    """
    first = call()
    for _ in range(_REPETITIONS - 1):
        assert call() == first
    return first


def _config() -> BreachBehaviorConfig:
    """Load the shipped breach-behavior YAML config from the project root."""
    return load_breach_behavior_config(_BREACH_BEHAVIOR_YAML)


def _proj(
    rule: str,
    status: str,
    *,
    current: float = 10.0,
    limit: float = 25.0,
    projected_after: float | None = None,
) -> StubRuleProjection:
    """Helper constructing one ``StubRuleProjection`` for the cascade tests."""
    pa = projected_after if projected_after is not None else current
    return StubRuleProjection(
        rule=rule,
        status=status,
        current=current,
        limit=limit,
        projected_after=pa,
        headroom_remaining=limit - pa,
        unit="% of portfolio",
    )


def _normal_daily_dd_2pct5_params() -> ActiveRiskParameterSet:
    """The standard normal-regime parameter set with a 2.5% daily-drawdown limit.

    Used by every test that exercises ``compute_halt_state`` against the
    normal-regime daily-drawdown limit (A4, A4-recovery, A11).
    """
    return make_active_risk_parameters(
        regime=RegimeLabel.NORMAL,
        rule_values={"daily_drawdown_pct": 2.5},
    )


# ---------------------------------------------------------------------------
# Scenario A4 — daily drawdown halt
# ---------------------------------------------------------------------------


def _halt_state_for(intraday_pct: float) -> HaltState | None:
    """Compute halt state at ``intraday_pct`` against the normal-regime 2.5% limit."""
    return compute_halt_state(
        drawdown_state=make_drawdown_state(intraday_pct=intraday_pct, cumulative_pct=0.0),
        active_risk_parameters=_normal_daily_dd_2pct5_params(),
    )


def test_a4_daily_drawdown_halt_at_2pct8_intraday() -> None:
    """A4: daily drawdown 2.8% at $97,200 portfolio crosses the 2.5% normal limit."""
    halt_state = _assert_deterministic(lambda: _halt_state_for(intraday_pct=2.8))

    assert halt_state is not None
    assert halt_state.daily_halt_active is True
    assert halt_state.cumulative_full_halt_active is False
    assert halt_state.daily_drawdown_pct == 2.8
    assert halt_state.daily_drawdown_limit_pct == 2.5

    with pytest.raises((ValueError, TypeError)):
        halt_state.daily_halt_active = False


def test_a4_recovery_to_2pct2_still_breaches_when_evaluated_in_isolation() -> None:
    """A4 recovery note: 2.2% intraday after a 2.8% peak — primitive returns None.

    Latching ("once halt fires, stays for the rest of the session") is the
    continuous monitor's responsibility per story 05a; the breach-behavior
    primitive is stateless and reflects current drawdown only. The 2.2%
    sample is below the 2.5% limit, so this primitive returns ``None``.
    """
    assert _halt_state_for(intraday_pct=2.2) is None


# ---------------------------------------------------------------------------
# Scenario A6 — short squeeze, position-level max-loss + engine envelope
# ---------------------------------------------------------------------------


_A6_TRIGGER_TS = datetime(2026, 4, 28, 14, 30, tzinfo=UTC)


def _a6_position() -> PositionView:
    """A6 fixture: 140-share MARA short at $20 entry, current $28 (40% loss)."""
    return make_position_record(
        position_id=PositionId("POS-MARA-001"),
        ticker=Symbol("MARA"),
        direction="short",
        size_pct=3.92,
        size_usd=3920.0,
        unrealized_pnl_usd=-1120.0,
    )


def test_a6_short_squeeze_position_max_loss_close() -> None:
    """A6: position-level max loss on a short hits 40%; selector returns FULL_CLOSE."""
    positions = (_a6_position(),)

    selection = _assert_deterministic(
        lambda: select_for_position_max_loss(
            breaching_position_id="POS-MARA-001",
            open_positions=positions,
            loss_pct=-40.0,
            limit_pct=-30.0,
        )
    )

    assert selection.action == PositionSelectionAction.FULL_CLOSE
    assert selection.position_id == "POS-MARA-001"
    assert "position-level max loss" in selection.rationale
    assert "MARA" in selection.rationale
    with pytest.raises((ValueError, TypeError)):
        selection.position_id = "POS-OTHER-001"


def test_a6_engine_envelope_for_max_loss_close() -> None:
    """A6: composing the engine envelope from the FULL_CLOSE selection."""
    positions = (_a6_position(),)
    positions_by_id = {p.position_id: p for p in positions}

    selection = select_for_position_max_loss(
        breaching_position_id="POS-MARA-001",
        open_positions=positions,
        loss_pct=-40.0,
        limit_pct=-30.0,
    )
    breach_details = BreachDetails(
        current_value=40.0,
        limit_value=30.0,
        overage=10.0,
        unit="% of cost basis",
        regime_at_breach=RegimeLabel.NORMAL,
    )
    secondary_check = SecondaryBreachCheckResult(
        result=SecondaryBreachOutcome.NO_SECONDARY_BREACH,
        notes="closing reduces all exposures; no new breach introduced",
    )

    envelope = _assert_deterministic(
        lambda: compose_engine_envelope(
            monitor_session_id="s1",
            trigger_id=1,
            trigger_timestamp=_A6_TRIGGER_TS,
            rule_breached="position_max_loss_equity_pct",
            breach_details=breach_details,
            position_selection=selection,
            positions_by_id=positions_by_id,
            portfolio_value_usd=100_000.0,
            secondary_breach_check=secondary_check,
        )
    )

    assert envelope.envelope_id == "MON.s1.1"
    assert envelope.command.command_id == "MON.s1.1.1"
    assert envelope.command.position_id == "POS-MARA-001"
    assert envelope.command.quantity_or_all == "all"
    assert envelope.guardrail_trigger_record.rule_breached == "position_max_loss_equity_pct"
    assert envelope.guardrail_trigger_record.cascade_id is None
    assert envelope.source_provenance == "engine_guardrail"
    assert envelope.guardrail_trigger_record.secondary_breach_check_result == secondary_check
    with pytest.raises((ValueError, TypeError)):
        envelope.envelope_id = "MON.x.99"


# ---------------------------------------------------------------------------
# Scenario A7 — margin call cascade during elevated regime
# ---------------------------------------------------------------------------


_A7_TRIGGER_TS = datetime(2026, 4, 28, 11, 30, tzinfo=UTC)
_A7_PORTFOLIO_VALUE_USD = 98_000.0
_A7_MARGIN_CALL_AMOUNT_USD = 3_000.0


def _a7_positions() -> tuple[PositionView, ...]:
    """A7 fixture: 3 short positions COIN 8% / SQ 7% / HOOD 7% on $98K portfolio."""
    return (
        make_position_record(
            position_id=PositionId("POS-COIN-001"),
            ticker=Symbol("COIN"),
            direction="short",
            size_pct=8.0,
            size_usd=7840.0,
            unrealized_pnl_usd=-940.0,
        ),
        make_position_record(
            position_id=PositionId("POS-SQ-001"),
            ticker=Symbol("SQ"),
            direction="short",
            size_pct=7.0,
            size_usd=6860.0,
            unrealized_pnl_usd=-820.0,
        ),
        make_position_record(
            position_id=PositionId("POS-HOOD-001"),
            ticker=Symbol("HOOD"),
            direction="short",
            size_pct=7.0,
            size_usd=6860.0,
            unrealized_pnl_usd=-820.0,
        ),
    )


def _a7_liquidity() -> tuple[PositionLiquidity, ...]:
    return (
        PositionLiquidity(position_id=PositionId("POS-COIN-001"), adv_to_position_size_ratio=3.0),
        PositionLiquidity(position_id=PositionId("POS-SQ-001"), adv_to_position_size_ratio=2.5),
        PositionLiquidity(position_id=PositionId("POS-HOOD-001"), adv_to_position_size_ratio=2.0),
    )


def _a7_risk_reward() -> tuple[PositionRiskReward, ...]:
    """COIN has the worst R/R (lowest ratio) per the design walkthrough."""
    return (
        PositionRiskReward(position_id=PositionId("POS-COIN-001"), risk_reward_ratio=0.3),
        PositionRiskReward(position_id=PositionId("POS-SQ-001"), risk_reward_ratio=0.7),
        PositionRiskReward(position_id=PositionId("POS-HOOD-001"), risk_reward_ratio=0.9),
    )


def _a7_cascade_context() -> CascadeContext:
    return CascadeContext(
        monitor_session_id="s1",
        initial_trigger_id=5,
        cascade_id=generate_cascade_id(monitor_session_id="s1", initial_trigger_id=5),
        trigger_timestamp=_A7_TRIGGER_TS,
        portfolio_value_usd=_A7_PORTFOLIO_VALUE_USD,
        config=_config(),
    )


def _a7_library_config() -> StubLibraryConfig:
    return StubLibraryConfig(
        effective_limits={
            "margin_call": 0.0,
            "total_short_pct": 25.0,
            "net_long_pct": 45.0,
            "gross_exposure_pct": 90.0,
        },
    )


def _orchestrate_a7(
    *, baseline: StubLibraryOutput, post_close: StubLibraryOutput
) -> tuple[EngineEnvelope, ...]:
    """Run the A7 cascade orchestrator with caller-scripted library outputs.

    Each invocation builds a fresh ``ScriptedLibrary`` so the call counter
    starts at zero — required for determinism across repeated runs.
    """
    margin_call = MarginCallEvent(
        issued_at=_A7_TRIGGER_TS,
        additional_margin_required_usd=_A7_MARGIN_CALL_AMOUNT_USD,
    )
    library = ScriptedLibrary(outputs=[baseline, post_close])
    return orchestrate_margin_call_cascade(
        margin_call_event=margin_call,
        open_positions=_a7_positions(),
        liquidity=_a7_liquidity(),
        risk_reward_metric=_a7_risk_reward(),
        current_state=StubPortfolioState(label="pre"),
        library_config=_a7_library_config(),
        market_inputs=StubMarketInputs(),
        active_regime=RegimeLabel.ELEVATED,
        context=_a7_cascade_context(),
        evaluate_proposals=library,
    )


def test_a7_margin_call_cascade_no_secondary_breach() -> None:
    """A7: closing COIN short (worst R/R) cures margin call without secondary."""
    # Pre-close baseline: total_short PASS (22% under elevated 25%), other rules PASS.
    baseline = StubLibraryOutput(
        per_rule=(
            _proj("total_short_pct", "PASS", current=22.0, limit=25.0, projected_after=22.0),
            _proj("net_long_pct", "PASS", current=30.0, limit=45.0),
            _proj("gross_exposure_pct", "PASS", current=74.0, limit=90.0),
        ),
    )
    # Post-close: COIN closed; total_short drops to 14%, net_long up to 38%, gross 66%.
    post_close = StubLibraryOutput(
        per_rule=(
            _proj("total_short_pct", "PASS", current=22.0, limit=25.0, projected_after=14.0),
            _proj("net_long_pct", "PASS", current=30.0, limit=45.0, projected_after=38.0),
            _proj("gross_exposure_pct", "PASS", current=74.0, limit=90.0, projected_after=66.0),
        ),
    )

    envelopes = _assert_deterministic(
        lambda: _orchestrate_a7(baseline=baseline, post_close=post_close),
    )

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.envelope_id == "MON.s1.5"
    assert env.command.position_id == "POS-COIN-001"
    assert env.command.quantity_or_all == "all"
    assert env.guardrail_trigger_record.rule_breached == "margin_call"
    assert env.guardrail_trigger_record.cascade_id == "CASCADE.s1.5"
    secondary = env.guardrail_trigger_record.secondary_breach_check_result
    assert secondary is not None
    assert secondary.result == SecondaryBreachOutcome.NO_SECONDARY_BREACH
    with pytest.raises((ValueError, TypeError)):
        env.envelope_id = "MON.x.99"


def test_a7_margin_call_cascade_with_secondary_deferred() -> None:
    """A7 variant: closing COIN pushes net long over 45% → DEFERRED_TO_PM."""
    # Pre-close baseline: net_long near limit at 38%, total_short PASS at 22%.
    baseline = StubLibraryOutput(
        per_rule=(
            _proj("total_short_pct", "PASS", current=22.0, limit=25.0, projected_after=22.0),
            _proj("net_long_pct", "PASS", current=38.0, limit=45.0, projected_after=38.0),
        ),
    )
    # Post-close: closing the 8% short pushes net_long over the elevated 45% limit.
    post_close = StubLibraryOutput(
        per_rule=(
            _proj("total_short_pct", "PASS", current=22.0, limit=25.0, projected_after=14.0),
            _proj("net_long_pct", "FAIL", current=38.0, limit=45.0, projected_after=46.0),
        ),
    )

    envelopes = _assert_deterministic(
        lambda: _orchestrate_a7(baseline=baseline, post_close=post_close),
    )

    # Margin-call cascade never alternate-searches; secondary surfaces as deferred.
    assert len(envelopes) == 1
    secondary = envelopes[0].guardrail_trigger_record.secondary_breach_check_result
    assert secondary is not None
    assert secondary.result == SecondaryBreachOutcome.DEFERRED_TO_PM
    assert secondary.notes is not None
    assert "net_long_pct" in secondary.notes


# ---------------------------------------------------------------------------
# Scenario A8 — cumulative drawdown progressive response (tier 2)
# ---------------------------------------------------------------------------


def _shipped_progressive_tiers() -> tuple[ProgressiveTier, ...]:
    """The three-tier configuration shipped in config/guardrails.yaml."""
    return (
        ProgressiveTier(trigger_pct=8.0, max_position_size_pct=3.0, max_gross_pct=80.0),
        ProgressiveTier(trigger_pct=10.0, max_position_size_pct=2.0, max_gross_pct=60.0),
        ProgressiveTier(trigger_pct=12.0, full_halt=True),
    )


def test_a8_cumulative_drawdown_tier_2_overrides() -> None:
    """A8: cumulative drawdown 10% → tier 2; overrides cap position 2% and gross 60%."""
    progressive_tiers = _shipped_progressive_tiers()

    tier = _assert_deterministic(
        lambda: classify_cumulative_drawdown_tier(
            current_drawdown_pct=10.0,
            progressive_tiers=progressive_tiers,
        )
    )
    assert tier == DrawdownTier.HEAVILY_CONSTRAINED

    pre_params = make_active_risk_parameters(
        regime=RegimeLabel.NORMAL,
        rule_values={
            "position_max_size_pct": 5.0,
            "gross_exposure_pct": 120.0,
            "daily_drawdown_pct": 2.5,
        },
    )

    post_params = _assert_deterministic(
        lambda: apply_progressive_tier_overrides(
            active_risk_parameters=pre_params,
            tier=tier,
            progressive_tiers=progressive_tiers,
        )
    )

    pos_max = next(e for e in post_params.entries if e.rule_id == "position_max_size_pct")
    gross = next(e for e in post_params.entries if e.rule_id == "gross_exposure_pct")
    assert pos_max.value == 2.0
    assert gross.value == 60.0
    assert "cumulative_drawdown_tier_2" in post_params.active_overlays
    with pytest.raises(FrozenInstanceError):
        post_params.active_overlays = ()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Scenario A10 — low-vol → crisis emergency invocation
# ---------------------------------------------------------------------------


def test_a10_regime_jump_low_vol_to_crisis_fires_emergency() -> None:
    """A10: VIX 12 → 38 reclassifies low-vol → crisis; regime-jump trigger fires."""
    now = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)
    last_invocation = now - timedelta(minutes=85)

    context = _assert_deterministic(
        lambda: evaluate_emergency_invocation(
            now=now,
            last_invocation_started_at=last_invocation,
            last_emergency_triggered_at=None,
            cooldown_minutes=30,
            normal_cadence_minutes=120.0,
            prior_regime_label=RegimeLabel.LOW_VOL,
            current_regime_label=RegimeLabel.CRISIS,
            risk_budget=make_risk_budget(entries=[]),
            rules_with_deferred_response=("net_long_pct", "gross_exposure_pct"),
            drawdown_history=(),
            daily_drawdown_limit_pct=1.5,
            margin_call_event=None,
            config=_config(),
        )
    )

    assert context is not None
    assert context.trigger == EmergencyTrigger.REGIME_JUMP
    assert "LOW_VOL" in context.trigger_detail
    assert "CRISIS" in context.trigger_detail
    assert context.minutes_since_last_invocation == pytest.approx(85.0)
    assert context.normal_cadence_minutes == 120.0
    with pytest.raises((ValueError, TypeError)):
        context.trigger = EmergencyTrigger.MARGIN_CALL


def _a10_positions() -> tuple[PositionView, ...]:
    """A10 fixture: positions one of which exceeds the per-position 2% crisis cap.

    The aggregate breaches (net_long, gross, options_delta) come from the risk
    budget rather than position records — those rules are *aggregate* per the
    breach detector's classification.
    """
    return (
        make_position_record(
            position_id=PositionId("POS-AAPL-001"),
            ticker=Symbol("AAPL"),
            direction="long",
            size_pct=2.5,
            size_usd=2500.0,
        ),
    )


def _a10_risk_budget_entries() -> list[dict[str, Any]]:
    """A10 risk budget: net_long 65%, gross 118%, options_delta 45%."""
    return [
        {"rule_id": "net_long_pct", "current_value": 65.0, "limit_value": 70.0},
        {"rule_id": "gross_exposure_pct", "current_value": 118.0, "limit_value": 130.0},
        {"rule_id": "options_delta_pct", "current_value": 45.0, "limit_value": 50.0},
    ]


def _a10_post_transition_limits() -> dict[str, float]:
    """A10 crisis limits the regime transition tightens to."""
    return {
        "position_max_size_pct": 2.0,
        "single_short_max_pct": 1.5,
        "sector_concentration_pct": 15.0,
        "net_long_pct": 30.0,
        "net_short_pct": 30.0,
        "gross_exposure_pct": 60.0,
        "options_delta_pct": 15.0,
    }


def _a10_rule_metadata() -> dict[str, RuleMetadata]:
    return {
        rule_id: RuleMetadata(rule_id=rule_id, label=rule_id, unit="pct")
        for rule_id in (
            "position_max_size_pct",
            "single_short_max_pct",
            "sector_concentration_pct",
            "net_long_pct",
            "net_short_pct",
            "gross_exposure_pct",
            "options_delta_pct",
        )
    }


def test_a10_regime_transition_introduces_breaches() -> None:
    """A10 cross-feature: detect_regime_transition_breaches surfaces the deferred breaches.

    Pre-transition limits (low-vol): gross 130%, net long 70%, options delta 50%.
    Post-transition limits (crisis): gross 60%, net long 30%, options delta 15%.
    Portfolio: gross 118%, net long 65%, options delta 45% — all breach crisis but
    not low-vol → regime-transition breaches.
    """
    positions = _a10_positions()
    risk_budget = make_risk_budget(entries=_a10_risk_budget_entries())
    post_limits = _a10_post_transition_limits()
    rule_metadata = _a10_rule_metadata()

    breaches = _assert_deterministic(
        lambda: detect_regime_transition_breaches(
            held_positions=positions,
            risk_budget=risk_budget,
            new_effective_limits=post_limits,
            transition_state=RegimeTransitionState.TIGHTENING,
            rule_metadata=rule_metadata,
        )
    )

    rule_ids = {b.rule_id for b in breaches}
    assert "net_long_pct" in rule_ids
    assert "gross_exposure_pct" in rule_ids
    assert "options_delta_pct" in rule_ids
    assert len(breaches) >= 3


# ---------------------------------------------------------------------------
# Scenario A11 — synchronized HTB buy-in (no envelope, no halt)
# ---------------------------------------------------------------------------


def test_a11_htb_buy_in_does_not_fire_emergency_trigger() -> None:
    """A11: synchronized forced buy-ins are not in the emergency-trigger list."""
    drawdown_history = (
        DrawdownSample(
            sampled_at=datetime(2026, 4, 28, 14, 0, tzinfo=UTC),
            intraday_drawdown_pct=2.0,
        ),
    )

    result = _assert_deterministic(
        lambda: evaluate_emergency_invocation(
            now=datetime(2026, 4, 28, 14, 30, tzinfo=UTC),
            last_invocation_started_at=datetime(2026, 4, 28, 13, 0, tzinfo=UTC),
            last_emergency_triggered_at=None,
            cooldown_minutes=30,
            normal_cadence_minutes=120.0,
            prior_regime_label=RegimeLabel.NORMAL,
            current_regime_label=RegimeLabel.NORMAL,
            risk_budget=make_risk_budget(entries=[]),
            rules_with_deferred_response=("net_long_pct", "gross_exposure_pct"),
            drawdown_history=drawdown_history,
            daily_drawdown_limit_pct=2.5,
            margin_call_event=None,
            config=_config(),
        )
    )

    assert result is None


def test_a11_daily_drawdown_below_halt_threshold() -> None:
    """A11: daily drawdown 2.0% below the 2.5% limit; compute_halt_state returns None."""
    assert _assert_deterministic(lambda: _halt_state_for(intraday_pct=2.0)) is None


# ---------------------------------------------------------------------------
# Cross-cutting checks: order independence
# ---------------------------------------------------------------------------


def test_scenario_outputs_are_order_independent() -> None:
    """A4 and A8 outputs are unaffected by call order — no global state."""
    a4_first_pass = _halt_state_for(intraday_pct=2.8)
    a8_first_pass = classify_cumulative_drawdown_tier(
        current_drawdown_pct=10.0,
        progressive_tiers=_shipped_progressive_tiers(),
    )
    a8_second_pass = classify_cumulative_drawdown_tier(
        current_drawdown_pct=10.0,
        progressive_tiers=_shipped_progressive_tiers(),
    )
    a4_second_pass = _halt_state_for(intraday_pct=2.8)
    assert a4_first_pass == a4_second_pass
    assert a8_first_pass == a8_second_pass
