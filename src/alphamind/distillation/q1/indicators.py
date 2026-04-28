"""Multi-timeframe technical indicators - story 02-distillation/08a.

Pure deterministic computations: each function takes its inputs as plain
sequences and returns a frozen result dataclass. No I/O, no module-level
mutable state. The indicator math follows the conventional definitions
referenced in
``docs/design/01-data-layer/external/quantitative.md`` section 1c (RSI 14,
MACD 12/26/9, Stochastic 14/3/3, Bollinger 20 SMA with 2 stdev bands,
Keltner 20 EMA with 2 ATR bands, EMA 20/50/200, ADX 14, ATR 14).

The 14-period default for RSI / Stochastic / ADX is the conventional Wilder /
Lane / Wilder period - a math constant of those algorithms, not a tunable
threshold. The Bollinger and Keltner parameters above are the conventional
defaults from each indicator's published definition; treat them as
algorithmic defaults that callers may override for sensitivity tests but
that have no Class A configuration counterpart.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from alphamind.distillation.normalization import compute_atr

# ---------------------------------------------------------------------------
# RSI
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RsiResult:
    """RSI value plus the period it was computed at.

    The period is carried alongside the value so a multi-timeframe rollup
    can pair the indicator with the bar series it came from without a
    separate bookkeeping table.
    """

    value: float
    period: int


def compute_rsi(closes: Sequence[float], *, period: int) -> RsiResult:
    """Wilder-smoothed Relative Strength Index over ``period`` bars.

    The implementation uses the conventional Wilder smoothing — the same
    smoothing as :func:`compute_atr` so the two indicators behave
    consistently across timeframes.

    Raises :class:`ValueError` if fewer than ``period + 1`` closes are
    provided. The minimum is the same as :func:`compute_atr`'s — need
    ``period`` deltas plus one anchor.
    """
    if len(closes) < period + 1:
        raise ValueError(
            f"compute_rsi requires at least period+1 closes; got {len(closes)} for period={period}"
        )
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # Seed with simple averages over the first ``period`` deltas.
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder smoothing: ``avg_t = (avg_{t-1} * (period - 1) + value_t) / period``.
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        # No losses → RSI saturates at 100 by definition.
        rsi_value = 100.0 if avg_gain > 0.0 else 50.0
    else:
        rs = avg_gain / avg_loss
        rsi_value = 100.0 - (100.0 / (1.0 + rs))
    return RsiResult(value=rsi_value, period=period)


# ---------------------------------------------------------------------------
# EMA primitive (used by MACD, Bollinger, Keltner, EMA pairs)
# ---------------------------------------------------------------------------


def _compute_ema_series(values: Sequence[float], *, period: int) -> list[float]:
    """Return the full EMA series for ``values`` over ``period``.

    Seeded with the simple mean of the first ``period`` values; subsequent
    values use the standard ``alpha = 2 / (period + 1)`` smoothing constant.

    Returns an empty list when ``values`` has fewer than ``period``
    entries; callers detect this and either raise or fall back depending on
    their semantics.
    """
    if len(values) < period:
        return []
    alpha = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    out: list[float] = [ema]
    for value in values[period:]:
        ema = alpha * value + (1.0 - alpha) * ema
        out.append(ema)
    return out


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


MacdCrossoverState = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True, slots=True)
class MacdResult:
    """MACD line, signal line, histogram, and the crossover state.

    ``crossover_state`` is the categorical bullish / bearish / neutral label
    derived from the sign of ``histogram``: positive histogram is bullish,
    negative is bearish, zero or undefined is neutral.
    """

    macd_line: float
    signal_line: float
    histogram: float
    crossover_state: MacdCrossoverState


def compute_macd(
    closes: Sequence[float],
    *,
    fast_period: int,
    slow_period: int,
    signal_period: int,
) -> MacdResult:
    """Standard MACD: fast EMA - slow EMA, signal EMA, histogram.

    Raises :class:`ValueError` when ``len(closes)`` cannot accommodate the
    slow EMA plus the signal EMA.
    """
    minimum = slow_period + signal_period
    if len(closes) < minimum:
        raise ValueError(
            f"compute_macd requires at least slow_period+signal_period closes; "
            f"got {len(closes)} need {minimum}"
        )
    fast_series = _compute_ema_series(closes, period=fast_period)
    slow_series = _compute_ema_series(closes, period=slow_period)
    # Align the two EMA series at the slow-period anchor — the slow series
    # starts later so we trim the fast series to match the slow series'
    # length.
    offset = slow_period - fast_period
    fast_aligned = fast_series[offset:]
    macd_series = [f - s for f, s in zip(fast_aligned, slow_series, strict=True)]
    signal_series = _compute_ema_series(macd_series, period=signal_period)

    macd_line = macd_series[-1]
    signal_line = signal_series[-1]
    histogram = macd_line - signal_line
    if histogram > 0.0:
        crossover_state: MacdCrossoverState = "bullish"
    elif histogram < 0.0:
        crossover_state = "bearish"
    else:
        crossover_state = "neutral"
    return MacdResult(
        macd_line=macd_line,
        signal_line=signal_line,
        histogram=histogram,
        crossover_state=crossover_state,
    )


# ---------------------------------------------------------------------------
# Stochastic
# ---------------------------------------------------------------------------


StochasticCrossoverState = Literal["bullish", "bearish", "neutral"]


@dataclass(frozen=True, slots=True)
class StochasticResult:
    """%K and %D values plus the crossover state."""

    k: float
    d: float
    crossover_state: StochasticCrossoverState


def _stochastic_raw_k_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    k_period: int,
) -> list[float]:
    """Return the raw %K series over the input bars.

    ``%K_t = 100 * (close_t - min_low_t) / (max_high_t - min_low_t)`` where
    ``min_low_t`` and ``max_high_t`` span the trailing ``k_period`` bars.
    """
    out: list[float] = []
    for i in range(k_period - 1, len(closes)):
        window_high = max(highs[i - k_period + 1 : i + 1])
        window_low = min(lows[i - k_period + 1 : i + 1])
        denom = window_high - window_low
        if denom == 0.0:
            out.append(50.0)
        else:
            out.append(100.0 * (closes[i] - window_low) / denom)
    return out


def compute_stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    k_period: int,
    d_period: int,
    smooth_k: int,
) -> StochasticResult:
    """Smoothed Stochastic Oscillator (%K / %D).

    The unsmoothed-K series passes through a simple moving average of length
    ``smooth_k`` to give the reported %K, and a further SMA of length
    ``d_period`` gives %D — the slow stochastic convention.

    Raises :class:`ValueError` when the input is too short for the requested
    periods.
    """
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise ValueError(
            f"compute_stochastic requires equal-length high/low/close sequences; "
            f"got highs={len(highs)} lows={len(lows)} closes={len(closes)}"
        )
    minimum = k_period + smooth_k + d_period - 2
    if len(closes) < minimum:
        raise ValueError(
            f"compute_stochastic requires at least {minimum} bars "
            f"for k_period={k_period} smooth_k={smooth_k} d_period={d_period}; got {len(closes)}"
        )
    raw_k = _stochastic_raw_k_series(highs, lows, closes, k_period=k_period)
    smoothed_k = [
        sum(raw_k[i - smooth_k + 1 : i + 1]) / smooth_k for i in range(smooth_k - 1, len(raw_k))
    ]
    smoothed_d = [
        sum(smoothed_k[i - d_period + 1 : i + 1]) / d_period
        for i in range(d_period - 1, len(smoothed_k))
    ]
    k_value = smoothed_k[-1]
    d_value = smoothed_d[-1]
    if k_value > d_value:
        crossover_state: StochasticCrossoverState = "bullish"
    elif k_value < d_value:
        crossover_state = "bearish"
    else:
        crossover_state = "neutral"
    return StochasticResult(k=k_value, d=d_value, crossover_state=crossover_state)


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BollingerResult:
    """Bollinger band values plus normalized position and width."""

    upper: float
    middle: float
    lower: float
    position_within_bands: float
    band_width: float


def compute_bollinger(
    closes: Sequence[float],
    *,
    period: int,
    num_std: float,
) -> BollingerResult:
    """Bollinger bands using simple moving average + population stdev.

    ``position_within_bands`` is normalized to 0..1 between the lower and
    upper bands. When the bands collapse (zero variance) the position is
    reported as 0.5 — neutral midpoint — to avoid division by zero.
    ``band_width`` is the absolute spread ``upper - lower``.
    """
    if len(closes) < period:
        raise ValueError(
            f"compute_bollinger requires at least period closes; "
            f"got {len(closes)} for period={period}"
        )
    window = closes[-period:]
    middle = statistics.fmean(window)
    stdev = statistics.pstdev(window)
    upper = middle + num_std * stdev
    lower = middle - num_std * stdev
    band_width = upper - lower
    last_close = closes[-1]
    position = 0.5 if band_width == 0.0 else (last_close - lower) / band_width
    return BollingerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        position_within_bands=position,
        band_width=band_width,
    )


# ---------------------------------------------------------------------------
# Keltner Channels
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeltnerResult:
    """Keltner channel values plus normalized position and width."""

    upper: float
    middle: float
    lower: float
    position_within_channel: float
    channel_width: float


def compute_keltner(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int,
    atr_multiple: float,
) -> KeltnerResult:
    """Keltner channels — EMA midline, ATR-multiple bands.

    Raises :class:`ValueError` when the input is too short for the EMA seed
    (``period`` bars) plus the ATR seed (``period + 1`` bars). Width and
    position semantics mirror :func:`compute_bollinger`.
    """
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise ValueError(
            f"compute_keltner requires equal-length high/low/close sequences; "
            f"got highs={len(highs)} lows={len(lows)} closes={len(closes)}"
        )
    if len(closes) < period + 1:
        raise ValueError(
            f"compute_keltner requires at least period+1 closes; "
            f"got {len(closes)} for period={period}"
        )
    ema_series = _compute_ema_series(closes, period=period)
    atr = compute_atr(highs, lows, closes, period=period)
    middle = ema_series[-1]
    upper = middle + atr_multiple * atr
    lower = middle - atr_multiple * atr
    channel_width = upper - lower
    last_close = closes[-1]
    position = 0.5 if channel_width == 0.0 else (last_close - lower) / channel_width
    return KeltnerResult(
        upper=upper,
        middle=middle,
        lower=lower,
        position_within_channel=position,
        channel_width=channel_width,
    )


# ---------------------------------------------------------------------------
# EMA pairs (20 / 50 / 200)
# ---------------------------------------------------------------------------


class EmaCrossoverState(StrEnum):
    """EMA pair crossover state — fast above slow, fast below slow, or aligned."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class EmaPairResult:
    """EMA values for the 20 / 50 / 200 trio plus pair crossover states.

    Slopes are reported as the simple end-minus-start slope over the last
    five samples of each EMA series — a coarse but cheap directional proxy
    that does not need its own derivative routine.
    """

    ema_20: float
    ema_50: float
    ema_200: float
    ema_20_slope: float
    ema_50_slope: float
    ema_200_slope: float
    crossover_state_20_50: EmaCrossoverState
    crossover_state_50_200: EmaCrossoverState


_EMA_PAIR_SLOPE_LOOKBACK = 5
"""Number of trailing EMA samples used to estimate slope.

Five samples is a conservative tail — long enough to smooth tick-to-tick
noise on the daily-bar series we run against and short enough that a
genuine direction change reflects in the slope within a session. Treated
as an algorithmic detail of the EMA-pair indicator, not a tunable threshold.
"""


def _slope_over_tail(series: Sequence[float], lookback: int) -> float:
    """End-minus-start slope over the trailing ``lookback`` samples of ``series``."""
    if len(series) < 2:
        return 0.0
    tail = series[-lookback:] if len(series) >= lookback else list(series)
    return (tail[-1] - tail[0]) / max(len(tail) - 1, 1)


def _crossover_state(fast: float, slow: float) -> EmaCrossoverState:
    if fast > slow:
        return EmaCrossoverState.BULLISH
    if fast < slow:
        return EmaCrossoverState.BEARISH
    return EmaCrossoverState.NEUTRAL


_EMA_PAIR_PERIODS: tuple[int, int, int] = (20, 50, 200)
"""20 / 50 / 200 EMA pair periods.

Conventional moving-average set referenced by external.md § 2 and
quantitative.md § 1c. Algorithmic defaults of the EMA-pair indicator, not
Class A thresholds.
"""


def compute_ema_pairs(closes: Sequence[float]) -> EmaPairResult:
    """Compute the 20 / 50 / 200 EMA trio plus pairwise crossover states.

    Raises :class:`ValueError` when ``len(closes)`` is shorter than the
    longest period — a 200-EMA needs at least 200 closes to seed.
    """
    short, mid, long = _EMA_PAIR_PERIODS
    if len(closes) < long:
        raise ValueError(f"compute_ema_pairs requires at least {long} closes; got {len(closes)}")
    ema_short = _compute_ema_series(closes, period=short)
    ema_mid = _compute_ema_series(closes, period=mid)
    ema_long = _compute_ema_series(closes, period=long)
    return EmaPairResult(
        ema_20=ema_short[-1],
        ema_50=ema_mid[-1],
        ema_200=ema_long[-1],
        ema_20_slope=_slope_over_tail(ema_short, _EMA_PAIR_SLOPE_LOOKBACK),
        ema_50_slope=_slope_over_tail(ema_mid, _EMA_PAIR_SLOPE_LOOKBACK),
        ema_200_slope=_slope_over_tail(ema_long, _EMA_PAIR_SLOPE_LOOKBACK),
        crossover_state_20_50=_crossover_state(ema_short[-1], ema_mid[-1]),
        crossover_state_50_200=_crossover_state(ema_mid[-1], ema_long[-1]),
    )


# ---------------------------------------------------------------------------
# ADX (trend strength, direction-agnostic)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdxResult:
    """ADX value over the requested period."""

    value: float
    period: int


def compute_adx(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    *,
    period: int,
) -> AdxResult:
    """Wilder's Average Directional Index — trend strength regardless of direction.

    Implements the standard ADX construction:

    1. Per-bar ``+DM`` and ``-DM`` from the directional movement comparison.
    2. Wilder-smoothed True Range, ``+DM``, ``-DM``.
    3. ``+DI`` = 100 * smoothed +DM / smoothed TR; ``-DI`` analogously.
    4. ``DX`` = 100 * |+DI - -DI| / (+DI + -DI).
    5. ``ADX`` = Wilder smoothing of ``DX``.

    Raises :class:`ValueError` if fewer than ``2 * period + 1`` bars are
    provided — need ``period`` bars for the directional-movement seed plus
    ``period`` bars for the ADX seed plus one anchor.
    """
    if len(highs) != len(lows) or len(lows) != len(closes):
        raise ValueError(
            f"compute_adx requires equal-length sequences; "
            f"got highs={len(highs)} lows={len(lows)} closes={len(closes)}"
        )
    minimum = 2 * period + 1
    if len(closes) < minimum:
        raise ValueError(
            f"compute_adx requires at least 2*period+1 bars; got {len(closes)} for period={period}"
        )
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    true_range: list[float] = []
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_range.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        )

    # Wilder-smooth +DM / -DM / TR.
    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    smoothed_tr = sum(true_range[:period])
    dx_series: list[float] = []
    for i in range(period, len(true_range)):
        smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm[i]
        smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm[i]
        smoothed_tr = smoothed_tr - smoothed_tr / period + true_range[i]
        if smoothed_tr == 0:
            dx_series.append(0.0)
            continue
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        denom = plus_di + minus_di
        if denom == 0.0:
            dx_series.append(0.0)
        else:
            dx_series.append(100.0 * abs(plus_di - minus_di) / denom)

    if not dx_series:
        return AdxResult(value=0.0, period=period)
    seed_window = dx_series[:period] if len(dx_series) >= period else dx_series
    adx = sum(seed_window) / len(seed_window)
    for dx in dx_series[period:]:
        adx = (adx * (period - 1) + dx) / period
    return AdxResult(value=adx, period=period)


# ---------------------------------------------------------------------------
# ATR regime (compression / neutral / expansion)
# ---------------------------------------------------------------------------


ATR_REGIME_COMPRESSION: str = "compression"
ATR_REGIME_NEUTRAL: str = "neutral"
ATR_REGIME_EXPANSION: str = "expansion"


@dataclass(frozen=True, slots=True)
class AtrRegimeResult:
    """Per-name ATR regime classification.

    ``regime`` is one of :data:`ATR_REGIME_COMPRESSION`,
    :data:`ATR_REGIME_NEUTRAL`, or :data:`ATR_REGIME_EXPANSION` per the
    rule documented in
    ``docs/implementation/02-distillation-layer/08a-q1-price-volume-indicators.md``
    section "Multi-timeframe technical indicators": compression when current
    ATR is at-or-below baseline mean minus 1 stdev; expansion when at-or-above
    baseline mean plus 1 stdev; neutral otherwise.
    """

    regime: str
    z_score: float


def classify_atr_regime(
    *,
    current_atr: float,
    baseline_mean: float,
    baseline_stdev: float,
) -> AtrRegimeResult:
    """Classify the current ATR against its trailing baseline.

    The one-stdev thresholds follow the indicator-story spec; they are
    algorithmic constants of the regime classifier rather than Class A
    thresholds.

    A zero ``baseline_stdev`` collapses the z-score to zero, classifying the
    bar as :data:`ATR_REGIME_NEUTRAL` — the expected behavior when the
    trailing window is degenerate.
    """
    if baseline_stdev == 0.0:
        return AtrRegimeResult(regime=ATR_REGIME_NEUTRAL, z_score=0.0)
    z = (current_atr - baseline_mean) / baseline_stdev
    if z >= 1.0:
        regime = ATR_REGIME_EXPANSION
    elif z <= -1.0:
        regime = ATR_REGIME_COMPRESSION
    else:
        regime = ATR_REGIME_NEUTRAL
    return AtrRegimeResult(regime=regime, z_score=z)


__all__ = [
    "ATR_REGIME_COMPRESSION",
    "ATR_REGIME_EXPANSION",
    "ATR_REGIME_NEUTRAL",
    "AdxResult",
    "AtrRegimeResult",
    "BollingerResult",
    "EmaCrossoverState",
    "EmaPairResult",
    "KeltnerResult",
    "MacdCrossoverState",
    "MacdResult",
    "RsiResult",
    "StochasticCrossoverState",
    "StochasticResult",
    "classify_atr_regime",
    "compute_adx",
    "compute_bollinger",
    "compute_ema_pairs",
    "compute_keltner",
    "compute_macd",
    "compute_rsi",
    "compute_stochastic",
]
