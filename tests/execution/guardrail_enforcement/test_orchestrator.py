"""Tests for ``compose_phase_1_enforcement`` (story 02).

Per-invocation Phase 1 entry point that wraps a regime-adaptation output with
the drawdown-tier composition primitive (story 01) and bundles the result.
All tier values referenced come from the shipped ``config/guardrails.yaml`` —
no hard-coded numerics in test bodies.
"""

from __future__ import annotations

import pydantic
import pytest

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.config.models.regimes import Regime
from alphamind.execution.guardrail_enforcement import (
    Phase1EnforcementResult,
    compose_active_risk_parameters,
    compose_phase_1_enforcement,
)
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.risk_guardrails.breach_behavior import DrawdownTier
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationOutput,
    RegimeAdaptationState,
)
from tests.execution.guardrail_enforcement._helpers import (
    FULL_HALT_TIER,
    NON_HALT_TIERS,
    TIERS,
    assert_deterministic,
    baseline_normal_parameters,
    entry_by_id,
)
from tests.risk_guardrails.breach_behavior.fixtures import make_drawdown_state


def _baseline_state() -> RegimeAdaptationState:
    """Construct a stable-regime ``RegimeAdaptationState`` for the orchestrator output."""
    return RegimeAdaptationState(
        as_of="2026-04-29T12:00:00+00:00",
        invocation_id="INV-001",
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="vol_expansion",
        distillation_vix_level=18.5,
        regime_skip_emergency=False,
    )


def _build_regime_output(
    *,
    parameters: ActiveRiskParameterSet,
) -> RegimeAdaptationOutput:
    """Construct a ``RegimeAdaptationOutput`` wrapping the given parameter set."""
    return RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=_baseline_state(),
        audit_log_entries=(),
    )


# ---------------------------------------------------------------------------
# Tracer-bullet: zero drawdown is a pass-through
# ---------------------------------------------------------------------------


def test_zero_drawdown_returns_input_unchanged_and_no_tier() -> None:
    """When ``current_drawdown_pct == 0.0``, the input parameter set passes through."""
    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=0.0)

    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert isinstance(result, Phase1EnforcementResult)
    assert result.active_risk_parameters is pre_params
    assert result.drawdown_tier is None


# ---------------------------------------------------------------------------
# Per-tier behaviour
# ---------------------------------------------------------------------------


def test_first_non_halt_tier_yields_constrained_with_clamped_overrides() -> None:
    """Crossing the first non-halt trigger yields ``CONSTRAINED`` + tier-1 overlay + clamps."""
    first_tier = NON_HALT_TIERS[0]
    assert first_tier.max_position_size_pct is not None
    assert first_tier.max_gross_pct is not None

    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=first_tier.trigger_pct)

    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert result.drawdown_tier == DrawdownTier.CONSTRAINED
    assert "cumulative_drawdown_tier_1" in result.active_risk_parameters.active_overlays
    assert (
        entry_by_id(result.active_risk_parameters, "position_max_size_pct").value
        == first_tier.max_position_size_pct
    )
    assert (
        entry_by_id(result.active_risk_parameters, "gross_exposure_pct").value
        == first_tier.max_gross_pct
    )


def test_second_non_halt_tier_yields_heavily_constrained() -> None:
    """Crossing the second non-halt trigger yields ``HEAVILY_CONSTRAINED`` + tier-2 overlay."""
    second_tier = NON_HALT_TIERS[1]

    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=second_tier.trigger_pct)

    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert result.drawdown_tier == DrawdownTier.HEAVILY_CONSTRAINED
    assert "cumulative_drawdown_tier_2" in result.active_risk_parameters.active_overlays


def test_full_halt_tier_appends_tier_3_overlay() -> None:
    """Crossing the full-halt trigger yields ``FULL_HALT`` + tier-3 overlay."""
    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(
        intraday_pct=0.0,
        cumulative_pct=FULL_HALT_TIER.trigger_pct,
    )

    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert result.drawdown_tier == DrawdownTier.FULL_HALT
    assert "cumulative_drawdown_tier_3" in result.active_risk_parameters.active_overlays


# ---------------------------------------------------------------------------
# Determinism guard
# ---------------------------------------------------------------------------


def test_compose_phase_1_enforcement_is_deterministic() -> None:
    """Five identical-input invocations produce identical outputs."""
    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(
        intraday_pct=0.0,
        cumulative_pct=NON_HALT_TIERS[1].trigger_pct,
    )

    result = assert_deterministic(
        lambda: compose_phase_1_enforcement(
            regime_output=regime_output,
            drawdown_state=drawdown,
            progressive_tiers=TIERS,
        )
    )

    assert result.drawdown_tier == DrawdownTier.HEAVILY_CONSTRAINED


# ---------------------------------------------------------------------------
# Input-immutability regression
# ---------------------------------------------------------------------------


def test_inputs_are_unchanged_by_invocation() -> None:
    """Calling the function does not mutate ``regime_output`` or ``drawdown_state``."""
    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(
        intraday_pct=0.0,
        cumulative_pct=NON_HALT_TIERS[0].trigger_pct,
    )

    regime_output_snapshot = _build_regime_output(parameters=pre_params)
    drawdown_snapshot = make_drawdown_state(
        intraday_pct=0.0,
        cumulative_pct=NON_HALT_TIERS[0].trigger_pct,
    )

    compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert regime_output == regime_output_snapshot
    assert drawdown == drawdown_snapshot


# ---------------------------------------------------------------------------
# Frozen Pydantic invariant on the result type
# ---------------------------------------------------------------------------


def test_phase_1_enforcement_result_is_frozen() -> None:
    """``Phase1EnforcementResult`` rejects mutation."""
    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=0.0)

    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    with pytest.raises(pydantic.ValidationError):
        result.drawdown_tier = DrawdownTier.CONSTRAINED


# ---------------------------------------------------------------------------
# Orchestrator-vs-primitive consistency
# ---------------------------------------------------------------------------


def test_result_matches_underlying_primitive_for_tier_1() -> None:
    """The orchestrator's per-rule overrides match what the underlying primitive returns.

    Sanity check the orchestrator does not strip or duplicate primitive output.
    """
    first_tier = NON_HALT_TIERS[0]
    pre_params = baseline_normal_parameters()
    regime_output = _build_regime_output(parameters=pre_params)
    drawdown = make_drawdown_state(intraday_pct=0.0, cumulative_pct=first_tier.trigger_pct)

    expected_params, expected_tier = compose_active_risk_parameters(
        regime_resolved_parameters=regime_output.active_risk_parameter_set,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )

    assert result.active_risk_parameters == expected_params
    assert result.drawdown_tier == expected_tier
