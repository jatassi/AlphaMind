"""Repository-provider adapter for ``Phase1EnforcementResult``.

Bridges the guardrail-enforcement orchestrator's output to the zero-arg
awaitable callable shape ``SqlPortfolioStateRepository`` expects for its
``active_risk_parameters_provider`` slot.

Pure typed seam — the implementation is a one-line closure; the value is
that the repository factory and the orchestrator do not need to know about
each other's internals to be wired together.

Design reference: ``docs/design/05-execution-layer/architecture.md`` § 3.
Guardrail enforcement layer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alphamind.execution.guardrail_enforcement.orchestrator import Phase1EnforcementResult
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet


def make_active_risk_parameters_provider(
    result: Phase1EnforcementResult,
) -> Callable[[], Awaitable[ActiveRiskParameterSet]]:
    """Build the provider callable expected by ``SqlPortfolioStateRepository``.

    The returned callable, when awaited, yields ``result.active_risk_parameters``.
    Stateless — same input ``result`` produces a callable that always returns
    the same parameter set.
    """

    async def _provider() -> ActiveRiskParameterSet:
        return result.active_risk_parameters

    return _provider
