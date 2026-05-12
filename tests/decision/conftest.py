"""Shared fixtures for the decision-layer tests (analyst / strategist / portfolio_manager).

Centralizes the ``ActiveRiskParameterSet`` builder that routes through the
Phase-1 guardrail-enforcement orchestrator (``compose_phase_1_enforcement``)
rather than constructing the record inline. ALP-397 / story 03b migration.

The helper exists so every decision-layer fixture that previously constructed
:class:`ActiveRiskParameterSet` ad-hoc now obtains its value via the canonical
composition path. Consumers continue to receive a frozen
:class:`ActiveRiskParameterSet`; the call surface change is purely structural.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.config.models.regimes import Regime
from alphamind.execution.guardrail_enforcement import compose_phase_1_enforcement
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationOutput,
    RegimeAdaptationState,
)
from tests.execution.guardrail_enforcement._helpers import TIERS

if TYPE_CHECKING:
    from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterEntry


_GUARDRAIL_LABEL_TO_REGIME: dict[RegimeLabel, Regime] = {
    RegimeLabel.LOW_VOL: Regime.low_vol,
    RegimeLabel.NORMAL: Regime.normal,
    RegimeLabel.ELEVATED: Regime.elevated,
    RegimeLabel.CRISIS: Regime.crisis,
}


def compose_active_risk_parameters_via_orchestrator(
    *,
    regime_label: RegimeLabel = RegimeLabel.NORMAL,
    transition_state: RegimeTransitionState = RegimeTransitionState.STABLE,
    transition_invocations_remaining: int = 0,
    parameter_change_flag: bool = False,
    entries: tuple[ActiveRiskParameterEntry, ...] = (),
    active_overlays: tuple[str, ...] = (),
) -> ActiveRiskParameterSet:
    """Build an :class:`ActiveRiskParameterSet` via ``compose_phase_1_enforcement``.

    Wraps the supplied baseline parameters in a synthetic
    :class:`RegimeAdaptationOutput` plus a zero-drawdown
    :class:`DrawdownState`, then routes through the canonical Phase-1
    enforcement entry point. With ``current_drawdown_pct == 0.0`` the
    progressive-tier override path is a no-op (the orchestrator's classifier
    returns ``None``), so the baseline parameters pass through unchanged —
    values, regime label, transition state, and overlays preserved.

    Story 03b (ALP-397) migration: every decision-layer fixture that previously
    constructed :class:`ActiveRiskParameterSet` inline now obtains its value
    via this helper so the canonical composition path is exercised.
    """
    baseline = ActiveRiskParameterSet(
        regime_label=regime_label,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        parameter_change_flag=parameter_change_flag,
        entries=entries,
        active_overlays=active_overlays,
    )
    regime_state = RegimeAdaptationState(
        as_of="2026-05-09T12:00:00+00:00",
        invocation_id="INV-fixture",
        active_regime=_GUARDRAIL_LABEL_TO_REGIME[regime_label],
        prior_regime=None,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="vol_expansion",
        distillation_vix_level=18.5,
        regime_skip_emergency=False,
    )
    regime_output = RegimeAdaptationOutput(
        runtime_dimensions_active_regime=_GUARDRAIL_LABEL_TO_REGIME[regime_label],
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=baseline,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=regime_state,
        audit_log_entries=(),
    )
    drawdown = DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )
    # ``TIERS`` is the canonical progressive-tier sequence loaded once at
    # import time from ``config/guardrails.yaml`` (see the wave-1/2 helper).
    # With ``current_drawdown_pct == 0.0`` the classifier returns ``None``
    # regardless of tier values — the override is a no-op and the baseline
    # parameters pass through unchanged.
    result = compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=TIERS,
    )
    return result.active_risk_parameters
