"""Q1: Price and Volume — entity definitions.

7 entities covering multi-timeframe OHLCV, volume profiling, technical
indicators, gap analysis, relative performance, historical context, and
extended-hours data. This is the foundational data domain — nearly every
other domain depends on Q1 outputs.

Design note on multi-timeframe structure:
    The system uses 5 agent-visible timeframes (15min, 1hr, 4hr, daily, weekly).
    Rather than storing one record per timeframe, each entity packs all timeframes
    into a single record per ticker per invocation. This optimizes for LLM
    consumption — a sector analyst reading NVDA's price structure sees all
    timeframes in one context block, enabling cross-timeframe pattern recognition
    without additional retrieval.

Design note on raw vs. computed:
    These entities represent the TARGET data model — the complete payload
    passed to analysis layer agents. Some fields are raw ingested data (OHLCV
    bars from Polygon), others are computed by the distillation layer (trend
    state, swing detection, technical indicators). The schema doesn't distinguish
    between raw and computed — it defines what downstream consumers receive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ._common import (
    AnomalyFlag,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
    Timeframe,
    TrendState,
)

__all__ = [
    "DivergenceFlag",
    "ExtendedHoursBar",
    "GapAnalysis",
    "HistoricalContext",
    "IndicatorReading",
    "IntradayGap",
    "MultiTimeframePriceBar",
    "OHLCVBar",
    "ReferenceLevels",
    "RelativePerformance",
    "SwingPoint",
    "TechnicalIndicator",
    "TimeframeTrend",
    "VolumeProfile",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OHLCVBar:
    """A single OHLCV candle at a specific timeframe. The atomic unit of price data.

    All price fields are in USD. Volume is in shares. The period_start and
    period_end define the candle's time range (e.g., for a daily bar:
    period_start=9:30 AM ET, period_end=4:00 PM ET).
    """

    open: float
    high: float
    low: float
    close: float
    volume: int  # Share volume during this bar
    vwap: float  # Volume-weighted average price for this bar. More representative
    # of "true" price than close. Used by order flow (Q2:2e) for
    # VWAP trajectory analysis and execution layer for fill benchmarking.
    trade_count: int = 0  # Number of individual trades. Higher trade count at same volume
    # means smaller average trade size — potentially more retail.
    period_start: datetime | None = None  # UTC start of bar period
    period_end: datetime | None = None  # UTC end of bar period


@dataclass(frozen=True)
class ReferenceLevels:
    """Key reference price levels where institutional algos cluster orders,
    making them self-fulfilling support/resistance. These levels are the
    "magnet" prices that the analyst uses for entry/exit targeting.

    All prices in USD.
    """

    # Prior day levels — the most actively watched by algorithms
    prior_day_high: float  # Yesterday's high — breakout above is bullish
    prior_day_low: float  # Yesterday's low — breakdown below is bearish
    prior_day_close: float  # Official close — the anchor for overnight gap calculation
    prior_day_vwap: float  # Yesterday's VWAP — institutional execution benchmark

    # Longer-term reference levels
    weekly_open: float  # Monday's open price — weekly trend anchor
    monthly_open: float  # First trading day of month open — monthly trend anchor
    prior_week_high: float  # Last week's high
    prior_week_low: float  # Last week's low

    # Current session context
    current_session_vwap: float = 0.0  # Running VWAP for the current session. Institutional
    # execution algos benchmark against this — price persistently
    # above VWAP signals buy-side aggression, below signals sell-side.
    current_session_high: float = 0.0
    current_session_low: float = 0.0


@dataclass(frozen=True)
class SwingPoint:
    """A detected swing high or swing low — a local price extreme that defines
    trend structure. The sequence of swing points tells you whether the market
    is making higher highs/higher lows (uptrend) or lower highs/lower lows
    (downtrend)."""

    price: float  # Price level of the swing point
    timestamp: datetime  # When the swing point occurred (UTC)
    is_high: bool  # True for swing high, False for swing low
    strength: int = 1  # How many bars on each side confirm this swing point.
    # Higher = more significant. Minimum 1 (3-bar pivot).
    # Typical values: 1-5 for 15min, 2-10 for daily.


@dataclass(frozen=True)
class TimeframeTrend:
    """Trend state assessment at a single timeframe. Combines swing structure
    analysis with moving average positioning for a holistic trend read."""

    timeframe: Timeframe
    trend_state: TrendState  # Classified trend direction
    swing_highs: list[SwingPoint] = field(default_factory=list)
    # Recent swing highs (most recent first). Typically 3-5 points retained.
    # Sequence of higher highs confirms uptrend; lower highs confirms downtrend.
    swing_lows: list[SwingPoint] = field(default_factory=list)
    # Recent swing lows (most recent first). Same logic inverted.
    ma_alignment: str = ""  # Describes EMA stacking at this timeframe:
    # "bullish_stack" = price > 20 > 50 > 200 EMA
    # "bearish_stack" = price < 20 < 50 < 200 EMA
    # "mixed" = EMAs interleaved (range-bound typical)
    trend_duration_bars: int = 0  # How many bars the current trend state has persisted.
    # Longer duration = more established trend.
    trend_confidence: float = 0.0  # 0.0-1.0. Higher when swing structure and MA alignment
    # agree. Lower when they conflict (e.g., making higher
    # highs but below the 200 EMA).


# ── Primary entities ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MultiTimeframePriceBar:
    """Q1:1a — Multi-timeframe OHLCV bars with reference levels and trend state.

    Covers 15-min through weekly candles, prior-day/week/month reference levels,
    swing structure per timeframe, and composite trend state classification.

    This is the most consumed entity in the system — it feeds: volume profile
    computation (Q1:1b), all technical indicators (Q1:1c), gap analysis (Q1:1d),
    relative performance (Q1:1e), historical context (Q1:1f), order flow VWAP
    analysis (Q2:2e), and every sector analyst agent.

    Source: Polygon Stocks Starter ($29/mo)
    Fallback: Alpaca (free), yfinance
    Cadence: Every invocation
    Feasibility: HIGH
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── OHLCV bars at each timeframe ──
    # Using explicit fields rather than dict[Timeframe, OHLCVBar] for frozen
    # dataclass compatibility and LLM readability.
    bar_15min: OHLCVBar  # Most recent completed 15-min bar
    bar_1h: OHLCVBar  # Most recent completed 1-hour bar
    bar_4h: OHLCVBar  # Most recent completed 4-hour bar
    bar_daily: OHLCVBar  # Current or most recent completed daily bar
    bar_weekly: OHLCVBar  # Current or most recent completed weekly bar
    last_trade_price: float = 0.0  # Real-time last trade price. May differ from bar close
    # if the bar is still forming.
    last_trade_time: datetime | None = None  # Timestamp of last trade (UTC)

    # ── Reference levels ──
    reference_levels: ReferenceLevels | None = None

    # ── Trend structure at each timeframe ──
    trend_15min: TimeframeTrend | None = None
    trend_1h: TimeframeTrend | None = None
    trend_4h: TimeframeTrend | None = None
    trend_daily: TimeframeTrend | None = None
    trend_weekly: TimeframeTrend | None = None

    # ── Composite trend score ──
    composite_trend_score: float = 0.0
    # Multi-timeframe composite trend score. Range: -1.0 (all timeframes bearish,
    # aligned) to +1.0 (all timeframes bullish, aligned). Near 0.0 means
    # conflicting signals across timeframes.
    #
    # Weighting: higher timeframes carry more weight (weekly > daily > 4h > 1h > 15min)
    # because the 4-72 hour thesis horizon aligns more with daily/4h trends than
    # with 15min noise. The exact weights are a distillation layer parameter.
    composite_trend_label: str = ""
    # Human-readable composite label for LLM consumption:
    # "strong_uptrend" | "uptrend" | "weak_uptrend" | "neutral" |
    # "weak_downtrend" | "downtrend" | "strong_downtrend"

    # ── Anomalies detected by distillation layer ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class VolumeProfile:
    """Q1:1b — Volume-at-price distribution with value area, POC, and node detection.

    Value area (~70% volume range), point of control, high/low volume nodes,
    developing vs. settled profile comparison. One of the most useful inputs
    for the analyst's entry/exit targeting — POC acts as a price magnet,
    low-volume nodes are "air pockets" where price accelerates.

    Source: Derived from Polygon Stocks Starter 1-min bars
    Fallback: yfinance 1-min bars (60 days only)
    Cadence: Every invocation
    Feasibility: MEDIUM — true profile needs tick data (Developer $79/mo),
                 1-min approximation acceptable for swing trades
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Value area ──
    value_area_high: float  # Upper bound of the price range where ~70% of volume
    # transacted. Price above VAH = breakout territory.
    value_area_low: float  # Lower bound. Price below VAL = breakdown territory.
    value_area_volume_pct: float  # Actual percentage of volume within the VA (target ~70%).
    # Narrow VA = tight consolidation. Wide VA = distributed.

    # ── Point of control ──
    poc_price: float  # The single most-traded price level. Acts as a magnet —
    # price tends to return to POC. The analyst should
    # consider POC as a likely mean-reversion target.
    poc_volume: int  # Volume at the POC price level (shares)

    # ── Volume nodes ──
    high_volume_nodes: list[float] = field(default_factory=list)
    # Price levels with concentrated volume (above 1 std dev from mean volume
    # per level). These are "speed bumps" — price tends to stall and consolidate
    # at these levels. Sorted by volume descending.
    low_volume_nodes: list[float] = field(default_factory=list)
    # Price levels with minimal volume (below 0.5 std dev from mean). These are
    # "air pockets" — price tends to move quickly through them. Important for
    # the analyst: if price breaks through a high-volume node, it may
    # accelerate through the adjacent low-volume node before finding the next
    # high-volume support/resistance.

    # ── Profile classification ──
    profile_type: str = ""  # "single_distribution" — normal bell-shaped, mean-reversion
    # "double_distribution" — two value areas (breakout/breakdown
    #   happened, market hasn't decided which is "right")
    # "b_shaped" — volume concentrated at lows (buying absorption)
    # "p_shaped" — volume concentrated at highs (selling absorption)
    profile_developing: bool = True  # True if the current session's volume profile is still
    # building (intraday). False for settled (prior day/week).
    profile_migration: str = ""  # "higher" | "lower" | "stable" | "expanding"
    # How the current session's value area compares to the prior
    # session's. "higher" = buyers in control, rotating up.

    # ── Period context ──
    profile_period: str = ""  # "session" | "2_day" | "5_day" | "weekly"
    # Which period this profile covers. The session profile is
    # most relevant for intraday entries; multi-day profiles show
    # where the broader balance area sits.
    total_profile_volume: int = 0  # Total shares across all price levels in the profile

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class IndicatorReading:
    """A single indicator value at a specific timeframe. Used as building blocks
    within TechnicalIndicator."""

    timeframe: Timeframe
    value: float
    signal: float | None = None  # Signal line value (for MACD, stochastic, etc.)
    histogram: float | None = None  # Histogram value (MACD histogram)
    upper_band: float | None = None  # Upper band (Bollinger, Keltner)
    lower_band: float | None = None  # Lower band
    middle_band: float | None = None  # Middle band / basis


@dataclass(frozen=True)
class DivergenceFlag:
    """A detected divergence between an indicator and price, or between the same
    indicator at different timeframes. Divergences are among the most actionable
    technical signals for the 4-72 hour horizon."""

    divergence_type: str  # "bullish_regular" | "bearish_regular" |
    # "bullish_hidden" | "bearish_hidden" |
    # "cross_timeframe"
    indicator: str  # Which indicator (e.g., "rsi", "macd_histogram")
    timeframe: Timeframe  # Timeframe where the divergence is observed
    vs_timeframe: Timeframe | None = None  # For cross-timeframe divergences:
    # the conflicting timeframe
    description: str = ""  # Human-readable description for LLM consumption
    # e.g., "RSI bearish divergence on 4h: price making higher
    # highs but RSI making lower highs"
    strength: SignalStrength = SignalStrength.MODERATE
    bars_since_onset: int = 0  # How many bars ago the divergence started forming


@dataclass(frozen=True)
class TechnicalIndicator:
    """Q1:1c — Standard technical indicators computed across multiple timeframes.

    RSI, MACD, stochastic, Bollinger/Keltner bands, MA slopes/crossovers,
    ADX, ATR, and multi-timeframe divergence flags.

    The distillation layer computes all indicators; this entity stores the results.
    The most valuable output isn't any individual indicator — it's the divergence
    flags that surface conflicts between timeframes or between indicator and price.

    Source: Computed from Q1:1a OHLCV (TA-Lib / pandas-ta)
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived, no external source needed
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Momentum oscillators ──
    rsi: list[IndicatorReading] = field(default_factory=list)
    # RSI(14) at each timeframe. Values: 0-100.
    # >70 = overbought, <30 = oversold (classic), but thresholds should be
    # read in context of the trend (RSI can stay >70 for extended periods
    # in strong uptrends). The divergence flags below are more actionable.
    macd: list[IndicatorReading] = field(default_factory=list)
    # MACD(12,26,9) at each timeframe. The histogram (MACD - signal) is the
    # primary signal: histogram turning positive = bullish momentum building,
    # turning negative = bearish momentum building. Signal line crossovers
    # are lagging but confirm the histogram.
    stochastic: list[IndicatorReading] = field(default_factory=list)
    # Stochastic(14,3,3) at each timeframe. Similar to RSI but more sensitive
    # to recent price action. %K (value) and %D (signal). Crossovers in
    # extreme zones (>80 or <20) are the primary signal.

    # ── Mean-reversion bands ──
    bollinger: list[IndicatorReading] = field(default_factory=list)
    # Bollinger Bands(20,2) at each timeframe. Key signals:
    # - Position within bands (percentile: 0 = at lower band, 100 = at upper)
    # - Band width (stored in histogram field): narrow = squeeze (vol compression),
    #   wide = expansion. Squeeze → expansion transitions are high-conviction.
    # - Price outside bands: >2 std dev move, often mean-reverts on the 4h+ horizon.
    keltner: list[IndicatorReading] = field(default_factory=list)
    # Keltner Channels(20, 1.5xATR). When Bollinger bands contract inside
    # Keltner channels, it's a "squeeze" — the TTM Squeeze setup that signals
    # an imminent volatility expansion. Stored for squeeze detection logic.

    # ── Trend-following ──
    ema_20: list[IndicatorReading] = field(default_factory=list)
    # 20-period EMA at each timeframe. Short-term trend.
    ema_50: list[IndicatorReading] = field(default_factory=list)
    # 50-period EMA. Intermediate trend.
    ema_200: list[IndicatorReading] = field(default_factory=list)
    # 200-period EMA. Long-term trend. Price below 200 EMA on the daily is
    # the classic "bear market" signal for institutional positioning.
    adx: list[IndicatorReading] = field(default_factory=list)
    # ADX(14) at each timeframe. Measures trend strength regardless of direction.
    # >25 = trending, <20 = range-bound. Rising ADX confirms directional moves;
    # falling ADX suggests the trend is losing steam.

    # ── Volatility ──
    atr: list[IndicatorReading] = field(default_factory=list)
    # ATR(14) at each timeframe. The universal volatility normalizer — used
    # throughout the system to express moves in "this ticker's normal range"
    # units. A 2 ATR daily move is exceptional for any name.
    atr_expansion_regime: str = ""  # "compressing" | "expanding" | "stable"
    # Whether ATR is trending up (vol expansion) or down
    # (vol compression). Compression → expansion transitions
    # are the highest-conviction volatility signals.
    bollinger_bandwidth: list[IndicatorReading] = field(default_factory=list)
    # Bollinger bandwidth at each timeframe. Low bandwidth = squeeze.

    # ── Cross-timeframe divergences ──
    divergences: list[DivergenceFlag] = field(default_factory=list)
    # The crown jewel of this entity. Divergences between indicator and price
    # (e.g., RSI bearish divergence on 4h — price higher high, RSI lower high)
    # and between the same indicator at different timeframes (e.g., MACD bullish
    # on daily but bearish on 4h). These are the distillation layer's most
    # opinionated output — each divergence is a potential thesis seed for the
    # sector analyst.

    # ── Squeeze detection (TTM Squeeze) ──
    squeeze_active: bool = False  # True when Bollinger bands are inside Keltner channels
    # at the daily timeframe. A squeeze release (Bollinger
    # expanding outside Keltner) combined with MACD histogram
    # direction indicates the breakout direction.
    squeeze_timeframes: list[Timeframe] = field(default_factory=list)
    # Which timeframes currently show an active squeeze. Multiple timeframes
    # in squeeze simultaneously = higher conviction setup.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class GapAnalysis:
    """Q1:1d — Overnight and intraday gap classification and fill probability.

    Gap magnitude (absolute and ATR-relative), classification (full/partial,
    with-trend/counter-trend), historical fill probability. Especially
    important for the pre-open anchored run — overnight gaps represent
    information being priced discontinuously.

    Source: Computed from Q1:1a OHLCV
    Cadence: Every invocation
    Feasibility: HIGH — derived from OHLCV + historical gap-fill stats
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Overnight gap (regular session open vs. prior close) ──
    overnight_gap_abs: float = 0.0  # Absolute dollar gap: today's open - yesterday's close
    overnight_gap_pct: float = 0.0  # Percentage gap: gap / prior close x 100
    overnight_gap_atr: float = 0.0  # Gap expressed in ATR multiples. This is the primary
    # metric for the analyst agents — a 0.5 ATR gap is routine,
    # a 2.0 ATR gap is exceptional for any name.
    gap_direction: Direction = (
        Direction.NEUTRAL
    )  # BULLISH (gap up), BEARISH (gap down), NEUTRAL (< threshold)

    # ── Gap classification ──
    gap_type: str = ""  # "full_gap_up" — open above prior high (true gap)
    # "full_gap_down" — open below prior low (true gap)
    # "partial_gap_up" — open above prior close but within prior range
    # "partial_gap_down" — below prior close but within prior range
    # "no_gap" — open within noise threshold of prior close
    gap_vs_trend: str = ""  # "with_trend" — gap direction aligns with the daily trend
    # "counter_trend" — gap direction opposes the daily trend
    # Counter-trend gaps are more likely to fill and offer
    # mean-reversion setups; with-trend gaps more often extend.

    # ── Fill probability ──
    historical_fill_rate: float = 0.0  # 0.0-1.0. Based on this ticker's historical gap fill
    # behavior for this gap type (full/partial, with/counter trend).
    # Lookback: trailing 1 year of gap events.
    avg_fill_time_hours: float = (
        0.0  # Average time to fill in hours, conditional on fill occurring.
    )
    # Helps the analyst set time-based expectations for
    # gap-fill theses.
    fill_rate_by_size: str = ""  # "small_gaps_fill_more" | "large_gaps_fill_less" | "no_pattern"
    # Whether there's a size-dependent fill pattern for this ticker.

    # ── Intraday gaps ──
    intraday_gaps: list[IntradayGap] = field(default_factory=list)
    # Sudden intraday dislocations on news or large orders. Timestamped for
    # correlation with flow data (Q2) and news events (Qual 1).

    # ── Extended hours gap context ──
    extended_hours_gap: float = 0.0  # Gap from regular close to most recent extended-hours trade.
    # The "live" overnight gap before the regular session opens.
    # Particularly relevant for the pre-open run.
    extended_hours_gap_atr: float = 0.0  # Same, in ATR multiples

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class IntradayGap:
    """A sudden intraday price dislocation — a gap within the session caused by
    news, block orders, or liquidity vacuums."""

    timestamp: datetime  # When the gap occurred (UTC)
    gap_magnitude_pct: float  # Percentage move in the gap
    gap_magnitude_atr: float  # ATR-relative magnitude
    direction: Direction
    likely_cause: str = ""  # "news" | "block_trade" | "liquidity_vacuum" | "unknown"
    # Inferred by correlation with Q2 flow data and Qual 1 news.


@dataclass(frozen=True)
class RelativePerformance:
    """Q1:1e — Relative strength vs. sector ETFs, SPY, and intra-sector ranking.

    RS vs. sector ETF (XLK/XLF/XLE/SMH), RS vs. SPY, intra-sector quartile
    ranking, and RS regime change detection. A stock flat on a day when its
    sector is up 3% is effectively down 3% in relative terms. Relative strength
    divergences often precede absolute moves.

    Source: Computed from Q1:1a OHLCV (ticker vs. benchmarks)
    Cadence: Every invocation
    Feasibility: HIGH — derived by comparing OHLCV across tickers
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── RS vs. sector ETF ──
    sector_etf: Ticker = ""  # The sector ETF benchmark (from SectorClassification.sector_etf)
    rs_vs_sector_1d: float = 0.0  # 1-day relative return: ticker return - sector ETF return.
    # Positive = outperforming sector. In percentage points.
    rs_vs_sector_5d: float = 0.0  # 5-day relative return
    rs_vs_sector_20d: float = 0.0  # 20-day relative return (roughly 1 month)
    rs_ratio_sector: float = 1.0  # Rolling price ratio (ticker / sector ETF). Rising ratio
    # = outperforming. The trend of this ratio is more useful
    # than its level.
    rs_ratio_sector_trend: TrendState = TrendState.RANGE_BOUND
    # Trend of the RS ratio: is relative performance improving, deteriorating, or flat?

    # ── RS vs. broad market (SPY) ──
    rs_vs_spy_1d: float = 0.0  # 1-day relative return vs. SPY
    rs_vs_spy_5d: float = 0.0  # 5-day relative return vs. SPY
    rs_vs_spy_20d: float = 0.0  # 20-day relative return vs. SPY
    rs_ratio_spy: float = 1.0  # Rolling price ratio (ticker / SPY)
    rs_ratio_spy_trend: TrendState = TrendState.RANGE_BOUND

    # ── Intra-sector ranking ──
    sector_rank_today: int = 0  # Rank within sector by today's return (1 = best performer)
    sector_rank_5d: int = 0  # Rank within sector by 5-day return
    sector_peer_count: int = 0  # How many names in the sector (for context: rank 3/15 vs 3/35)
    sector_quartile: int = 2  # 1 = top quartile, 4 = bottom quartile. Quick filter for
    # "is this name leading or lagging its sector?"

    # ── RS regime change detection ──
    rs_regime_change: bool = False  # True when a multi-day RS regime shift is detected:
    # a name transitioning from sector leader to laggard or
    # vice versa. This is a high-signal, low-frequency event
    # that often precedes absolute price moves.
    rs_regime_change_direction: str = ""  # "leader_to_laggard" | "laggard_to_leader"
    rs_regime_change_duration_days: int = 0  # How many days the shift has been in progress.
    # Early detection (2-3 days) is more actionable than
    # confirmed (10+ days) which is already priced.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class HistoricalContext:
    """Q1:1f — Historical regime context: ATR-normalized moves, 52-week positioning.

    ATR-normalized move magnitude, position in 52-week range, distance from
    key MAs (20/50/200 EMA in ATR terms), volatility regime classification.
    Answers the question: "Is this move normal or exceptional for this name?"

    Source: Computed from Q1:1a OHLCV + rolling statistics
    Cadence: Every invocation
    Feasibility: HIGH — derived from OHLCV
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── ATR-normalized move magnitude ──
    daily_range_atr: float = 0.0  # Today's high-low range as a multiple of ATR(14).
    # Normal is ~1.0 ATR. >1.5 ATR = expanded range day.
    # >2.0 ATR = exceptional, likely catalyst-driven.
    daily_move_atr: float = 0.0  # Today's directional move (close vs. prior close) in ATR
    # multiples. Signed: positive = up, negative = down.
    # A 2% move on TSLA might be 0.5 ATR (routine); a 2% move
    # on JPM might be 2.0 ATR (exceptional).
    move_5d_atr: float = 0.0  # 5-day cumulative directional move in ATR multiples
    move_20d_atr: float = 0.0  # 20-day cumulative directional move in ATR multiples

    # ── 52-week positioning ──
    pct_of_52w_range: float = 0.0  # Where current price sits in the 52-week range.
    # 0.0 = at 52-week low, 100.0 = at 52-week high.
    # Names above 90% are "at the highs" (breakout territory
    # or exhaustion risk). Names below 10% are "at the lows"
    # (capitulation or value trap).
    high_52w: float = 0.0  # 52-week high price
    low_52w: float = 0.0  # 52-week low price
    days_since_52w_high: int = 0  # Calendar days since the 52-week high was set
    days_since_52w_low: int = 0  # Calendar days since the 52-week low was set
    near_52w_high: bool = False  # Within 3% of 52-week high (potential breakout)
    near_52w_low: bool = False  # Within 3% of 52-week low (potential capitulation)

    # ── Distance from key moving averages (in ATR terms) ──
    dist_ema_20_atr: float = 0.0  # (Price - EMA20) / ATR. Positive = above, negative = below.
    # Extreme values (> ±2 ATR) suggest mean-reversion potential.
    dist_ema_50_atr: float = 0.0  # Same for 50 EMA. Mean-reversion signal at ±3 ATR.
    dist_ema_200_atr: float = 0.0  # Same for 200 EMA. Extreme distance from 200 EMA is the
    # strongest mean-reversion signal (typically on the daily
    # timeframe, resolves over days to weeks).

    # ── Per-ticker volatility regime ──
    vol_regime: str = ""  # "compression" | "expansion" | "stable"
    # Whether this specific name (not the market) is in a low-vol
    # or high-vol phase. Based on Bollinger bandwidth (Q1:1c) and
    # ATR trend. Distinct from the market-wide VolatilityRegime
    # (Q11:11f) which is a systemic regime.
    bollinger_squeeze: bool = False  # True when Bollinger bands are at historically narrow width
    # for this name (< 20th percentile of trailing 1-year bandwidth).
    # Squeeze → expansion is one of the most reliable trade setups.
    atr_percentile_1y: float = 50.0  # Where current ATR sits vs. its own trailing 1-year range.
    # 0 = lowest vol in a year, 100 = highest vol in a year.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class ExtendedHoursBar:
    """Q1:1g — Pre-market and after-hours OHLCV with volume context.

    Pre-market (4:00-9:30 AM ET), after-hours (4:00-8:00 PM ET) candles,
    extended-hours volume relative to regular session, overnight gap.
    Critical for off-hours and pre-open runs — earnings reactions, overnight
    news, and overseas developments get priced here first.

    All extended-hours data carries a confidence discount (DataConfidence.MEDIUM
    at best) because thinner liquidity and wider spreads mean price signals are
    less reliable than regular-session data.

    Source: Polygon Stocks Starter ($29/mo)
    Fallback: Alpaca
    Cadence: Every invocation
    Feasibility: HIGH — Polygon Stocks Starter provides ext-hours bars
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Pre-market data (4:00-9:30 AM ET) ──
    pre_market_bar: OHLCVBar | None = None  # Aggregated pre-market candle. None if no
    # pre-market activity (rare for universe names, but possible
    # for smaller names on quiet mornings).
    pre_market_change_pct: float = 0.0  # % change from prior regular close to pre-market last trade
    pre_market_change_atr: float = 0.0  # Same, in ATR multiples

    # ── After-hours data (4:00-8:00 PM ET) ──
    after_hours_bar: OHLCVBar | None = None  # Aggregated after-hours candle.
    after_hours_change_pct: float = 0.0  # % change from regular close to after-hours last trade
    after_hours_change_atr: float = 0.0

    # ── Volume context ──
    pre_market_volume_vs_avg: float = 0.0
    # Pre-market volume as a ratio of the average pre-market volume for this
    # ticker (20-day trailing). > 2.0 = unusually active pre-market, suggesting
    # a catalyst or institutional positioning. < 0.5 = quiet, pre-market price
    # moves are less reliable.
    after_hours_volume_vs_avg: float = 0.0
    # Same ratio for after-hours session.
    extended_hours_volume_vs_regular_session: float = 0.0
    # Total extended-hours volume as a percentage of the most recent regular
    # session volume. Normal range: 2-10%. Above 15% is exceptional and usually
    # means an earnings release or major news event occurred.

    # ── Gap from regular close ──
    gap_from_close: float = 0.0  # Dollar gap from regular session close to most recent
    # extended-hours trade. This is the "live" overnight gap.
    gap_from_close_pct: float = 0.0  # Same, as percentage
    gap_from_close_atr: float = 0.0  # Same, in ATR multiples

    # ── Confidence assessment ──
    confidence_discount: float = 1.0  # 0.0-1.0 multiplier on signal reliability. 1.0 = full
    # confidence (high volume, tight spreads). Lower values for
    # thin activity. The distillation layer computes this from
    # volume relative to average and spread width.
    ext_hours_spread_ratio: float = 1.0  # Average ext-hours spread / average regular-hours spread.
    # Values > 3.0 mean the ext-hours market is very thin and
    # price signals should be heavily discounted.

    # ── Historical confirmation pattern ──
    historical_confirmation_rate: float = 0.5
    # Over the trailing 60 days, what percentage of extended-hours moves in
    # this name were confirmed (continued in the same direction) during the
    # first 30 minutes of the next regular session. Range: 0.0-1.0.
    # Names with high confirmation rates (>0.7) produce more reliable
    # extended-hours signals. Names with low rates (<0.3) frequently reverse
    # at the open — a contrarian signal for the pre-open run.

    anomalies: list[AnomalyFlag] = field(default_factory=list)
