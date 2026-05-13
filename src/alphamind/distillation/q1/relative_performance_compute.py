"""Compute-alias re-export for q1 relative_performance (ALP-467).

See :mod:`.anomalies_compute` for the convention.
"""

from __future__ import annotations

from alphamind.distillation.q1.relative_performance import (
    IntraSectorRankResult,
    RelativePerformanceResult,
    RelativeStrengthRegimeChangeFlag,
    compute_relative_performance,
    detect_relative_strength_regime_change,
    rank_intra_sector,
)

__all__ = [
    "IntraSectorRankResult",
    "RelativePerformanceResult",
    "RelativeStrengthRegimeChangeFlag",
    "compute_relative_performance",
    "detect_relative_strength_regime_change",
    "rank_intra_sector",
]
