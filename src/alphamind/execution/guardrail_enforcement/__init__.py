"""Guardrail enforcement layer (execution-layer engine component).

Sits between portfolio manager command intake and broker adapter routing.
Composes the canonical ``ActiveRiskParameterSet`` consumed by state-delivery
renderers and the engine's T3 validation, and produces synchronous
hard-rejection feedback to the PM. Mode-agnostic.

Design reference: ``docs/design/05-execution-layer/architecture.md`` § 3.
"""

from alphamind.execution.guardrail_enforcement.composition import (
    compose_active_risk_parameters,
)
from alphamind.execution.guardrail_enforcement.orchestrator import (
    Phase1EnforcementResult,
    compose_phase_1_enforcement,
)
from alphamind.execution.guardrail_enforcement.repository_provider import (
    make_active_risk_parameters_provider,
)

__all__ = [
    "Phase1EnforcementResult",
    "compose_active_risk_parameters",
    "compose_phase_1_enforcement",
    "make_active_risk_parameters_provider",
]
