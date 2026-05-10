"""Tests for ``make_active_risk_parameters_provider`` (story 03a).

Adapter that turns a ``Phase1EnforcementResult`` into the zero-arg awaitable
callable shape ``SqlPortfolioStateRepository`` expects for its
``active_risk_parameters_provider`` slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from alphamind.execution.guardrail_enforcement import (
    Phase1EnforcementResult,
    make_active_risk_parameters_provider,
)
from alphamind.portfolio_state.records.capital import ActiveRiskParameterSet
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
    """The provider awaits to ``result.active_risk_parameters`` (object identity)."""
    parameters = baseline_normal_parameters()
    result = _build_result(parameters)

    provider = make_active_risk_parameters_provider(result)

    async def _call() -> ActiveRiskParameterSet:
        return await provider()

    yielded = asyncio.run(_call())

    assert yielded is result.active_risk_parameters


# ---------------------------------------------------------------------------
# Idempotency — multiple calls return the same parameter set
# ---------------------------------------------------------------------------


def test_provider_returns_same_parameter_set_on_multiple_calls() -> None:
    """Calling the provider multiple times yields the same parameter set object."""
    parameters = baseline_normal_parameters()
    result = _build_result(parameters)

    provider = make_active_risk_parameters_provider(result)

    async def _call_three_times() -> tuple[
        ActiveRiskParameterSet, ActiveRiskParameterSet, ActiveRiskParameterSet
    ]:
        a = await provider()
        b = await provider()
        c = await provider()
        return a, b, c

    a, b, c = asyncio.run(_call_three_times())

    assert a is b is c
    assert a is parameters


# ---------------------------------------------------------------------------
# Type-shape contract — assignable to the repository's expected callable type
# ---------------------------------------------------------------------------


def test_provider_signature_matches_repository_slot() -> None:
    """The provider type-checks as ``Callable[[], Awaitable[ActiveRiskParameterSet]]``.

    This is the slot ``SqlPortfolioStateRepository.__init__`` expects.
    Static checking is mypy's job; this runtime assertion documents the
    contract and binds the helper's output to a variable of the expected
    annotated type so a regression in the return signature surfaces in mypy.
    """
    parameters = baseline_normal_parameters()
    result = _build_result(parameters)

    provider: Callable[[], Awaitable[ActiveRiskParameterSet]] = (
        make_active_risk_parameters_provider(result)
    )

    async def _call() -> ActiveRiskParameterSet:
        return await provider()

    yielded = asyncio.run(_call())
    assert isinstance(yielded, ActiveRiskParameterSet)
