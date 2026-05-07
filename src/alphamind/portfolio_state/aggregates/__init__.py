"""Tier 3 — derived/aggregate records per ``state-persistence.md`` § Tier 3.

Pre-computed values that accelerate the read path; not authoritative — always
recomputable from Tier 1 + Tier 2 data. Hosts drawdown, risk-budget consumption,
the active risk parameter set, and the thesis-quality aggregate consumed by
raw state category 6.
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
from alphamind.portfolio_state.aggregates.thesis_quality import (
    AlphaBetaDecomposition,
    AttributionDimension,
    ConvictionCalibrationEntry,
    ConvictionSizingDeviation,
    InvalidationTimingClass,
    InvalidationTimingStat,
    PerformanceAttributionEntry,
    ResolutionWindowCounts,
    SignalHitRate,
    SignalToThesisConversion,
    ThesisDurationStat,
    ThesisQualityAggregate,
    ThesisType,
    TrailingWindow,
)

__all__ = [
    "ActiveRiskParameterEntry",
    "ActiveRiskParameterSet",
    "AlphaBetaDecomposition",
    "AttributionDimension",
    "ConvictionCalibrationEntry",
    "ConvictionSizingDeviation",
    "DrawdownState",
    "InvalidationTimingClass",
    "InvalidationTimingStat",
    "PerformanceAttributionEntry",
    "ResolutionWindowCounts",
    "RiskBudgetConsumption",
    "RiskBudgetEntry",
    "SignalHitRate",
    "SignalToThesisConversion",
    "ThesisDurationStat",
    "ThesisQualityAggregate",
    "ThesisType",
    "TrailingWindow",
]
