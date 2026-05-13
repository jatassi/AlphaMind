"""Compute-alias re-export for q1 trend_state (ALP-467).

See :mod:`.anomalies_compute` for the convention.
"""

from __future__ import annotations

from alphamind.distillation.q1.trend_state import (
    classify_trend_state,
    classify_volatility_regime,
    compute_distance_from_ema_in_atr,
    compute_fifty_two_week_range_percentile,
    compute_multi_timeframe_trend_score,
)

__all__ = [
    "classify_trend_state",
    "classify_volatility_regime",
    "compute_distance_from_ema_in_atr",
    "compute_fifty_two_week_range_percentile",
    "compute_multi_timeframe_trend_score",
]
