"""Repository-provider adapter for ``ActiveGuardrails``.

Bridges the guardrail-enforcement orchestrator's output to the zero-arg
callable shape ``SqlPortfolioStateRepository`` expects for its
``active_risk_parameters_provider`` slot.

Pure typed seam — the implementation is a one-line closure; the value is
that the repository factory and the orchestrator do not need to know about
each other's internals to be wired together.

Design reference: ``docs/design/05-execution-layer/architecture.md`` § 3.
Guardrail enforcement layer. ALP-454 Pre-resolved decision (C): the
provider is synchronous, matching the sync ``PortfolioStateRepository``
Protocol.
"""

from __future__ import annotations

from collections.abc import Callable

from alphamind.execution.guardrail_enforcement.orchestrator import ActiveGuardrails
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet


def make_active_risk_parameters_provider(
    result: ActiveGuardrails,
) -> Callable[[], ActiveRiskParameterSet]:
    """Build the provider callable expected by ``SqlPortfolioStateRepository``.

    The returned callable, when called, yields ``result.active_risk_parameters``.
    Stateless — same input ``result`` produces a callable that always returns
    the same parameter set.
    """

    def _provider() -> ActiveRiskParameterSet:
        return result.active_risk_parameters

    return _provider
