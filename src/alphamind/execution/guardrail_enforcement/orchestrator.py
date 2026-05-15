"""Phase 1 guardrail-enforcement orchestrator.

Per-invocation Phase 1 entry point that wraps a regime-adaptation output with
the drawdown-tier composition primitive (story 01) and bundles the result.

Pure synchronous function. Caller is responsible for invoking
``resolve_regime_adaptation`` and reading ``DrawdownState`` from the
repository — this orchestrator only composes their outputs.

Design reference: ``docs/design/05-execution-layer/architecture.md`` § 3.
Guardrail enforcement layer; ``docs/design/06-risk-guardrails/state-delivery.md``
§ Portfolio state ingestion payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from alphamind.execution.guardrail_enforcement.composition import (
    compose_active_risk_parameters,
)
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier

if TYPE_CHECKING:
    from alphamind.config.models.guardrails import ProgressiveTier
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeAdaptationOutput


@dataclass(frozen=True, slots=True)
class Phase1EnforcementResult:
    """Per-invocation Phase 1 enforcement-layer output.

    Bundles the canonical ``ActiveRiskParameterSet`` (regime-resolved +
    drawdown-tier-overridden) consumed by state-delivery renderers, the
    validation tool, and the engine T3 check, plus the classified
    drawdown tier for downstream halt-mode and emergency-trigger logic.
    """

    active_risk_parameters: ActiveRiskParameterSet
    drawdown_tier: DrawdownTier | None


def compose_phase_1_enforcement(
    *,
    regime_output: RegimeAdaptationOutput,
    drawdown_state: DrawdownState,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> Phase1EnforcementResult:
    """Compose Phase 1 enforcement output from regime + drawdown inputs.

    Extracts ``regime_output.active_risk_parameter_set`` as the regime-resolved
    starting point, calls :func:`compose_active_risk_parameters` to apply
    progressive-tier overrides, and bundles the result.

    Pure function — same inputs always produce identical outputs. Caller
    is responsible for invoking ``resolve_regime_adaptation`` and reading
    ``DrawdownState`` from the repository.
    """
    active_risk_parameters, drawdown_tier = compose_active_risk_parameters(
        regime_resolved_parameters=regime_output.active_risk_parameter_set,
        drawdown_state=drawdown_state,
        progressive_tiers=progressive_tiers,
    )
    return Phase1EnforcementResult(
        active_risk_parameters=active_risk_parameters,
        drawdown_tier=drawdown_tier,
    )
