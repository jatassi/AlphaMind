"""Tests for ``compose_active_risk_parameters`` (story 01).

Composes ``classify_cumulative_drawdown_tier`` and
``apply_progressive_tier_overrides`` from the breach-behavior package into a
single canonical entry point that callers (story 02's orchestrator and the
continuous monitor) consume. All tier values referenced come from the shipped
``config/guardrails.yaml`` — no hard-coded numerics in test bodies.
"""

from __future__ import annotations

from alphamind.execution.guardrail_enforcement import compose_active_risk_parameters
from alphamind.risk_guardrails.breach_behavior import (
    DrawdownTier,
    RegimeLabel,
)
from tests.execution.guardrail_enforcement._helpers import (
    FULL_HALT_TIER,
    NON_HALT_TIERS,
    TIERS,
    assert_deterministic,
    baseline_normal_parameters,
    entry_by_id,
)
from tests.risk_guardrails.breach_behavior.fixtures import (
    make_active_risk_parameters,
    make_drawdown_state,
)

# ---------------------------------------------------------------------------
# Tracer-bullet: zero drawdown is a pass-through
# ---------------------------------------------------------------------------


def test_zero_drawdown_returns_input_unchanged_and_no_tier() -> None:
    """When ``current_drawdown_pct == 0.0``, the input parameter set passes through."""
    pre_params = baseline_normal_parameters()
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=0.0)

    final, tier = compose_active_risk_parameters(
        regime_resolved_parameters=pre_params,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert final is pre_params
    assert tier is None


# ---------------------------------------------------------------------------
# Per-tier behaviour
# ---------------------------------------------------------------------------


def test_first_non_halt_tier_clamps_position_and_gross_with_tier_1_overlay() -> None:
    """Crossing the first non-halt trigger yields ``CONSTRAINED`` + tier-1 overlay + clamps."""
    first_tier = NON_HALT_TIERS[0]
    assert first_tier.max_position_size_pct is not None
    assert first_tier.max_gross_pct is not None

    pre_params = baseline_normal_parameters()
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=first_tier.trigger_pct)

    final, tier = compose_active_risk_parameters(
        regime_resolved_parameters=pre_params,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert tier == DrawdownTier.CONSTRAINED
    assert "cumulative_drawdown_tier_1" in final.active_overlays
    assert entry_by_id(final, "position_max_size_pct").value == first_tier.max_position_size_pct
    assert entry_by_id(final, "gross_exposure_pct").value == first_tier.max_gross_pct


def test_second_non_halt_tier_clamps_position_and_gross_with_tier_2_overlay() -> None:
    """Crossing the second non-halt trigger yields ``HEAVILY_CONSTRAINED`` + tier-2 overlay."""
    second_tier = NON_HALT_TIERS[1]
    assert second_tier.max_position_size_pct is not None
    assert second_tier.max_gross_pct is not None

    pre_params = baseline_normal_parameters()
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=second_tier.trigger_pct)

    final, tier = compose_active_risk_parameters(
        regime_resolved_parameters=pre_params,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert tier == DrawdownTier.HEAVILY_CONSTRAINED
    assert "cumulative_drawdown_tier_2" in final.active_overlays
    assert entry_by_id(final, "position_max_size_pct").value == second_tier.max_position_size_pct
    assert entry_by_id(final, "gross_exposure_pct").value == second_tier.max_gross_pct


def test_full_halt_tier_appends_tier_3_overlay_without_per_rule_overrides() -> None:
    """Crossing the full-halt trigger yields ``FULL_HALT`` + tier-3 overlay.

    Per-rule values are preserved — full-halt is enforced via the action
    vocabulary, not parameter clamps.
    """
    pre_params = baseline_normal_parameters()
    drawdown = make_drawdown_state(
        intraday_pct=0.0,
        cumulative_pct=FULL_HALT_TIER.trigger_pct,
    )

    final, tier = compose_active_risk_parameters(
        regime_resolved_parameters=pre_params,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert tier == DrawdownTier.FULL_HALT
    assert "cumulative_drawdown_tier_3" in final.active_overlays
    assert (
        entry_by_id(final, "position_max_size_pct").value
        == entry_by_id(pre_params, "position_max_size_pct").value
    )
    assert (
        entry_by_id(final, "gross_exposure_pct").value
        == entry_by_id(pre_params, "gross_exposure_pct").value
    )


def test_override_never_loosens_when_regime_already_tighter_than_tier() -> None:
    """When the regime-resolved value is tighter than the tier, the regime value wins."""
    first_tier = NON_HALT_TIERS[0]
    assert first_tier.max_position_size_pct is not None
    assert first_tier.max_gross_pct is not None

    # Crisis-regime-resolved values strictly tighter than tier 1's overrides.
    crisis_position = first_tier.max_position_size_pct / 2.0
    crisis_gross = first_tier.max_gross_pct / 2.0
    pre_params = make_active_risk_parameters(
        regime=RegimeLabel.CRISIS,
        rule_values={
            "position_max_size_pct": crisis_position,
            "gross_exposure_pct": crisis_gross,
            "daily_drawdown_pct": 1.5,
        },
    )
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=first_tier.trigger_pct)

    final, tier = compose_active_risk_parameters(
        regime_resolved_parameters=pre_params,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert tier == DrawdownTier.CONSTRAINED
    assert entry_by_id(final, "position_max_size_pct").value == crisis_position
    assert entry_by_id(final, "gross_exposure_pct").value == crisis_gross


# ---------------------------------------------------------------------------
# Determinism guard
# ---------------------------------------------------------------------------


def test_compose_is_deterministic_under_repeated_invocation() -> None:
    """Five identical-input invocations produce identical outputs."""
    pre_params = baseline_normal_parameters()
    drawdown = make_drawdown_state(
        intraday_pct=0.0,
        cumulative_pct=NON_HALT_TIERS[1].trigger_pct,
    )

    final, tier = assert_deterministic(
        lambda: compose_active_risk_parameters(
            regime_resolved_parameters=pre_params,
            drawdown_state=drawdown,
            progressive_tiers=TIERS,
        )
    )

    assert tier == DrawdownTier.HEAVILY_CONSTRAINED
    assert "cumulative_drawdown_tier_2" in final.active_overlays


# ---------------------------------------------------------------------------
# Mirror of inlined A8 composition pattern from breach-behavior e2e scenarios
# ---------------------------------------------------------------------------


def test_a8_inlined_composition_pattern_matches_new_primitive() -> None:
    """A8: cumulative drawdown crossing the second non-halt trigger.

    Mirrors the inlined composition in
    ``tests/risk_guardrails/breach_behavior/test_e2e_scenarios.py`` § A8 against
    the new primitive — same fixtures, same assertions on the post-override
    parameter set.
    """
    second_tier = NON_HALT_TIERS[1]
    assert second_tier.max_position_size_pct is not None
    assert second_tier.max_gross_pct is not None

    pre_params = baseline_normal_parameters()
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=second_tier.trigger_pct)

    post_params, tier = assert_deterministic(
        lambda: compose_active_risk_parameters(
            regime_resolved_parameters=pre_params,
            drawdown_state=drawdown,
            progressive_tiers=TIERS,
        )
    )

    assert tier == DrawdownTier.HEAVILY_CONSTRAINED
    pos_max = entry_by_id(post_params, "position_max_size_pct")
    gross = entry_by_id(post_params, "gross_exposure_pct")
    assert pos_max.value == second_tier.max_position_size_pct
    assert gross.value == second_tier.max_gross_pct
    assert "cumulative_drawdown_tier_2" in post_params.active_overlays
