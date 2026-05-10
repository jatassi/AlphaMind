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

__all__ = ["compose_active_risk_parameters"]
