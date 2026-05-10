"""Composition of regime-resolved parameters with cumulative-drawdown tier overrides.

Pure synchronous primitive that wraps the two breach-behavior primitives
(``classify_cumulative_drawdown_tier`` + ``apply_progressive_tier_overrides``)
into a single canonical entry point. Consumed by the Phase 1 enforcement
orchestrator (story 02) and (eventually) by the continuous monitor when it
recomputes parameters mid-session at an emergency trigger.

Design reference: ``docs/design/05-execution-layer/architecture.md`` § 3.
Guardrail enforcement layer; ``docs/design/06-risk-guardrails/breach-behavior.md``
§ Cumulative drawdown response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alphamind.risk_guardrails.breach_behavior import (
    apply_progressive_tier_overrides,
    classify_cumulative_drawdown_tier,
)

if TYPE_CHECKING:
    from alphamind.config.models.guardrails import ProgressiveTier
    from alphamind.portfolio_state.records.capital import (
        ActiveRiskParameterSet,
        DrawdownState,
    )
    from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier


def compose_active_risk_parameters(
    *,
    regime_resolved_parameters: ActiveRiskParameterSet,
    drawdown_state: DrawdownState,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> tuple[ActiveRiskParameterSet, DrawdownTier | None]:
    """Apply cumulative-drawdown progressive-tier overrides on top of regime-resolved parameters.

    1. Classify the drawdown tier via ``classify_cumulative_drawdown_tier``.
    2. Apply the tier override via ``apply_progressive_tier_overrides`` (no-op
       when tier is ``None``).
    3. Return the final parameter set + the classified tier.

    Pure function. No DB reads, no mutation of inputs. Same inputs always
    produce identical outputs.
    """
    tier = classify_cumulative_drawdown_tier(
        current_drawdown_pct=drawdown_state.current_drawdown_pct,
        progressive_tiers=progressive_tiers,
    )
    final_parameters = apply_progressive_tier_overrides(
        active_risk_parameters=regime_resolved_parameters,
        tier=tier,
        progressive_tiers=progressive_tiers,
    )
    return final_parameters, tier
