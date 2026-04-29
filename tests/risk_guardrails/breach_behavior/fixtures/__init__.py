"""Fixture builders for the breach-behavior end-to-end scenario tests (story 08).

Helpers under this package construct typed records (PositionRecord,
ActiveRiskParameterSet, RiskBudgetConsumption, DrawdownState, plus library
Protocol stubs) from compact keyword arguments. They centralize the setup
boilerplate that would otherwise dominate scenario tests, leaving each test
focused on the breach-behavior assertion.

These builders live under ``tests/`` not ``src/``; production code does not
import them.
"""

from tests.risk_guardrails.breach_behavior.fixtures.builders import (
    ScriptedLibrary,
    StubLibraryConfig,
    StubLibraryOutput,
    StubMarketInputs,
    StubPortfolioState,
    StubRuleProjection,
    make_active_risk_parameters,
    make_drawdown_state,
    make_position_record,
    make_risk_budget,
)

__all__ = [
    "ScriptedLibrary",
    "StubLibraryConfig",
    "StubLibraryOutput",
    "StubMarketInputs",
    "StubPortfolioState",
    "StubRuleProjection",
    "make_active_risk_parameters",
    "make_drawdown_state",
    "make_position_record",
    "make_risk_budget",
]
