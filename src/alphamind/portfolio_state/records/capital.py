"""Backward-compat re-export. Capital records were split in ALP-347 into:

* ``records/cash.py`` — Tier 1 cash ledger.
* ``aggregates/drawdown.py`` — Tier 3 drawdown aggregate.
* ``aggregates/risk_budget.py`` — Tier 3 risk-budget consumption.
* ``aggregates/risk_parameters.py`` — Tier 3 active risk parameter set.

The four risk-guardrail enums (``RegimeLabel``, ``RegimeTransitionState``,
``RiskZone``, ``DrawdownTier``) live under ``risk_guardrails/`` (relocated in
ALP-344) and are re-exported here so the legacy importers that still reference
``portfolio_state.records.capital`` continue to work. Identity is preserved
across all import paths.
"""

from __future__ import annotations

from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.cash import (
    CashLedger,
    UnsettledProceedsEntry,
)
from alphamind.risk_guardrails.breach_behavior.types import DrawdownTier
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone
from alphamind.risk_guardrails.regime_adaptation.types import (
    RegimeLabel,
    RegimeTransitionState,
)

__all__ = [
    "ActiveRiskParameterEntry",
    "ActiveRiskParameterSet",
    "CashLedger",
    "DrawdownState",
    "DrawdownTier",
    "RegimeLabel",
    "RegimeTransitionState",
    "RiskBudgetConsumption",
    "RiskBudgetEntry",
    "RiskZone",
    "UnsettledProceedsEntry",
]
