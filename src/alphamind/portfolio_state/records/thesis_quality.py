"""Backward-compat re-export. Thesis quality aggregates moved to ``aggregates/``
in ALP-347.

Prefer ``from alphamind.portfolio_state.aggregates import ...`` (or
``...aggregates.thesis_quality``) for new code. This shim exists so the ~22
existing import sites keep working unchanged.
"""

from __future__ import annotations

from alphamind.portfolio_state.aggregates.thesis_quality import (
    AlphaBetaDecomposition,
    AttributionDimension,
    ConvictionCalibrationEntry,
    ConvictionSizingDeviation,
    InvalidationTimingClass,
    InvalidationTimingStat,
    PerformanceAttributionEntry,
    RegimeLabel,
    ResolutionWindowCounts,
    SignalHitRate,
    SignalToThesisConversion,
    ThesisDurationStat,
    ThesisQualityAggregate,
    ThesisType,
    TrailingWindow,
)

__all__ = [
    "AlphaBetaDecomposition",
    "AttributionDimension",
    "ConvictionCalibrationEntry",
    "ConvictionSizingDeviation",
    "InvalidationTimingClass",
    "InvalidationTimingStat",
    "PerformanceAttributionEntry",
    "RegimeLabel",
    "ResolutionWindowCounts",
    "SignalHitRate",
    "SignalToThesisConversion",
    "ThesisDurationStat",
    "ThesisQualityAggregate",
    "ThesisType",
    "TrailingWindow",
]
