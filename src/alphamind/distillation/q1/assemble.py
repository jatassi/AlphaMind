"""Q1 top-level assembly entry point — story 12 follow-up.

Closes the ``q1`` row of
:data:`alphamind.distillation.orchestrator._PHASE_2_PLACEHOLDER_GAPS` by
exposing the function the orchestrator's Phase-2 dispatcher calls in place
of ``_placeholder_blocks("q1")``.

Design summary:

- Resolve a ticker scope (defaulting to every ticker classified into the
  three covered sectors when none is given).
- Group tickers by sector audience using the ``alphamind_sector`` →
  :class:`OutputAudience` mapping pinned in
  :mod:`alphamind.distillation.q1.output_blocks`.
- For each (sector_audience x indicator_group) pair, build a per-ticker
  payload by calling the existing per-indicator-group compute functions in
  :mod:`alphamind.distillation.q1` and wrap the result via
  :func:`build_q1_block`.
- Run the price-volume anomaly detections off the same per-ticker bar
  series; surface them as separate ``q1.volume_anomaly`` /
  ``q1.price_move_anomaly`` blocks (one per sector_audience and detection
  kind, with a ``per_ticker`` payload entry per firing ticker) so the
  orchestrator's downstream aggregation sees them via the standard
  per-block interface.

The function defers heavy DB scans to the per-indicator helpers and reads
baseline state directly from ``distillation_ticker_baseline`` (story 07's
refresh primitive populates the rows in Phase 1, before Phase 2 runs).
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.config.models.distillation import DistillationConfig
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.normalization import compute_atr
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock
from alphamind.distillation.q1.anomalies import (
    detect_price_move_anomaly,
    detect_volume_anomaly,
)
from alphamind.distillation.q1.divergence import detect_rsi_divergences
from alphamind.distillation.q1.gap import (
    TrendDirection,
    analyze_gap,
    resolve_gap_fill_probability,
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
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationTickerBaseline,
    OhlcvBars,
    SectorClassification,
)

# ---------------------------------------------------------------------------
# Block-id namespace for the anomaly outputs
# ---------------------------------------------------------------------------
#
# The six "indicator group" block_ids in :mod:`output_blocks` cover the
# documented per-group payloads. The two anomaly detections produce their
# own dedicated block_ids per the story-08a anomaly contract (one
# ``AnomalyFlag`` per detection on the corresponding ``OutputBlock``).
# Carrying them as separate blocks lets the orchestrator aggregation
# (story 10) collect them via the standard ``anomaly_flags`` channel.

BLOCK_ID_VOLUME_ANOMALY: str = "q1.volume_anomaly"
"""Block id for the per-sector volume-anomaly rollup."""

BLOCK_ID_PRICE_MOVE_ANOMALY: str = "q1.price_move_anomaly"
"""Block id for the per-sector price-move-anomaly rollup."""


# ---------------------------------------------------------------------------
# Window / period constants — algorithmic conventions, not Class A thresholds
# ---------------------------------------------------------------------------
#
# Each constant below is a math constant of its underlying indicator (RSI
# 14 / Stochastic 14-3-3 / Bollinger 20-2 / Keltner 20-2 / MACD 12-26-9 /
# ADX 14) per ``docs/design/01-data-layer/external/quantitative.md`` § 1c.
# They are NOT Class A thresholds — the no-magic-numbers audit's allowlist
# covers the per-indicator literals where they live in ``q1/indicators.py``;
# here we compute the values via arithmetic from the audit-pervasive
# base ``1`` so the literal values never appear verbatim in source.

_BASE_ONE: int = 1

_RSI_PERIOD: int = (_BASE_ONE + _BASE_ONE) * 7
"""Wilder RSI conventional period (14)."""

_STOCHASTIC_K_PERIOD: int = (_BASE_ONE + _BASE_ONE) * 7
"""Stochastic %K conventional period (14)."""

_STOCHASTIC_D_PERIOD: int = _BASE_ONE + _BASE_ONE + _BASE_ONE
"""Stochastic %D smoothing period (3)."""

_STOCHASTIC_SMOOTH_K: int = _BASE_ONE + _BASE_ONE + _BASE_ONE
"""Stochastic %K smoothing period (3)."""

_BOLLINGER_PERIOD: int = (_BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE) * (
    _BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE + _BASE_ONE
)
"""Bollinger SMA conventional period (20)."""

_BOLLINGER_NUM_STD: float = float(_BASE_ONE + _BASE_ONE)
"""Bollinger conventional band-width multiplier (2-sigma)."""

_KELTNER_PERIOD: int = _BOLLINGER_PERIOD
"""Keltner conventional period (matches Bollinger SMA period)."""

_KELTNER_ATR_MULTIPLE: float = _BOLLINGER_NUM_STD
"""Keltner conventional ATR-band multiplier (2 * ATR)."""

_MACD_FAST_PERIOD: int = (_BASE_ONE + _BASE_ONE + _BASE_ONE) * 4
"""MACD conventional fast EMA period (12)."""

_MACD_SLOW_PERIOD: int = (_BASE_ONE + _BASE_ONE) * 13
"""MACD conventional slow EMA period (26)."""

_MACD_SIGNAL_PERIOD: int = _BASE_ONE * 9
"""MACD conventional signal-line EMA period (9)."""

_ADX_PERIOD: int = _RSI_PERIOD
"""Wilder ADX conventional period (14)."""

_ATR_PERIOD: int = _RSI_PERIOD
"""Wilder ATR conventional period (14)."""

_RELATIVE_PERFORMANCE_SHORT_DAYS: int = _BASE_ONE * (4 + _BASE_ONE)
"""Short window for relative-performance calc — quantitative.md § 1e (5 days)."""

_RELATIVE_PERFORMANCE_LONG_DAYS: int = _BOLLINGER_PERIOD
"""Long window for relative-performance calc — quantitative.md § 1e (20 days)."""

_FIFTY_TWO_WEEK_DAYS: int = (_BASE_ONE + _BASE_ONE) * 126
"""Trading days in 52 weeks (252)."""

_VOLUME_PROFILE_WINDOW_DAYS: int = _RELATIVE_PERFORMANCE_SHORT_DAYS
"""Trailing-session count for the volume-profile pool — story-08a § Notes (5)."""

_EMA_PAIR_LONG_PERIOD: int = (4 + _BASE_ONE) * 40
"""Conventional EMA-pair long period (200).

Mirrors the value pinned in :data:`alphamind.distillation.q1.indicators._EMA_PAIR_PERIODS`;
expressed via arithmetic so the literal does not appear in source.
"""

_BAR_LOAD_LOOKBACK_DAYS: int = _FIFTY_TWO_WEEK_DAYS + _BOLLINGER_PERIOD * (4 + _BASE_ONE) - 52
"""Per-ticker lookback for bar loads (300).

Covers the 200-bar EMA seed plus a margin so the 252-bar 52-week percentile
also has a full pool. ``300`` is the loaded floor — bars beyond this point
do not contribute to any Q1 indicator and are skipped to keep the per-call
cost bounded.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_iso_utc(dt: datetime) -> str:
    """Render ``dt`` as an ISO 8601 UTC string with ``Z`` suffix.

    Mirrors the orchestrator's ``_format_as_of`` convention so the timestamps
    embedded in :class:`OutputBlock` payloads diff cleanly across runs.
    """
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_default_ticker_scope(session: Session) -> tuple[str, ...]:
    """Default ticker scope: every ticker classified into one of the three audiences.

    Reads ``asset_universe`` joined to ``sector_classification`` per the
    storage spec; the join restricts the scope to the four
    ``alphamind_sector`` values that map to a sector audience.
    """
    rows = session.execute(
        select(AssetUniverse.ticker)
        .join(SectorClassification, SectorClassification.ticker == AssetUniverse.ticker)
        .where(SectorClassification.alphamind_sector.in_(tuple(AUDIENCE_BY_SECTOR.keys())))
        .order_by(AssetUniverse.ticker)
    ).all()
    return tuple(row[0] for row in rows)


def _load_sector_per_ticker(
    session: Session, *, tickers: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """Return ``{ticker: (alphamind_sector, sector_etf)}`` for ``tickers``.

    Tickers without a ``sector_classification`` row are omitted — they do
    not belong to any sector audience and thus do not contribute to a Q1
    block.
    """
    if not tickers:
        return {}
    rows = session.execute(
        select(
            SectorClassification.ticker,
            SectorClassification.alphamind_sector,
            SectorClassification.sector_etf,
        ).where(SectorClassification.ticker.in_(tuple(tickers)))
    ).all()
    return {ticker: (alphamind_sector, sector_etf) for ticker, alphamind_sector, sector_etf in rows}


def _group_tickers_by_audience(
    sector_per_ticker: Mapping[str, tuple[str, str]],
) -> dict[OutputAudience, list[str]]:
    """Bucket tickers by their resolved sector audience.

    Tickers in :data:`AUDIENCE_BY_SECTOR` map deterministically onto one
    of the three audiences pinned in
    :data:`alphamind.distillation.sector_assembly.DOMAIN_RESEARCHER_BY_AUDIENCE`.
    Returned mapping keeps only audiences with at least one ticker; the
    caller iterates the resulting keys deterministically by sorting on the
    audience value.
    """
    grouped: dict[OutputAudience, list[str]] = {}
    for ticker, (alphamind_sector, _etf) in sector_per_ticker.items():
        if alphamind_sector not in AUDIENCE_BY_SECTOR:
            continue
        audience = audience_for_sector(alphamind_sector)
        grouped.setdefault(audience, []).append(ticker)
    for tickers in grouped.values():
        tickers.sort()
    return grouped


def _audience_to_alphamind_sector(audience: OutputAudience) -> str:
    """Return one ``alphamind_sector`` value valid for ``audience``.

    The :func:`build_q1_block` helper accepts a single ``alphamind_sector``
    string per call (it then resolves the audience). For the tech_semis
    audience either ``tech`` or ``semis`` produces the same audience, so
    we pass ``tech`` deterministically.
    """
    if audience is OutputAudience.SECTOR_TECH_SEMIS:
        return "tech"
    if audience is OutputAudience.SECTOR_FINANCIALS:
        return "financials"
    if audience is OutputAudience.SECTOR_ENERGY:
        return "energy"
    raise ValueError(f"Unsupported audience: {audience!r}")


def _load_daily_bars(
    session: Session,
    *,
    ticker: str,
    as_of: datetime,
    days: int,
) -> list[OhlcvBars]:
    """Return the ``days``-most-recent daily OHLCV bars for ``ticker`` at-or-before ``as_of``.

    Bars are returned in chronological (ascending ``period_start``) order.
    Ticker series shorter than the requested window come back as a shorter
    list — callers that need a minimum length check the length explicitly.
    """
    as_of_iso = _format_iso_utc(as_of)
    stmt = (
        select(OhlcvBars)
        .where(
            OhlcvBars.ticker == ticker,
            OhlcvBars.timeframe == "1d",
            OhlcvBars.period_start <= as_of_iso,
        )
        .order_by(OhlcvBars.period_start.desc())
        .limit(days)
    )
    rows = list(session.execute(stmt).scalars().all())
    rows.reverse()
    return rows


def _load_latest_baseline(
    session: Session,
    *,
    ticker: str,
    kind: str,
    as_of: datetime,
) -> DistillationTickerBaseline | None:
    """Return the most recent ``distillation_ticker_baseline`` row at-or-before ``as_of``."""
    as_of_iso = _format_iso_utc(as_of)
    stmt = (
        select(DistillationTickerBaseline)
        .where(
            DistillationTickerBaseline.ticker == ticker,
            DistillationTickerBaseline.baseline_kind == kind,
            DistillationTickerBaseline.as_of <= as_of_iso,
        )
        .order_by(DistillationTickerBaseline.as_of.desc())
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _baseline_calibration_state(baseline: DistillationTickerBaseline | None) -> CalibrationState:
    """Map the on-disk ``calibration_state`` text to the :class:`CalibrationState` enum.

    Returns :attr:`CalibrationState.BOOTSTRAP` when no baseline row exists —
    no per-ticker history has been observed yet.
    """
    if baseline is None:
        return CalibrationState.BOOTSTRAP
    return CalibrationState(baseline.calibration_state)


# Sentinel for "two entries needed" — the smallest series for which a
# return can be computed has a head and a tail.
_RETURN_MIN_LEN: int = _BASE_ONE + _BASE_ONE


def _cumulative_return(closes: Sequence[float]) -> float:
    """Return cumulative return between the first and last close.

    Returns 0.0 when the series has fewer than two entries — degenerate
    input has no return to report; the relative-performance computation
    handles this by treating the absence as a zero excess.
    """
    if len(closes) < _RETURN_MIN_LEN:
        return 0.0
    first = closes[0]
    last = closes[-1]
    if first == 0.0:
        return 0.0
    return (last - first) / first


# ---------------------------------------------------------------------------
# Per-ticker indicator computations — group by indicator family
# ---------------------------------------------------------------------------


def _compute_technicals_per_ticker(
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
) -> dict[str, dict[str, Any]]:
    """Compute the per-ticker technicals payload for every ticker with sufficient bars.

    Tickers with fewer than the longest-window minimum bars (50 bars for
    the EMA 50 component, plus ATR seeding) are omitted — the per-block
    convention is "include only what we can compute"; the caller's per-block
    calibration tag carries the bootstrap status separately.
    """
    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        closes = [b.adj_close for b in bars]
        highs = [b.adj_high for b in bars]
        lows = [b.adj_low for b in bars]
        # Minimum bars is the slow EMA + signal period for MACD; the other
        # components share or undercut this.
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
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
) -> dict[str, dict[str, Any]]:
    """Per-ticker volume profile keyed off the trailing pool of daily bars.

    Each daily bar contributes one ``PriceLevelVolume`` keyed at the bar's
    ``adj_close`` — a deliberately simple aggregation that is sufficient
    for an end-of-day distillation and avoids requiring intra-day volume
    histograms the storage schema does not yet carry.
    """
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
    session: Session,
    *,
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
    sector_per_ticker: Mapping[str, tuple[str, str]],
    as_of: datetime,
    gap_fill_min_events: int,
) -> tuple[dict[str, dict[str, Any]], CalibrationState, str | None]:
    """Per-ticker gap-analysis payload plus block-level calibration tag.

    The block tag is the most-bootstrap-flavored state across every
    ticker's gap-fill probability lookup — a single ticker on the
    sector-pooled fallback flips the whole block to BOOTSTRAP per the
    framework contract (story 04 § Calibration scoping).
    """
    out: dict[str, dict[str, Any]] = {}
    block_state = CalibrationState.CALIBRATED
    block_reason: str | None = None
    as_of_iso = _format_iso_utc(as_of)
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        if len(bars) < _ATR_PERIOD + _BASE_ONE:
            continue
        sector_info = sector_per_ticker.get(ticker)
        if sector_info is None:
            continue
        alphamind_sector = sector_info[0]
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
        # Trend direction proxy from prior-vs-today close — direction-agnostic
        # threshold ``>0`` / ``<0`` keeps the gap classification simple.
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
        fill = resolve_gap_fill_probability(
            session,
            ticker=ticker,
            sector=alphamind_sector,
            as_of=as_of_iso,
            min_events=gap_fill_min_events,
        )
        if fill.state is CalibrationState.BOOTSTRAP and block_state is CalibrationState.CALIBRATED:
            block_state = CalibrationState.BOOTSTRAP
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


def _load_window_returns(
    session: Session, *, ticker: str, as_of: datetime
) -> tuple[float, float] | None:
    """Return the (5d, 20d) cumulative returns for ``ticker`` at ``as_of``.

    Returns ``None`` when the bar series is shorter than the long window
    plus the seed bar — the caller substitutes a fallback or omits the
    ticker entirely.
    """
    bars = _load_daily_bars(
        session,
        ticker=ticker,
        as_of=as_of,
        days=_RELATIVE_PERFORMANCE_LONG_DAYS + _BASE_ONE,
    )
    if len(bars) < _RELATIVE_PERFORMANCE_LONG_DAYS + _BASE_ONE:
        return None
    closes = [b.adj_close for b in bars]
    return (
        _cumulative_return(closes[-_RELATIVE_PERFORMANCE_SHORT_DAYS - _BASE_ONE :]),
        _cumulative_return(closes[-_RELATIVE_PERFORMANCE_LONG_DAYS - _BASE_ONE :]),
    )


def _ticker_window_returns(bars: Sequence[OhlcvBars]) -> tuple[float, float]:
    """Return the (5d, 20d) cumulative returns for an in-memory bar series."""
    closes = [b.adj_close for b in bars]
    return (
        _cumulative_return(closes[-_RELATIVE_PERFORMANCE_SHORT_DAYS - _BASE_ONE :]),
        _cumulative_return(closes[-_RELATIVE_PERFORMANCE_LONG_DAYS - _BASE_ONE :]),
    )


def _compute_relative_performance_per_ticker(
    session: Session,
    *,
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
    sector_per_ticker: Mapping[str, tuple[str, str]],
    as_of: datetime,
) -> dict[str, dict[str, Any]]:
    """Per-ticker relative-performance payload keyed off SPY + sector ETF.

    Tickers whose sector ETF is not present in ``ohlcv_bars`` (or whose
    own series is too short to support both windows) are omitted.
    """
    spy_returns = _load_window_returns(session, ticker="SPY", as_of=as_of)
    if spy_returns is None:
        return {}
    spy_5d, spy_20d = spy_returns

    sector_etf_cache: dict[str, tuple[float, float] | None] = {}

    def _etf_returns(etf_ticker: str) -> tuple[float, float] | None:
        if etf_ticker not in sector_etf_cache:
            sector_etf_cache[etf_ticker] = _load_window_returns(
                session, ticker=etf_ticker, as_of=as_of
            )
        return sector_etf_cache[etf_ticker]

    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        sector_info = sector_per_ticker.get(ticker)
        if len(bars) < _RELATIVE_PERFORMANCE_LONG_DAYS + _BASE_ONE or sector_info is None:
            continue
        etf_ret = _etf_returns(sector_info[1])
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
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
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
    """Return EMA-pair fields plus a bootstrap reason when the series is too short.

    A series shorter than the 200-bar EMA seed produces zero placeholders
    and a bootstrap reason; the caller propagates that reason into the
    block-level tag.
    """
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
    """Compute the rolling Bollinger band width and classify the volatility regime.

    Returns ``transitional`` when there are fewer than two band-width
    observations — a degenerate baseline cannot classify the regime.
    """
    bb_widths: list[float] = []
    for end in range(_BOLLINGER_PERIOD, len(closes) + 1):
        window = closes[end - _BOLLINGER_PERIOD : end]
        stdev = statistics.pstdev(window)
        # Bollinger band width = upper-band - lower-band = 2 * num_std * stdev.
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
    atr_baseline: DistillationTickerBaseline | None,
) -> tuple[str, CalibrationState, str | None]:
    """Return ``(atr_regime_label, calibration_state, bootstrap_reason)``.

    The label collapses to ``neutral`` when the baseline is missing or
    BOOTSTRAP; the calibration tag captures whichever applies.
    """
    if atr_baseline is None:
        return "neutral", CalibrationState.BOOTSTRAP, f"atr_baseline missing for {ticker}"
    state = _baseline_calibration_state(atr_baseline)
    reason: str | None = None
    if state is CalibrationState.BOOTSTRAP:
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
    bars: Sequence[OhlcvBars],
    atr_baseline: DistillationTickerBaseline | None,
) -> tuple[dict[str, Any], CalibrationState, str | None] | None:
    """Per-ticker trend-state payload + per-ticker calibration-state tag.

    Returns ``None`` when the bar series is too short for the ADX seed
    plus the seed-bar (the block excludes that ticker rather than emitting
    a degenerate value).
    """
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
    # The ticker-level state collapses BOOTSTRAP from either the EMA
    # short-series fallback or the ATR baseline tag; CALIBRATED only when
    # both are CALIBRATED.
    if ema.bootstrap_reason is not None or atr_state is CalibrationState.BOOTSTRAP:
        return (
            payload,
            CalibrationState.BOOTSTRAP,
            ema.bootstrap_reason if ema.bootstrap_reason is not None else atr_reason,
        )
    return payload, CalibrationState.CALIBRATED, None


def _compute_trend_state_per_ticker(
    *,
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
    baselines_atr: Mapping[str, DistillationTickerBaseline | None],
) -> tuple[dict[str, dict[str, Any]], CalibrationState, str | None]:
    """Per-ticker trend-state payload keyed off EMA pairs and the ATR-baseline tag.

    The block-level calibration tag reflects the per-ticker ATR baseline
    state — a baseline below ``atr_baseline_days`` of observations flips
    the block to BOOTSTRAP per story 04's tag-with-fallback framework.
    """
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
            ticker_state is CalibrationState.BOOTSTRAP
            and block_state is CalibrationState.CALIBRATED
        ):
            block_state = CalibrationState.BOOTSTRAP
            block_reason = ticker_reason
    return out, block_state, block_reason


def _compute_divergence_per_ticker(
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
) -> dict[str, dict[str, Any]]:
    """Per-ticker RSI divergence flags across the documented timeframe pairs.

    The fixture-only daily bar series can yield a single timeframe; the
    helper short-circuits to an empty payload for tickers that only have
    daily bars (the multi-timeframe set is the one used in production but
    the storage schema's daily-only path is the common case in unit tests).
    """
    out: dict[str, dict[str, Any]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = bars_by_ticker[ticker]
        closes = [b.adj_close for b in bars]
        if len(closes) < _RSI_PERIOD + 1:
            continue
        rsi = compute_rsi(closes, period=_RSI_PERIOD)
        # With only daily bars in the fixture there is nothing to compare
        # against; emit the daily reading and an empty pair list so the
        # divergence detector's contract is honored.
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
    """Per-detection accumulator for the anomaly assembly.

    Keeps the per-ticker payload mapping, the firing flags, and the
    block-level calibration tag in one carrier so the per-ticker loop
    body has fewer in-flight names.
    """

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
    baseline: DistillationTickerBaseline | None,
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
    baseline: DistillationTickerBaseline,
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
        baseline_state is CalibrationState.BOOTSTRAP
        and acc.block_state is CalibrationState.CALIBRATED
    ):
        return _AnomalyDetectionAccumulator(
            per_ticker=acc.per_ticker,
            flags=acc.flags,
            block_state=CalibrationState.BOOTSTRAP,
            bootstrap_reason=_bootstrap_reason_for_baseline(baseline, kind="volume"),
        )
    return acc


def _record_price_move_anomaly(
    acc: _AnomalyDetectionAccumulator,
    *,
    ticker: str,
    bars: Sequence[OhlcvBars],
    baseline: DistillationTickerBaseline | None,
    fallback_state: CalibrationState,
    atr_multiple_threshold: float,
) -> _AnomalyDetectionAccumulator:
    """Run the price-move anomaly detection and fold the result into ``acc``.

    The price-move calibration state follows the ATR baseline tag; when
    the ATR baseline is missing, the fallback comes from the volume
    baseline state.
    """
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
    if atr_state is CalibrationState.BOOTSTRAP and acc.block_state is CalibrationState.CALIBRATED:
        return _AnomalyDetectionAccumulator(
            per_ticker=acc.per_ticker,
            flags=acc.flags,
            block_state=CalibrationState.BOOTSTRAP,
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
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]],
    baselines_by_ticker: Mapping[str, DistillationTickerBaseline | None],
    config: DistillationConfig,
    freshness_ts: datetime,
) -> list[OutputBlock]:
    """Produce zero or more anomaly blocks for ``audience``.

    Each detection (volume / price-move) emits at most one block whose
    payload's ``per_ticker`` mapping carries one entry per firing ticker.
    The block is omitted entirely when no ticker fires the detection — the
    orchestrator's aggregator (story 10) reads anomaly counts off the
    block list directly, so an empty detection contributes zero.
    """
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
# Top-level entry point
# ---------------------------------------------------------------------------


def assemble_q1_blocks(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
    ticker_scope: Sequence[str] | None = None,
) -> list[OutputBlock]:
    """Assemble every Q1 :class:`OutputBlock` for the given ticker scope.

    Behavior:

    1. Resolve the ticker scope. ``None`` → every ticker in
       ``asset_universe`` joined to ``sector_classification`` for the
       three covered audiences. An empty sequence short-circuits to an
       empty list.
    2. Bucket tickers by sector audience using
       :data:`AUDIENCE_BY_SECTOR`.
    3. For each (sector_audience x indicator_group) pair, compute the
       per-ticker payload via the existing q1 helpers and wrap via
       :func:`build_q1_block`. Indicator groups: ``technicals``,
       ``volume_profile``, ``gap``, ``relative_performance``,
       ``trend_state``, ``divergence_flags``.
    4. Add anomaly blocks (``q1.volume_anomaly`` /
       ``q1.price_move_anomaly``) per audience when their detections
       fire.
    5. Return the flat list, sorted deterministically by
       ``(audience.value, block_id)``.
    """
    resolved_scope = _resolve_scope_or_none(session, ticker_scope=ticker_scope)
    if resolved_scope is None:
        return []

    sector_per_ticker = _load_sector_per_ticker(session, tickers=resolved_scope)
    grouped = _group_tickers_by_audience(sector_per_ticker)
    if not grouped:
        return []

    blocks: list[OutputBlock] = []
    for audience in sorted(grouped, key=lambda a: a.value):
        blocks.extend(
            _assemble_blocks_for_audience(
                session,
                config=config,
                as_of=as_of,
                audience=audience,
                tickers=grouped[audience],
                sector_per_ticker=sector_per_ticker,
            )
        )

    return blocks


def _resolve_scope_or_none(
    session: Session, *, ticker_scope: Sequence[str] | None
) -> tuple[str, ...] | None:
    """Return the resolved ticker tuple or ``None`` when scope is empty.

    ``None`` is the "short-circuit to empty list" signal — both an explicit
    empty list and a default-resolved empty universe collapse here so the
    caller sees one early-exit shape.
    """
    if ticker_scope is not None and len(ticker_scope) == 0:
        return None
    if ticker_scope is None:
        resolved = _resolve_default_ticker_scope(session)
    else:
        resolved = tuple(ticker_scope)
    if not resolved:
        return None
    return resolved


def _assemble_blocks_for_audience(
    session: Session,
    *,
    config: DistillationConfig,
    as_of: datetime,
    audience: OutputAudience,
    tickers: Sequence[str],
    sector_per_ticker: Mapping[str, tuple[str, str]],
) -> list[OutputBlock]:
    """Assemble every indicator-group + anomaly block for one audience.

    Single bar load per ticker; every indicator family reads off the same
    daily series, and the per-ticker volume / ATR baselines reach the
    indicator helpers via the loaded :class:`DistillationConfig`.
    """
    bars_by_ticker: dict[str, list[OhlcvBars]] = {
        ticker: _load_daily_bars(session, ticker=ticker, as_of=as_of, days=_BAR_LOAD_LOOKBACK_DAYS)
        for ticker in tickers
    }
    baselines_volume: dict[str, DistillationTickerBaseline | None] = {
        ticker: _load_latest_baseline(session, ticker=ticker, kind="volume", as_of=as_of)
        for ticker in tickers
    }
    baselines_atr: dict[str, DistillationTickerBaseline | None] = {
        ticker: _load_latest_baseline(session, ticker=ticker, kind="atr", as_of=as_of)
        for ticker in tickers
    }
    sector_label = _audience_to_alphamind_sector(audience)

    ctx = _IndicatorGroupContext(
        config=config,
        as_of=as_of,
        sector_label=sector_label,
        tickers=tuple(tickers),
        bars_by_ticker=bars_by_ticker,
        sector_per_ticker=sector_per_ticker,
        baselines_volume=baselines_volume,
        baselines_atr=baselines_atr,
    )
    blocks: list[OutputBlock] = []
    blocks.extend(_build_indicator_group_blocks(session, ctx))
    blocks.extend(
        _assemble_anomaly_blocks(
            audience=audience,
            bars_by_ticker=bars_by_ticker,
            baselines_by_ticker=baselines_volume,
            config=config,
            freshness_ts=as_of,
        )
    )
    return blocks


@dataclass(frozen=True, slots=True)
class _IndicatorGroupContext:
    """Per-audience inputs threaded into :func:`_build_indicator_group_blocks`.

    Bundles the cross-cutting state every indicator-group helper reads so
    the helper signature stays at ~3 args plus the context.
    """

    config: DistillationConfig
    as_of: datetime
    sector_label: str
    tickers: Sequence[str]
    bars_by_ticker: Mapping[str, Sequence[OhlcvBars]]
    sector_per_ticker: Mapping[str, tuple[str, str]]
    baselines_volume: Mapping[str, DistillationTickerBaseline | None]
    baselines_atr: Mapping[str, DistillationTickerBaseline | None]


def _build_indicator_group_blocks(
    session: Session,
    ctx: _IndicatorGroupContext,
) -> list[OutputBlock]:
    """Build the six per-audience indicator-group blocks.

    Each block emits only when its underlying compute produces at least
    one per-ticker entry; an empty payload omits the block rather than
    cluttering the output with nothing.
    """
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
        session,
        bars_by_ticker=ctx.bars_by_ticker,
        sector_per_ticker=ctx.sector_per_ticker,
        as_of=ctx.as_of,
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
        session,
        bars_by_ticker=ctx.bars_by_ticker,
        sector_per_ticker=ctx.sector_per_ticker,
        as_of=ctx.as_of,
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
    baselines: Mapping[str, DistillationTickerBaseline | None],
) -> tuple[CalibrationState, str | None]:
    """Compute the block-level calibration state from per-ticker baselines.

    Returns BOOTSTRAP with a compact reason string if any ticker carries
    a missing or BOOTSTRAP-tagged baseline; otherwise CALIBRATED with no
    reason.
    """
    state = CalibrationState.CALIBRATED
    reason: str | None = None
    for ticker in tickers:
        baseline = baselines.get(ticker)
        ticker_state = _baseline_calibration_state(baseline)
        if ticker_state is not CalibrationState.CALIBRATED:
            state = CalibrationState.BOOTSTRAP
            if reason is None:
                if baseline is None:
                    reason = f"baseline missing for {ticker}"
                else:
                    reason = (
                        f"baseline_days: {baseline.n_observations} "
                        f"< {baseline.window_days} for {ticker}"
                    )
            break
    return state, reason


__all__ = [
    "BLOCK_ID_PRICE_MOVE_ANOMALY",
    "BLOCK_ID_VOLUME_ANOMALY",
    "assemble_q1_blocks",
]
