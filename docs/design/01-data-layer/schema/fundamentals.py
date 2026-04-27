"""Q5: Fundamental and Earnings — entity definitions.

8 entities covering earnings estimates, calendar/proximity, revenue trajectory,
margin shifts, guidance commentary, analyst ratings, insider/institutional
ownership, and valuation multiples. SEC EDGAR XBRL provides structured
financials (free); yfinance provides consensus estimates (free).

Design principle from spec: The distillation layer maintains a per-ticker
"expectations vs. reality scorecard" tracking the gap between market expectations
(consensus estimates, guidance, analyst targets) and incoming reality signals
(estimate revisions, reported actuals, insider behavior). The signal is in the
delta between what the market expects and what's happening.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ._common import (
    AnomalyFlag,
    Direction,
    InvocationMetadata,
    Ticker,
)

__all__ = [
    "AnalystRatingChange",
    "AnalystRatings",
    "AnalystTargetSnapshot",
    "EarningsCalendar",
    "EarningsEstimateDynamics",
    "EarningsEstimates",
    "EstimateRevision",
    "InsiderInstitutionalOwnership",
    "InsiderTransaction",
    "MarginProfitabilityShift",
    "RevenueGrowthTrajectory",
    "ValuationMultiples",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EstimateRevision:
    """A single estimate revision event tracked to the hour for intraday timing."""

    revision_timestamp: datetime  # UTC timestamp when the revision was published
    field: str  # "eps_current_quarter", "eps_next_quarter", "eps_fy", "revenue_cq", etc.
    prior_estimate: float  # Previous consensus before this revision
    new_estimate: float  # New consensus after this revision
    revision_magnitude: float  # Absolute change in estimate
    revision_pct: float  # Percentage change in estimate
    revising_analyst_count: int = 0  # How many analysts revised in this cluster
    revision_direction: Direction = Direction.NEUTRAL  # UP / DOWN / NEUTRAL


@dataclass(frozen=True)
class AnalystRatingChange:
    """A timestamped rating change event from a covering analyst."""

    change_timestamp: datetime
    analyst_name: str = ""
    firm: str = ""
    prior_rating: str = ""  # "buy" | "hold" | "sell"
    new_rating: str = ""
    prior_price_target: float | None = None
    new_price_target: float | None = None


@dataclass(frozen=True)
class InsiderTransaction:
    """A Form 4 filing: insider transaction (buy, sell, exercise)."""

    transaction_date: date
    transaction_type: str  # "buy" | "sell" | "exercise" | "grant"
    insider_name: str = ""
    insider_title: str = ""  # "CEO", "CFO", "Director", etc.
    shares_transacted: int = 0
    transaction_price: float = 0.0  # Price per share at transaction
    transaction_value: float = 0.0  # Total transaction value
    shares_held_after: int = 0  # Insider's holdings after the transaction


@dataclass(frozen=True)
class AnalystTargetSnapshot:
    """Snapshot of analyst price target distribution at a point in time."""

    snapshot_date: date
    mean_target: float  # Mean price target across analysts
    median_target: float  # Median price target
    high_target: float  # Highest price target
    low_target: float  # Lowest price target
    target_spread_pct: float  # (high - low) / mean x 100 — dispersion measure
    analyst_count: int = 0  # Number of analysts contributing targets


# ── Primary entities ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EarningsEstimateDynamics:
    """Q5:5a — Consensus EPS/revenue estimates with revision tracking.

    The single most actionable subcategory. Sell-side consensus estimates are a
    proxy for what the market has priced in. When estimates move, prices follow —
    but not always immediately and not always proportionally. That lag is the
    opportunity.

    Covers:
    - Consensus EPS and revenue estimates (current quarter, next quarter, full year)
    - Revision direction, magnitude, velocity, and breadth
    - Estimate dispersion (high-low spread indicating uncertainty)
    - Revision momentum (accelerating vs. decelerating)
    - Intraday revision timestamps for revision-price lag detection

    Source: yfinance estimate endpoints (free) — get_earnings_estimate, get_eps_trend,
            get_eps_revisions, get_revenue_estimate
    Fallback: Finnhub (premium $11.99/mo for estimates)
    Cadence: Daily (cached)
    Feasibility: HIGH — yfinance 7 estimate endpoints, tested 62/62 tickers
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Current consensus estimates ──
    eps_estimate_cq: float | None = None  # Consensus EPS estimate for current quarter
    eps_estimate_nq: float | None = None  # Consensus EPS estimate for next quarter
    eps_estimate_fy: float | None = None  # Consensus EPS estimate for full year
    revenue_estimate_cq: float | None = None  # Revenue estimate, current quarter (millions)
    revenue_estimate_nq: float | None = None  # Revenue estimate, next quarter (millions)
    revenue_estimate_fy: float | None = None  # Revenue estimate, full year (millions)

    # ── Estimate dispersion (uncertainty proxy) ──
    eps_estimate_high_cq: float | None = None  # Highest EPS estimate from covering analysts (CQ)
    eps_estimate_low_cq: float | None = None  # Lowest EPS estimate (CQ)
    eps_estimate_spread_cq: float = 0.0  # (high - low) / mean x 100 — dispersion % (CQ)
    revenue_estimate_high_cq: float | None = None  # Revenue estimate high (CQ, millions)
    revenue_estimate_low_cq: float | None = None  # Revenue estimate low (CQ, millions)

    # ── Revision tracking (most recent quarter lookback) ──
    eps_revision_direction_cq: Direction = Direction.NEUTRAL
    # Direction of consensus EPS revisions: UP, DOWN, or NEUTRAL
    eps_revision_magnitude_cq: float = 0.0
    # Absolute change in EPS estimate (CQ) in current tracking window
    eps_revision_pct_cq: float = 0.0
    # Percentage change in EPS estimate (CQ)
    revenue_revision_direction_cq: Direction = Direction.NEUTRAL
    revenue_revision_magnitude_cq: float = 0.0  # In millions
    revenue_revision_pct_cq: float = 0.0

    # ── Revision breadth and velocity ──
    analysts_revising_cq_count: int = 0  # Number of analysts revising EPS in the tracking window
    analysts_revising_cq_pct: float = 0.0  # Percentage of covering analysts that revised
    total_covering_analysts_cq: int = 0  # Total number of covering analysts
    revision_breadth_days: int = 3  # Lookback window for revision breadth (typically 3-5 days)

    # ── Revision momentum (acceleration/deceleration) ──
    revision_momentum_cq: str = ""
    # "accelerating" — revisions increasing in frequency/magnitude this week vs. last
    # "decelerating" — revisions slowing
    # "stable" — consistent pace
    revisions_this_period: int = 0  # Count of revisions in current period
    revisions_prior_period: int = 0  # Count of revisions in prior period (for momentum)

    # ── Recent revision events (timestamped for lag detection) ──
    recent_revisions: list[EstimateRevision] = field(default_factory=list)
    # Most recent revision events (last 5-10). Timestamped to the hour.
    # Distillation layer uses these to flag "revision published but not yet priced".

    # ── Anomalies detected by distillation layer ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class EarningsCalendar:
    """Q5:5b — Earnings event calendar with proximity and drift patterns.

    Where each ticker sits relative to its next earnings date. Partly catalyst
    scheduling, partly risk management — as earnings approach, IV ramps, volume
    patterns shift, and the thesis framework changes.

    Covers:
    - Days to next earnings with flags at key thresholds
    - Event window mapping (quiet period, report date, call time)
    - Historical post-earnings drift patterns
    - Earnings clustering detection (when multiple names in same sector report)

    Source: Finnhub (free tier)
    Fallback: Alpha Vantage
    Cadence: Daily check
    Feasibility: HIGH — Finnhub free tier covers earnings calendar
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Proximity to earnings ──
    days_to_next_earnings: int = 0
    # Integer count. Flags are typically: within 7 days, within 2 days, today.
    next_earnings_date: date | None = None  # Expected earnings report date (ET)
    next_earnings_time: str | None = None  # "pre_market", "post_market", "unknown"
    last_earnings_date: date | None = None  # Date of the most recent reported earnings

    # ── Event window mapping ──
    quiet_period_start: date | None = None  # Typically 10-15 days before earnings
    quiet_period_end: date | None = None  # Day before earnings
    earnings_window_label: str = ""
    # "pre_quiet" | "in_quiet" | "in_event_window" | "post_event" | "no_earnings"
    days_in_event_window: int = 0  # Count of days in the event window (typically 3-5)

    # ── Historical post-earnings drift (PED) ──
    historical_ped_direction: Direction = Direction.NEUTRAL
    # Does this name historically continue in the earnings-reaction direction
    # (positive PED) or mean-revert (negative PED)?
    historical_ped_magnitude_atr: float = 0.0
    # Average additional drift magnitude in ATR terms over the 1-5 days post-earnings
    historical_ped_lookback_quarters: int = 4  # How many prior quarters of history analyzed
    mean_revert_probability_post_earnings: float = 0.5
    # 0.0-1.0: likelihood that the name mean-reverts within 2 days post-earnings

    # ── Earnings clustering ──
    sector_earnings_cluster: bool = False
    # True if multiple names in the same sector report within ±2 days
    sector_cluster_count: int = 0  # How many names in the sector reporting in the window
    sector_cluster_window_start: date | None = None
    sector_cluster_window_end: date | None = None

    # ── IV context (computed by distillation layer from Q3 data) ──
    implied_vol_rank: float = 0.5
    # 0.0-1.0: current implied vol percentile vs. 1-year history (IVR proxy)
    implied_vol_vs_realized: float = 0.0
    # IV implied move magnitude vs. realized move post-earnings (historical average)

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class RevenueGrowthTrajectory:
    """Q5:5c — Revenue and EPS growth rates with acceleration detection.

    Trailing reported numbers as a baseline for where consensus should be
    anchoring. The system needs context: is this a company accelerating,
    decelerating, or inflecting? A stock with three quarters of accelerating
    revenue growth gets treated very differently on a miss than one that's
    been decelerating.

    Covers:
    - Revenue QoQ and YoY growth rates (trailing 4-8 quarters)
    - Growth acceleration/deceleration (second derivative)
    - Beat/miss history vs. consensus
    - Revenue mix shifts (for diversified names)

    Source: SEC EDGAR XBRL (free)
    Fallback: Alpha Vantage (free, 25 calls/day)
    Cadence: Quarterly
    Feasibility: HIGH — EDGAR CompanyFacts API provides structured financials
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Most recent quarter (actual reported) ──
    last_reported_revenue: float | None = None  # Millions
    last_reported_revenue_yoy_growth: float = 0.0  # Year-over-year growth rate (%)
    last_reported_revenue_qoq_growth: float = 0.0  # Quarter-over-quarter growth rate (%)

    # ── EPS growth (same quarter) ──
    last_reported_eps: float | None = None
    last_reported_eps_yoy_growth: float = 0.0  # Year-over-year growth rate (%)
    last_reported_eps_qoq_growth: float = 0.0

    # ── Trailing growth rates (last 4 quarters) ──
    revenue_growth_rates_trailing_4q: list[float] = field(default_factory=list)
    # Most recent 4 quarters' YoY growth rates, most recent first
    eps_growth_rates_trailing_4q: list[float] = field(default_factory=list)
    # Most recent 4 quarters' YoY growth rates

    # ── Growth acceleration/deceleration (second derivative) ──
    revenue_acceleration: float = 0.0
    # Δ(growth_this_quarter - growth_prior_quarter). Positive = accelerating.
    eps_acceleration: float = 0.0
    # Same for EPS. The second derivative is the inflection signal.
    acceleration_direction: Direction = Direction.NEUTRAL
    # "accelerating" | "decelerating" | "stable"
    inflection_candidate: bool = False
    # True if growth trajectory shows potential inflection (first positive/negative
    # acceleration after sustained opposite trend)

    # ── Beat/miss history (vs. consensus estimates) ──
    beat_miss_ratio_trailing_4q: float = 0.0
    # Count of beats / (beats + misses) over trailing 4 quarters
    # E.g., 0.75 means 3 beats and 1 miss in the last 4 quarters
    last_eps_surprise_pct: float = 0.0
    # (actual - consensus) / consensus x 100. Positive = beat, negative = miss.
    last_revenue_surprise_pct: float = 0.0
    surprise_consistency: str = ""
    # "serial_beater" | "serial_misser" | "mixed" — historical pattern
    # Used to contextualize the next earnings reaction

    # ── Revenue mix (for diversified names) ──
    segment_growth_rates: dict[str, float] = field(default_factory=dict)
    # Segment name → YoY growth rate. E.g., {"cloud": 35, "legacy": -5, "other": 10}
    # For tech companies, cloud/AI growth vs. legacy matters for multiple assignment.
    primary_growth_segment: str = ""
    # Which segment is driving growth (e.g., "AI", "cloud", "semiconductors")

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class MarginProfitabilityShift:
    """Q5:5d — Margin trajectory and operating leverage with inflection detection.

    The market's "quality of earnings" read. Revenue beats get a very different
    reaction depending on whether margins expanded or compressed alongside them.

    Covers:
    - Gross margin trajectory (trailing 4-8 quarters, trend, rate of change)
    - Operating leverage (revenue growth vs. OpEx growth spread)
    - EPS growth vs. revenue growth divergence
    - Profitability inflection detection

    Source: SEC EDGAR XBRL (free)
    Cadence: Quarterly
    Feasibility: HIGH — derived from XBRL financial statements
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Most recent margin data ──
    gross_margin_latest: float | None = None  # % (e.g., 42.5 for 42.5%)
    operating_margin_latest: float | None = None
    net_margin_latest: float | None = None

    # ── Margin trajectory (trailing 4-8 quarters) ──
    gross_margin_trajectory: list[float] = field(default_factory=list)
    # Most recent 4 quarters, most recent first. Each value is a percentage.
    operating_margin_trajectory: list[float] = field(default_factory=list)
    net_margin_trajectory: list[float] = field(default_factory=list)

    # ── Margin change direction ──
    gross_margin_direction: Direction = Direction.NEUTRAL
    # Is margin expanding ("BULLISH") or compressing ("BEARISH")?
    gross_margin_rate_of_change: float = 0.0
    # Change in gross margin (percentage points) from last quarter to prior quarter
    operating_margin_direction: Direction = Direction.NEUTRAL
    operating_margin_rate_of_change: float = 0.0

    # ── Operating leverage ──
    revenue_growth_rate_latest: float = 0.0  # % YoY
    opex_growth_rate_latest: float = 0.0  # % YoY (operating expense growth)
    operating_leverage_spread: float = 0.0
    # Revenue growth % - OpEx growth %. Positive = leverage improving (scaling).
    leverage_direction: Direction = Direction.NEUTRAL

    # ── EPS vs. revenue growth divergence ──
    eps_growth_rate_latest: float = 0.0  # % YoY
    revenue_growth_rate_latest_dup: float = 0.0  # Duplicate for clarity (same as above)
    eps_vs_revenue_divergence: float = 0.0
    # EPS growth % - revenue growth %. Positive = margins expanding, negative = compressing.
    divergence_direction: Direction = Direction.NEUTRAL
    # If EPS growing much faster than revenue, margins expanding (bullish for quality)
    # If EPS growing slower than revenue, margins compressing (bearish)

    # ── Profitability inflection detection ──
    profitability_inflection: bool = False
    # True if the company is transitioning from unprofitable to profitable or vice versa
    inflection_type: str = ""
    # "unprofitable_to_profitable" | "profitable_to_unprofitable" | "none"
    quarters_until_profitability: int | None = None
    # If currently unprofitable, estimated quarters until profitability (analyst guidance)

    # ── Multi-quarter margin trend ──
    margin_trend_direction: str = ""
    # "improving" | "deteriorating" | "stable" (based on trailing trajectory)
    margin_stability_score: float = 0.5
    # 0.0-1.0: how consistent margins have been over trailing quarters
    # Higher = more stable, lower = volatile

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class EarningsEstimates:
    """Q5:5e — Forward guidance vs. consensus with commentary tone scoring.

    What management signaled about the future. There's a quantifiable dimension
    that sits alongside the qualitative analysis (which the qualitative pipeline
    handles separately).

    Covers:
    - Guidance vs. consensus (above/in-line/below)
    - Guidance range width (confidence proxy)
    - Guidance revision direction
    - Forward commentary tone score

    Note: This entity focuses on management forward guidance and tone. The earnings
    calendar aspect is in EarningsCalendar (Q5:5b).

    Source: Parsed from earnings call transcripts and investor relations statements
    Cadence: Post-earnings (quarterly), updated if management re-guides
    Feasibility: HIGH — structured guidance typically available from company websites
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Guidance vs. consensus (for the next quarter/FY) ──
    guidance_source_date: date | None = None  # Date guidance was issued
    guidance_applies_to: str = ""  # "next_quarter" | "full_year"

    # Next quarter guidance
    nq_guidance_low: float | None = None  # Next quarter EPS guidance low
    nq_guidance_high: float | None = None  # Next quarter EPS guidance high
    nq_guidance_midpoint: float | None = None
    nq_consensus_estimate: float | None = None  # Consensus estimate at time of guidance
    nq_guidance_vs_consensus: str = ""
    # "above" | "in_line" | "below"
    # Comparison of guidance midpoint to consensus at the time guidance was given
    nq_guidance_surprise: float = 0.0  # % (guidance_midpoint - consensus) / consensus x 100

    # Full year guidance
    fy_guidance_low: float | None = None
    fy_guidance_high: float | None = None
    fy_guidance_midpoint: float | None = None
    fy_consensus_estimate: float | None = None
    fy_guidance_vs_consensus: str = ""
    fy_guidance_surprise: float = 0.0

    # ── Guidance range width (confidence proxy) ──
    nq_guidance_range_width_pct: float = 0.0
    # (high - low) / midpoint x 100. Narrow = confidence, wide = uncertainty.
    fy_guidance_range_width_pct: float = 0.0
    guidance_confidence_level: str = ""
    # "high_confidence" (range < 5%) | "moderate" (5-15%) | "low_confidence" (> 15%)

    # ── Guidance revision (vs. prior guidance if available) ──
    nq_prior_guidance_midpoint: float | None = None
    nq_guidance_revision_direction: Direction = Direction.NEUTRAL
    # Has management raised, lowered, or maintained guidance?
    nq_guidance_revision_magnitude: float = 0.0  # In percentage points
    fy_prior_guidance_midpoint: float | None = None
    fy_guidance_revision_direction: Direction = Direction.NEUTRAL
    fy_guidance_revision_magnitude: float = 0.0

    # ── Commentary tone scoring (simplified, not full NLP) ──
    forward_tone_sentiment: Direction = Direction.NEUTRAL
    # "bullish" | "neutral" | "bearish"
    # Aggregate of management's forward-looking language in call script/guidance
    tone_confidence_score: float = 0.5  # 0.0-1.0: how confident/clear the tone is
    tone_key_phrases: list[str] = field(default_factory=list)
    # Sample phrases extracted: ["strong demand", "headwinds", "accelerating"] — for LLM context

    # ── Guidance quality signal ──
    beat_and_raise: bool = False
    # True if company both beat prior quarter and raised forward guidance
    # (strongest bull signal: reality beat expectations AND future expectations rising)
    beat_and_lower_guidance: bool = False
    # True if beat earnings but lowered forward guidance (sell-the-news candidate)

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class AnalystRatings:
    """Q5:5f — Analyst consensus ratings, price targets, and rating change events.

    Aggregate sell-side positioning. Not because analysts are right (they're often
    late), but because rating changes cause mechanical flow — upgrades trigger
    buying programs at funds that screen on consensus ratings.

    Covers:
    - Consensus rating distribution (buy/hold/sell)
    - Rating changes (upgrades, downgrades, initiations) with timestamps
    - Price target distribution and current stock vs. targets
    - Target revision clustering

    Source: Finnhub (free tier) + yfinance (get_analyst_price_targets)
    Cadence: Daily
    Feasibility: MEDIUM — Finnhub provides basic ratings, less structured than paid
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Consensus rating ──
    consensus_rating: str = ""  # "buy" | "hold" | "sell"
    rating_snapshot_date: date = field(default_factory=lambda: datetime.now(tz=UTC).date())

    # ── Rating distribution (buy/hold/sell counts) ──
    buy_count: int = 0
    hold_count: int = 0
    sell_count: int = 0
    total_raters: int = 0
    buy_pct: float = 0.0  # Percentage of buy ratings
    hold_pct: float = 0.0
    sell_pct: float = 0.0

    # ── Rating change events (most recent) ──
    recent_rating_changes: list[AnalystRatingChange] = field(default_factory=list)
    # Recent upgrades/downgrades/initiations, most recent first (limit ~5)
    rating_changes_last_30d: int = 0  # Count of rating changes in past 30 days

    # ── Price target distribution ──
    price_target_mean: float | None = None
    price_target_median: float | None = None
    price_target_high: float | None = None
    price_target_low: float | None = None
    price_target_spread_pct: float = 0.0
    # (high - low) / mean x 100 — consensus target dispersion
    price_target_snapshot: AnalystTargetSnapshot | None = None

    # ── Price vs. consensus target ──
    current_price: float = 0.0  # Current stock price (as of metadata.collected_at)
    price_vs_mean_target: float = 0.0  # % (current - mean_target) / mean_target x 100
    price_vs_median_target: float = 0.0
    target_achievement_gap: str = ""
    # "above_all_targets" | "above_median" | "at_consensus" |
    # "below_median" | "below_all_targets"

    # ── Target revision clustering ──
    target_revisions_last_7d: int = 0  # Count of price target changes in last 7 days
    target_revision_direction_7d: Direction = Direction.NEUTRAL
    # On net, are analysts raising or lowering targets?
    target_revision_magnitude_avg: float = 0.0  # Average target revision magnitude (% change)

    # ── Rating trend (improvement or deterioration) ──
    rating_momentum: Direction = Direction.NEUTRAL
    # "bullish" — more upgrades than downgrades recently
    # "bearish" — more downgrades than upgrades
    # "neutral" — balanced
    rating_momentum_period_days: int = 30  # Lookback period for momentum assessment

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class InsiderInstitutionalOwnership:
    """Q5:5g — Insider transactions (Form 4) and institutional ownership shifts.

    How the people closest to the company are positioning. Insider buying is a
    stronger signal than insider selling (selling can be for liquidity, buying
    is almost always conviction). Institutional data is lagged but directionally
    useful.

    Covers:
    - Insider transactions: Form 4 filings (buys, sells, option exercises)
    - Insider buy/sell ratio (30/90 day lookback)
    - 13F institutional ownership changes (quarterly)
    - Ownership concentration (top 10 holders)

    Source: SEC EDGAR Form 4 RSS (free) + Finnhub (free)
    Cadence: Daily (insider tx), Quarterly (13F)
    Feasibility: HIGH — SEC Form 4 filings are free and near real-time
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Insider transactions (Form 4) ──
    recent_insider_transactions: list[InsiderTransaction] = field(default_factory=list)
    # Most recent Form 4 filings (limit ~10), most recent first

    # ── Insider buy/sell ratio (trailing lookback) ──
    insider_buyback_count_30d: int = 0  # Count of insider buys (last 30 days)
    insider_sell_count_30d: int = 0  # Count of insider sells
    insider_exercise_count_30d: int = 0  # Option exercises
    insider_buy_sell_ratio_30d: float = 0.0
    # buys / (buys + sells). Values > 0.5 indicate net buying conviction.
    insider_aggregate_buy_value_30d: float = 0.0  # Total $ value of insider buys (last 30d)
    insider_aggregate_sell_value_30d: float = 0.0  # Total $ value of insider sells

    # ── 90-day view (for longer-term trend) ──
    insider_buyback_count_90d: int = 0
    insider_sell_count_90d: int = 0
    insider_buy_sell_ratio_90d: float = 0.0

    # ── Insider transaction clustering (signal) ──
    clustered_insider_buying: bool = False
    # True if multiple insiders bought within a short window (3-5 days)
    # Stronger signal than isolated buys
    insider_cluster_count: int = 0  # Number of insiders in the buying cluster

    # ── 13F institutional ownership (quarterly snapshot) ──
    last_13f_date: date | None = None  # Date of most recent 13F filing quarter-end
    institutional_ownership_pct: float = 0.0  # % of shares held by institutional investors
    institutional_ownership_change_pct: float = 0.0
    # % point change from prior quarter (e.g., 5.2 percentage points)
    institutional_ownership_direction: Direction = Direction.NEUTRAL
    # "increasing" | "decreasing" | "stable"

    # ── Ownership concentration ──
    top_10_holders_pct: float = 0.0  # % of shares held by top 10 shareholders
    top_10_concentration_trend: Direction = Direction.NEUTRAL
    # Is the ownership becoming more concentrated (fewer hands, conviction)
    # or more distributed?

    # ── Notable institutional moves ──
    new_13f_positions: list[str] = field(default_factory=list)
    # List of prominent funds that initiated new positions (last 13F quarter)
    closed_13f_positions: list[str] = field(default_factory=list)
    # Prominent funds that exited positions
    major_13f_increases: dict[str, float] = field(default_factory=dict)
    # Fund name → % increase in position size
    major_13f_decreases: dict[str, float] = field(default_factory=dict)

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class ValuationMultiples:
    """Q5:VAL — Valuation ratios with peer-relative context for sector analysis.

    Current valuation multiples (P/E trailing + forward, P/S, P/B, EV/EBITDA),
    peer group averages, relative valuation (premium/discount to sector/peers),
    valuation percentile vs. own history, valuation dispersion within sector.

    Used by domain researchers (tech-semis, financials, energy) for relative value
    assessment and by the synthesizer for cross-sector valuation context.

    On a 4-72 hour horizon, the system doesn't care about deep fundamental
    valuation. However, relative valuation context helps sector analysts detect
    when consensus has become stretched or when a name has become cheap relative
    to peers — inflection moments often trigger repricing.

    Source: Derived from SEC EDGAR XBRL (financials) + Polygon Stocks Starter (price)
            + yfinance (forward estimates for forward P/E)
    Fallback: Finnhub basic metrics
    Cadence: Daily (price-driven updates), Quarterly (earnings-driven updates)
    Feasibility: HIGH — all inputs already in stack, pure computation
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Absolute multiples (current snapshot) ──
    pe_trailing: float | None = None  # P/E ratio based on trailing 12-month earnings
    pe_forward: float | None = None  # P/E based on forward 12-month consensus estimates
    ps_ratio: float | None = None  # Price-to-Sales (market cap / ttm revenue)
    pb_ratio: float | None = None  # Price-to-Book (market cap / book value)
    ev_ebitda: float | None = None  # EV / EBITDA
    peg_ratio: float | None = None  # P/E / growth rate (relevance for growth stocks)

    # ── Peer group context (sector averages) ──
    sector_pe_trailing_avg: float | None = None
    sector_pe_forward_avg: float | None = None
    sector_ps_avg: float | None = None
    sector_pb_avg: float | None = None
    sector_ev_ebitda_avg: float | None = None

    # ── Relative valuation vs. sector ──
    pe_vs_sector_pct: float = 0.0
    # (ticker PE - sector avg PE) / sector avg PE x 100
    # Positive = trading at premium, negative = trading at discount
    ps_vs_sector_pct: float = 0.0
    pb_vs_sector_pct: float = 0.0
    ev_ebitda_vs_sector_pct: float = 0.0
    valuation_vs_sector_label: str = ""
    # "significant_premium" | "moderate_premium" | "at_avg" |
    # "moderate_discount" | "significant_discount"

    # ── Valuation percentile (vs. own history) ──
    pe_percentile_1y: float = 50.0
    # 0-100 percentile where current P/E sits vs. 1-year history
    # 0 = lowest valuation in a year, 100 = highest
    pe_percentile_3y: float = 50.0
    ps_percentile_1y: float = 50.0
    pb_percentile_1y: float = 50.0

    # ── Valuation regime ──
    valuation_regime: str = ""
    # "at_highs" (>80 percentile) — overextended, reversal risk
    # "above_avg" (50-80) — premium to history
    # "at_avg" (40-60) — fair value vs. own history
    # "below_avg" (20-50) — discount to history
    # "at_lows" (<20) — deeply cheap, recovery potential

    # ── Dispersion within sector (uncertainty proxy) ──
    sector_valuation_dispersion_pe: float = 0.0
    # Standard deviation of P/E multiples within sector (or coefficient of variation)
    # High dispersion = market unsure how to value the group
    sector_valuation_dispersion_ps: float = 0.0

    # ── Change metrics (quarter-over-quarter) ──
    pe_change_qoq: float = 0.0  # P/E change (percentage points)
    pe_change_direction: Direction = Direction.NEUTRAL
    # Is the P/E multiple expanding (BULLISH — earnings growing slower than price)
    # or contracting (BEARISH — earnings growing faster than price)?
    ps_change_qoq: float = 0.0

    # ── Forward yield context (for rate-sensitive sectors like financials) ──
    forward_earnings_yield: float | None = None  # 1 / pe_forward x 100 — earnings yield %

    anomalies: list[AnomalyFlag] = field(default_factory=list)
