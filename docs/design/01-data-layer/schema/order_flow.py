"""Q2: Order Flow and Microstructure — entity definitions.

6 entities covering trade tape analysis, liquidity/depth, institutional flow,
auction mechanics, intraday flow patterns, and extended-hours flow. This domain
captures market microstructure signals that reveal positioning and intent.

Design note on data limitations:
    Order flow is the domain with the widest gap between "what institutional
    desks see" and "what's available at hobby scale." Real-time dark pool
    attribution, true order book depth, and auction imbalance data require
    expensive institutional feeds. The schema defines the TARGET entity —
    the complete picture the system aspires to — while feasibility notes on
    each entity document what's actually available. Fields that depend on
    data sources rated VERY LOW feasibility will be Optional and may be
    consistently None in early implementation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ._common import (
    AnomalyFlag,
    DataConfidence,
    Direction,
    InvocationMetadata,
    MarketSession,
    SignalStrength,
    Ticker,
    Timeframe,
)


# ── Supporting types ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FlowWindow:
    """Aggregated trade flow metrics over a specific time window. The fundamental
    building block for flow analysis — captures "who's in control" over a period."""
    window_label: str                # "1h" | "4h" | "session" | "pre_market" | "after_hours"
    window_start: datetime           # UTC
    window_end: datetime             # UTC

    net_dollar_flow: float = 0.0     # Buy-side minus sell-side dollar volume (Lee-Ready classified).
                                     # Positive = net buying pressure. Negative = net selling.
                                     # This is the primary "who's in control" metric.
    total_dollar_volume: float = 0.0  # Total dollar volume in the window (denominator for intensity)
    flow_intensity: float = 0.0      # |net_dollar_flow| / total_dollar_volume. Range 0.0–1.0.
                                     # High intensity (>0.3) = strong directional conviction.
                                     # Low intensity (<0.1) = balanced, no clear aggressor.

    aggressor_ratio: float = 0.5     # Ratio of trades hitting the ask (buy aggressor) vs. hitting
                                     # the bid (sell aggressor). 0.0 = all sell aggressor, 1.0 = all
                                     # buy aggressor, 0.5 = balanced. Based on Lee-Ready algorithm
                                     # applied to trade-level data.

    volume_shares: int = 0           # Share volume in the window
    trade_count: int = 0             # Number of individual trades


@dataclass(frozen=True)
class TradeSizeBreakdown:
    """Distribution of trade sizes within a window. Shifting mix signals changing
    participant composition — more small prints suggests retail, more large prints
    suggests institutional activity."""
    window_label: str
    small_pct: float = 0.0           # % of volume from small prints (< 100 shares). Higher = more
                                     # retail-like activity. Note: odd-lot analysis is a dated
                                     # heuristic since institutional algos also use small orders.
    medium_pct: float = 0.0          # % of volume from medium prints (100–9,999 shares)
    large_pct: float = 0.0           # % of volume from large prints (≥ 10,000 shares). Higher =
                                     # more institutional. Large prints in the same direction as
                                     # net flow strongly confirms the flow signal.
    avg_trade_size: float = 0.0      # Average trade size in shares
    median_trade_size: float = 0.0   # Median trade size — more robust to outlier blocks
    size_trend: str = ""             # "growing" | "shrinking" | "stable" — is average trade size
                                     # increasing or decreasing within the window?


@dataclass(frozen=True)
class BlockTrade:
    """A single detected block trade — a large print that signals institutional
    activity. Block thresholds: >10,000 shares or >$500,000 notional."""
    timestamp: datetime              # UTC
    shares: int
    price: float
    notional_usd: float              # shares × price
    side: Direction                   # BULLISH (buy-side) | BEARISH (sell-side) | NEUTRAL (uncertain)
    venue: str = ""                  # Exchange or dark pool venue (e.g., "NYSE", "SIGMA_X", "IEX",
                                     # "BATS_DARK"). Empty if venue unknown.
    venue_type: str = ""             # "lit" | "dark" | "unknown"
    is_above_vwap: Optional[bool] = None  # Whether the print was above session VWAP. Institutional
                                     # buying above VWAP signals urgency (willing to pay up).
    condition_codes: list[str] = field(default_factory=list)
        # Exchange condition codes (e.g., "T" for extended hours, "X" for cross).


@dataclass(frozen=True)
class VenueVolume:
    """Volume breakdown by execution venue. Different venues have different
    participant profiles, which helps classify flow."""
    venue_name: str                  # "NYSE" | "NASDAQ" | "SIGMA_X" | "IEX" | "BATS" | etc.
    venue_type: str                  # "lit" | "dark" | "ats"
    volume_shares: int = 0
    volume_pct_of_total: float = 0.0
    likely_participant_type: str = ""  # "institutional" | "retail" | "mixed" | "unknown"


@dataclass(frozen=True)
class TimeOfDayAnomaly:
    """A burst of unusual volume at an atypical time of day."""
    window_start: datetime
    window_end: datetime
    volume_vs_typical: float = 0.0   # Volume in this window vs. typical volume for this time of day
                                     # for this ticker. > 2.0 = anomalous.
    direction: Direction = Direction.NEUTRAL
    likely_cause: str = ""           # "programmatic_execution" | "rebalancing" | "news_driven" |
                                     # "unknown"


# ── Primary entities ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeFlowAggregate:
    """Q2:2a — Aggressor-side trade flow with size distribution and directional signals.

    Net dollar flow (buy vs. sell), trade size distribution, aggressor-side
    balance (Lee-Ready classification), volume-weighted flow direction.
    Critical for distinguishing "price moved because of real directional flow"
    vs. "price drifted on thin volume."

    Source: Alpaca IEX trades (free) + Lee-Ready algorithm + Polygon Stocks Starter bars
    Fallback: Polygon Stocks Developer ($79/mo for full-market ticks)
    Cadence: Every invocation
    Feasibility: MEDIUM — Alpaca provides ~2-3% of volume (IEX only), partial but directional signal

    Consumers:
        - Sector analysts (is the move flow-confirmed?)
        - Portfolio manager (is our position being accumulated into or distributed out of?)
        - Distillation layer (anomaly detection: flow vs. price divergences)
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Flow windows at multiple time horizons ──
    flow_1h: Optional[FlowWindow] = None    # Last 1 hour — most recent directional read
    flow_4h: Optional[FlowWindow] = None    # Last 4 hours — aligns with 4h candle timeframe
    flow_session: Optional[FlowWindow] = None  # Full current/most recent regular session

    # ── Trade size distribution ──
    size_breakdown_1h: Optional[TradeSizeBreakdown] = None
    size_breakdown_session: Optional[TradeSizeBreakdown] = None

    # ── Flow-derived signals ──
    flow_price_alignment: str = ""   # "confirming" | "diverging" | "neutral"
                                     # Whether flow direction aligns with price direction.
                                     # Price up + net buying = confirming (move has legs).
                                     # Price up + net selling = diverging (distribution into strength,
                                     #   move may reverse). This is one of the highest-signal outputs.
    cumulative_flow_trend: str = ""  # "accumulation" | "distribution" | "neutral"
                                     # Multi-day trend in net flow direction. Sustained accumulation
                                     # (net buying over multiple sessions) is the classic institutional
                                     # footprint. Sustained distribution suggests smart money exiting.
    flow_vs_prior_session: float = 0.0  # Net flow today vs. average net flow over prior 5 sessions.
                                     # Large positive = unusually strong buying. Expressed as z-score.

    # ── IEX representativeness (data quality) ──
    iex_volume_pct: float = 0.0      # IEX volume as % of consolidated volume. Typically 2–3%.
                                     # If significantly below normal, the flow signal is less reliable.
    data_coverage_note: str = ""     # Human-readable note on data limitations for this invocation.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class LiquiditySnapshot:
    """Q2:2b — Bid-ask spread and liquidity scoring from NBBO snapshots.

    Average spread trends, spread widening as stress signal, book imbalance
    ratio, composite liquidity score. The Portfolio manager especially needs this —
    a thinning book on a name where the system holds a position is a risk
    signal independent of direction.

    Source: Polygon Stocks Starter (last quote spread)
    Cadence: Every invocation
    Feasibility: LOW-MEDIUM — multi-level depth not available, NBBO spread as proxy
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Spread metrics ──
    current_spread_bps: float = 0.0  # Current bid-ask spread in basis points.
    avg_spread_1h_bps: float = 0.0   # Average spread over the last hour
    avg_spread_session_bps: float = 0.0  # Average spread for the current/most recent session
    avg_spread_20d_bps: float = 0.0  # 20-day trailing average spread — the baseline
    spread_z_score: float = 0.0      # Current spread vs. 20-day average, in standard deviations.
                                     # Positive = wider than normal (stress signal).
                                     # >2.0 = significant widening, flag for Portfolio manager.
    spread_widening_alert: bool = False  # True when spread has widened >50% vs. 20-day average.

    # ── Book imbalance (where available) ──
    book_imbalance_ratio: Optional[float] = None
        # Bid depth / (bid depth + ask depth) at best levels. Range 0.0–1.0.
        # >0.6 = more resting buy orders (bullish lean), <0.4 = more resting sell.
        # None when only NBBO is available.

    # ── Composite liquidity score ──
    liquidity_score: float = 50.0    # Composite score 0–100. Combines spread, available depth,
                                     # and fill probability for the system's typical position sizes.
                                     # Below 30 = Portfolio manager should consider reducing position size.
    liquidity_vs_20d: float = 0.0    # Liquidity score change vs. 20-day average. Negative =
                                     # deteriorating.

    # ── Fill probability estimates ──
    est_fill_prob_1k_shares: float = 1.0  # Estimated probability of filling 1,000 shares within
                                     # 1 cent of NBBO midpoint.
    est_fill_prob_10k_shares: float = 0.9  # Same for 10,000 shares.
    est_slippage_10k_bps: float = 0.0  # Estimated slippage in bps for 10,000-share order.
                                     # Used by execution layer for fill modeling.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class InstitutionalFlow:
    """Q2:2c — Dark pool volume, FINRA short volume, and block trade detection.

    Block trades (>10k shares or >$500k notional), dark pool volume %,
    FINRA short volume reports, venue-level dark pool attribution.
    This is the primary institutional footprint signal.

    Source: FINRA Short Vol (free) + FINRA ATS weekly data (free)
    Fallback: Polygon trade condition codes (Stocks Developer)
    Cadence: Every invocation (daily/weekly delayed data)
    Feasibility: LOW — real-time dark pool attribution deferred, delayed proxies only

    Cross-reference: Block prints here + unusual options activity (Q3:3b)
    on the same name within the same window is a much stronger signal.
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Block trades detected in current/recent session ──
    block_trades: list[BlockTrade] = field(default_factory=list)
        # All detected block trades (>10k shares or >$500k notional).
        # Sorted by timestamp descending (most recent first).
    block_count_session: int = 0
    block_net_direction: Direction = Direction.NEUTRAL
    block_notional_total: float = 0.0  # Total notional value of all blocks in session (USD)

    # ── Dark pool activity ──
    dark_pool_volume_pct: float = 0.0  # Off-exchange volume as % of total. Normal: 35–50%.
    dark_pool_net_direction: Direction = Direction.NEUTRAL
        # Inferred direction of dark pool flow relative to VWAP.
    dark_pool_vs_20d_avg: float = 0.0  # Dark pool volume % today vs. 20-day average.
    venue_breakdown: list[VenueVolume] = field(default_factory=list)
        # Volume breakdown by venue/dark pool where available.

    # ── FINRA short volume ──
    finra_short_volume: int = 0      # Daily short sale volume from FINRA.
    finra_short_volume_ratio: float = 0.0  # Short volume as % of total daily volume.
                                     # Normal: 40–60% (market making). >65% = directional signal.
    finra_short_vol_5d_avg: float = 0.0  # 5-day rolling average for trend detection
    short_volume_classification: str = ""  # "directional" | "mechanical" | "mixed"

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class AuctionData:
    """Q2:2d — MOC/MOO auction imbalance and closing auction mechanics.

    MOC/MOO imbalance (buy vs. sell), imbalance evolution, auction price vs.
    continuous price, closing auction volume concentration. Specifically
    relevant for the pre-open and pre-close anchored runs.

    Source: Not available at hobby scale
    Cadence: Pre-open + pre-close runs
    Feasibility: VERY LOW — requires direct exchange feeds, gap accepted

    Note: Most fields will be None in early implementation.
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Opening auction ──
    moo_imbalance_shares: Optional[int] = None
    moo_imbalance_direction: Direction = Direction.NEUTRAL
    opening_auction_price: Optional[float] = None
    open_vs_continuous_gap: Optional[float] = None  # Auction print vs. last pre-open trade

    # ── Closing auction ──
    moc_imbalance_shares: Optional[int] = None
    moc_imbalance_direction: Direction = Direction.NEUTRAL
    moc_imbalance_evolution: str = ""  # "growing_buy" | "growing_sell" | "stable" | "reversing"
    closing_auction_price: Optional[float] = None
    close_vs_continuous_gap: Optional[float] = None

    # ── Closing auction volume context ──
    closing_auction_volume_pct: Optional[float] = None  # Fraction of daily volume in close auction
    closing_auction_volume_vs_avg: Optional[float] = None  # vs. 20-day average

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class IntradayFlowPattern:
    """Q2:2e — VWAP trajectory, volume distribution, and time-of-day anomalies.

    The temporal structure of trading activity. Unusual concentration of volume
    at atypical times often signals programmatic execution or rebalancing.

    Source: Derived from Q1:1a (1-min bars)
    Cadence: Every invocation
    Feasibility: MEDIUM — VWAP and time-of-day patterns computable from 1-min bars
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── VWAP trajectory ──
    price_vs_vwap: float = 0.0       # Current price minus session VWAP (USD)
    price_vs_vwap_pct: float = 0.0   # Same, as percentage of VWAP
    vwap_trajectory: str = ""        # "persistent_above" | "persistent_below" | "oscillating" |
                                     # "crossing_above" | "crossing_below"
    vwap_deviation_duration_mins: int = 0  # Minutes continuously on current side of VWAP

    # ── Volume distribution (time-of-day) ──
    volume_profile_intraday: str = ""  # "front_loaded" | "back_loaded" | "u_shaped" |
                                     # "unusual_midday" | "normal"
    volume_first_hour_pct: float = 0.0  # % of daily volume in first hour. Normal: 20–30%.
    volume_last_hour_pct: float = 0.0  # % of daily volume in last hour. Normal: 15–25%.
    volume_midday_anomaly: bool = False  # True if midday volume >1.5x typical

    # ── End-of-day positioning flows ──
    eod_flow_direction: Direction = Direction.NEUTRAL
    eod_flow_intensity: float = 0.0  # 0.0–1.0 strength of end-of-day directional flow
    eod_flow_vs_session: str = ""    # "same_direction" | "reversal" | "intensifying" | "fading"

    # ── Time-of-day anomalies ──
    tod_anomalies: list[TimeOfDayAnomaly] = field(default_factory=list)

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class ExtendedHoursFlow:
    """Q2:2f — Extended-hours trade tape and block detection signals.

    The flow-side complement to Q1:1g's extended-hours price data. Distinguishing
    a pre-market gap driven by institutional block flow from one driven by thin
    retail volume is the difference between a high-conviction opening thesis and a trap.

    Source: Polygon Stocks Starter (ext-hours bars)
    Fallback: Alpaca
    Cadence: Every invocation
    Feasibility: LOW — ext-hours bars available, block-level detection needs Developer tick data
    """

    ticker: Ticker
    metadata: InvocationMetadata

    # ── Pre-market flow ──
    pre_market_flow: Optional[FlowWindow] = None
    pre_market_size_breakdown: Optional[TradeSizeBreakdown] = None
    pre_market_blocks: list[BlockTrade] = field(default_factory=list)

    # ── After-hours flow ──
    after_hours_flow: Optional[FlowWindow] = None
    after_hours_size_breakdown: Optional[TradeSizeBreakdown] = None
    after_hours_blocks: list[BlockTrade] = field(default_factory=list)

    # ── Extended-hours dark pool activity ──
    ext_hours_dark_pool_pct: Optional[float] = None

    # ── Regular-session confirmation ──
    historical_confirmation_rate: float = 0.5
        # What % of pre-market flow direction was confirmed at regular open (60-day trailing).
    prior_session_confirmed: Optional[bool] = None

    # ── Signal quality ──
    flow_confidence: DataConfidence = DataConfidence.LOW

    anomalies: list[AnomalyFlag] = field(default_factory=list)
