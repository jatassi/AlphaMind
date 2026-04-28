"""Trend state and per-name volatility regime — story 02-distillation/08a.

Per ``docs/design/01-data-layer/external/quantitative.md`` § 1f:

- Per-timeframe trend state ∈ {trending_up, trending_down, range_bound}
  derived from ADX (strength) and EMA slope (direction).
- Composite multi-timeframe trend score (weighted by timeframe per the
  design's "structure of price matters" principle — daily and 4h dominate).
- 52-week range percentile.
- Distance from EMAs in ATR units.
- Per-name volatility regime ∈ {low_vol_compression, high_vol_expansion,
  transitional} based on Bollinger band width vs. its 60-day baseline.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping

from alphamind.distillation.normalization import atr_normalize

# ---------------------------------------------------------------------------
# Trend state labels
# ---------------------------------------------------------------------------

TREND_TRENDING_UP: str = "trending_up"
TREND_TRENDING_DOWN: str = "trending_down"
TREND_RANGE_BOUND: str = "range_bound"


_ADX_TRENDING_FLOOR: float = 20.0
"""ADX value at-or-above which a directional EMA slope qualifies as trending.

Wilder's conventional threshold for a trending market. Algorithmic
constant of the ADX indicator, not a tunable Class A threshold.
"""


# Multi-timeframe trend score tiers. The score is the unweighted mean of
# two tier means — the longer timeframes (4h, 1d) carry the structural
# trend signal, the shorter and weekly timeframes corroborate. Equal
# representation between tiers gives the longer timeframes implicit
# dominance: each long timeframe is one of two, while each secondary is
# one of three, per the design's "structure of price matters" principle.
_PRIMARY_TIMEFRAMES: frozenset[str] = frozenset({"4h", "1d"})
_SECONDARY_TIMEFRAMES: frozenset[str] = frozenset({"15min", "1h", "1w"})


# ---------------------------------------------------------------------------
# Per-name volatility regime labels
# ---------------------------------------------------------------------------


VOLATILITY_REGIME_LOW_COMPRESSION: str = "low_vol_compression"
VOLATILITY_REGIME_HIGH_EXPANSION: str = "high_vol_expansion"
VOLATILITY_REGIME_TRANSITIONAL: str = "transitional"


_VOL_REGIME_SIGMA_BAND: float = 1.0
"""One-stdev band for the volatility regime classifier.

Band-width within plus-or-minus one stdev of the 60-day baseline mean is
transitional; outside is compression (below) or expansion (above).
Algorithmic constant of the classifier, not a Class A threshold.
"""


# ---------------------------------------------------------------------------
# Per-timeframe trend state
# ---------------------------------------------------------------------------


def classify_trend_state(
    *,
    adx: float,
    ema_20_slope: float,
    ema_50_slope: float,
) -> str:
    """Classify the trend state for one timeframe.

    Rule: when ADX clears the trending floor and both EMAs slope the same
    way, return ``trending_up`` / ``trending_down``; otherwise
    ``range_bound``.
    """
    if adx < _ADX_TRENDING_FLOOR:
        return TREND_RANGE_BOUND
    if ema_20_slope > 0 and ema_50_slope > 0:
        return TREND_TRENDING_UP
    if ema_20_slope < 0 and ema_50_slope < 0:
        return TREND_TRENDING_DOWN
    return TREND_RANGE_BOUND


# ---------------------------------------------------------------------------
# Composite multi-timeframe trend score
# ---------------------------------------------------------------------------


def _trend_state_to_signed_value(label: str) -> float:
    if label == TREND_TRENDING_UP:
        return 1.0
    if label == TREND_TRENDING_DOWN:
        return -1.0
    return 0.0


def compute_multi_timeframe_trend_score(per_timeframe: Mapping[str, str]) -> float:
    """Aggregate per-timeframe trend states into a single signed score.

    Score in [-1, 1] — saturates to ±1 when every timeframe agrees, with
    the daily/4h tier carrying the structural signal and the
    15min/1h/1w tier corroborating. Computed as the unweighted mean of
    the two tier means so the dominance arises structurally from tier
    membership rather than from per-timeframe numeric anchors.

    Missing timeframes are treated as range-bound (zero contribution) so
    a partial input does not blow up the score; the caller may decide to
    raise instead.
    """
    primary_values = [
        _trend_state_to_signed_value(per_timeframe.get(tf, TREND_RANGE_BOUND))
        for tf in _PRIMARY_TIMEFRAMES
    ]
    secondary_values = [
        _trend_state_to_signed_value(per_timeframe.get(tf, TREND_RANGE_BOUND))
        for tf in _SECONDARY_TIMEFRAMES
    ]
    primary_mean = statistics.fmean(primary_values) if primary_values else 0.0
    secondary_mean = statistics.fmean(secondary_values) if secondary_values else 0.0
    return statistics.fmean([primary_mean, secondary_mean])


# ---------------------------------------------------------------------------
# 52-week range percentile and EMA distance
# ---------------------------------------------------------------------------


def compute_fifty_two_week_range_percentile(
    *,
    current_price: float,
    year_high: float,
    year_low: float,
) -> float:
    """Position of ``current_price`` in the 52-week range, normalized to [0, 1].

    A degenerate window with ``year_high == year_low`` returns 0.5 — neutral
    midpoint when the range collapses.
    """
    range_width = year_high - year_low
    if range_width == 0.0:
        return 0.5
    return (current_price - year_low) / range_width


def compute_distance_from_ema_in_atr(
    *,
    price: float,
    ema: float,
    atr: float,
) -> float:
    """Distance from ``price`` to the EMA, expressed as a multiple of ATR.

    Positive when price is above the EMA, negative when below — so the
    sign carries directional context without an extra Boolean.
    """
    return atr_normalize(price - ema, atr)


# ---------------------------------------------------------------------------
# Per-name volatility regime
# ---------------------------------------------------------------------------


def classify_volatility_regime(
    *,
    current_band_width: float,
    baseline_mean: float,
    baseline_stdev: float,
) -> str:
    """Classify the per-name Bollinger-bandwidth regime.

    Compression when the current band width is at-or-below ``baseline_mean
    - 1*baseline_stdev``; expansion when at-or-above ``baseline_mean +
    1*baseline_stdev``; transitional otherwise.

    A degenerate baseline (zero stdev) collapses to transitional, consistent
    with how :func:`alphamind.distillation.q1.indicators.classify_atr_regime`
    treats the same edge case for ATR.
    """
    if baseline_stdev == 0.0:
        return VOLATILITY_REGIME_TRANSITIONAL
    z = (current_band_width - baseline_mean) / baseline_stdev
    if z >= _VOL_REGIME_SIGMA_BAND:
        return VOLATILITY_REGIME_HIGH_EXPANSION
    if z <= -_VOL_REGIME_SIGMA_BAND:
        return VOLATILITY_REGIME_LOW_COMPRESSION
    return VOLATILITY_REGIME_TRANSITIONAL


__all__ = [
    "TREND_RANGE_BOUND",
    "TREND_TRENDING_DOWN",
    "TREND_TRENDING_UP",
    "VOLATILITY_REGIME_HIGH_EXPANSION",
    "VOLATILITY_REGIME_LOW_COMPRESSION",
    "VOLATILITY_REGIME_TRANSITIONAL",
    "classify_trend_state",
    "classify_volatility_regime",
    "compute_distance_from_ema_in_atr",
    "compute_fifty_two_week_range_percentile",
    "compute_multi_timeframe_trend_score",
]
