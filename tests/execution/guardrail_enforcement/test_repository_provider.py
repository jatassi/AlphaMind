"""Tests for ``make_active_risk_parameters_provider`` (story 03a).

Adapter that turns a ``Phase1EnforcementResult`` into the zero-arg callable
shape ``SqlPortfolioStateRepository`` expects for its
``active_risk_parameters_provider`` slot.

Per ALP-454 Pre-resolved decision (C): the provider is synchronous,
matching the sync ``PortfolioStateRepository`` Protocol.
"""

from __future__ import annotations

from collections.abc import Callable

from alphamind.execution.guardrail_enforcement import (
    Phase1EnforcementResult,
    make_active_risk_parameters_provider,
)
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from tests.execution.guardrail_enforcement._helpers import baseline_normal_parameters


def _build_result(parameters: ActiveRiskParameterSet) -> Phase1EnforcementResult:
    return Phase1EnforcementResult(
        active_risk_parameters=parameters,
        drawdown_tier=None,
    )


# ---------------------------------------------------------------------------
# Tracer bullet — provider yields the parameter set
# ---------------------------------------------------------------------------


def test_provider_yields_result_active_risk_parameters() -> None:
    """The provider returns ``result.active_risk_parameters`` (object identity)."""
    parameters = baseline_normal_parameters()
    result = _build_result(parameters)

    provider = make_active_risk_parameters_provider(result)

    yielded = provider()

    assert yielded is result.active_risk_parameters


# ---------------------------------------------------------------------------
# Idempotency — multiple calls return the same parameter set
# ---------------------------------------------------------------------------


def test_provider_returns_same_parameter_set_on_multiple_calls() -> None:
    """Calling the provider multiple times yields the same parameter set object."""
    parameters = baseline_normal_parameters()
    result = _build_result(parameters)

    provider = make_active_risk_parameters_provider(result)

    a = provider()
    b = provider()
    c = provider()

    assert a is b is c
    assert a is parameters


# ---------------------------------------------------------------------------
# Type-shape contract — assignable to the repository's expected callable type
# ---------------------------------------------------------------------------


def test_provider_signature_matches_repository_slot() -> None:
    """The provider type-checks as ``Callable[[], ActiveRiskParameterSet]``.

    This is the slot ``SqlPortfolioStateRepository.__init__`` expects.
    Static checking is mypy's job; this runtime assertion documents the
    contract and binds the helper's output to a variable of the expected
    annotated type so a regression in the return signature surfaces in mypy.
    """
    parameters = baseline_normal_parameters()
    result = _build_result(parameters)

    provider: Callable[[], ActiveRiskParameterSet] = make_active_risk_parameters_provider(result)

    yielded = provider()
    assert isinstance(yielded, ActiveRiskParameterSet)
