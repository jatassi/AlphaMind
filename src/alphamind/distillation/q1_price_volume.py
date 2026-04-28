"""Q1 price/volume distillation — story 02-distillation/08a public surface.

The implementation is split across the :mod:`alphamind.distillation.q1`
sub-package so each indicator family lives in its own module. This file is
the import shim the orchestrator (story 12) will reach for: it re-exports
the public API from each sub-module so call sites do not need to know the
internal layout.

Structure:

- :mod:`alphamind.distillation.q1.indicators` — RSI / MACD / Stochastic /
  Bollinger / Keltner / EMA pairs / ADX / ATR regime.
- :mod:`alphamind.distillation.q1.divergence` — multi-timeframe divergence
  flags.
- :mod:`alphamind.distillation.q1.volume_profile` — volume profile.
- :mod:`alphamind.distillation.q1.gap` — gap analysis.
- :mod:`alphamind.distillation.q1.relative_performance` — relative
  performance vs sector ETF and SPY.
- :mod:`alphamind.distillation.q1.trend_state` — trend state, 52-week range,
  EMA distance, per-name volatility regime.
- :mod:`alphamind.distillation.q1.anomalies` — volume / price-move anomaly
  detections.
- :mod:`alphamind.distillation.q1.output_blocks` — per-sector ``OutputBlock``
  assembly with the per-ticker payload convention.
"""

from __future__ import annotations

from alphamind.distillation.q1.anomalies import (
    detect_price_move_anomaly,
    detect_volume_anomaly,
)
from alphamind.distillation.q1.divergence import (
    DivergenceFlag,
    detect_rsi_divergences,
)
from alphamind.distillation.q1.gap import (
    GAP_KIND_FULL,
    GAP_KIND_PARTIAL,
    GAP_TREND_COUNTER,
    GAP_TREND_WITH,
    DetectedGap,
    GapAnalysisResult,
    analyze_gap,
    detect_session_gap,
    record_pending_gap_event,
    resolve_gap_fill_probability,
)
from alphamind.distillation.q1.indicators import (
    ATR_REGIME_COMPRESSION,
    ATR_REGIME_EXPANSION,
    ATR_REGIME_NEUTRAL,
    AdxResult,
    AtrRegimeResult,
    BollingerResult,
    EmaCrossoverState,
    EmaPairResult,
    KeltnerResult,
    MacdResult,
    RsiResult,
    StochasticResult,
    classify_atr_regime,
    compute_adx,
    compute_bollinger,
    compute_ema_pairs,
    compute_keltner,
    compute_macd,
    compute_rsi,
    compute_stochastic,
)
from alphamind.distillation.q1.output_blocks import (
    AUDIENCE_BY_SECTOR,
    BLOCK_ID_DIVERGENCE_FLAGS,
    BLOCK_ID_GAP,
    BLOCK_ID_RELATIVE_PERFORMANCE,
    BLOCK_ID_TECHNICALS,
    BLOCK_ID_TREND_STATE,
    BLOCK_ID_VOLUME_PROFILE,
    audience_for_sector,
    build_q1_block,
)
from alphamind.distillation.q1.relative_performance import (
    RS_REGIME_LAGGARD,
    RS_REGIME_LEADER,
    RS_REGIME_MID,
    IntraSectorRankResult,
    RelativePerformanceResult,
    RelativeStrengthRegimeChangeFlag,
    compute_relative_performance,
    detect_relative_strength_regime_change,
    rank_intra_sector,
)
from alphamind.distillation.q1.trend_state import (
    TREND_RANGE_BOUND,
    TREND_TRENDING_DOWN,
    TREND_TRENDING_UP,
    VOLATILITY_REGIME_HIGH_EXPANSION,
    VOLATILITY_REGIME_LOW_COMPRESSION,
    VOLATILITY_REGIME_TRANSITIONAL,
    classify_trend_state,
    classify_volatility_regime,
    compute_distance_from_ema_in_atr,
    compute_fifty_two_week_range_percentile,
    compute_multi_timeframe_trend_score,
)
from alphamind.distillation.q1.volume_profile import (
    PROFILE_DEVELOPING,
    PROFILE_SETTLED,
    SETTLED_OVERLAP_THRESHOLD,
    VALUE_AREA_VOLUME_FRACTION,
    PriceLevelVolume,
    VolumeProfileResult,
    classify_session_profile,
    compute_volume_profile,
)

__all__ = [
    "ATR_REGIME_COMPRESSION",
    "ATR_REGIME_EXPANSION",
    "ATR_REGIME_NEUTRAL",
    "AUDIENCE_BY_SECTOR",
    "BLOCK_ID_DIVERGENCE_FLAGS",
    "BLOCK_ID_GAP",
    "BLOCK_ID_RELATIVE_PERFORMANCE",
    "BLOCK_ID_TECHNICALS",
    "BLOCK_ID_TREND_STATE",
    "BLOCK_ID_VOLUME_PROFILE",
    "GAP_KIND_FULL",
    "GAP_KIND_PARTIAL",
    "GAP_TREND_COUNTER",
    "GAP_TREND_WITH",
    "PROFILE_DEVELOPING",
    "PROFILE_SETTLED",
    "RS_REGIME_LAGGARD",
    "RS_REGIME_LEADER",
    "RS_REGIME_MID",
    "SETTLED_OVERLAP_THRESHOLD",
    "TREND_RANGE_BOUND",
    "TREND_TRENDING_DOWN",
    "TREND_TRENDING_UP",
    "VALUE_AREA_VOLUME_FRACTION",
    "VOLATILITY_REGIME_HIGH_EXPANSION",
    "VOLATILITY_REGIME_LOW_COMPRESSION",
    "VOLATILITY_REGIME_TRANSITIONAL",
    "AdxResult",
    "AtrRegimeResult",
    "BollingerResult",
    "DetectedGap",
    "DivergenceFlag",
    "EmaCrossoverState",
    "EmaPairResult",
    "GapAnalysisResult",
    "IntraSectorRankResult",
    "KeltnerResult",
    "MacdResult",
    "PriceLevelVolume",
    "RelativePerformanceResult",
    "RelativeStrengthRegimeChangeFlag",
    "RsiResult",
    "StochasticResult",
    "VolumeProfileResult",
    "analyze_gap",
    "audience_for_sector",
    "build_q1_block",
    "classify_atr_regime",
    "classify_session_profile",
    "classify_trend_state",
    "classify_volatility_regime",
    "compute_adx",
    "compute_bollinger",
    "compute_distance_from_ema_in_atr",
    "compute_ema_pairs",
    "compute_fifty_two_week_range_percentile",
    "compute_keltner",
    "compute_macd",
    "compute_multi_timeframe_trend_score",
    "compute_relative_performance",
    "compute_rsi",
    "compute_stochastic",
    "compute_volume_profile",
    "detect_price_move_anomaly",
    "detect_relative_strength_regime_change",
    "detect_rsi_divergences",
    "detect_session_gap",
    "detect_volume_anomaly",
    "rank_intra_sector",
    "record_pending_gap_event",
    "resolve_gap_fill_probability",
]
