"""Qual 3: Prediction Markets — entity definitions.

3 entities covering monetary policy outcome markets, regulatory/political
outcome markets, and geopolitical outcome markets. Polymarket (free) and
Kalshi (free, CFTC-regulated) are both available with generous rate limits.

DESIGN NOTES:
- Prediction markets express expectations as tradeable prices — conviction
  weighted by money at risk. More reliable than surveys.
- Delta over level: rate of change in odds (24hr, 7d shifts) is more actionable
  than absolute levels. A 15pp swing overnight signals new information.
- Market-wide entities: no ticker field. Outcomes are macro/policy-level and
  affect multiple tickers across sectors.
- Cross-reference divergences: prediction market vs futures (Q6:6b rates, Q8:8a oil)
  indicates mispricing opportunities.
- Liquidity matters: low-liquidity contracts can be outliers. Track contract_liquidity_usd.
- Platforms: Polymarket (free, 300 req/10s), Kalshi (free, CFTC-regulated),
  Metaculus (free). Normalize probabilities across platforms when same outcome
  is traded on multiple venues.
- Ingestion cadence: every invocation (lightweight API calls). Distillation layer
  computes deltas and flags >5pp moves as anomalies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
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


# ── Supporting types for MonetaryPolicyOutcome ─────────────────────────────────

@dataclass(frozen=True)
class RateProbabilityPair:
    """A rate level and its probability from a terminal rate distribution.

    Used to represent the full probability distribution of where the fed funds
    rate will settle at some terminal point (e.g., end of 2026). For example:
    - rate_bps=450, probability=0.25 means 25% chance fed funds end at 450bp.
    - rate_bps=500, probability=0.40 means 40% chance fed funds end at 500bp.

    Why: Distributions are more informative than point estimates. They show
    where tail risk lies and whether the market expects a wide range of outcomes
    or a narrow consensus.
    """
    rate_bps: int                    # Basis points (e.g., 450 = 4.50%)
    probability: float               # Probability mass at this level (0.0–1.0)


@dataclass(frozen=True)
class FOMCMeetingOdds:
    """Per-meeting odds for an upcoming FOMC decision.

    Every FOMC meeting, prediction markets price three mutually exclusive outcomes:
    hike (increase fed funds rate), cut (decrease), or hold (no change). These odds
    must sum to ~100% (allowing for rounding and bid-ask spread).

    Why: Individual meeting odds are the granular input. Terminal rate distributions
    are computed from the path of meeting-by-meeting odds. Days until meeting helps
    the system prioritize which meetings are most relevant right now.
    """
    meeting_date: date               # FOMC meeting date (e.g., 2026-03-15)
    probability_hike: float          # P(rate hike) (0.0–1.0)
    probability_cut: float           # P(rate cut) (0.0–1.0)
    probability_hold: float          # P(no change) (0.0–1.0)
    expected_bp_magnitude: int       # Expected magnitude if hike/cut (e.g., 25 for 0.25%)
    days_until_meeting: int          # Days from now until this meeting
    market_consensus_action: str     # "hike" | "cut" | "hold" — the most likely outcome


# ── Supporting types for RegulatoryPoliticalOutcome ────────────────────────────

@dataclass(frozen=True)
class PolicyContract:
    """A single regulatory/political outcome contract on a prediction market.

    Example contracts:
    - "Will the FTC block the Oracle-TikTok merger?" (antitrust)
    - "Will the US impose 25%+ tariffs on semiconductors?" (trade)
    - "Will the SEC approve spot bitcoin ETF by end of 2026?" (financial_reg)
    - "Will corporate tax rate drop below 18% by 2027?" (tax)

    Why: Policy risks are diffuse and affect multiple tickers. Prediction market
    prices aggregate all available information about regulatory outcomes. Tracking
    per-contract liquidity, deltas (24hr and 7d), and affected tickers lets us
    model cross-sectional policy risk exposure.

    Delta fields: 24hr and 7d shifts are more actionable than absolute levels.
    A contract that was 30% likely 7 days ago but now 50% means the market
    repriced materially.
    """
    contract_id: str                 # Polymarket or Kalshi contract ID (unique identifier)
    description: str                 # Human-readable contract title
    category: str                    # "antitrust" | "trade" | "financial_reg" | "tax" | "election"
    probability: float               # Current market probability (0.0–1.0)
    delta_24hr: float                # Change in probability over last 24 hours (pp)
    delta_7d: float                  # Change in probability over last 7 days (pp)
    platform: str                    # "polymarket" | "kalshi" | "metaculus"
    contract_liquidity_usd: float    # USD liquidity depth (larger = more reliable)
    prior_probability: Optional[float] = None  # Probability from 7d ago (to compute delta)
    affected_tickers: list[Ticker] = field(default_factory=list)  # Which tickers this impacts
    affected_sectors: list[AlphaMindSector] = field(default_factory=list)  # Which sectors
    resolution_date: Optional[date] = None  # When this contract will resolve (if known)


# ── Supporting types for GeopoliticalOutcome ──────────────────────────────────

@dataclass(frozen=True)
class GeopoliticalContract:
    """A single geopolitical outcome contract (conflict, sanctions, OPEC decisions).

    Example contracts:
    - "Will there be a military action in the Taiwan Strait by Q3 2026?" (theater=taiwan_strait)
    - "Will OPEC+ announce production cuts in their next meeting?" (opec)
    - "Will new sanctions on Russia be announced within 30 days?" (sanctions)

    Why: Geopolitical risk is highly tradeable (futures, oil complex, semis/energy
    tickers are sensitive to supply disruptions). Prediction markets aggregate
    geopolitical expectations. Theater field helps us scope Taiwan-specific
    semiconductor supply risks vs Middle East oil supply risks separately.

    Escalation level: OPEC decisions and conflict states can be classified into
    levels of severity. Helps the system prioritize which geopolitical outcomes
    matter most for its portfolio.
    """
    contract_id: str                 # Unique market identifier
    description: str                 # Contract title
    theater: str                     # "taiwan_strait" | "middle_east" | "russia_ukraine" | "china_economic" | "opec"
    escalation_level: str            # "low" | "medium" | "high" | "critical" — severity classification
    probability: float               # Current market probability (0.0–1.0)
    delta_24hr: float                # Change in probability over last 24 hours (pp)
    delta_7d: float                  # Change in probability over last 7 days (pp)
    platform: str                    # "polymarket" | "kalshi" | "metaculus"
    contract_liquidity_usd: float    # USD liquidity (higher = more reliable)
    prior_probability: Optional[float] = None  # Probability from 7d ago
    affected_tickers: list[Ticker] = field(default_factory=list)  # Tickers most sensitive
    affected_sectors: list[AlphaMindSector] = field(default_factory=list)  # Sectors affected
    resolution_date: Optional[date] = None  # When contract resolves


# ── Main entities ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MonetaryPolicyOutcome:
    """Qual 3:3a — Fed rate decision probabilities from prediction markets.

    Fed funds rate decision probabilities (hike/cut/hold per FOMC), basis point
    magnitude expectations, terminal rate probability distribution, inter-meeting
    action probability, prediction market vs. futures divergence.

    This is a MARKET-WIDE entity (no ticker field). It captures the market's
    consensus expectations about Fed policy over the next 6–24 months.

    Data is ingested from Polymarket and Kalshi APIs every invocation cycle.
    The distillation layer computes 24hr and 7d deltas, flags >5pp moves, and
    compares prediction market odds to fed funds futures (Q6:6b) to identify
    divergences (mispricing opportunities).

    Why: Fed rate expectations are a primary driver of equity valuations, sector
    rotation, and carry costs. Prediction markets are more reliable than Fed
    guidance because money is at risk. Terminal rate distribution shows where
    the market thinks rates will end up, not just the next 1–2 meetings.
    Inter-meeting action probability flags crisis tail risk (emergency cuts).

    Source: Polymarket API (free, 300 req/10s) + Kalshi API (free, CFTC-regulated)
    Fallback: Metaculus (free)
    Cadence: Every invocation (~30 min)
    Feasibility: HIGH
    """
    upcoming_fomc_meetings: list[FOMCMeetingOdds]
    """List of upcoming FOMC meetings with odds for hike/cut/hold.

    Ordered chronologically. Each meeting is independent but the path of meetings
    determines where the fed funds rate settles. Used to build the terminal rate
    distribution and identify turning points in policy.
    """

    terminal_rate_median: int
    """Median terminal rate across all probability-weighted outcomes (basis points).

    Example: if the market expects fed funds to settle at 450bp at end of 2026,
    this field is 450. Single-point summary of the full distribution. More
    actionable for traders than the median of the median, but the full distribution
    (terminal_rate_distribution) provides tail risk information.
    """

    inter_meeting_action_probability: float
    """Probability of emergency Fed action (cut or hike) between scheduled FOMC meetings.

    Range: 0.0–1.0. Used to flag tail risk of crisis response. If this is >20%,
    the system should widen stop losses and reduce long-duration positions.
    Zero or near-zero in normal regimes; spikes during financial stress.
    """

    prediction_market_vs_futures_divergence: bool
    """True if prediction market odds diverge materially from fed funds futures curve (Q6:6b).

    Prediction markets and futures express the same thing but via different mechanisms.
    If they diverge significantly (e.g., markets price 60% chance of hike, futures price
    40%), it signals either arbitrage opportunity or that one market is better informed.
    """

    primary_source: str
    """Which prediction market platform has the deepest liquidity for fed rate contracts.

    Values: "polymarket" | "kalshi". Ingestion layer prioritizes this platform's
    contracts when liquidity is concentrated.
    """

    contract_liquidity_usd: float
    """Total USD liquidity depth across all fed rate contracts on the primary source.

    Larger = more reliable. Contracts with <$10k liquidity can be outliers.
    Used to discount low-liquidity estimates.
    """

    metadata: InvocationMetadata
    """Pipeline invocation metadata (when collected, confidence, vol regime, etc)."""

    terminal_rate_distribution: list[RateProbabilityPair] = field(default_factory=list)
    """Probability distribution of terminal fed funds rate at end of policy cycle.

    Example: [RateProbabilityPair(rate_bps=400, probability=0.15),
              RateProbabilityPair(rate_bps=450, probability=0.40),
              RateProbabilityPair(rate_bps=500, probability=0.35),
              RateProbabilityPair(rate_bps=550, probability=0.10)]

    Why: Allows the system to assess tail risk. If there's a 10% chance rates go
    to 550bp, carry costs and long-duration exposure could face severe drawdown.
    Distributions are richer than point estimates.
    """

    divergence_description: Optional[str] = None
    """Plain-English description of the divergence (if one exists).

    Example: "Markets price 60% chance of 25bp hike in March meeting; fed funds
    futures price 35%. Markets may be over-weighting hawkish dot plot comments."
    """

    divergence_magnitude_bp: Optional[int] = None
    """Magnitude of divergence in basis points of implied rate (if divergence is True).

    Example: if futures price 450bp terminal rate but markets price 475bp, this is 25bp.
    Larger divergences are more actionable.
    """

    rate_path_shift_24hr: Optional[float] = None
    """24-hour shift in the expected rate path (basis points).

    Example: yesterday the market expected 450bp terminal rate, today 465bp.
    This field is +15bp. More actionable than the absolute level because it tells
    us new information arrived. A +30bp shift overnight is a big repricing event.

    Why delta over level: the system cares about what changed, not what the
    absolute level is. Rate expected to 450bp is not news by itself; but going
    from 450bp to 480bp overnight is.
    """

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., odds swing >5pp in 24hr, divergence emergence).

    Examples:
    - "rate_shift_spike": terminal rate expectations shifted +30bp in 24hr (z_score=2.5)
    - "divergence_emerged": prediction markets and futures diverged by 50bp
    - "extreme_tail_probability": inter-meeting action probability spiked to 45%
    """


@dataclass(frozen=True)
class RegulatoryPoliticalOutcome:
    """Qual 3:3b — Regulatory, trade, and political event probability markets.

    Antitrust outcomes (enforcement, merger approval), trade policy (tariffs,
    export controls), financial regulation (capital requirements, crypto),
    tax policy (corporate rate changes), election/transition markets.

    This is a MARKET-WIDE entity (no ticker field). It captures the market's
    expectations about regulatory and political outcomes that affect multiple
    tickers across sectors.

    Why: Regulatory risk is heterogeneous across the portfolio. A tariff regime
    shift affects Energy and Tech differently than Semis. An antitrust action
    targets specific companies. Prediction markets price these outcomes at high
    granularity (per-contract) and update in real-time.

    Source: Polymarket API (free) + Kalshi API (free, CFTC-regulated)
    Fallback: Metaculus (free)
    Cadence: Every invocation (~30 min)
    Feasibility: HIGH
    """
    metadata: InvocationMetadata
    """Pipeline invocation metadata."""

    active_contracts: list[PolicyContract] = field(default_factory=list)
    """All active regulatory/political outcome contracts currently tracked.

    Includes antitrust, trade, financial regulation, tax, and election markets.
    Each contract specifies which tickers and sectors it affects. Ordered by
    resolution date (nearest-term first).
    """

    high_delta_contracts: list[PolicyContract] = field(default_factory=list)
    """Contracts where probability shifted >5pp in last 24 hours.

    Filtered subset of active_contracts. New information arrived on these outcomes.
    The distillation layer flags >5pp moves as anomalies. Useful for identifying
    where the market's expectations shifted most dramatically overnight.

    Example: antitrust contract shifted from 35% to 42% (7pp move) after court
    filing leaked. Suggests agents should re-evaluate tech/semi merger risk.
    """

    sector_impact_summary: dict[str, str] = field(default_factory=dict)
    """Per-sector assessment of net regulatory risk direction.

    Keys: "tech", "semis", "financials", "energy"
    Values: "bullish", "bearish", "neutral", "mixed"

    Example:
    - tech: "bearish" (antitrust, export controls shifting unfavorably)
    - semis: "mixed" (export controls unfavorable, but tariff on China exports bullish)
    - financials: "neutral" (no major regulatory catalyst in near term)
    - energy: "bullish" (reduced regulatory scrutiny on carbon rules)

    Why: Distilled judgment for quick routing to agents. Each agent gets a
    sector-level risk assessment so they know which policy headwinds/tailwinds
    apply to their universe.
    """

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., contract probability spike, sector sentiment shift).

    Examples:
    - "policy_contract_repriced": tariff contract jumped 8pp on trade war escalation
    - "sector_risk_rotation": financial regulation contracts moved sharply bearish
    - "divergence_across_platforms": same outcome priced 55% on Polymarket, 40% on Kalshi
    """


@dataclass(frozen=True)
class GeopoliticalOutcome:
    """Qual 3:3c — Geopolitical conflict, sanctions, and OPEC decision markets.

    Conflict escalation/de-escalation probabilities, sanctions/trade disruption
    outcomes, OPEC decision outcomes (production cuts/increases/holds).

    This is a MARKET-WIDE entity (no ticker field). It captures the market's
    expectations about geopolitical shocks that affect energy supply, semiconductor
    supply chains, and global trade.

    Why: Geopolitical risk is highly tradeable and influences specific sectors
    asymmetrically. Taiwan Strait escalation directly threatens semiconductor
    supply (Semis sector). OPEC decisions move oil futures, which affect Energy
    and Energy-correlated names. Russia/Ukraine sanctions affect energy supply
    and global trade. Prediction markets aggregate all available information and
    update in real-time.

    Source: Polymarket API (free) + Kalshi API (free, CFTC-regulated)
    Fallback: Metaculus (free)
    Cadence: Every invocation (~30 min)
    Feasibility: HIGH
    """
    opec_futures_divergence: bool
    """True if OPEC prediction market odds diverge materially from oil futures curve (Q8:8a).

    OPEC production decisions and oil price expectations are tradeable on both
    prediction markets and futures. If they diverge significantly, it signals
    mispricing or that one market is better informed. Example: markets price 70%
    chance of production cut, but WTI futures price in only 40% probability.
    """

    energy_supply_risk_level: str
    """Composite risk assessment of energy supply disruption (affects Energy sector).

    Values: "low" | "moderate" | "high" | "critical"

    Factors considered:
    - OPEC decision probability (cuts lower supply, increase prices)
    - Russia/Middle East conflict escalation (supply chain disruption)
    - Sanctions risk (export restrictions)
    - Overall geopolitical tension index

    Used for position sizing and hedging decisions in Energy names.
    """

    semiconductor_supply_risk_level: str
    """Composite risk assessment of semiconductor supply disruption (affects Semis sector).

    Values: "low" | "moderate" | "high" | "critical"

    Factors considered:
    - Taiwan Strait escalation probability
    - China economic coercion risk
    - Sanctions risk (affecting TSMC, Samsung supply chains)
    - Overall geopolitical tension in Indo-Pacific

    Used for position sizing and hedging decisions in Semi names, especially
    those with Taiwan/South Korea supply chain exposure.
    """

    metadata: InvocationMetadata
    """Pipeline invocation metadata."""

    active_contracts: list[GeopoliticalContract] = field(default_factory=list)
    """All active geopolitical outcome contracts across all theaters.

    Includes conflict escalation (Taiwan, Middle East, Russia/Ukraine), sanctions,
    economic coercion (China), and OPEC decisions. Ordered by resolution date.
    """

    high_delta_contracts: list[GeopoliticalContract] = field(default_factory=list)
    """Contracts where probability shifted >5pp in last 24 hours.

    Filtered subset of active_contracts. Used to flag where geopolitical risk
    repriced overnight. A Taiwan escalation contract that jumped from 15% to 25%
    signals a material change in market perception of conflict risk.
    """

    opec_decision_contracts: list[GeopoliticalContract] = field(default_factory=list)
    """Contracts related to OPEC decisions (production cuts, holds, increases).

    Filtered subset of active_contracts where theater == "opec". OPEC decisions
    are critical for oil futures (Q8:8a) and Energy sector profitability. This
    list is isolated for easy routing to energy analysts.
    """

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., conflict escalation spike, OPEC divergence, Taiwan risk).

    Examples:
    - "taiwan_escalation_repriced": Taiwan Strait contract jumped 12pp on military activity
    - "opec_divergence_emerged": OPEC production cut odds diverged 30pp from oil futures
    - "supply_chain_risk_spike": semiconductor supply risk jumped to "critical"
    - "geopolitical_volatility_spike": multiple theaters repriced simultaneously
    """
