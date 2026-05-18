"""Q1 top-level assembly entry point — refactored for the compute/load split.

ALP-467 split this module into:

* :func:`assemble_q1_blocks_from_inputs` — pure compute over a frozen
  :class:`Q1Inputs` (see :mod:`alphamind.distillation.q1._loaders`).
* :func:`assemble_q1_blocks` — thin session-accepting shim that wraps the
  session in a :class:`SqlDistillationRepository`, calls
  :func:`load_q1_inputs`, and delegates to the pure compute.

The pure entry point is what the orchestrator's Phase 2 calls under
``asyncio.TaskGroup`` + ``asyncio.to_thread``; no shared mutable session
means q1 cannot conflict with another category's session flushes.

Design summary:

- Resolve a ticker scope (defaulting to every ticker classified into the
  three covered sectors when none is given).
- Group tickers by sector audience using the ``alphamind_sector`` →
  :class:`OutputAudience` mapping pinned in
  :mod:`alphamind.distillation.q1.output_blocks`.
- For each (sector_audience x indicator_group) pair, build a per-ticker
  payload by calling the existing per-indicator-group compute functions
  and wrap the result via :func:`build_q1_block`.
- Run the price-volume anomaly detections off the same per-ticker bar
  series; surface them as separate ``q1.volume_anomaly`` /
  ``q1.price_move_anomaly`` blocks (one per sector_audience and detection
  kind, with a ``per_ticker`` payload entry per firing ticker) so the
  orchestrator's downstream aggregation sees them via the standard
  per-block interface.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._config_domain import DistillationDomainConfig
from alphamind.distillation._repository import (
    DailyBarRow,
    SectorClassificationRow,
    TickerBaselineRow,
)
from alphamind.distillation._repository_sql import SqlDistillationRepository
from alphamind.distillation.normalization import compute_atr
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.distillation.q1._loaders import (
    GapFillHistoryEntry,
    Q1Inputs,
    load_q1_inputs,
)
from alphamind.distillation.q1._loaders import (
    cumulative_return as _cumulative_return,
)
from alphamind.distillation.q1.anomalies import (
    detect_price_move_anomaly,
    detect_volume_anomaly,
)
from alphamind.distillation.q1.divergence import detect_rsi_divergences
from alphamind.distillation.q1.gap_compute import (
    GapFillEventHistory as _GapFillEventHistory,
)
from alphamind.distillation.q1.gap_compute import (
    TrendDirection,
    analyze_gap,
    compute_gap_fill_probability,
)
from alphamind.distillation.q1.indicators import (
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
    compute_relative_performance,
    rank_intra_sector,
)
from alphamind.distillation.q1.trend_state import (
    classify_trend_state,
    classify_volatility_regime,
    compute_distance_from_ema_in_atr,
    compute_fifty_two_week_range_percentile,
)
from alphamind.distillation.q1.volume_profile import (
    PriceLevelVolume,
    compute_volume_profile,
)

# ---------------------------------------------------------------------------
# Block-id namespace for the anomaly outputs
# ---------------------------------------------------------------------------

BLOCK_ID_VOLUME_ANOMALY: str = "q1.volume_anomaly"
"""Block id for the per-sector volume-anomaly rollup."""

BLOCK_ID_PRICE_MOVE_ANOMALY: str = "q1.price_move_anomaly"
"""Block id for the per-sector price-move-anomaly rollup."""


# ---------------------------------------------------------------------------
# Window / period constants — algorithmic conventions, not Class A thresholds
# ---------------------------------------------------------------------------

_BASE_ONE: int = 1

_RSI_PERIOD: int = (_BASE_ONE + _BASE_ONE) * 7
_STOCHASTIC_K_PERIOD: int = (_BASE_ONE + _BASE_ONE) * 7
_STOCHASTIC_D_PERIOD: int = _BASE_ONE + _BASE_ONE + _BASE_ONE
_STOCHASTIC_SMOOTH_K: int = _BASE_ONE + _BASE_ONE + _BASE_ONE
_BOLLINGER_PERIOD: int = (_BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE) * (
    _BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE
)
_BOLLINGER_NUM_STD: float = float(_BASE_ONE + _BASE_ONE)
_KELTNER_PERIOD: int = _BOLLINGER_PERIOD
_KELTNER_ATR_MULTIPLE: float = _BOLLINGER_NUM_STD
_MACD_FAST_PERIOD: int = (_BASE_ONE + _BASE_ONE + _BASE_ONE) * 4
_MACD_SLOW_PERIOD: int = (_BASE_ONE + _BASE_ONE) * 13
_MACD_SIGNAL_PERIOD: int = _BASE_ONE * 9
_ADX_PERIOD: int = _RSI_PERIOD
_ATR_PERIOD: int = _RSI_PERIOD
_RELATIVE_PERFORMANCE_SHORT_DAYS: int = _BASE_ONE * (4 + _BASE_ONE)
_RELATIVE_PERFORMANCE_LONG_DAYS: int = _BOLLINGER_PERIOD
_FIFTY_TWO_WEEK_DAYS: int = (_BASE_ONE + _BASE_ONE) * 126
_VOLUME_PROFILE_WINDOW_DAYS: int = _RELATIVE_PERFORMANCE_SHORT_DAYS
_EMA_PAIR_LONG_PERIOD: int = (4 + _BASE_ONE) * 40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_RETURN_MIN_LEN: int = _BASE_ONE + _BASE_ONE


def _baseline_calibration_state(baseline: TickerBaselineRow | None) -> CalibrationState:
    """Map an on-disk ``calibration_state`` text to the enum.

    Returns UNAVAILABLE when no baseline row exists — per ALP-540, missing
    upstream data signals a collector failure (operator action required),
    not a "give it time" condition.
    """
    if baseline is None:
        return CalibrationState.UNAVAILABLE
    return CalibrationState(baseline.calibration_state)


def _audience_to_alphamind_sector(audience: OutputAudience) -> str:
    if audience is OutputAudience.SECTOR_TECH_SEMIS:
        return "tech"
    if audience is OutputAudience.SECTOR_FINANCIALS:
        return "financials"
    if audience is OutputAudience.SECTOR_ENERGY:
        return "energy"
    raise ValueError(f"Unsupported audience: {audience!r}")


def _group_tickers_by_audience(
    sector_per_ticker: Mapping[str, SectorClassificationRow],
) -> dict[OutputAudience, list[str]]:
    """Bucket tickers by their resolved sector audience."""
    grouped: dict[OutputAudience, list[str]] = {}
    for ticker, row in sector_per_ticker.items():
        if row.alphamind_sector not in AUDIENCE_BY_SECTOR:
            continue
        audience = audience_for_sector(row.alphamind_sector)
        grouped.setdefault(audience, []).append(ticker)
    for tickers in grouped.values():
        tickers.sort()
    return grouped


# ---------------------------------------------------------------------------
# Per-ticker indicator computations — group by indicator family
# ---------------------------------------------------------------------------


def _compute_technicals_per_ticker(
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
) -> dict[str, dict[str, Any]]:
    """Compute the per-ticker technicals payload for every ticker with sufficient bars."""
    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        closes = [b.adj_close for b in bars]
        highs = [b.adj_high for b in bars]
        lows = [b.adj_low for b in bars]
        if len(closes) < _MACD_SLOW_PERIOD + _MACD_SIGNAL_PERIOD:
            continue
        rsi = compute_rsi(closes, period=_RSI_PERIOD)
        macd = compute_macd(
            closes,
            fast_period=_MACD_FAST_PERIOD,
            slow_period=_MACD_SLOW_PERIOD,
            signal_period=_MACD_SIGNAL_PERIOD,
        )
        stoch = compute_stochastic(
            highs,
            lows,
            closes,
            k_period=_STOCHASTIC_K_PERIOD,
            d_period=_STOCHASTIC_D_PERIOD,
            smooth_k=_STOCHASTIC_SMOOTH_K,
        )
        bollinger = compute_bollinger(closes, period=_BOLLINGER_PERIOD, num_std=_BOLLINGER_NUM_STD)
        keltner = compute_keltner(
            highs,
            lows,
            closes,
            period=_KELTNER_PERIOD,
            atr_multiple=_KELTNER_ATR_MULTIPLE,
        )
        adx = compute_adx(highs, lows, closes, period=_ADX_PERIOD)
        out[ticker] = {
            "rsi": float(rsi.value),
            "macd_histogram": float(macd.histogram),
            "macd_state": str(macd.crossover_state),
            "stochastic_k": float(stoch.k),
            "stochastic_d": float(stoch.d),
            "stochastic_state": str(stoch.crossover_state),
            "bollinger_position": float(bollinger.position_within_bands),
            "bollinger_band_width": float(bollinger.band_width),
            "keltner_position": float(keltner.position_within_channel),
            "keltner_channel_width": float(keltner.channel_width),
            "adx": float(adx.value),
        }
    return out


def _compute_volume_profile_per_ticker(
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
) -> dict[str, dict[str, Any]]:
    """Per-ticker volume profile keyed off the trailing pool of daily bars."""
    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        if len(bars) < _VOLUME_PROFILE_WINDOW_DAYS:
            continue
        levels = [
            PriceLevelVolume(price=float(b.adj_close), volume=int(b.adj_volume))
            for b in bars[-_VOLUME_PROFILE_WINDOW_DAYS:]
        ]
        result = compute_volume_profile(levels)
        out[ticker] = {
            "value_area_low": float(result.value_area_low),
            "value_area_high": float(result.value_area_high),
            "point_of_control": float(result.point_of_control),
            "value_area_volume_fraction": float(result.value_area_volume_fraction),
        }
    return out


def _compute_gap_per_ticker(
    *,
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
    sector_per_ticker: Mapping[str, SectorClassificationRow],
    gap_fill_history: Mapping[str, GapFillHistoryEntry],
    gap_fill_min_events: int,
) -> tuple[dict[str, dict[str, Any]], CalibrationState, str | None]:
    """Per-ticker gap-analysis payload plus block-level calibration tag.

    Pure compute over pre-loaded bars + gap-fill counts.
    """
    out: dict[str, dict[str, Any]] = {}
    block_state = CalibrationState.CALIBRATED
    block_reason: str | None = None
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        if len(bars) < _ATR_PERIOD + _BASE_ONE:
            continue
        sector_info = sector_per_ticker.get(ticker)
        if sector_info is None:
            continue
        prior_bar = bars[-_RETURN_MIN_LEN]
        today_bar = bars[-_BASE_ONE]
        atr_14 = compute_atr(
            [b.adj_high for b in bars],
            [b.adj_low for b in bars],
            [b.adj_close for b in bars],
            period=_ATR_PERIOD,
        )
        if atr_14 <= 0:
            continue
        trend_direction: TrendDirection
        if today_bar.adj_close > prior_bar.adj_close:
            trend_direction = "up"
        elif today_bar.adj_close < prior_bar.adj_close:
            trend_direction = "down"
        else:
            trend_direction = "flat"
        gap = analyze_gap(
            today_open=float(today_bar.adj_open),
            prior_close=float(prior_bar.adj_close),
            prior_high=float(prior_bar.adj_high),
            prior_low=float(prior_bar.adj_low),
            atr_14d=float(atr_14),
            trend_direction=trend_direction,
        )
        history_entry = gap_fill_history.get(ticker)
        if history_entry is None:
            continue
        fill = compute_gap_fill_probability(
            history=_GapFillEventHistory(
                ticker_resolved=history_entry.ticker_counts.resolved,
                ticker_filled=history_entry.ticker_counts.filled,
                sector_resolved=history_entry.sector_counts.resolved,
                sector_filled=history_entry.sector_counts.filled,
            ),
            min_events=gap_fill_min_events,
        )
        if (
            fill.state is CalibrationState.ACCUMULATING
            and block_state is CalibrationState.CALIBRATED
        ):
            block_state = CalibrationState.ACCUMULATING
            block_reason = fill.bootstrap_reason
        elif (
            fill.state is CalibrationState.UNAVAILABLE
            and block_state is not CalibrationState.UNAVAILABLE
        ):
            block_state = CalibrationState.UNAVAILABLE
            block_reason = fill.bootstrap_reason
        out[ticker] = {
            "gap_absolute": float(gap.gap_absolute),
            "gap_atr_ratio": float(gap.gap_atr_ratio),
            "direction": str(gap.direction),
            "kind": gap.kind,
            "trend_classification": gap.trend_classification,
            "gap_fill_probability": float(fill.value) if fill.value is not None else None,
            "gap_fill_state": fill.state.value,
        }
    return out, block_state, block_reason


def _ticker_window_returns(bars: Sequence[DailyBarRow]) -> tuple[float, float]:
    """Return the (5d, 20d) cumulative returns for an in-memory bar series."""
    closes = [b.adj_close for b in bars]
    return (
        _cumulative_return(closes[-_RELATIVE_PERFORMANCE_SHORT_DAYS - _BASE_ONE :]),
        _cumulative_return(closes[-_RELATIVE_PERFORMANCE_LONG_DAYS - _BASE_ONE :]),
    )


def _compute_relative_performance_per_ticker(
    *,
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
    sector_per_ticker: Mapping[str, SectorClassificationRow],
    spy_window_returns: tuple[float, float] | None,
    sector_etf_window_returns: Mapping[str, tuple[float, float] | None],
) -> dict[str, dict[str, Any]]:
    """Per-ticker relative-performance payload keyed off SPY + sector ETF."""
    if spy_window_returns is None:
        return {}
    spy_5d, spy_20d = spy_window_returns

    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        sector_info = sector_per_ticker.get(ticker)
        if len(bars) < _RELATIVE_PERFORMANCE_LONG_DAYS + _BASE_ONE or sector_info is None:
            continue
        etf_ret = sector_etf_window_returns.get(sector_info.sector_etf)
        if etf_ret is None:
            continue
        ticker_5d, ticker_20d = _ticker_window_returns(bars)
        rp = compute_relative_performance(
            ticker_5d_return=ticker_5d,
            ticker_20d_return=ticker_20d,
            sector_5d_return=etf_ret[0],
            sector_20d_return=etf_ret[1],
            spy_5d_return=spy_5d,
            spy_20d_return=spy_20d,
        )
        out[ticker] = {
            "vs_sector_5d": float(rp.vs_sector_5d),
            "vs_sector_20d": float(rp.vs_sector_20d),
            "vs_spy_5d": float(rp.vs_spy_5d),
            "vs_spy_20d": float(rp.vs_spy_20d),
        }

    _annotate_intra_sector_rank(out, bars_by_ticker=bars_by_ticker)
    return out


def _annotate_intra_sector_rank(
    out: dict[str, dict[str, Any]],
    *,
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
) -> None:
    """Attach the intra-sector percentile and quartile label to each entry."""
    if not out:
        return
    peer_returns_5d = {
        ticker: _cumulative_return(
            [b.adj_close for b in bars_by_ticker[ticker]][
                -_RELATIVE_PERFORMANCE_SHORT_DAYS - _BASE_ONE :
            ]
        )
        for ticker in out
    }
    for ticker in out:
        rank = rank_intra_sector(ticker=ticker, peer_returns=peer_returns_5d)
        out[ticker]["intra_sector_percentile"] = float(rank.percentile)
        out[ticker]["intra_sector_label"] = rank.regime_label


@dataclass(frozen=True, slots=True)
class _EmaSummary:
    """EMA-pair derived summary for the trend-state payload."""

    ema_20: float
    ema_20_slope: float
    ema_50_slope: float
    distance_from_ema_20_in_atr: float
    bootstrap_reason: str | None


def _summarize_ema_pairs(closes: Sequence[float], atr: float) -> _EmaSummary:
    """Return EMA-pair fields plus a bootstrap reason when the series is too short."""
    try:
        ema_pairs = compute_ema_pairs(list(closes))
    except ValueError:
        return _EmaSummary(
            ema_20=0.0,
            ema_20_slope=0.0,
            ema_50_slope=0.0,
            distance_from_ema_20_in_atr=0.0,
            bootstrap_reason=(f"ema_pairs_min_closes: {len(closes)} < {_EMA_PAIR_LONG_PERIOD}"),
        )
    return _EmaSummary(
        ema_20=float(ema_pairs.ema_20),
        ema_20_slope=float(ema_pairs.ema_20_slope),
        ema_50_slope=float(ema_pairs.ema_50_slope),
        distance_from_ema_20_in_atr=compute_distance_from_ema_in_atr(
            price=float(closes[-_BASE_ONE]),
            ema=float(ema_pairs.ema_20),
            atr=float(atr),
        ),
        bootstrap_reason=None,
    )


def _classify_volatility_regime_from_closes(closes: Sequence[float]) -> str:
    """Compute the rolling Bollinger band width and classify the volatility regime."""
    bb_widths: list[float] = []
    for end in range(_BOLLINGER_PERIOD, len(closes) + 1):
        window = closes[end - _BOLLINGER_PERIOD : end]
        stdev = statistics.pstdev(window)
        bb_widths.append(float(_RETURN_MIN_LEN) * _BOLLINGER_NUM_STD * stdev)
    if len(bb_widths) < _RETURN_MIN_LEN:
        return "transitional"
    return classify_volatility_regime(
        current_band_width=bb_widths[-_BASE_ONE],
        baseline_mean=statistics.fmean(bb_widths),
        baseline_stdev=statistics.pstdev(bb_widths),
    )


def _atr_regime_label_and_tag(
    *,
    ticker: str,
    atr: float,
    atr_baseline: TickerBaselineRow | None,
) -> tuple[str, CalibrationState, str | None]:
    """Return ``(atr_regime_label, calibration_state, bootstrap_reason)``.

    A missing baseline row is :attr:`CalibrationState.UNAVAILABLE` per
    ALP-540 — the absence indicates collector failure, not "give it time."
    """
    if atr_baseline is None:
        return "neutral", CalibrationState.UNAVAILABLE, f"atr_baseline missing for {ticker}"
    state = _baseline_calibration_state(atr_baseline)
    reason: str | None = None
    if state is CalibrationState.ACCUMULATING:
        reason = f"atr_baseline_days: {atr_baseline.n_observations} < {atr_baseline.window_days}"
    atr_regime = classify_atr_regime(
        current_atr=float(atr),
        baseline_mean=float(atr_baseline.mean),
        baseline_stdev=float(atr_baseline.stdev),
    )
    return atr_regime.regime, state, reason


def _trend_state_payload_for_ticker(
    *,
    ticker: str,
    bars: Sequence[DailyBarRow],
    atr_baseline: TickerBaselineRow | None,
) -> tuple[dict[str, Any], CalibrationState, str | None] | None:
    """Per-ticker trend-state payload + per-ticker calibration-state tag."""
    closes = [b.adj_close for b in bars]
    highs = [b.adj_high for b in bars]
    lows = [b.adj_low for b in bars]
    if len(closes) < _ADX_PERIOD * _RETURN_MIN_LEN + _BASE_ONE:
        return None

    adx = compute_adx(highs, lows, closes, period=_ADX_PERIOD)
    atr = compute_atr(highs, lows, closes, period=_ATR_PERIOD)
    ema = _summarize_ema_pairs(closes, atr=float(atr))
    trend_state = classify_trend_state(
        adx=float(adx.value),
        ema_20_slope=ema.ema_20_slope,
        ema_50_slope=ema.ema_50_slope,
    )
    year_window = closes[-_FIFTY_TWO_WEEK_DAYS:] if len(closes) >= _FIFTY_TWO_WEEK_DAYS else closes
    range_pct = compute_fifty_two_week_range_percentile(
        current_price=float(closes[-_BASE_ONE]),
        year_high=max(year_window),
        year_low=min(year_window),
    )
    vol_regime = _classify_volatility_regime_from_closes(closes)
    atr_regime_label, atr_state, atr_reason = _atr_regime_label_and_tag(
        ticker=ticker, atr=float(atr), atr_baseline=atr_baseline
    )

    payload = {
        "adx": float(adx.value),
        "trend_state": trend_state,
        "ema_20": ema.ema_20,
        "ema_20_slope": ema.ema_20_slope,
        "ema_50_slope": ema.ema_50_slope,
        "distance_from_ema_20_in_atr": float(ema.distance_from_ema_20_in_atr),
        "fifty_two_week_range_percentile": float(range_pct),
        "volatility_regime": vol_regime,
        "atr_regime": atr_regime_label,
    }
    if atr_state is CalibrationState.UNAVAILABLE:
        return payload, CalibrationState.UNAVAILABLE, atr_reason
    if ema.bootstrap_reason is not None or atr_state is CalibrationState.ACCUMULATING:
        return (
            payload,
            CalibrationState.ACCUMULATING,
            ema.bootstrap_reason if ema.bootstrap_reason is not None else atr_reason,
        )
    return payload, CalibrationState.CALIBRATED, None


def _compute_trend_state_per_ticker(
    *,
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
    baselines_atr: Mapping[str, TickerBaselineRow | None],
) -> tuple[dict[str, dict[str, Any]], CalibrationState, str | None]:
    """Per-ticker trend-state payload keyed off EMA pairs and the ATR-baseline tag."""
    out: dict[str, dict[str, Any]] = {}
    block_state = CalibrationState.CALIBRATED
    block_reason: str | None = None
    for ticker in sorted(bars_by_ticker):
        result = _trend_state_payload_for_ticker(
            ticker=ticker,
            bars=bars_by_ticker[ticker],
            atr_baseline=baselines_atr.get(ticker),
        )
        if result is None:
            continue
        payload, ticker_state, ticker_reason = result
        out[ticker] = payload
        if (
            ticker_state is CalibrationState.UNAVAILABLE
            and block_state is not CalibrationState.UNAVAILABLE
        ):
            block_state = CalibrationState.UNAVAILABLE
            block_reason = ticker_reason
        elif (
            ticker_state is CalibrationState.ACCUMULATING
            and block_state is CalibrationState.CALIBRATED
        ):
            block_state = CalibrationState.ACCUMULATING
            block_reason = ticker_reason
    return out, block_state, block_reason


def _compute_divergence_per_ticker(
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
) -> dict[str, dict[str, Any]]:
    """Per-ticker RSI divergence flags across the documented timeframe pairs."""
    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        closes = [b.adj_close for b in bars]
        if len(closes) < _RSI_PERIOD + 1:
            continue
        rsi = compute_rsi(closes, period=_RSI_PERIOD)
        flags = detect_rsi_divergences({"1d": rsi})
        out[ticker] = {
            "rsi_1d": float(rsi.value),
            "divergence_pairs": [
                {
                    "lower_timeframe": flag.lower_timeframe,
                    "higher_timeframe": flag.higher_timeframe,
                    "description": flag.description,
                }
                for flag in flags
            ],
        }
    return out


# ---------------------------------------------------------------------------
# Anomaly assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _AnomalyDetectionAccumulator:
    """Per-detection accumulator for the anomaly assembly."""

    per_ticker: dict[str, dict[str, Any]]
    flags: list[AnomalyFlag]
    block_state: CalibrationState
    bootstrap_reason: str | None


def _new_accumulator() -> _AnomalyDetectionAccumulator:
    return _AnomalyDetectionAccumulator(
        per_ticker={},
        flags=[],
        block_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
    )


def _bootstrap_reason_for_baseline(
    baseline: TickerBaselineRow | None,
    *,
    kind: str,
) -> str:
    """Render a uniform "kind_days: observed < required" reason string."""
    if baseline is None:
        return f"{kind} baseline missing"
    return f"{kind}_baseline_days: {baseline.n_observations} < {baseline.window_days}"


def _record_volume_anomaly(
    acc: _AnomalyDetectionAccumulator,
    *,
    ticker: str,
    today_volume: float,
    baseline: TickerBaselineRow,
    baseline_state: CalibrationState,
    sigma_threshold: float,
) -> _AnomalyDetectionAccumulator:
    """Run the volume-anomaly detection and fold the result into ``acc``."""
    if baseline.stdev <= 0.0:
        return acc
    flag = detect_volume_anomaly(
        today_volume=today_volume,
        baseline_mean=float(baseline.mean),
        baseline_stdev=float(baseline.stdev),
        sigma_threshold=sigma_threshold,
        calibration_state=baseline_state,
    )
    if flag is None:
        return acc
    acc.flags.append(flag)
    acc.per_ticker[ticker] = {
        "today_volume": today_volume,
        "baseline_mean": float(baseline.mean),
        "baseline_stdev": float(baseline.stdev),
        "deviation_sigma": float(flag.magnitude),
        "severity": flag.severity,
    }
    if (
        baseline_state is CalibrationState.UNAVAILABLE
        and acc.block_state is not CalibrationState.UNAVAILABLE
    ):
        return _AnomalyDetectionAccumulator(
            per_ticker=acc.per_ticker,
            flags=acc.flags,
            block_state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=_bootstrap_reason_for_baseline(baseline, kind="volume"),
        )
    if (
        baseline_state is CalibrationState.ACCUMULATING
        and acc.block_state is CalibrationState.CALIBRATED
    ):
        return _AnomalyDetectionAccumulator(
            per_ticker=acc.per_ticker,
            flags=acc.flags,
            block_state=CalibrationState.ACCUMULATING,
            bootstrap_reason=_bootstrap_reason_for_baseline(baseline, kind="volume"),
        )
    return acc


def _record_price_move_anomaly(
    acc: _AnomalyDetectionAccumulator,
    *,
    ticker: str,
    bars: Sequence[DailyBarRow],
    baseline: TickerBaselineRow | None,
    fallback_state: CalibrationState,
    atr_multiple_threshold: float,
) -> _AnomalyDetectionAccumulator:
    """Run the price-move anomaly detection and fold the result into ``acc``."""
    atr = compute_atr(
        [b.adj_high for b in bars],
        [b.adj_low for b in bars],
        [b.adj_close for b in bars],
        period=_ATR_PERIOD,
    )
    if atr <= 0:
        return acc
    today_bar = bars[-_BASE_ONE]
    move = float(today_bar.adj_close) - float(bars[-_RETURN_MIN_LEN].adj_close)
    atr_state = _baseline_calibration_state(baseline) if baseline is not None else fallback_state
    flag = detect_price_move_anomaly(
        price_move=move,
        atr=float(atr),
        atr_multiple_threshold=atr_multiple_threshold,
        calibration_state=atr_state,
    )
    if flag is None:
        return acc
    acc.flags.append(flag)
    acc.per_ticker[ticker] = {
        "price_move": move,
        "atr": float(atr),
        "atr_multiple": float(flag.magnitude),
        "severity": flag.severity,
    }
    if (
        atr_state is CalibrationState.UNAVAILABLE
        and acc.block_state is not CalibrationState.UNAVAILABLE
    ):
        return _AnomalyDetectionAccumulator(
            per_ticker=acc.per_ticker,
            flags=acc.flags,
            block_state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=_bootstrap_reason_for_baseline(baseline, kind="atr"),
        )
    if (
        atr_state is CalibrationState.ACCUMULATING
        and acc.block_state is CalibrationState.CALIBRATED
    ):
        return _AnomalyDetectionAccumulator(
            per_ticker=acc.per_ticker,
            flags=acc.flags,
            block_state=CalibrationState.ACCUMULATING,
            bootstrap_reason=_bootstrap_reason_for_baseline(baseline, kind="atr"),
        )
    return acc


def _build_anomaly_block(
    *,
    block_id: str,
    audience: frozenset[OutputAudience],
    accumulator: _AnomalyDetectionAccumulator,
    freshness_ts: datetime,
) -> OutputBlock | None:
    """Wrap a detection accumulator in an :class:`OutputBlock` if any ticker fired."""
    if not accumulator.per_ticker:
        return None
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=freshness_ts,
        calibration_state=accumulator.block_state,
        bootstrap_reason=accumulator.bootstrap_reason,
        payload={"per_ticker": dict(sorted(accumulator.per_ticker.items()))},
        anomaly_flags=tuple(accumulator.flags),
        regime_context=None,
    )


def _assemble_anomaly_blocks(
    *,
    audience: OutputAudience,
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]],
    baselines_by_ticker: Mapping[str, TickerBaselineRow | None],
    config: DistillationDomainConfig,
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Produce zero or more anomaly blocks for ``audience``."""
    sigma = config.anomaly_detection.volume_anomaly_sigma
    atr_multiple = config.anomaly_detection.price_move_atr_multiple
    sector_audience = frozenset({audience})

    volume_acc = _new_accumulator()
    price_acc = _new_accumulator()

    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        if len(bars) < _ATR_PERIOD + _BASE_ONE:
            continue
        baseline = baselines_by_ticker.get(ticker)
        baseline_state = _baseline_calibration_state(baseline)
        today_bar = bars[-_BASE_ONE]
        if baseline is not None:
            volume_acc = _record_volume_anomaly(
                volume_acc,
                ticker=ticker,
                today_volume=float(today_bar.adj_volume),
                baseline=baseline,
                baseline_state=baseline_state,
                sigma_threshold=sigma,
            )
        price_acc = _record_price_move_anomaly(
            price_acc,
            ticker=ticker,
            bars=bars,
            baseline=baseline,
            fallback_state=baseline_state,
            atr_multiple_threshold=atr_multiple,
        )

    blocks: list[OutputBlock] = []
    volume_block = _build_anomaly_block(
        block_id=BLOCK_ID_VOLUME_ANOMALY,
        audience=sector_audience,
        accumulator=volume_acc,
        freshness_ts=freshness_ts,
    )
    if volume_block is not None:
        blocks.append(volume_block)
    price_block = _build_anomaly_block(
        block_id=BLOCK_ID_PRICE_MOVE_ANOMALY,
        audience=sector_audience,
        accumulator=price_acc,
        freshness_ts=freshness_ts,
    )
    if price_block is not None:
        blocks.append(price_block)
    return blocks


# ---------------------------------------------------------------------------
# Indicator-group context + dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _IndicatorGroupContext:
    """Per-audience inputs threaded into :func:`_build_indicator_group_blocks`."""

    config: DistillationDomainConfig
    as_of: datetime
    sector_label: str
    tickers: Sequence[str]
    bars_by_ticker: Mapping[str, Sequence[DailyBarRow]]
    sector_per_ticker: Mapping[str, SectorClassificationRow]
    baselines_volume: Mapping[str, TickerBaselineRow | None]
    baselines_atr: Mapping[str, TickerBaselineRow | None]
    gap_fill_history: Mapping[str, GapFillHistoryEntry]
    spy_window_returns: tuple[float, float] | None
    sector_etf_window_returns: Mapping[str, tuple[float, float] | None]


def _build_indicator_group_blocks(
    ctx: _IndicatorGroupContext,
) -> list[OutputBlock]:
    """Build the six per-audience indicator-group blocks."""
    blocks: list[OutputBlock] = []

    technicals = _compute_technicals_per_ticker(ctx.bars_by_ticker)
    if technicals:
        tech_state, tech_reason = _block_state_from_baselines(
            tickers=ctx.tickers,
            baselines=ctx.baselines_atr,
        )
        blocks.append(
            build_q1_block(
                block_id=BLOCK_ID_TECHNICALS,
                sector=ctx.sector_label,
                per_ticker=technicals,
                freshness_ts=ctx.as_of,
                calibration_state=tech_state,
                bootstrap_reason=tech_reason,
            )
        )

    volume_profile = _compute_volume_profile_per_ticker(ctx.bars_by_ticker)
    if volume_profile:
        vp_state, vp_reason = _block_state_from_baselines(
            tickers=ctx.tickers,
            baselines=ctx.baselines_volume,
        )
        blocks.append(
            build_q1_block(
                block_id=BLOCK_ID_VOLUME_PROFILE,
                sector=ctx.sector_label,
                per_ticker=volume_profile,
                freshness_ts=ctx.as_of,
                calibration_state=vp_state,
                bootstrap_reason=vp_reason,
            )
        )

    gap_payload, gap_state, gap_reason = _compute_gap_per_ticker(
        bars_by_ticker=ctx.bars_by_ticker,
        sector_per_ticker=ctx.sector_per_ticker,
        gap_fill_history=ctx.gap_fill_history,
        gap_fill_min_events=ctx.config.persistence_windows.gap_fill_min_events,
    )
    if gap_payload:
        blocks.append(
            build_q1_block(
                block_id=BLOCK_ID_GAP,
                sector=ctx.sector_label,
                per_ticker=gap_payload,
                freshness_ts=ctx.as_of,
                calibration_state=gap_state,
                bootstrap_reason=gap_reason,
            )
        )

    relative_performance = _compute_relative_performance_per_ticker(
        bars_by_ticker=ctx.bars_by_ticker,
        sector_per_ticker=ctx.sector_per_ticker,
        spy_window_returns=ctx.spy_window_returns,
        sector_etf_window_returns=ctx.sector_etf_window_returns,
    )
    if relative_performance:
        rp_state, rp_reason = _block_state_from_baselines(
            tickers=ctx.tickers,
            baselines=ctx.baselines_volume,
        )
        blocks.append(
            build_q1_block(
                block_id=BLOCK_ID_RELATIVE_PERFORMANCE,
                sector=ctx.sector_label,
                per_ticker=relative_performance,
                freshness_ts=ctx.as_of,
                calibration_state=rp_state,
                bootstrap_reason=rp_reason,
            )
        )

    trend_payload, trend_state, trend_reason = _compute_trend_state_per_ticker(
        bars_by_ticker=ctx.bars_by_ticker,
        baselines_atr=ctx.baselines_atr,
    )
    if trend_payload:
        blocks.append(
            build_q1_block(
                block_id=BLOCK_ID_TREND_STATE,
                sector=ctx.sector_label,
                per_ticker=trend_payload,
                freshness_ts=ctx.as_of,
                calibration_state=trend_state,
                bootstrap_reason=trend_reason,
            )
        )

    divergence_payload = _compute_divergence_per_ticker(ctx.bars_by_ticker)
    if divergence_payload:
        div_state, div_reason = _block_state_from_baselines(
            tickers=ctx.tickers,
            baselines=ctx.baselines_volume,
        )
        blocks.append(
            build_q1_block(
                block_id=BLOCK_ID_DIVERGENCE_FLAGS,
                sector=ctx.sector_label,
                per_ticker=divergence_payload,
                freshness_ts=ctx.as_of,
                calibration_state=div_state,
                bootstrap_reason=div_reason,
            )
        )

    return blocks


def _block_state_from_baselines(
    *,
    tickers: Sequence[str],
    baselines: Mapping[str, TickerBaselineRow | None],
) -> tuple[CalibrationState, str | None]:
    """Compute the block-level calibration state from per-ticker baselines.

    The block escalates to :attr:`CalibrationState.UNAVAILABLE` if any
    ticker carries that state (collector failure overrides "give it
    time"); otherwise falls back to :attr:`CalibrationState.ACCUMULATING`
    on the first sub-calibrated ticker.
    """
    state = CalibrationState.CALIBRATED
    reason: str | None = None
    for ticker in tickers:
        baseline = baselines.get(ticker)
        ticker_state = _baseline_calibration_state(baseline)
        if ticker_state is CalibrationState.UNAVAILABLE:
            ticker_reason = (
                f"baseline missing for {ticker}"
                if baseline is None
                else f"baseline calibration_state=unavailable for {ticker}"
            )
            return CalibrationState.UNAVAILABLE, ticker_reason
        if ticker_state is CalibrationState.ACCUMULATING and state is CalibrationState.CALIBRATED:
            state = CalibrationState.ACCUMULATING
            reason = (
                f"baseline_days: {baseline.n_observations} < {baseline.window_days} for {ticker}"
                if baseline is not None
                else f"baseline missing for {ticker}"
            )
    return state, reason


def _assemble_blocks_for_audience_from_inputs(
    *,
    config: DistillationDomainConfig,
    inputs: Q1Inputs,
    audience: OutputAudience,
    tickers: Sequence[str],
) -> list[OutputBlock]:
    """Assemble every indicator-group + anomaly block for one audience."""
    bars_by_ticker = {ticker: inputs.bars_by_ticker[ticker] for ticker in tickers}
    baselines_volume = {ticker: inputs.baselines_volume.get(ticker) for ticker in tickers}
    baselines_atr = {ticker: inputs.baselines_atr.get(ticker) for ticker in tickers}
    sector_label = _audience_to_alphamind_sector(audience)
    gap_fill_history = {
        ticker: inputs.gap_fill_history[ticker]
        for ticker in tickers
        if ticker in inputs.gap_fill_history
    }

    ctx = _IndicatorGroupContext(
        config=config,
        as_of=inputs.as_of,
        sector_label=sector_label,
        tickers=tuple(tickers),
        bars_by_ticker=bars_by_ticker,
        sector_per_ticker=inputs.sector_per_ticker,
        baselines_volume=baselines_volume,
        baselines_atr=baselines_atr,
        gap_fill_history=gap_fill_history,
        spy_window_returns=inputs.spy_window_returns,
        sector_etf_window_returns=inputs.sector_etf_window_returns,
    )
    blocks: list[OutputBlock] = []
    blocks.extend(_build_indicator_group_blocks(ctx))
    blocks.extend(
        _assemble_anomaly_blocks(
            audience=audience,
            bars_by_ticker=bars_by_ticker,
            baselines_by_ticker=baselines_volume,
            config=config,
            freshness_ts=inputs.as_of,
        )
    )
    return blocks


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


def assemble_q1_blocks_from_inputs(
    inputs: Q1Inputs,
    *,
    config: DistillationDomainConfig,
) -> list[OutputBlock]:
    """Pure-compute assembly of every Q1 :class:`OutputBlock`.

    Operates entirely on the pre-loaded :class:`Q1Inputs`; no DB access.
    This is the function the orchestrator's Phase 2 calls under
    ``asyncio.TaskGroup`` + ``asyncio.to_thread``.
    """
    if not inputs.ticker_scope:
        return []
    grouped = _group_tickers_by_audience(inputs.sector_per_ticker)
    if not grouped:
        return []
    blocks: list[OutputBlock] = []
    for audience in sorted(grouped, key=lambda a: a.value):
        blocks.extend(
            _assemble_blocks_for_audience_from_inputs(
                config=config,
                inputs=inputs,
                audience=audience,
                tickers=grouped[audience],
            )
        )
    return blocks


def assemble_q1_blocks(
    session: Session,
    *,
    config: DistillationDomainConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
) -> list[OutputBlock]:
    """Session-accepting shim that delegates to the pure assembly path.

    Existing call sites pass a ``Session`` directly; the shim constructs a
    :class:`SqlDistillationRepository`, pre-loads the :class:`Q1Inputs`, and
    delegates to :func:`assemble_q1_blocks_from_inputs`.
    """
    repository = SqlDistillationRepository(session)
    inputs = load_q1_inputs(repository, config=config, as_of=as_of, ticker_scope=ticker_scope)
    return assemble_q1_blocks_from_inputs(inputs, config=config)


__all__ = [
    "BLOCK_ID_PRICE_MOVE_ANOMALY",
    "BLOCK_ID_VOLUME_ANOMALY",
    "Q1Inputs",
    "assemble_q1_blocks",
    "assemble_q1_blocks_from_inputs",
    "load_q1_inputs",
]
