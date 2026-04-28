"""Tests for the multi-timeframe technical indicators — story 08a.

Each indicator (RSI, MACD, Stochastic, Bollinger, Keltner, EMA pairs, ADX,
ATR regime) has one canonical test against a hand-constructed bar series
with known expected values.

The bars are simple, monotonic, or oscillating sequences chosen so the
expected indicator value can be derived analytically — failing tests here
indicate the formula is wrong, not that the data fixture moved.
"""

from __future__ import annotations

import math

import pytest

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

# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


class TestComputeRsi:
    def test_strict_uptrend_yields_rsi_near_100(self) -> None:
        """A monotonically rising close series with no losses produces RSI ~100."""
        # 20 monotonically increasing closes — no down bar, so the average loss
        # is 0 and RSI saturates at 100.
        closes = [100.0 + i for i in range(20)]
        result = compute_rsi(closes, period=14)
        assert isinstance(result, RsiResult)
        assert result.value == pytest.approx(100.0)

    def test_strict_downtrend_yields_rsi_near_zero(self) -> None:
        """A monotonically falling close series with no gains produces RSI ~0."""
        closes = [100.0 - i for i in range(20)]
        result = compute_rsi(closes, period=14)
        assert result.value == pytest.approx(0.0)

    def test_balanced_series_yields_rsi_near_50(self) -> None:
        """Equal up/down moves over many bars centers RSI near 50."""
        # Alternating +1 / -1 moves — gains and losses cancel.
        closes = [100.0]
        for i in range(40):
            closes.append(closes[-1] + (1.0 if i % 2 == 0 else -1.0))
        result = compute_rsi(closes, period=14)
        assert 45.0 <= result.value <= 55.0

    def test_too_short_series_raises(self) -> None:
        """Need at least period + 1 closes to compute RSI."""
        closes = [100.0, 101.0, 102.0]
        with pytest.raises(ValueError, match="period"):
            compute_rsi(closes, period=14)


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


class TestComputeMacd:
    def test_uptrend_produces_bullish_signal_state(self) -> None:
        """An accelerating uptrend has the MACD line above the signal line."""
        # Accelerating closes: linearly-increasing closes settle into a
        # constant MACD plateau where macd ≈ signal; an *accelerating* slope
        # keeps the fast EMA pulling ahead of the slow EMA and the signal
        # lagging the macd line, so histogram stays > 0.
        closes = [100.0 + 0.05 * i * i for i in range(60)]
        result = compute_macd(closes, fast_period=12, slow_period=26, signal_period=9)
        assert isinstance(result, MacdResult)
        assert result.macd_line > 0
        assert result.signal_line > 0
        assert result.histogram > 0
        assert result.crossover_state == "bullish"

    def test_downtrend_produces_bearish_signal_state(self) -> None:
        """An accelerating downtrend has the MACD line below the signal line."""
        closes = [400.0 - 0.05 * i * i for i in range(60)]
        result = compute_macd(closes, fast_period=12, slow_period=26, signal_period=9)
        assert result.macd_line < 0
        assert result.signal_line < 0
        assert result.histogram < 0
        assert result.crossover_state == "bearish"


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------


class TestComputeStochastic:
    def test_close_at_period_high_produces_k_at_100(self) -> None:
        """When close is at the K-period high, %K saturates at 100."""
        # Monotonically rising bars; the last close equals the last (and
        # highest) close in the period.
        highs = [100.0 + i for i in range(20)]
        lows = [99.0 + i for i in range(20)]
        closes = [99.5 + i for i in range(20)]
        # Force last close to equal the last high so %K = 100.
        closes[-1] = highs[-1]
        result = compute_stochastic(highs, lows, closes, k_period=14, d_period=3, smooth_k=3)
        assert isinstance(result, StochasticResult)
        assert result.k > 90.0  # near 100; smoothed %K may smooth slightly

    def test_close_at_period_low_produces_k_at_zero(self) -> None:
        highs = [100.0 - i for i in range(20)]
        lows = [99.0 - i for i in range(20)]
        closes = [99.5 - i for i in range(20)]
        closes[-1] = lows[-1]
        result = compute_stochastic(highs, lows, closes, k_period=14, d_period=3, smooth_k=3)
        assert result.k < 10.0


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


class TestComputeBollinger:
    def test_constant_series_collapses_band_width_to_zero(self) -> None:
        """Zero variance → upper == middle == lower; width = 0."""
        closes = [100.0] * 30
        result = compute_bollinger(closes, period=20, num_std=2.0)
        assert isinstance(result, BollingerResult)
        assert result.upper == pytest.approx(result.middle)
        assert result.lower == pytest.approx(result.middle)
        assert result.band_width == pytest.approx(0.0)

    def test_close_at_upper_band_produces_position_at_one(self) -> None:
        """When the close lands on the upper band, position normalizes to 1."""
        # Hand-construct closes with known mean and stdev, then read the
        # upper band off the result and set the final close equal to it.
        closes = [100.0 + (1.0 if i % 2 == 0 else -1.0) for i in range(20)]
        first_result = compute_bollinger(closes, period=20, num_std=2.0)
        # Replace the last close with the upper-band value while keeping the
        # earlier 19 closes — the period-20 SMA / stdev are unchanged because
        # they read the trailing 20 closes; the new last close shifts only
        # the position read.
        adjusted_closes = [*closes[:-1], first_result.upper]
        result = compute_bollinger(adjusted_closes, period=20, num_std=2.0)
        # Recompute against the adjusted series — the last close is now the
        # upper band of the original, but the band has moved slightly because
        # one of its 20 inputs changed. Position is at the moved upper band.
        assert result.position_within_bands == pytest.approx(1.0, abs=0.10)


# ---------------------------------------------------------------------------
# Keltner Channels
# ---------------------------------------------------------------------------


class TestComputeKeltner:
    def test_keltner_position_centered_on_steady_close(self) -> None:
        """When the close hugs the EMA, Keltner position is ~0.5."""
        # Stable bars: EMA ≈ close ≈ midpoint of channel.
        highs = [101.0] * 30
        lows = [99.0] * 30
        closes = [100.0] * 30
        result = compute_keltner(highs, lows, closes, period=20, atr_multiple=2.0)
        assert isinstance(result, KeltnerResult)
        assert result.position_within_channel == pytest.approx(0.5, abs=0.05)
        # Channel width is positive — high/low range gives the ATR a non-zero floor.
        assert result.channel_width > 0


# ---------------------------------------------------------------------------
# EMA pairs (20 / 50 / 200)
# ---------------------------------------------------------------------------


class TestComputeEmaPairs:
    def test_sustained_uptrend_yields_positive_slopes_and_bullish_crossovers(self) -> None:
        """All three EMAs slope up; faster EMA above slower EMA = bullish."""
        closes = [100.0 + 0.3 * i for i in range(250)]
        result = compute_ema_pairs(closes)
        assert isinstance(result, EmaPairResult)
        assert result.ema_20_slope > 0
        assert result.ema_50_slope > 0
        assert result.ema_200_slope > 0
        assert result.crossover_state_20_50 == EmaCrossoverState.BULLISH
        assert result.crossover_state_50_200 == EmaCrossoverState.BULLISH

    def test_sustained_downtrend_yields_negative_slopes_and_bearish_crossovers(self) -> None:
        closes = [200.0 - 0.3 * i for i in range(250)]
        result = compute_ema_pairs(closes)
        assert result.ema_20_slope < 0
        assert result.ema_50_slope < 0
        assert result.ema_200_slope < 0
        assert result.crossover_state_20_50 == EmaCrossoverState.BEARISH
        assert result.crossover_state_50_200 == EmaCrossoverState.BEARISH


# ---------------------------------------------------------------------------
# ADX
# ---------------------------------------------------------------------------


class TestComputeAdx:
    def test_strong_trend_produces_high_adx(self) -> None:
        """A persistent directional trend pushes ADX above 25 (conventional 'trending')."""
        # Steady uptrend with widening daily range — strong directional movement.
        highs = [100.0 + i + 0.5 for i in range(60)]
        lows = [99.5 + i for i in range(60)]
        closes = [100.0 + i for i in range(60)]
        result = compute_adx(highs, lows, closes, period=14)
        assert isinstance(result, AdxResult)
        # ADX is direction-agnostic, so it should be high regardless of trend sign.
        assert result.value > 25.0

    def test_choppy_range_produces_low_adx(self) -> None:
        """An oscillating range with no net direction produces low ADX."""
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        for i in range(60):
            phase = i % 4
            base = 100.0 + (1.0 if phase < 2 else -1.0) * 0.5
            highs.append(base + 0.5)
            lows.append(base - 0.5)
            closes.append(base)
        result = compute_adx(highs, lows, closes, period=14)
        assert result.value < 25.0


# ---------------------------------------------------------------------------
# ATR regime
# ---------------------------------------------------------------------------


class TestClassifyAtrRegime:
    def test_atr_well_above_baseline_yields_expansion(self) -> None:
        """Current ATR above baseline mean + 1 stdev should classify as expansion."""
        baseline_mean = 1.0
        baseline_stdev = 0.1
        current_atr = baseline_mean + 2.0 * baseline_stdev
        result = classify_atr_regime(
            current_atr=current_atr,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
        )
        assert isinstance(result, AtrRegimeResult)
        assert result.regime == ATR_REGIME_EXPANSION

    def test_atr_well_below_baseline_yields_compression(self) -> None:
        baseline_mean = 1.0
        baseline_stdev = 0.1
        current_atr = baseline_mean - 2.0 * baseline_stdev
        result = classify_atr_regime(
            current_atr=current_atr,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
        )
        assert result.regime == ATR_REGIME_COMPRESSION

    def test_atr_within_one_sigma_yields_neutral(self) -> None:
        """0.5 stdev above baseline mean is well inside the neutral band."""
        baseline_mean = 1.0
        baseline_stdev = 0.1
        current_atr = baseline_mean + 0.5 * baseline_stdev
        result = classify_atr_regime(
            current_atr=current_atr,
            baseline_mean=baseline_mean,
            baseline_stdev=baseline_stdev,
        )
        assert result.regime == ATR_REGIME_NEUTRAL


# ---------------------------------------------------------------------------
# Smoke checks at edge cases — guarded so a regression in basic math is loud.
# ---------------------------------------------------------------------------


def test_rsi_handles_period_plus_one_minimum_input() -> None:
    """Exactly period + 1 closes is the minimum acceptable input length."""
    closes = [100.0 + 0.1 * i for i in range(15)]  # period + 1 = 14 + 1
    result = compute_rsi(closes, period=14)
    assert math.isfinite(result.value)
