"""Tests for the trend state and per-name volatility regime — story 08a.

Covers:

- Per-timeframe trend state ∈ {trending_up, trending_down, range_bound}.
- Composite multi-timeframe trend score (daily and 4h dominate).
- 52-week range percentile and distance from EMAs in ATR units.
- Per-name volatility regime ∈ {low_vol_compression, high_vol_expansion,
  transitional} based on Bollinger band width vs. its 60-day baseline.
"""

from __future__ import annotations

import pytest

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


class TestClassifyTrendState:
    def test_strong_positive_adx_with_uptrending_emas_is_trending_up(self) -> None:
        result = classify_trend_state(
            adx=35.0,
            ema_20_slope=1.5,
            ema_50_slope=1.0,
        )
        assert result == TREND_TRENDING_UP

    def test_strong_positive_adx_with_downtrending_emas_is_trending_down(self) -> None:
        result = classify_trend_state(
            adx=35.0,
            ema_20_slope=-1.5,
            ema_50_slope=-1.0,
        )
        assert result == TREND_TRENDING_DOWN

    def test_low_adx_is_range_bound(self) -> None:
        result = classify_trend_state(
            adx=15.0,
            ema_20_slope=0.1,
            ema_50_slope=0.05,
        )
        assert result == TREND_RANGE_BOUND


class TestComputeMultiTimeframeTrendScore:
    def test_all_timeframes_uptrending_yields_strong_positive_score(self) -> None:
        per_tf = {
            "15min": TREND_TRENDING_UP,
            "1h": TREND_TRENDING_UP,
            "4h": TREND_TRENDING_UP,
            "1d": TREND_TRENDING_UP,
            "1w": TREND_TRENDING_UP,
        }
        score = compute_multi_timeframe_trend_score(per_tf)
        # All timeframes pull the score positive — saturate to a positive
        # value within [-1, 1].
        assert score > 0.5

    def test_all_timeframes_downtrending_yields_strong_negative_score(self) -> None:
        per_tf = {
            "15min": TREND_TRENDING_DOWN,
            "1h": TREND_TRENDING_DOWN,
            "4h": TREND_TRENDING_DOWN,
            "1d": TREND_TRENDING_DOWN,
            "1w": TREND_TRENDING_DOWN,
        }
        score = compute_multi_timeframe_trend_score(per_tf)
        assert score < -0.5

    def test_mixed_timeframes_yields_score_near_zero(self) -> None:
        per_tf = {
            "15min": TREND_TRENDING_UP,
            "1h": TREND_TRENDING_DOWN,
            "4h": TREND_RANGE_BOUND,
            "1d": TREND_TRENDING_DOWN,
            "1w": TREND_TRENDING_UP,
        }
        score = compute_multi_timeframe_trend_score(per_tf)
        assert -0.5 < score < 0.5

    def test_higher_timeframes_dominate(self) -> None:
        """A daily uptrend with conflicting 15-min should be net-positive."""
        per_tf_daily_dominant = {
            "15min": TREND_TRENDING_DOWN,
            "1h": TREND_RANGE_BOUND,
            "4h": TREND_TRENDING_UP,
            "1d": TREND_TRENDING_UP,
            "1w": TREND_TRENDING_UP,
        }
        score = compute_multi_timeframe_trend_score(per_tf_daily_dominant)
        assert score > 0.0


class TestComputeFiftyTwoWeekRangePercentile:
    def test_at_high_returns_one(self) -> None:
        result = compute_fifty_two_week_range_percentile(
            current_price=120.0,
            year_high=120.0,
            year_low=100.0,
        )
        assert result == pytest.approx(1.0)

    def test_at_low_returns_zero(self) -> None:
        result = compute_fifty_two_week_range_percentile(
            current_price=100.0,
            year_high=120.0,
            year_low=100.0,
        )
        assert result == pytest.approx(0.0)

    def test_at_midpoint_returns_half(self) -> None:
        result = compute_fifty_two_week_range_percentile(
            current_price=110.0,
            year_high=120.0,
            year_low=100.0,
        )
        assert result == pytest.approx(0.5)


class TestComputeDistanceFromEmaInAtr:
    def test_one_atr_above_ema_returns_one(self) -> None:
        result = compute_distance_from_ema_in_atr(
            price=102.0,
            ema=100.0,
            atr=2.0,
        )
        assert result == pytest.approx(1.0)

    def test_below_ema_returns_negative(self) -> None:
        result = compute_distance_from_ema_in_atr(
            price=98.0,
            ema=100.0,
            atr=2.0,
        )
        assert result == pytest.approx(-1.0)


class TestClassifyVolatilityRegime:
    def test_band_width_well_below_baseline_is_compression(self) -> None:
        result = classify_volatility_regime(
            current_band_width=1.0,
            baseline_mean=2.0,
            baseline_stdev=0.2,
        )
        assert result == VOLATILITY_REGIME_LOW_COMPRESSION

    def test_band_width_well_above_baseline_is_expansion(self) -> None:
        result = classify_volatility_regime(
            current_band_width=3.0,
            baseline_mean=2.0,
            baseline_stdev=0.2,
        )
        assert result == VOLATILITY_REGIME_HIGH_EXPANSION

    def test_band_width_within_band_is_transitional(self) -> None:
        result = classify_volatility_regime(
            current_band_width=2.05,
            baseline_mean=2.0,
            baseline_stdev=0.2,
        )
        assert result == VOLATILITY_REGIME_TRANSITIONAL
