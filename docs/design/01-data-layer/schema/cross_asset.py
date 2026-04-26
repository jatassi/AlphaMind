"""Q7: Cross-Asset and Correlation — entity definitions.

7 entities covering intra-sector correlation, cross-sector rotation, market
breadth, intermarket regime signals, implied vs. realized correlation,
lead-lag relationships, and regime change detection. 100% derived locally
from other domains' outputs.

Design note on Q7:
    Q7 is fundamentally different from Q1–Q6: it measures RELATIONSHIPS
    between things, not things themselves. It has no raw data sources —
    100% derived from Q1–Q6 and Q8 outputs. Divergences between correlated
    assets are among the clearest setups for the 4–72 hour horizon.

    Some entities are market-wide (7d, 7g), others are sector-scoped (7a),
    still others track cross-sector dynamics (7b). Some require specific
    ticker pairs (7a uses intra-sector pairs), others use Optional[Ticker]
    for market-wide signals. Design each entity to match its natural scope.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ._common import (
    AlphaMindSector,
    AnomalyFlag,
    DataConfidence,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
)


# ── Supporting types ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class CorrelationPair:
    """A pairwise correlation measurement between two tickers (intra-sector) or
    two indices/assets (intermarket). Includes trailing estimates at multiple windows."""
    ticker_1: Ticker                     # First ticker in the pair
    ticker_2: Ticker                     # Second ticker in the pair
    correlation_20d: float = 0.0         # 20-day rolling correlation (default window)
    correlation_60d: float = 0.0         # 60-day rolling correlation (trend context)
    correlation_trend: str = ""          # "strengthening" | "weakening" | "stable"
                                         # Whether correlation is increasing or decreasing
    last_updated: Optional[datetime] = None  # When this pair's correlation was last computed


@dataclass(frozen=True)
class DivergenceEvent:
    """A detected divergence: one name breaking from its sector cohort or from
    a correlated peer. Core signal for the 4–72 hour horizon."""
    ticker: Ticker                       # The name that has diverged
    peers: list[Ticker] = field(default_factory=list)  # Names it should move with
    divergence_magnitude_pct: float = 0.0  # How far this name has drifted relative to peer group (%)
    divergence_duration_days: int = 0    # How many days the divergence has persisted
    is_fresh: bool = False               # True if divergence < 3 days old (more actionable)
    historical_resolution_pattern: str = ""  # "leader_caught_up" | "leader_pulled_back" |
                                         # "divergence_persisted" based on prior occurrences
    expected_resolution_timeframe_days: int = 0  # Estimated days until resolution based on history


@dataclass(frozen=True)
class SectorRotationSignal:
    """A detected sector rotation: capital flowing between sectors or sectors
    rotating relative to the broad market."""
    from_sector: Optional[AlphaMindSector] = None  # Sector being exited (None for outflow from equities)
    to_sector: Optional[AlphaMindSector] = None  # Sector being entered (None for inflow to equities)
    relative_performance_gap_pct: float = 0.0  # % outperformance of destination vs. source
    rotation_velocity_pct_per_day: float = 0.0  # Speed of rotation (% per day). Slow = regime shift,
                                         # fast = event-driven, often mean-reverting.
    rotation_narrative: str = ""         # "rate_driven" | "growth_driven" | "risk_appetite_driven"
    net_flow_direction: Direction = Direction.NEUTRAL  # Capital flowing into destination
    flow_conviction: SignalStrength = SignalStrength.MODERATE  # How confident is the rotation signal


@dataclass(frozen=True)
class BreadthMetric:
    """Market-wide or sector-wide breadth indicator: the composition of a move."""
    metric_type: str                     # "pct_above_20ema" | "pct_above_50ema" | "pct_above_200ema" |
                                         # "advance_decline_ratio" | "new_highs" | "new_lows"
    value: float = 0.0                   # The metric value (%, count, or ratio depending on type)
    change_from_prior_session: float = 0.0  # How it changed since last invocation
    sector: Optional[AlphaMindSector] = None  # If None, metric is market-wide; else sector-specific


@dataclass(frozen=True)
class LeadLagTiming:
    """Estimated lead-lag relationship between two assets or chains of assets."""
    leader: str                          # Asset that moves first (e.g., "funding_rate", "credit_spreads", "semis")
    follower: str                        # Asset expected to respond (e.g., "equity_weakness", "tech")
    estimated_lag_hours: float = 0.0     # Estimated hours for follower to respond (0–48 typical)
    lag_consistency: float = 0.0         # 0.0–1.0. How consistently the lag holds (1.0 = very consistent)
    regime_active: bool = False          # Whether this lead-lag relationship is currently active
    time_since_last_lead_signal: Optional[int] = None  # Hours since leader last moved significantly


@dataclass(frozen=True)
class IntermarketRegimeFlag:
    """A detected regime signal from a major cross-asset relationship."""
    relationship: str                    # "spy_tlt" | "gold_real_yields" | "oil_energy" | "vix_spy" |
                                         # "margin_debt" | "sentiment_extreme"
    current_direction: Direction         # What the relationship is currently doing
    regime_classification: str = ""      # "bullish_regime" | "bearish_regime" | "risk_off" | "risk_on" | etc.
    regime_stability: float = 0.0        # 0.0–1.0. Higher = relationship has been stable for longer
    days_in_current_regime: int = 0      # How many days this regime has persisted


# ── Primary entities ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class IntraSectorCorrelation:
    """Q7:7a — Rolling pairwise correlation and divergence detection within sectors.

    Rolling correlation matrix (20-day, 60-day), divergence detection (names
    breaking from sector), divergence magnitude/duration, historical resolution
    patterns. Output delivered to sector analyst agents to flag peers breaking
    from the cohort.

    Source: Computed from Q1 OHLCV across sector tickers
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived locally
    """

    # ── Identity ──
    sector: AlphaMindSector              # Which sector this correlation matrix covers
    metadata: InvocationMetadata

    # ── Correlation matrix ──
    pairwise_correlations: list[CorrelationPair] = field(default_factory=list)
        # All pairwise correlations within the sector at 20-day and 60-day windows.
        # For a 15-name sector: 105 pairs. Sorted by most recent update timestamp.

    # ── Sector-level correlation characteristics ──
    avg_pairwise_correlation_20d: float = 0.0
        # Average correlation across all pairs (20-day). Range: -1.0 to 1.0.
        # High (>0.7) = tight cohesion, divergences are more anomalous.
        # Low (<0.3) = loose coupling, harder to distinguish signal from noise.
    avg_pairwise_correlation_60d: float = 0.0
        # Same, 60-day window. Trend direction (comparing 60d to 20d) indicates
        # whether sector cohesion is tightening or loosening.
    correlation_volatility: float = 0.0  # Std dev of trailing 20-day correlation estimates.
                                         # High volatility = unstable relationships, regime in flux.

    # ── Divergence detection ──
    active_divergences: list[DivergenceEvent] = field(default_factory=list)
        # Names currently diverging from their sector peer group. Sorted by
        # divergence_magnitude (largest first). A 4-name tech sector might show
        # NVDA rallying while AMD/AVGO/QCOM consolidate — NVDA entry in this list.
        # Most actionable divergences are <5 days old and magnitude >10%.
    divergence_count: int = 0            # How many names currently showing detectable divergence
    divergence_history_30d: int = 0      # Count of divergence events that occurred (even if resolved)
                                         # in the trailing 30 days. Rising count = unstable sector.

    # ── Sector cohesion trend ──
    sector_cohesion_trend: str = ""      # "tightening" | "loosening" | "stable"
                                         # Multi-day trend: is the sector pulling together or coming apart?
    cohesion_trend_days: int = 0         # How many days the current cohesion trend has persisted

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CrossSectorRotation:
    """Q7:7b — Sector ETF relative performance, flows, and rotation classification.

    Sector ETF relative performance (XLK/XLF/XLE/SMH vs. SPY), sector ETF flows,
    broad equity fund flows, rotation velocity, rotation narrative classification
    (rate-driven, growth-driven, risk-appetite-driven). Delivered to synthesizer
    agent as market regime context.

    Source: Computed from Q1 OHLCV + ETF data
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived locally
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Sector ETF relative performance (market-wide, not per-sector) ──
    sector_etf_pairs: list[CorrelationPair] = field(default_factory=list)
        # Relative performance pairs: XLK/SPY, XLF/SPY, XLE/SPY, SMH/SPY, XLK/XLF, etc.
        # Stored as rolling ratio: positive correlation_20d = outperforming, negative = underperforming.
        # Also includes inter-sector pairs (XLK vs. XLF) to show relative flows.

    # ── Broad equity flows ──
    equity_fund_net_flows_weekly_usd: float = 0.0
        # Weekly net inflows/outflows into equity mutual funds and ETFs (from ICI).
        # Positive = capital flowing into equities, negative = outflows.
        # Sign of broad market demand/supply. Sustained negative flows = rising tide is going out.
    equity_fund_flows_trend: str = ""    # "accumulation" | "distribution" | "equilibrium"
                                         # Multi-week trend: is capital persistently flowing in or out?
    days_in_flow_trend: int = 0          # How many days/weeks the current flow trend has held

    # ── Sector ETF flows (if available) ──
    sector_etf_flows: dict[str, float] = field(default_factory=dict)
        # Sector ETF symbol -> net inflows (USD). Only populated if flow data is available.
        # Example: {"XLK": 150_000_000, "XLF": -50_000_000} means tech ETF is in vogue.

    # ── Active rotation signals ──
    active_rotations: list[SectorRotationSignal] = field(default_factory=list)
        # Currently detected sector rotations. A "growth to value" rotation would show
        # XLK/XLF declining (tech underperforming financials) with high velocity.
        # Sorted by velocity (fastest first). Most actionable are high-velocity recent rotations.
    rotation_count_20d: int = 0          # How many distinct rotations detected in last 20 days.
                                         # High count = choppy regime, rotations reversing quickly.

    # ── Rotation characteristics ──
    dominant_rotation_driver: str = ""   # "rate_environment" | "growth_outlook" | "risk_sentiment" |
                                         # "earnings_strength" | "unclear"
                                         # Inferred from which sectors are rotating together.
    rotation_intensity: str = ""         # "gradual_shift" | "pronounced_rotation" | "sharp_reversal"
                                         # Classification based on velocity and magnitude of active rotations

    # ── Market structure for rotation context ──
    broad_market_trend: str = ""         # "up" | "down" | "sideways" during rotation period
    rotation_against_trend: bool = False # True if rotation is counter to broad market direction
                                         # (e.g., tech outperforming while broad market weakens)

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class IndexConstituentBehavior:
    """Q7:7c — Market breadth, advance/decline, and equal-weight vs. cap-weight divergence.

    % of universe above 20/50/200-day EMA, advance/decline per sector,
    equal-weight vs. cap-weight performance, new highs vs. lows, aggregate
    spread and volume context, market-wide liquidity score. Market-wide entity
    delivered to Portfolio manager for position-sizing and risk assessment.

    Source: Computed from Q1 OHLCV across universe
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived locally
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Breadth indicators (market-wide) ──
    pct_above_20ema: float = 0.0         # % of universe trading above 20-day EMA.
                                         # >70% = broad strength, <30% = broad weakness.
                                         # 40–60% = balanced market.
    pct_above_50ema: float = 0.0         # % above 50-day EMA (longer-term trend filter)
    pct_above_200ema: float = 0.0        # % above 200-day EMA (major trend indicator)
    breadth_trend: str = ""              # "improving" | "deteriorating" | "stable"
                                         # Multi-day trend of breadth indicators

    # ── Advance/decline statistics (market-wide) ──
    advance_decline_ratio: float = 0.0   # Advancing names / Declining names. >1.2 = strength, <0.8 = weakness
    advances_count: int = 0              # Names up on the day
    declines_count: int = 0              # Names down on the day
    unchanged_count: int = 0             # Names flat (within 0.5%)

    # ── Advance/decline by sector ──
    ad_by_sector: dict[str, tuple[int, int]] = field(default_factory=dict)
        # Sector name -> (advances, declines). Example: {"tech": (12, 3), "semis": (10, 5)}
        # Useful for identifying which sectors are leading/lagging within a broad move.

    # ── Equal-weight vs. cap-weight divergence ──
    eq_weight_vs_cap_weight_rsperformance_1d: float = 0.0
        # Equal-weight S&P 500 return minus Cap-weight (SPY) return for the day.
        # Positive = small/mid-caps outperforming (broad move), negative = mega-caps dominating.
        # When SPY is up 2% but this is negative, the rally is concentrated in 5–10 names.
    eq_weight_vs_cap_weight_rsperformance_5d: float = 0.0  # 5-day relative return
    eq_weight_vs_cap_weight_rsperformance_20d: float = 0.0  # 20-day relative return
    rally_breadth_classification: str = ""  # "broad_based" | "concentrated" | "neutral"
                                         # Classification: is this rally/selloff participation broad or narrow?

    # ── New highs/lows ──
    new_52w_highs: int = 0               # Names making new 52-week highs today
    new_52w_lows: int = 0                # Names making new 52-week lows today
    new_highs_lows_ratio: float = 0.0    # Highs / Lows ratio. >1.5 = bullish breadth, <0.67 = bearish
    new_highs_lows_trend: str = ""       # "highs_expanding" | "lows_expanding" | "balanced"

    # ── Aggregate microstructure (market-wide liquidity) ──
    avg_bid_ask_spread_bps: float = 0.0  # Average bid-ask spread across the universe in basis points.
                                         # <2 bps = excellent liquidity, >5 bps = stress/off-hours.
    spread_trend: str = ""               # "tightening" | "widening" | "stable"
    aggregate_volume_vs_20d_avg: float = 0.0  # Today's total volume / 20-day average volume.
                                         # >1.2 = elevated volume (conviction), <0.8 = light volume (skepticism).
    market_wide_liquidity_score: float = 0.0  # Composite 0.0–1.0 score combining spread + volume + depth.
                                         # >0.8 = healthy, <0.4 = stressed. Informs PM position sizing.

    # ── Market structure regime ──
    market_microstructure_regime: str = ""  # "healthy" | "stressed" | "transitioning"
    microstructure_trend_days: int = 0   # How long current microstructure regime has persisted

    # ── Breadth vs. price divergence ──
    breadth_price_divergence: bool = False  # True if price (SPY) is making higher highs but breadth
                                         # indicators are declining (classic exhaustion warning).
    divergence_magnitude: str = ""       # "subtle" | "clear" | "extreme" (how pronounced is the divergence)

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class IntermarketRegimeSignal:
    """Q7:7d — Cross-asset correlations (stocks/bonds/gold/oil/VIX) and regime detection.

    SPY vs. TLT correlation, gold vs. real yields, oil vs. energy stocks beta,
    VIX vs. SPY correlation, correlation regime stability, margin debt, sentiment
    surveys (AAII, CNN Fear & Greed). Delivered to synthesizer agent to assess
    macro regime and inform strategy positioning.

    Source: Computed from Q1, Q6, Q8, Q11 data
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived locally (margin debt monthly, sentiment weekly)
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Core intermarket correlations ──
    spy_tlt_correlation_20d: float = 0.0  # SPY vs. TLT correlation.
                                         # Positive = inflation regime (both sell off on inflation),
                                         # Negative = growth/deflation regime (bonds hedge equities).
                                         # Regime transitions are high-conviction signals.
    spy_tlt_correlation_60d: float = 0.0  # Longer-term trend in the relationship

    gold_real_yields_correlation_20d: float = 0.0
        # Gold vs. real yields (10Y Treasury yield - inflation expectations).
        # Normally negative (gold is inflation hedge). Positive = safe-haven demand.
        # Breakdown = regime shift in real rate regime or safe-haven appetite.

    oil_energy_stocks_beta_20d: float = 0.0
        # Energy sector (XLE) return / Crude futures return. Normally 0.5–0.8.
        # >0.9 = energy tightly coupled to oil, <0.3 = decoupling (earnings/cash flow
        # repricing independent of commodity).

    vix_spy_correlation_20d: float = 0.0  # VIX vs. SPY correlation.
                                         # Normally -0.7 to -0.9 (inverse). Breakdown = hedging demand
                                         # building beneath surface (rising VIX on rising market).

    # ── Regime classifications ──
    regime_flags: list[IntermarketRegimeFlag] = field(default_factory=list)
        # Active regime signals derived from the above correlations.
        # Example: [IntermarketRegimeFlag(relationship="spy_tlt", ..., regime_classification="inflation_regime")]

    # ── Margin debt and leverage context ──
    margin_debt_level_usd: Optional[float] = None  # FINRA margin debt in USD (millions). Monthly frequency.
    margin_debt_pct_of_market_cap: Optional[float] = None  # Margin debt / total US market cap.
                                         # 3.5%+ = elevated, approaching extremes. Signals fragility.
    margin_debt_vs_trailing_avg: float = 0.0  # Current margin debt / 1-year avg. >1.1 = building leverage.
    margin_debt_regime: str = ""         # "expanding_leverage" | "contracting_leverage" | "stable"
    days_since_margin_debt_update: int = 0  # FINRA data is monthly; this tracks freshness.

    # ── Sentiment surveys (weekly frequency) ──
    aaii_bull_pct: Optional[float] = None  # AAII Investor Sentiment: % bullish
    aaii_bear_pct: Optional[float] = None  # % bearish. Bull/Bear ratio = sentiment extremes.
    cnn_fear_and_greed_index: Optional[int] = None  # 0–100 scale. <25 = extreme fear, >75 = extreme greed.
                                         # Contrarian indicator: extreme bullishness = crowded, bearishness = capitulation.
    sentiment_regime: str = ""           # "extreme_bullishness" | "extreme_bearishness" | "neutral"
    days_since_sentiment_update: int = 0  # Surveys are weekly; this tracks freshness.

    # ── Overall regime assessment ──
    macro_regime: str = ""               # "growth_regime" | "inflation_regime" | "stagflation_regime" |
                                         # "deflation_risk" | "transition" based on correlation structure
    regime_confidence: SignalStrength = SignalStrength.MODERATE  # How convinced we are of the macro regime

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class ImpliedRealizedCorrelation:
    """Q7:7e — CBOE implied correlation vs. realized correlation gap.

    CBOE implied correlation index, trailing realized correlation, implied-realized
    gap, correlation regime for strategy selection. Signals to synthesizer agent
    whether individual-name thesis generation or macro/sector bets are favored.

    Source: CBOE (delayed) + computed from OHLCV
    Cadence: Every invocation
    Feasibility: HIGH — derivable
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── CBOE implied correlation (market's expectation) ──
    cboe_implied_correlation_index: Optional[float] = None
        # CBOE implied correlation index value. Delayed by 1–2 days.
        # Range: typically 30–70. Higher = market expects correlations to be tighter.
        # None if CBOE data not yet available this session.
    cboe_index_level_date: Optional[datetime] = None  # When the CBOE index was last updated

    # ── Realized correlation (actual market behavior) ──
    realized_correlation_20d: float = 0.0  # Actual average pairwise correlation over trailing 20 days.
                                         # Computed from Q1 OHLCV across all universe pairs.
    realized_correlation_60d: float = 0.0  # 60-day realized correlation (longer-term perspective)

    # ── Implied-realized gap ──
    implied_minus_realized_gap: Optional[float] = None
        # CBOE implied - realized_20d. Positive gap (implied > realized) = market overpricing
        # correlation hedges (dispersion opportunity, single-name theses favored).
        # Negative gap (implied < realized) = market underpricing systemic risk.
    gap_interpretation: str = ""         # "implied_high_dispersion_opportunity" | "implied_low_systemic_risk_cheap" |
                                         # "gap_neutral" (for analysis interpretation)
    gap_extreme: bool = False            # True if gap exceeds 1 std dev from rolling 20-day avg of gaps

    # ── Correlation regime for strategy selection ──
    correlation_regime: str = ""         # "high_correlation" (>0.6, macro dominates) |
                                         # "low_correlation" (<0.4, individual names favored) |
                                         # "moderate_correlation" (0.4–0.6, mixed strategies work)
    regime_confidence: float = 0.0       # 0.0–1.0. How stable is the current regime? High confidence
                                         # = regime has been consistent for multiple weeks.
    regime_persistence_days: int = 0     # How many days the current correlation regime has held

    # ── Implied correlation trend ──
    implied_correlation_trend: str = ""  # "rising" | "falling" | "stable"
    realized_correlation_trend: str = "" # "rising" | "falling" | "stable"
    trends_converging: bool = False      # True if implied and realized are moving toward each other
                                         # (gap closing). Suggests regime transition imminent.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class LeadLagRelationship:
    """Q7:7f — Cross-domain lead-lag timing estimates and regime shifts.

    Funding → credit → equity lead-lag, semis → tech, financials → market,
    commodity futures → energy stocks lead-lag, rolling timing estimates,
    regime shift detection. Delivered to synthesizer agent to flag when expected
    lag responses are missing (high-conviction short-term thesis).

    Source: Computed from Q1, Q6, Q8 data
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived locally
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Estimated lead-lag relationships (key chains) ──
    funding_to_credit_to_equity_lag: LeadLagTiming = field(default_factory=lambda: LeadLagTiming(
        leader="funding_rate", follower="credit_spreads", estimated_lag_hours=12.0, lag_consistency=0.75
    ))
        # Full chain: funding market stress (repo/SOFR from Q6:6e) → credit spread widening
        # → equity weakness. If funding stress appears without credit/equity response, thesis.

    semis_to_tech_lead_lag: LeadLagTiming = field(default_factory=lambda: LeadLagTiming(
        leader="semiconductors", follower="broad_tech", estimated_lag_hours=6.0, lag_consistency=0.65
    ))
        # Semiconductor sector (SMH) often leads broader tech (XLK) on AI/infrastructure narratives
        # by hours to days. When semis move, watch if XLK follows within lag window.

    financials_to_market_lead_lag: LeadLagTiming = field(default_factory=lambda: LeadLagTiming(
        leader="financial_stocks", follower="broad_market", estimated_lag_hours=8.0, lag_consistency=0.70
    ))
        # Financial stocks often lead broad market on rate-driven moves. Credit spreads lead
        # equity weakness. When XLF moves, SPY typically follows within hours.

    commodity_to_energy_lead_lag: LeadLagTiming = field(default_factory=lambda: LeadLagTiming(
        leader="crude_and_gas_futures", follower="energy_sector", estimated_lag_hours=4.0, lag_consistency=0.60
    ))
        # Commodity futures (oil/nat gas) move first; energy sector (XLE) responds within hours.
        # When crude rallies sharply, XLE catches up within 2–6 hours typically.

    # ── Additional custom lead-lag relationships ──
    custom_lead_lag_pairs: list[LeadLagTiming] = field(default_factory=list)
        # Space for ad-hoc lead-lag relationships discovered by the distillation layer.
        # Example: "credit_spreads leading XLF" or "semiconductor inventory leading SMH".

    # ── Lead-lag regime shifts ──
    regime_shifts_detected: bool = False  # True if any major lead-lag relationship has inverted
    shifted_relationships: list[str] = field(default_factory=list)
        # Names of relationships whose leader/follower structure has flipped.
        # Example: ["semis_to_tech"] means tech now leading semis (unusual, signals regime change).
    regime_shift_confidence: SignalStrength = SignalStrength.MODERATE

    # ── Missing lag response signals ──
    overdue_lag_responses: list[str] = field(default_factory=list)
        # Lead-lag pairs where the leader has moved significantly but the expected
        # follower has not yet responded. Sorted by how long overdue.
        # Example: ["funding_to_credit_to_equity"] if repo stress spiked but credit spreads
        # haven't widened yet. High-conviction thesis seed.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CorrelationRegimeChange:
    """Q7:7g — Correlation stability, breakdown detection, and dispersion shifts.

    Rolling correlation stability (std dev of trailing estimates), correlation
    breakdown detection, dispersion shifts, narrative lag indicator. Meta-layer
    entity flagging regime transitions before narrative catches up. Delivered to
    synthesizer agent.

    Source: Computed from Q7:7a–7f
    Cadence: Every invocation
    Feasibility: HIGH — 100% derived locally
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Correlation stability measurement ──
    correlation_stability_score: float = 0.0  # 0.0–1.0. Std dev of trailing 20-day rolling
                                         # correlation estimates. 0.0 = perfectly stable,
                                         # 1.0 = high flux. Scores >0.15 = regime in transition.
    stability_trend: str = ""            # "stabilizing" | "destabilizing" | "stable"
    stability_trend_days: int = 0        # How long stability trend has persisted

    # ── Correlation breakdown detection ──
    breakdown_detected: bool = False     # True if rate of change in pairwise correlations
                                         # exceeds historical norms (>2 std dev)
    breakdown_severity: SignalStrength = SignalStrength.WEAK  # How pronounced is the breakdown
    affected_relationship_types: list[str] = field(default_factory=list)
        # Which types of relationships are breaking: "sector_correlations" | "intermarket_relationships" |
        # "sector_rotation_pairs" (multiple can be affected simultaneously)
    breakdown_duration_days: int = 0     # How long the breakdown has persisted

    # ── Dispersion shifts ──
    dispersion_metric: float = 0.0       # Average absolute deviation of returns across universe.
                                         # Low dispersion = names move together. High = independent movement.
                                         # Large increases signal regime transition: what moved together is decoupling.
    dispersion_change_pct: float = 0.0   # % change in dispersion since prior measurement
    dispersion_surge: bool = False       # True if dispersion exceeded trailing 1-year 95th percentile
    dispersion_shift_narrative: str = ""  # "broadening" | "narrowing" | "normal_range"

    # ── Narrative lag indicator ──
    narrative_lag_detected: bool = False  # True if correlation structure has shifted but
                                         # financial media narrative hasn't caught up.
                                         # This is where the synthesizer agent adds value.
    inferred_emerging_regime: str = ""   # The regime that the new correlation structure suggests
                                         # but that hasn't been named in mainstream coverage yet.
                                         # Example: "stagflation_pivot" or "yield_curve_driven_rotation"
    regime_narrative_origin_date: Optional[datetime] = None  # When new correlation pattern started
    days_ahead_of_narrative: int = 0     # Rough estimate of how many days the structure shift is
                                         # ahead of consensus narrative (subjective, guidance for agent)

    # ── Cross-domain regime coherence ──
    sector_correlation_regime: str = ""  # "cohesive" | "fragmenting" | "rotating"
    intermarket_regime: str = ""         # Classification from Q7:7d, for context
    all_regimes_coherent: bool = False   # True if sector, intermarket, and dispersion signals
                                         # are pointing to the same regime transition. Signals high confidence.

    anomalies: list[AnomalyFlag] = field(default_factory=list)
