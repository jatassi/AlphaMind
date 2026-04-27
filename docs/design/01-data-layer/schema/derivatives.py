"""Q3: Derivatives and Options — entity definitions.

8 entities covering implied volatility structure, unusual activity detection,
open interest landscape, dealer exposure, earnings-implied moves, put/call
dynamics, cross-ticker signals, and sector ETF options flow. This domain is
powered primarily by Polygon Options Starter ($29/mo).

Design note on options data:
    Options markets are forward-looking and require conviction to trade. Positioning
    data (OI, IV, Greeks) reveals what sophisticated traders expect to happen within
    the system's 4-72 hour horizon. Unusual activity detection (sweeps, blocks) is
    the institutional footprint detector — high-signal because someone is spending
    real capital on time-bound directional bets.

Design note on Greeks computation:
    All Greeks (gamma, delta, vanna, charm) are derived from Polygon's per-contract
    greeks and OI data. No external Greeks feed is needed; they're computed by
    aggregating at the ticker level.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ._common import (
    AnomalyFlag,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
)

__all__ = [
    "COTPositioning",
    "CrossTickerOptionsSignal",
    "CrossTickerPairSignal",
    "DealerExposure",
    "EarningsImpliedMove",
    "EarningsImpliedMoveData",
    "ExpirationCluster",
    "GreekExposure",
    "IVTerm",
    "ImpliedVolSurface",
    "OpenInterestLandscape",
    "PCFlowBreakdown",
    "PutCallDynamics",
    "SectorETFFlowSnapshot",
    "SectorETFOptionsFlow",
    "StrikeOI",
    "StrikeOIDistribution",
    "SweepDetection",
    "UnusualBlock",
    "UnusualOptionsActivity",
    "VolumeOIAnomaly",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IVTerm:
    """Implied volatility at a specific expiration. IV term structure (near vs.
    far, contango/backwardation) reveals market expectations for move magnitude
    across time."""

    days_to_expiration: int  # Days until this expiration. 1-365.
    iv_level: float  # IV (annualized, %). E.g., 35.5 means 35.5% IV
    call_iv: float  # IV on ATM calls for this expiration
    put_iv: float  # IV on ATM puts for this expiration
    iv_skew: float  # put_iv - call_iv. Positive = put premium,
    # bearish skew. Negative = call premium, bullish skew.


@dataclass(frozen=True)
class StrikeOI:
    """Open Interest concentrated at a specific strike. Used to identify put walls,
    call magnets, and max pain levels."""

    strike_price: float  # Strike level (USD)
    call_oi: int  # Open interest on call side (contracts)
    put_oi: int  # Open interest on put side (contracts)
    total_oi: int = 0  # call_oi + put_oi
    put_call_oi_ratio: float = 0.0  # put_oi / call_oi. >1.0 = put-heavy, <1.0 = call-heavy
    oi_rank_pct: float = 0.0  # This strike's OI as % of max OI across all strikes
    # at this expiration. Top concentration identified by
    # the distillation layer for max pain detection.


@dataclass(frozen=True)
class ExpirationCluster:
    """Open Interest clustering analysis at a specific expiration. Reveals where
    positions are concentrated in time, affecting roll behavior and gamma risk."""

    days_to_expiration: int  # Days until this expiration
    total_call_oi: int  # Sum of all call OI at this expiration
    total_put_oi: int  # Sum of all put OI at this expiration
    total_oi: int = 0  # call + put OI
    put_call_oi_ratio: float = 0.0  # put_oi / call_oi at this expiration
    oi_pct_of_total: float = 0.0  # This expiration's OI as % of all expirations


@dataclass(frozen=True)
class UnusualBlock:
    """A single block trade (large institutional print) detected in options. Alert
    when volume spikes on low-OI strikes suggest new positioning."""

    timestamp: datetime  # UTC timestamp of the block
    strike_price: float  # Strike of the block trade
    expiration_date: str  # ISO format: "YYYY-MM-DD"
    contract_type: str  # "call" | "put"
    contracts: int  # Number of contracts
    notional_usd: float  # Notional value (contracts x price per contract x 100)
    side: Direction  # BULLISH (buy) | BEARISH (sell)
    volume_vs_oi_ratio: float = 0.0  # Today's volume on this strike / existing OI.
    # > 0.5 suggests new positioning (not closing).


@dataclass(frozen=True)
class VolumeOIAnomaly:
    """Detected spike in options volume relative to open interest at a specific
    strike/expiration. Distinguishes new positioning from rolling/closing."""

    strike_price: float  # Strike level
    expiration_date: str  # ISO format: "YYYY-MM-DD"
    contract_type: str  # "call" | "put"
    today_volume: int  # Contracts traded today at this strike/exp
    trailing_avg_volume: float  # 5-day average volume at this strike
    volume_spike_multiple: float  # today_volume / trailing_avg_volume. > 3.0 = anomaly
    current_oi: int  # Open interest at this strike (snapshot)
    volume_to_oi_ratio: float = 0.0  # today_volume / current_oi. > 0.5 = new positioning


@dataclass(frozen=True)
class SweepDetection:
    """Detected sweep order — institutional urgency signal. Sweeps hit multiple
    exchanges in rapid succession, suggesting a trader willing to accept market
    price to fill a large size immediately."""

    timestamp: datetime  # UTC when the sweep was detected
    strike_price: float  # Strike being swept
    expiration_date: str  # ISO format: "YYYY-MM-DD"
    contract_type: str  # "call" | "put"
    side: Direction  # BULLISH (buying) | BEARISH (selling)
    contracts: int  # Total contracts across all legs of sweep
    notional_usd: float  # Total notional value
    venues_count: int = 0  # How many venues the order hit (>1 suggests sweep)
    price_paid_vs_ask: float = 0.0  # Average price paid vs. ask at time of sweep.
    # Positive = paid up (aggressive buyer).


@dataclass(frozen=True)
class COTPositioning:
    """Equity index futures positioning from CFTC Commitments of Traders report.
    Tracks asset manager and leveraged fund conviction on broad indices."""

    index_name: str  # "SP500" | "NQ100" | etc. (derived from futures ticker)
    report_date: str  # ISO format: "YYYY-MM-DD" — CFTC reports weekly
    asset_manager_net_contracts: int  # Asset manager net position (contracts)
    asset_manager_pct_of_total: float  # As % of total open interest
    leveraged_fund_net_contracts: int  # Leveraged fund/hedge fund net position
    leveraged_fund_pct_of_total: float  # As % of total
    positioning_extreme_flag: str  # "very_bullish" | "bullish" | "neutral" | "bearish" |
    # "very_bearish" — assessed against historical extremes


@dataclass(frozen=True)
class GreekExposure:
    """Aggregated gamma/delta/vanna/charm exposure at the ticker level. Determines
    the CHARACTER of the next move — whether dealer hedging will amplify or dampen."""

    timestamp: datetime  # UTC timestamp of calculation
    expiration_date: str | None = None  # If specific expiration; None = all expirations aggregated

    # Gamma Exposure (GEX)
    net_gamma_usd: float = 0.0  # Total dealer gamma in USD. Computed as:
    # Σ(OI x gamma x 100 x spot²) across all contracts.
    # Positive = long gamma (dealer benefits from moves, dampens volatility).
    # Negative = short gamma (dealer loses on moves, amplifies volatility).
    gamma_flip_level_up: float | None = None  # Price level where gamma flips from
    # negative to positive (upside). If price breaks above,
    # dealer gamma turns long, dampening further upside.
    gamma_flip_level_down: float | None = None  # Price level where gamma flips from
    # positive to negative (downside). Dealer gamma turns short,
    # amplifying further downside moves.

    # Delta Exposure (DEX)
    net_delta_contracts: float = 0.0  # Net dealer delta position (positive = short delta,
    # dealer hedges by selling spot). Indicates directional
    # hedging pressure. Large negative delta = dealer needs
    # to buy to hedge (bullish).
    dex_direction: Direction = Direction.NEUTRAL  # BULLISH (dealer short delta, hedges by buying) |
    # BEARISH (dealer long delta, hedges by selling)

    # Vanna and Charm
    net_vanna_notional: float = 0.0  # Net vanna (notional USD). Vanna = ∂delta/∂volatility.
    # Reveals how dealer delta hedging changes with IV moves.
    net_charm_notional: float = 0.0  # Net charm = ∂gamma/∂time (or delta decay). Indicates
    # how dealer gamma/delta positioning changes as time passes.


@dataclass(frozen=True)
class StrikeOIDistribution:
    """Snapshot of OI distribution across strikes at a specific expiration. Identifies
    walls (concentrated put OI at low strikes) and magnets (concentrated call OI at
    high strikes)."""

    expiration_date: str  # ISO format: "YYYY-MM-DD"
    total_call_oi: int
    total_put_oi: int
    call_oi_skew: float  # Concentration measure: std dev of call OI across
    # strikes / mean. High = concentrated.
    put_oi_skew: float  # Same for puts
    max_pain_level: float = 0.0  # Strike where max OI x gamma decay would cause most
    # options to expire worthless (theoretical pain point).
    put_wall_identified: bool = False  # True if a significant wall (>25% of total put OI)
    # detected at a single low strike
    call_magnet_identified: bool = False  # True if significant call concentration at a high strike


@dataclass(frozen=True)
class EarningsImpliedMoveData:
    """Pricing of ATM straddle spanning an earnings event. Reveals market's
    conviction about the move magnitude."""

    earnings_date: str  # ISO format: "YYYY-MM-DD"
    days_to_earnings: int  # Days until earnings (on invocation date)
    atm_strike: float  # At-the-money strike used for straddle
    atm_call_price: float  # Call price (USD per share)
    atm_put_price: float  # Put price (USD per share)
    atm_straddle_price: float = 0.0  # atm_call_price + atm_put_price
    implied_move_pct: float = 0.0  # Implied move as % of stock price. Derived from
    # ATM straddle price ÷ stock price. E.g., 5.2% move implied.
    implied_move_abs_usd: float = 0.0  # Implied move in absolute dollars
    iv_level_atm: float = 0.0  # IV at ATM strike (expiration spanning earnings)
    iv_ramp_rate: float = 0.0  # IV change per day leading into earnings. Positive =
    # IV expanding (uncertainty rising). Measures how quickly
    # the market is pricing in event risk.


@dataclass(frozen=True)
class PCFlowBreakdown:
    """Put/call flow directionality classification. Same ratio reads very differently
    depending on whether puts are bought (bearish) or sold (income/bullish)."""

    expiration_date: str | None = None  # If specific expiration; None = all expirations
    put_volume_total: int = 0  # Total put contracts traded
    call_volume_total: int = 0  # Total call contracts traded
    put_call_volume_ratio: float = 0.0  # put_volume_total / call_volume_total

    # Flow directionality — distinguishes hedging from speculation
    put_buy_to_open_pct: float = 0.0  # % of put volume that is buy-to-open (bearish conviction)
    put_sell_to_open_pct: float = 0.0  # % of put volume that is sell-to-open (income, bullish bias)
    call_buy_to_open_pct: float = 0.0  # % of call volume that is buy-to-open (bullish)
    call_sell_to_open_pct: float = 0.0  # % of call volume that is sell-to-open (bearish)

    # Protective vs. speculative classification
    protective_vs_speculative_flag: str = (
        "balanced"  # "protective" = put buying dominates (hedging) |
    )
    # "speculative" = put selling / call buying dominates |
    # "balanced" = mixed signals

    # Index vs. single-stock skew context
    is_index_heavy: bool = False  # True if this represents index-level data (SPY/QQQ)
    is_single_stock: bool = True  # True if single-name


@dataclass(frozen=True)
class CrossTickerPairSignal:
    """A detected pair trade signature — simultaneous bullish flow on one name
    and bearish flow on a correlated peer. E.g., NVDA calls + AMD puts."""

    detected_at: datetime
    long_ticker: Ticker  # Name with bullish flow (calls bought)
    short_ticker: Ticker  # Peer with bearish flow (puts bought)
    correlation: float  # Historical correlation between the two names
    long_call_volume_surge: int  # Call volume on long_ticker as multiple of avg
    short_put_volume_surge: int  # Put volume on short_ticker as multiple of avg
    pair_signal_strength: SignalStrength  # STRONG | MODERATE | WEAK | NONE
    relative_value_hypothesis: str = ""  # Free-text hypothesis: "long_ticker_outperformer" |
    # "sector_rotation" | "competitor_divergence" | etc.


@dataclass(frozen=True)
class SectorETFFlowSnapshot:
    """Options flow data for a single sector ETF. Institutions express sector views
    through ETF options — large ETF put buying is a different signal than aggregating
    individual names."""

    etf_ticker: str  # "XLK" | "XLF" | "XLE" | "SMH" | "QQQ"
    timestamp: datetime  # UTC

    # Unusual activity detection (same as single-stock, but at ETF level)
    unusual_block_detected: bool = False  # Recent large block trade on this ETF's options
    sweep_activity_detected: bool = False  # Sweep detection on this ETF
    volume_oi_spike_detected: bool = False  # Volume spike relative to OI

    # Put/call flow character
    put_call_volume_ratio: float = 0.0  # ETF put volume / call volume
    etf_put_buy_to_open_pct: float = 0.0  # % of put volume that is buy-to-open
    etf_put_sell_to_open_pct: float = 0.0  # % of put volume that is sell-to-open

    # IV comparison (sector ETF vs. component stocks)
    etf_iv_level: float = 0.0  # ATM IV on the ETF
    component_iv_avg: float = 0.0  # Average IV on the top 10 component stocks
    iv_divergence_pct: float = 0.0  # (etf_iv_level - component_iv_avg) / component_iv_avg x 100
    # Negative = ETF IV lags component (opportunity if sector
    # view hasn't propagated to individual names yet).

    # Sentiment interpretation
    etf_options_sentiment: Direction = Direction.NEUTRAL  # BULLISH (call buying dominant) |
    # BEARISH (put buying dominant) | NEUTRAL

    # Hedging vs. conviction distinction
    hedging_indicator: bool = False  # True if this looks like index hedging (SPY/QQQ puts)
    # vs. sector conviction (XLF puts = financials-specific concern)


# ── Primary entities ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ImpliedVolSurface:
    """Q3:3a — IV term structure, skew, and rank/percentile metrics.

    Term structure (near vs. far IV, contango/backwardation), put/call skew,
    IV vs. realized vol, IV rank and IV percentile. Feeds sector analysts for
    contextualizing whether a price move is "expected" or anomalous, and feeds
    the analyst for assessing whether a setup is cheap or expensive relative to
    expected movement.

    Source: Polygon Options Starter ($29/mo)
    Fallback: yfinance EOD chains + local Black-Scholes model
    Cadence: Every invocation
    Feasibility: HIGH — Options Starter provides greeks + IV per contract, surface derivable

    Consumers:
        - Sector analysts (is this move expected?)
        - Analyst (is this cheap or expensive vol?)
        - Distillation layer (anomaly detection: IV vs. realized vol)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Term structure ──
    term_structure: list[IVTerm] = field(default_factory=list)
    # IV levels at key expirations (typically 7d, 14d, 30d, 60d, 90d, 180d).
    # Ordered by days_to_expiration ascending. Used to detect contango
    # (normal: near < far) vs. backwardation (crisis: near > far).

    # ── Contango/backwardation classification ──
    term_structure_type: str = "normal"  # "contango" = normal (near < far) |
    # "backwardation" = crisis/event (near > far) |
    # "flat" = uncertain environment

    # ── Skew metrics ──
    put_call_iv_skew_atm: float = 0.0  # IV(ATM put) - IV(ATM call) at nearest expiration.
    # Positive = put premium (fear, bearish skew).
    # Negative = call premium (greed, bullish skew).
    skew_direction: Direction = Direction.NEUTRAL  # BEARISH (put premium) |
    # BULLISH (call premium) | NEUTRAL

    # ── IV vs. Realized Vol ──
    iv_level_atm: float = 0.0  # Current ATM IV (annualized, %)
    realized_vol_20d: float = 0.0  # 20-day realized volatility of price moves
    iv_vs_realized_ratio: float = 0.0  # iv_level_atm / realized_vol_20d. > 1.0 = market
    # over-pricing move magnitude. < 1.0 = under-pricing.

    # ── IV Rank and Percentile ──
    iv_rank: float = 0.0  # IV's rank relative to its 252-day high/low range.
    # 0.0 = at 252d low, 1.0 = at 252d high. 0.5 = middle.
    # Used to assess whether vol is "cheap" or "expensive"
    # in historical context.
    iv_percentile: float = 0.0  # Percentile rank vs. trailing 252 days.
    # 90th percentile = IV historically elevated.
    iv_trending: Direction = Direction.NEUTRAL  # BULLISH (IV declining, contracting) |
    # BEARISH (IV expanding) | NEUTRAL (stable)

    # ── Anomalies detected by distillation layer ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class UnusualOptionsActivity:
    """Q3:3b — Sweep/block detection and volume-vs-OI spike identification.

    Large block trades, sweep detection (urgency signal), volume vs. OI spikes,
    new positioning vs. closing/rolling distinction. This is probably the single
    highest-signal subcategory for the system — large, deliberate positioning on
    time-bound directional bets.

    Source: Pseudo-sweep detector from Polygon Options Starter snapshots (poll every 60s)
    Fallback: Polygon Options Developer ($79/mo for true sweep detection)
    Cadence: Every invocation
    Feasibility: MEDIUM — ~65% accuracy via pseudo-sweep detector, true detection needs
        Developer tier

    Consumers:
        - Analyst (institutional footprint, immediate signal)
        - Sector analysts (what are institutions positioning for?)
        - Distillation layer (anomaly flagging)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Block detection ──
    large_blocks_detected: bool = False  # Whether any block trades (>10k shares or >$500k notional)
    # detected in the last session/polling window
    block_trades: list[UnusualBlock] = field(default_factory=list)
    # List of detected blocks, most recent first. Each block is a high-signal
    # institutional print. When a block is accompanied by low OI on the strike,
    # it indicates new positioning rather than closing.

    largest_block_size: int = 0  # Contracts in the largest block
    largest_block_notional_usd: float = 0.0  # Notional of the largest block
    block_count_session: int = 0  # Number of blocks detected this session

    # ── Sweep detection ──
    sweeps_detected: bool = False  # Whether any sweeps (multi-venue orders hitting rapidly)
    # were detected. Sweeps indicate urgency — trader willing
    # to hit the ask across multiple venues to fill immediately.
    sweep_list: list[SweepDetection] = field(default_factory=list)
    # Detected sweeps, most recent first. Key insight: a sweep on a low-OI
    # strike strongly suggests NEW positioning vs. closing.

    sweep_count_session: int = 0

    # ── Volume vs. OI anomalies ──
    volume_oi_spike_detected: bool = False  # Whether volume spikes on low-OI strikes
    # suggest new positioning (as opposed to roll/close)
    volume_oi_anomalies: list[VolumeOIAnomaly] = field(default_factory=list)
    # Specific strikes where volume >> typical, and volume >> existing OI.
    # High volume_to_oi_ratio (>0.5) suggests new contracts being opened,
    # not old positions closing.

    # ── Positioning type classification ──
    dominant_activity_type: str = "balanced"  # "new_opening" = mostly buy-to-open positioning |
    # "closing" = mostly sell-to-close, rolling |
    # "balanced" = mixed

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class OpenInterestLandscape:
    """Q3:3c — Strike-level OI concentration, max pain, and positioning data.

    Strike OI concentration (put walls, call magnets, max pain), OI day-over-day
    changes, expiration clustering, put/call OI ratio, COT positioning.
    Reveals the aggregate battlefield map — where positions are concentrated and
    how they're shifting.

    Source: Polygon Options Starter ($29/mo) + CFTC (free)
    Fallback: yfinance (unreliable)
    Cadence: Every invocation
    Feasibility: HIGH — Options Starter provides daily OI, all metrics derivable

    Consumers:
        - Analyst (max pain = likely reversion target)
        - Portfolio manager (current positioning, risk assessment)
        - Sector analysts (where is the market positioned?)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Strike-level OI distribution (multiple expirations) ──
    strike_oi_distributions: list[StrikeOIDistribution] = field(default_factory=list)
    # OI breakdown across strikes at each major expiration (7d, 14d, 30d, etc.).
    # Each includes max_pain calculation, wall/magnet detection.

    # ── Max pain levels ──
    max_pain_level_30d: float | None = None  # For 30-day options, the strike where
    # gamma decay causes most expirations worthless.
    # Options traders often gravitate toward max pain.
    max_pain_level_60d: float | None = None
    max_pain_distance_pct: float = 0.0  # Current price vs. nearest max pain level, as %.
    # < 2% = price near max pain (likely target).

    # ── Put walls and call magnets ──
    significant_put_wall_identified: bool = False  # True if >25% of total put OI at a
    # single low strike (bears concentrated here)
    put_wall_strike: float | None = None
    put_wall_oi_count: int = 0

    significant_call_magnet_identified: bool = False  # True if concentrated call OI
    # at a high strike (bulls targeting this level)
    call_magnet_strike: float | None = None
    call_magnet_oi_count: int = 0

    # ── Expiration clustering ──
    expiration_clusters: list[ExpirationCluster] = field(default_factory=list)
    # OI distribution across time. Shows where the bulk of positioning is.
    # Concentrated in near-term = event-driven. Spread across expirations =
    # longer-dated conviction.

    # ── OI changes (day-over-day) ──
    oi_change_pct_session: float = 0.0  # Total OI today vs. yesterday, as %. Positive =
    # new positioning building. Negative = unwinding.
    oi_change_direction: Direction = Direction.NEUTRAL
    bullish_oi_buildup: bool = False  # True if call OI building + put OI declining
    bearish_oi_buildup: bool = False  # True if put OI building + call OI declining

    # ── Put/Call OI ratio ──
    total_put_oi: int = 0
    total_call_oi: int = 0
    put_call_oi_ratio: float = 0.0  # put_oi / call_oi. > 1.0 = put-heavy (bearish bias).

    # ── COT positioning (equity index futures) ──
    cot_positioning: list[COTPositioning] = field(default_factory=list)
    # CFTC data for major indices (S&P 500, Nasdaq 100, etc.). Shows asset manager
    # and hedge fund conviction. Extreme positioning (very bullish or very bearish)
    # is often contrarian.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class DealerExposure:
    """Q3:3d — Gamma (GEX), delta (DEX), vanna, and charm exposure estimates.

    Net dealer gamma positioning, GEX flip levels, net directional delta hedging,
    vanna and charm flows. GEX determines the CHARACTER of the next move —
    whether dealer hedging will amplify or dampen volatility. DEX reveals
    directional hedging pressure.

    Source: Derived from Polygon Options Starter (OI + greeks)
    Cadence: Every invocation
    Feasibility: HIGH — OI + greeks per contract available, all computable locally

    Consumers:
        - Portfolio manager (dealer hedging = potential intra-day move amplification)
        - Analyst (GEX flips = levels where volatility character changes)
        - Risk layer (dealer gamma = systematic volatility risk)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Aggregated exposure (all expirations) ──
    aggregate_exposure: GreekExposure = field(
        default_factory=lambda: GreekExposure(timestamp=datetime.now(tz=UTC))
    )

    # ── Exposure by expiration (optional: for multi-day tracking) ──
    exposure_by_expiration: list[GreekExposure] = field(default_factory=list)
    # Breakdown of gamma/delta exposure at each major expiration. Shows if
    # dealer risk is concentrated in near-term or spread.

    # ── GEX flip levels (key technical levels from dealer hedging) ──
    # These levels are where dealer gamma changes sign. When price approaches
    # and breaks through a flip, dealer behavior changes character.
    gex_flip_levels: list[float] = field(default_factory=list)
    # Sorted ascending. Represents levels where dealer gamma flips from
    # positive (dampening, long gamma) to negative (amplifying, short gamma)
    # or vice versa. Price behavior often accelerates through flip levels.

    # ── Dealer delta trend ──
    dex_trend: Direction = Direction.NEUTRAL  # BULLISH (dealer increasingly short delta,
    # needs to buy spot to hedge) |
    # BEARISH (dealer increasingly long delta,
    # hedges by selling) | NEUTRAL

    # ── Vanna and charm interpretation ──
    vanna_direction: Direction = Direction.NEUTRAL  # How vanna will interact with vol moves.
    # If IV rises and vanna is bullish, dealer delta
    # hedging will turn buying (bullish).
    charm_direction: Direction = Direction.NEUTRAL  # How charm (time decay) affects dealer
    # positioning. Positive charm = dealer gamma
    # increases with time decay (favorable).

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class EarningsImpliedMove:
    """Q3:3e — Implied earnings move from ATM straddle pricing.

    Implied move magnitude, historical comparison, straddle richness assessment,
    pre-earnings IV ramp analysis. Earnings are the single biggest catalyst for
    individual names on the 4-72 hour horizon, making implied move critical for
    thesis framing.

    Source: Derived from Polygon Options Starter (ATM straddle pricing)
    Fallback: Market Chameleon (free limited data)
    Cadence: Earnings season (when within 30 days)
    Feasibility: HIGH — ATM straddle pricing directly available from chain snapshots

    Consumers:
        - Analyst (is earnings move rich or cheap relative to historical?)
        - Sector analysts (positioning for event)
        - Portfolio manager (risk assessment around earnings dates)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Current earnings event (if applicable) ──
    has_upcoming_earnings: bool = False  # True if next earnings within 30 days
    upcoming_earnings: EarningsImpliedMoveData | None = None
    # Straddle pricing and implied move for next earnings. None if no earnings
    # scheduled within 30 days or if options chain doesn't span the date.

    # ── Historical comparison ──
    earnings_history: list[str] = field(default_factory=list)
    # Formatted strings with prior earnings moves: "Implied: 4.2% | Actual: +5.1% | Beat"
    # (most recent first). Shows historical move magnitudes for this ticker.
    historical_realized_move_avg: float = 0.0  # Average realized move over last N earnings
    historical_implied_move_avg: float = 0.0  # Average implied move over last N earnings

    # ── Straddle richness ──
    current_implied_vs_historical_ratio: float = 0.0  # Next earnings implied move / average
    # historical realized move. > 1.0 = straddle expensive
    # (market pricing bigger move than usual). < 1.0 = cheap.
    straddle_richness_assessment: str = "fair"  # "rich" = expensive | "fair" = normal |
    # "cheap" = underpriced

    # ── IV ramp analysis ──
    iv_ramp_rate_per_day: float = 0.0  # Change in ATM IV per day approaching earnings.
    # Positive = IV expanding as event nears (normal).
    # Rapid ramp = high uncertainty pricing.
    iv_ramp_acceleration: float = 0.0  # Second derivative: is the ramp accelerating or
    # decelerating? Positive = IV ramp steepening (fear building).

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class PutCallDynamics:
    """Q3:3f — Put/call volume ratios and flow directionality classification.

    P/C volume ratio (per-ticker, per-sector, index-level), buy-to-open vs.
    sell-to-open flow, protective vs. speculative classification, index vs.
    single-stock skew. The same P/C ratio reads very differently depending on
    whether puts are being bought (bearish conviction) or sold (income, bullish).

    Source: Polygon Options Starter ($29/mo)
    Fallback: yfinance (unreliable)
    Cadence: Every invocation
    Feasibility: MEDIUM — P/C ratio from snapshots, directional classification limited
        without trades

    Consumers:
        - Analyst (what's the flow signal?)
        - Sector analysts (institutional positioning)
        - Risk layer (sector put buying = hedging vs. speculation)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Overall flow breakdown (all expirations aggregated) ──
    overall_flow: PCFlowBreakdown = field(
        default_factory=lambda: PCFlowBreakdown(expiration_date=None)
    )

    # ── Flow by expiration ──
    flow_by_expiration: list[PCFlowBreakdown] = field(default_factory=list)
    # Separate P/C breakdown for each major expiration (7d, 14d, 30d, etc.).
    # Near-term P/C ratio can differ significantly from longer-dated.

    # ── Classification summary ──
    flow_sentiment: Direction = Direction.NEUTRAL
    # BULLISH: call buying or put selling dominant.
    # BEARISH: put buying or call selling.
    # NEUTRAL: balanced.

    # ── Index vs. single-stock context ──
    index_put_call_ratio: float | None = None  # P/C ratio for SPY or QQQ, for reference.
    # When single-stock P/C >> index P/C, it suggests
    # idiosyncratic concern (not macro hedging).
    index_vs_stock_divergence: str = "aligned"  # "aligned" = single-stock and index P/C
    # moving together | "diverging" = single-stock puts
    # spiking without index support = idiosyncratic risk

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CrossTickerOptionsSignal:
    """Q3:3g — Cross-ticker correlated options activity and sector-wide patterns.

    Pair trade signatures, sector-wide sweeps, cross-sector hedging patterns,
    ETF vs. single-stock divergence. Lower frequency but high signal-to-noise
    when it appears — simultaneous positioning across multiple names reveals
    relative value theses or correlation arbitrage.

    Source: Derived from Q3:3a-3f across universe
    Cadence: Every invocation
    Feasibility: MEDIUM — cross-ticker OI/IV anomaly scanning from chain snapshots

    Consumers:
        - Analyst (pair trade setup)
        - Sector analysts (relative value and rotation signals)
        - Risk layer (correlation structure monitoring)
    """

    # ── Identity ──
    ticker: Ticker  # Primary ticker (the one being reported on)
    metadata: InvocationMetadata

    # ── Pair trade signatures (detected correlations) ──
    pair_signals: list[CrossTickerPairSignal] = field(default_factory=list)
    # Detected pair trades: long_ticker calls + short_ticker puts, or vice versa.
    # Example: NVDA call surge + AMD put surge = NVDA outperformance bet.
    # Sorted by signal strength descending.

    # ── Sector-wide sweep activity ──
    sector_sweep_activity_detected: bool = False  # True if multiple names in the same
    # sector show sweep activity within the same window
    # (suggests coordinated directional view)
    sector_sweep_names: list[Ticker] = field(default_factory=list)
    # Other names in this ticker's sector showing sweeps

    # ── Cross-sector hedging patterns ──
    cross_sector_hedge_detected: bool = False  # True if unusual options activity on
    # this ticker is paired with opposite activity
    # in a different sector (e.g., long energy calls +
    # long financials puts as inflation trade)
    hedge_pair_ticker: Ticker | None = None
    hedge_pair_sector: str | None = None
    hedge_hypothesis: str = ""  # Description of the hypothesized multi-sector trade

    # ── ETF vs. single-stock divergence ──
    etf_divergence_detected: bool = False  # True if this ticker's options flow diverges
    # from its sector ETF (e.g., XLK put buying but
    # individual tech names showing call buying)
    related_etf_ticker: str | None = None
    divergence_direction: str = (
        ""  # "etf_bearish_stock_bullish" | "etf_bullish_stock_bearish" | etc.
    )
    divergence_interpretation: str = (
        ""  # "stock_leading_sector" | "etf_hedging_not_propagated" | etc.
    )

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class SectorETFOptionsFlow:
    """Q3:3h — Sector ETF unusual options activity and flow character.

    ETF unusual activity (XLK/XLF/XLE/SMH/QQQ), ETF P/C flow, ETF IV vs.
    single-name IV composite, index hedging vs. sector conviction. Institutions
    frequently express sector-level views through ETF options rather than
    single-name positions, making this a distinct and valuable signal.

    Source: Polygon Options Starter — applied to sector ETFs
    Cadence: Every invocation
    Feasibility: MEDIUM — same Options Starter coverage applied to ETFs

    Consumers:
        - Sector analysts (sector-level sentiment barometer)
        - Analyst (sector rotation signals)
        - Risk layer (sector hedging vs. conviction distinction)
    """

    # ── Identity (sector context) ──
    sector: str  # "tech" | "semis" | "financials" | "energy"
    metadata: InvocationMetadata

    # ── ETF options activity snapshots (multiple ETFs per sector) ──
    etf_snapshots: list[SectorETFFlowSnapshot] = field(default_factory=list)
    # Unusual activity and flow breakdown for each major sector ETF
    # (XLK for tech, SMH for semis, XLF for financials, XLE for energy, QQQ as broad).
    # Sector analysts read this as their sector-level sentiment signal.

    # ── Sector-level composite signals ──
    sector_put_call_ratio: float = 0.0  # Aggregate P/C ratio across all sector ETFs.
    # Used to assess overall sector directional view.
    sector_sentiment: Direction = Direction.NEUTRAL  # BULLISH | BEARISH | NEUTRAL

    # ── ETF-level IV context ──
    primary_etf_ticker: str = ""  # Main sector ETF (XLK for tech, etc.)
    etf_iv_vs_components_delta: float = 0.0  # ETF IV - average component IV (%).
    # Negative = ETF IV lags (opportunity for ETF vol sellers).
    # Positive = ETF IV leads (unusual, may signal incoming
    # component volatility).

    # ── Hedging vs. conviction distinction ──
    broad_market_hedging_detected: bool = False  # True if SPY/QQQ puts spiking without
    # sector-specific concern (macro hedging)
    sector_specific_conviction: bool = False  # True if sector ETF puts or calls up
    # without corresponding broad-market activity
    # (sector-specific view)

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
