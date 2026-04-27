"""Q6: Macro and Rates — entity definitions.

7 entities covering yield curve/treasury rates, Fed policy expectations,
inflation metrics, growth indicators, credit conditions, dollar/FX, and
macro event calendar. FRED is the backbone source (free, 120 req/min).

Design principle: "surprise over level" — for the 4-72hr horizon, the surprise
component of macro data is almost always more actionable than the absolute level.
The distillation layer expresses macro data primarily in terms of deviation from
expectations rather than absolute values.

Note: Q6 entities are market-wide, not per-ticker. They carry InvocationMetadata
but no ticker field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ._common import (
    AnomalyFlag,
    Direction,
    InflationRegime,
    InvocationMetadata,
    SignalStrength,
    YieldCurveRegime,
)

__all__ = [
    "BreakevenInflation",
    "ConsumerData",
    "CreditConditions",
    "CurrencyAndDollar",
    "CurrencyLevel",
    "CurveSpread",
    "DollarMoveAttribution",
    "EmploymentData",
    "EventProximityFlag",
    "FedPolicyExpectations",
    "FedProbabilityAssessment",
    "FundingMarketMetrics",
    "FundingStressComposite",
    "GrowthIndicators",
    "HousingData",
    "InflationDataRelease",
    "InflationMetrics",
    "MacroEvent",
    "MacroEventCalendar",
    "PMISubcomponent",
    "PrivateCreditMetrics",
    "PublicCreditMetrics",
    "RatePathExpectation",
    "TreasuryAuctionResult",
    "YieldCurve",
    "YieldLevelAndChange",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class YieldLevelAndChange:
    """A single yield level with rate-of-change metrics at multiple horizons."""

    level: float  # Current yield in basis points (e.g., 4.25%)
    change_1d: float  # Daily change in basis points
    change_5d: float  # 5-day change in basis points
    change_20d: float  # 20-day (monthly) change in basis points
    z_score: float  # How many std devs from 20-day baseline. Volatility signal.
    # z_score > 2.0 = exceptional move deserving explanation.


@dataclass(frozen=True)
class CurveSpread:
    """A single yield curve spread and its dynamics."""

    spread: float  # Spread width in basis points
    change_1d: float  # Daily change
    change_5d: float  # 5-day change
    z_score: float  # Std devs from trailing baseline. Steepening/flattening signal.


@dataclass(frozen=True)
class TreasuryAuctionResult:
    """Results from a single Treasury auction (2Y, 5Y, 10Y, or 30Y)."""

    tenor: str  # "2Y", "5Y", "10Y", "30Y"
    auction_date: date
    auction_yield: float  # Yield at which the auction cleared (bp)
    bid_to_cover: float  # Bid-to-cover ratio. >2.5 is strong, <2.0 is weak.
    tail: float  # Spread between auction yield and when-issued at cutoff (bp).
    # Positive tail = weak demand. Large tail = red flag.
    primary_dealer_pct: float  # Percent of auction absorbed by primary dealers.
    # >25% signals weak end-user demand.
    indirect_pct: float  # Percent to foreign central banks and official institutions.
    direct_pct: float  # Percent to public (direct bidders).
    auction_size_usd: float  # Auction size in billions. For context vs. recent issuance.


@dataclass(frozen=True)
class FedProbabilityAssessment:
    """Market-implied probability distribution over Fed action at next FOMC meeting."""

    probability_hike: float  # Probability of 25bp+ hike (0.0-1.0)
    probability_hold: float  # Probability of unchanged (0.0-1.0)
    probability_cut: float  # Probability of 25bp+ cut (0.0-1.0)
    implied_probability_change_1d: float  # How much the most-likely outcome's prob shifted in 1d.
    # Velocity signal — changes faster than level changes matter.


@dataclass(frozen=True)
class RatePathExpectation:
    """Implied rate expectations over multiple horizons from Fed funds futures."""

    implied_terminal_rate: float  # Where markets expect the Fed funds rate to settle (bp)
    terminal_rate_timing: str  # Expected timing (e.g., "Q3 2025" or "end of tightening cycle")
    expected_hikes_next_12m: float  # Cumulative 25bp hikes priced in next 12 months
    expected_cuts_next_12m: float  # Cumulative 25bp cuts priced in next 12 months
    dot_plot_vs_market_gap: float  # Spread between Fed's latest dot-plot midpoint and market
    # pricing (bp). Positive = market expects lower rates than Fed;
    # large gaps often resolve at FOMC meetings.


@dataclass(frozen=True)
class InflationDataRelease:
    """A single inflation data release (CPI, PPI, or PCE) with surprise scoring."""

    metric: str  # "CPI" | "PPI" | "PCE"
    reported_value: float  # Actual released value (annual % change)
    consensus_estimate: float  # Economist consensus estimate
    surprise_bps: float  # Actual minus estimate (basis points). Positive = hotter than expected.
    surprise_pct: float  # Surprise as % of the actual value. For magnitude normalization.
    release_date: date
    core_value: float | None = None  # Core (ex-food/energy) for CPI/PPI; ex-volatile for PCE
    shelter_pct: float | None = None  # Shelter's contribution to overall (for CPI, major component)
    services_pct: float | None = None  # Services ex-shelter
    goods_pct: float | None = None  # Goods (ex-food/energy)
    energy_pct: float | None = None  # Energy component % change


@dataclass(frozen=True)
class BreakevenInflation:
    """Market-implied inflation expectations from TIPS spreads."""

    breakeven_5y: float  # 5Y breakeven inflation rate (annual %, bp)
    breakeven_10y: float  # 10Y breakeven inflation rate (annual %, bp)
    change_1d_5y: float  # 1-day change in 5Y breakeven (bp)
    change_1d_10y: float  # 1-day change in 10Y breakeven (bp)
    change_5d_5y: float  # 5-day change in 5Y breakeven (bp)
    change_5d_10y: float  # 5-day change in 10Y breakeven (bp)
    z_score_5y: float  # Std devs from trailing 20-day baseline for 5Y
    z_score_10y: float  # Std devs from trailing 20-day baseline for 10Y


@dataclass(frozen=True)
class PMISubcomponent:
    """A single PMI subcomponent (New Orders, Production, Inventories, etc.)."""

    component: str  # "new_orders" | "production" | "employment" |
    # "inventory" | "prices_paid" | "deliveries"
    index: float  # PMI subindex value (0-100, >50 = expansion)
    change_1m: float  # 1-month change. Direction matters more than level.


@dataclass(frozen=True)
class EmploymentData:
    """Latest employment snapshot from monthly NFP or weekly claims."""

    nonfarm_payroll_change: float | None = None  # Monthly change in NFP (thousands)
    unemployment_rate: float | None = None  # Unemployment rate (%, e.g., 3.9)
    wage_growth_yoy: float | None = None  # Wage growth year-over-year (%)
    initial_claims_weekly: float | None = (
        None  # Most recent week initial jobless claims (thousands)
    )
    # Leading indicator, weekly frequency
    adp_private_payroll: float | None = (
        None  # ADP monthly payroll change (thousands). Noisy NFP preview.
    )


@dataclass(frozen=True)
class ConsumerData:
    """Latest consumer-side economic data."""

    retail_sales_yoy: float | None = None  # Retail sales YoY growth (%)
    retail_sales_surprise_bps: float | None = None  # Beat/miss vs. consensus (bp)
    consumer_confidence: float | None = None  # Conference Board or Michigan sentiment score
    confidence_change_1m: float | None = None  # Monthly change in confidence


@dataclass(frozen=True)
class HousingData:
    """Housing market indicators."""

    new_home_sales_monthly: float | None = None  # Monthly new home sales (thousands)
    existing_home_sales_monthly: float | None = None  # Existing home sales (millions)
    building_permits_monthly: float | None = None  # Building permits (thousands)
    housing_starts_monthly: float | None = None  # Housing starts (thousands)


@dataclass(frozen=True)
class PublicCreditMetrics:
    """Investment grade and high-yield credit spread data."""

    ig_spread: float  # IG OAS vs. treasuries (basis points). Baseline risk appetite.
    ig_spread_change_1d: float  # 1-day change (bp)
    ig_spread_change_5d: float  # 5-day change (bp)
    ig_spread_widening_velocity: float  # Bp/day momentum if widening. Velocity often > level.

    hy_spread: float  # HY OAS vs. treasuries (bp). More sensitive to recession risk.
    hy_spread_change_1d: float
    hy_spread_change_5d: float
    hy_spread_widening_velocity: float

    ted_spread: float  # TED spread (LIBOR-Treasury, bp). Interbank stress indicator.
    sofr_treasury_spread: float  # SOFR rate minus comparable treasury yield (bp).
    # Widening signals funding stress or collateral scarcity.

    commercial_paper_rate: float | None = (
        None  # 3M A2/P2 CP rate (%). Short-term corp funding cost.
    )
    cdx_ig_index: float | None = None  # CDX Investment Grade CDS index spread (bp)
    cdx_hy_index: float | None = None  # CDX High Yield CDS index spread (bp)


@dataclass(frozen=True)
class PrivateCreditMetrics:
    """BDC and private lending market health indicators."""

    bdc_price_index: float | None = (
        None  # Weighted index of major BDC prices (ARCC, MAIN, BXSL, etc.)
    )
    bdc_price_change_1d: float = 0.0  # 1-day change (%)
    bdc_price_change_5d: float = 0.0  # 5-day change (%)

    bdc_avg_nav_discount: float | None = (
        None  # Average price-to-NAV discount across major BDCs (%).
    )
    # Widening discount = market distrust of book values.
    bdc_nav_discount_change_1d: float = 0.0  # 1-day change in avg discount (percentage points)

    bdc_vs_ig_divergence: bool = False  # True if BDCs are weakening while IG spreads stable.
    # Early warning: private credit stress before public credit.

    clo_senior_spread: float | None = (
        None  # CLO senior tranche spread (bp). Lower bound of credit stack.
    )
    clo_mezzanine_spread: float | None = None  # CLO mezzanine spread (bp)
    clo_equity_tranche_spread: float | None = None  # CLO equity tranche spread (bp).
    # Widening first = early stress signal.

    direct_lending_default_rate: float | None = None  # Latest available from Cliffwater/KBRA (%).
    direct_lending_recovery_rate: float | None = None  # Average recovery % on defaults
    direct_lending_weighted_avg_yield: float | None = (
        None  # Yield to maturity on private lending books (%)
    )


@dataclass(frozen=True)
class FundingMarketMetrics:
    """Overnight and short-term repo, SOFR, and money market funding conditions."""

    sofr_rate: float  # Current SOFR rate (annual %, bp). Baseline funding rate.
    sofr_change_1d: float  # 1-day change (bp)
    sofr_above_fed_target: float  # SOFR minus Fed funds target midpoint (bp).
    # Positive = collateral scarcity or reserve shortage signal.

    repo_treasury_spread: float  # Overnight repo rate minus comparable treasury yield (bp).
    # Widening = dealer balance sheet stress, collateral hoarding.
    repo_spread_change_1d: float  # 1-day change (bp)
    repo_spread_change_5d: float  # 5-day change (bp)

    term_repo_1w_premium: float | None = None  # 1-week repo rate minus overnight (bp).
    # >0 = term funding stress.
    term_repo_1m_premium: float | None = None  # 1-month term repo premium (bp)

    fed_reverse_repo_facility_usage: float | None = (
        None  # RRP usage (billions). Acts as liquidity floor.
    )
    # High/rising usage = excess reserves, not deployed.
    rrp_change_1d: float | None = None  # 1-day change (billions)
    rrp_change_5d: float | None = None  # 5-day change (billions)

    mmf_govt_assets: float | None = None  # Government money market fund assets (billions).
    # Rising = flight to quality.
    mmf_prime_assets: float | None = None  # Prime money market fund assets (billions).
    # Falling = risk aversion.
    mmf_govt_asset_change_1w: float | None = (
        None  # 1-week change in government MMF assets (billions)
    )
    mmf_prime_asset_change_1w: float | None = None  # 1-week change in prime MMF assets (billions)

    mmf_flow_direction: str = ""  # "flight_to_quality" | "risk_on" | "neutral"
    # Derived from large shifts in govt vs. prime asset flows.


@dataclass(frozen=True)
class FundingStressComposite:
    """High-level funding and credit stress aggregate maintained by distillation layer."""

    stress_score: float  # 0.0-100.0. Composite of SOFR spread, repo spread,
    # term premium, and MMF flow direction. Higher = more stress.
    stress_direction: Direction  # BULLISH (easing) | BEARISH (tightening) | NEUTRAL
    stress_components: list[str] = field(default_factory=list)  # Which components are flashing red
    # e.g., ["repo_spread_widening", "mmf_outflows"]


@dataclass(frozen=True)
class CurrencyLevel:
    """A single currency rate with change and context."""

    rate: float  # Current rate (e.g., 1.0850 for EUR/USD)
    change_1d: float  # 1-day change in the rate (e.g., +0.0025)
    change_1d_pct: float  # 1-day change as % (e.g., +0.23%)
    change_5d: float  # 5-day change (rate points)
    change_5d_pct: float  # 5-day change (%)
    z_score: float  # Std devs from 20-day trailing baseline


@dataclass(frozen=True)
class DollarMoveAttribution:
    """Distillation layer's interpretation of why the dollar moved."""

    primary_driver: str  # "rate_differential" | "risk_sentiment" | "trade_flow" | "mixed"
    rate_differential_score: float  # 0.0-1.0 confidence that rate expectations drove the move
    risk_sentiment_score: float  # 0.0-1.0 confidence that risk-on/risk-off drove it
    trade_flow_score: float  # 0.0-1.0 confidence that trade flows/commodities drove it
    explanation: str = ""  # Human-readable summary for LLM consumption


@dataclass(frozen=True)
class MacroEvent:
    """A single economic event scheduled or recently released."""

    event_name: str  # e.g., "FOMC Decision", "CPI Release", "NFP", "ISM Manufacturing"
    event_datetime: datetime  # UTC timestamp of event (e.g., FOMC decision time, data release time)
    event_type: str  # "fed_decision" | "inflation_data" | "employment" | "growth" | "credit" |
    # "fed_speaker" | "treasury_auction"
    consensus_estimate: float | None = None  # Expected value (if applicable)
    actual_result: float | None = None  # Posted result after release
    surprise_bps: float | None = None  # Actual minus estimate (bp), if applicable
    surprise_direction: Direction = Direction.NEUTRAL  # BULLISH (beat) | BEARISH (miss) | NEUTRAL
    surprise_magnitude: SignalStrength = SignalStrength.NONE  # How large the surprise is
    affected_sectors: list[str] = field(
        default_factory=list
    )  # Sector codes affected (e.g., ["tech", "energy"])


@dataclass(frozen=True)
class EventProximityFlag:
    """Alert that a market-moving event is imminent."""

    next_event: str  # Event name (e.g., "CPI Release")
    hours_until_event: float  # Time remaining until event (hours, float for minutes)
    event_risk_level: SignalStrength  # STRONG (high-impact event), MODERATE, WEAK, NONE


# ── Primary entities ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class YieldCurve:
    """Q6:6a — Treasury yield curve, spreads, real yields, and auction results.

    Key rates (2Y/5Y/10Y/30Y/fed funds), curve spreads (2s10s/2s30s/3m10s),
    rate of change (z-score), real yields (TIPS-implied), curve regime
    classification, treasury auction results.

    The curve's shape, level, and rate of change encode market expectations for
    growth, inflation, and monetary policy simultaneously. Steepening signals
    changing recession/expansion expectations; rate-of-change z-scores identify
    volatility events.

    Source: FRED (free) + Treasury Fiscal Data (free)
    Cadence: Daily+
    Feasibility: HIGH — excellent free coverage
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Key rate levels and dynamics ──
    rate_2y: YieldLevelAndChange  # 2Y yield — monetary policy proxy
    rate_5y: YieldLevelAndChange  # 5Y yield — discount rate for medium-term earnings
    rate_10y: YieldLevelAndChange  # 10Y yield — primary equity discount rate
    rate_30y: YieldLevelAndChange  # 30Y yield — long-term growth/inflation expectations
    fed_funds_rate: YieldLevelAndChange  # Federal funds effective rate — baseline policy rate

    # ── Curve spreads ──
    spread_2s10s: CurveSpread  # 2Y-10Y spread. Steepness of the curve's middle.
    # >0 = normal (upward sloping). <0 = inverted (recession risk).
    spread_2s30s: CurveSpread  # 2Y-30Y spread. Long-end curve shape.
    spread_3m10s: CurveSpread  # 3M-10Y spread. Full curve tilt, earliest inversion signal.

    # ── Real yields (TIPS-implied) ──
    real_yield_5y: float  # 5Y real yield (bp). Rising real yields headwind for growth multiples.
    real_yield_10y: float  # 10Y real yield (bp)
    real_yield_5y_change_1d: float  # 1-day change (bp)
    real_yield_10y_change_1d: float  # 1-day change (bp)

    # ── Curve regime ──
    curve_regime: YieldCurveRegime  # NORMAL | FLAT | INVERTED | STEEPENING | FLATTENING.
    # Regime transitions matter more than steady-state shape.

    # ── Regime confidence and auction results ──
    regime_confidence: float = 1.0  # 0.0-1.0. How clear the regime is (vs. noise). High when
    # spreads are unambiguous; low in the transition zones.

    # ── Most recent treasury auction results (optional) ──
    latest_auction_2y: TreasuryAuctionResult | None = None
    latest_auction_5y: TreasuryAuctionResult | None = None
    latest_auction_10y: TreasuryAuctionResult | None = None
    latest_auction_30y: TreasuryAuctionResult | None = None
    next_scheduled_auction: MacroEvent | None = None  # Upcoming auction with scheduled date/time

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class FedPolicyExpectations:
    """Q6:6b — Fed funds futures-implied rate probabilities and rate path.

    Implied probabilities (hike/cut/hold per FOMC), rate path expectations,
    probability shift velocity, dot plot vs. market pricing gap.

    One of the rare cases where the system has access to a clean, real-time
    probability distribution over a binary outcome. Probability shift velocity
    (how fast the distribution moves) often matters more than the absolute level.

    Source: FRED (free) — derive from fed funds futures
    Fallback: CME FedWatch web tool (free, manual)
    Cadence: FOMC cycle + daily futures
    Feasibility: MEDIUM — no FedWatch API ($25/mo deferred), derivation acceptable
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Probability assessment for next FOMC meeting ──
    next_fomc_meeting_date: date  # Date of the next FOMC decision
    next_fomc_probabilities: FedProbabilityAssessment

    # ── 12-month rate path expectations ──
    rate_path_12m: RatePathExpectation

    # ── Probability shift velocity (momentum) ──
    probability_change_velocity_1d: float  # How fast the most-likely outcome's probability shifted
    # in the last day (percentage points/day). Higher velocity
    # = more rapid repricing. Often more actionable than level.
    probability_change_velocity_5d: float  # 5-day velocity

    # ── Recent FOMC meeting history ──
    last_fomc_decision_date: date
    last_fomc_decision: str  # "hike" | "cut" | "hold"
    last_fomc_decision_size_bps: int  # Size of last move (25, 50, 75 bp), if any

    # ── Fed speaker impact (optional) ──
    next_fed_speaker_event: MacroEvent | None = (
        None  # Upcoming Chair/Vice Chair/FOMC member appearance
    )
    recent_speaker_impact: float | None = (
        None  # Market reaction to most recent speaker appearance (bp move)
    )

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class InflationMetrics:
    """Q6:6c — CPI/PPI/PCE with breakevens, surprise scoring, and regime classification.

    Breakeven inflation (5Y/10Y TIPS), CPI/PPI/PCE surprise (magnitude + direction),
    core vs. headline decomposition (shelter, services, goods, energy),
    inflation regime classification.

    For the 4-72hr horizon, the surprise component is almost always more
    actionable than the level. A 0.2% surprise in CPI is more tradeable than
    the absolute 3.2% headline number.

    Source: FRED (free) + BLS API (free)
    Cadence: Monthly (data release)
    Feasibility: HIGH — CPI, PPI, PCE, breakevens all on FRED
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Breakeven inflation (real-time market expectations) ──
    breakeven_inflation: BreakevenInflation

    # ── Inflation regime classification ──
    inflation_regime: InflationRegime  # HOT | COOLING | STABLE | DEFLATION_RISK.
    # Regime transitions change the playbook for rate expectations.

    # ── Latest inflation data releases ──
    latest_cpi: InflationDataRelease | None = None
    latest_ppi: InflationDataRelease | None = None
    latest_pce: InflationDataRelease | None = None

    # ── Regime and trend confidence ──
    regime_confidence: float = 0.8  # 0.0-1.0. How clear is the current regime classification.

    # ── Directional trend (accelerating vs. decelerating) ──
    inflation_trend: Direction = (
        Direction.NEUTRAL
    )  # BULLISH (accelerating) | BEARISH (decelerating) | NEUTRAL
    trend_basis: str = ""  # Brief explanation (e.g., "breakevens drifting higher between releases")

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class GrowthIndicators:
    """Q6:6d — PMI, employment, consumer data, housing, and growth surprise index.

    PMI/ISM (mfg + services, subcomponents), employment (claims, NFP, ADP),
    consumer data (retail sales, confidence), housing (sales, permits, starts),
    growth surprise index (Citi ESI-like aggregate).

    High-frequency data that tells you whether the economy is expanding or
    contracting — not for macro forecasting, but for detecting when the market's
    growth narrative is about to shift. New Orders PMI is the leading
    subcomponent.

    Source: FRED (free) + BLS API (free)
    Cadence: Monthly/quarterly
    Feasibility: HIGH — comprehensive free coverage
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── PMI/ISM indicators ──
    pmi_manufacturing_headline: float | None = (
        None  # ISM Manufacturing PMI headline (0-100, >50 = expansion)
    )
    pmi_manufacturing_change_1m: float | None = None  # 1-month change
    pmi_manufacturing_subcomponents: list[PMISubcomponent] = field(default_factory=list)
    # New Orders (leading), Production, Employment, Inventory, Prices Paid, Deliveries

    pmi_services_headline: float | None = None  # ISM Services PMI headline
    pmi_services_change_1m: float | None = None
    pmi_services_subcomponents: list[PMISubcomponent] = field(default_factory=list)

    # ── Employment data ──
    employment_latest: EmploymentData = field(default_factory=EmploymentData)

    # ── Consumer data ──
    consumer_latest: ConsumerData = field(default_factory=ConsumerData)

    # ── Housing data ──
    housing_latest: HousingData = field(default_factory=HousingData)

    # ── Growth surprise index (aggregate of beat/miss across all growth indicators) ──
    growth_surprise_index: float = 0.0  # -100.0 to +100.0. Like Citi Economic Surprise Index (ESI).
    # >0 = data beating expectations (consensus too pessimistic).
    # <0 = data missing expectations (consensus too optimistic).
    growth_surprise_trend: Direction = (
        Direction.NEUTRAL
    )  # Is the ESI rising (data improving vs. consensus)
    # or falling (data deteriorating)?

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CreditConditions:
    """Q6:6e — IG/HY spreads, funding markets, and financial stress indicators.

    Covers public credit (IG/HY spreads, CDS, TED), private credit (BDCs, CLOs,
    direct lending), and funding/repo markets (SOFR, reverse repo, MMF flows).

    The risk appetite barometer. Credit markets are often earlier than equities
    at detecting stress — IG spread widening typically leads equity weakness by
    days. For financials specifically, credit conditions directly affect earnings.

    Funding market dislocations lead credit spread widening, which leads equity
    weakness, making funding the earliest link in the stress transmission chain.

    Source: FRED (free)
    Cadence: Daily
    Feasibility: HIGH — ICE BofA spreads, STLFSI, SOFR all on FRED
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Public credit ──
    public_credit: PublicCreditMetrics

    # ── Private credit (BDC and CLO) ──
    private_credit: PrivateCreditMetrics = field(default_factory=PrivateCreditMetrics)

    # ── Funding and repo markets ──
    funding_markets: FundingMarketMetrics = field(default_factory=FundingMarketMetrics)

    # ── Composite funding stress score ──
    funding_stress_composite: FundingStressComposite = field(default_factory=FundingStressComposite)

    # ── Credit regime classification ──
    credit_regime: str = "normal"  # "normal" (spreads <300bp IG, <600bp HY) | "elevated" |
    # "stressed" (spreads >400bp IG, >700bp HY) | "crisis"
    credit_regime_confidence: float = 0.8  # 0.0-1.0

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CurrencyAndDollar:
    """Q6:6f — DXY, major currency pairs, and dollar move attribution.

    DXY (level, change, z-score), EUR/USD, USD/JPY (carry proxy), USD/CNY
    (China growth proxy), USD/commodity-currency crosses, dollar move attribution.

    The system doesn't trade FX, but dollar moves affect the entire universe
    through three distinct transmission channels:
    1. Earnings translation (tech with high international revenue)
    2. Commodity pricing (oil/gas priced in dollars; dollar strength = commodity headwind)
    3. Global financial conditions (carry trade via USD/JPY; yen unwind = de-risking)

    Source: FRED (free) or Polygon Stocks Starter (forex)
    Fallback: Yahoo Finance
    Cadence: Daily+
    Feasibility: HIGH — DXY and major pairs available
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── US Dollar Index (DXY) ──
    dxy: CurrencyLevel  # Basket of major currencies weighted by trade volume

    # ── Major currency pairs ──
    eur_usd: CurrencyLevel  # EUR/USD. EUR is largest DXY component; Eurozone recession fears
    # strengthen dollar through this cross.
    usd_jpy: CurrencyLevel  # USD/JPY. Carry trade proxy — sharp yen strengthening (unwind)
    # is a de-risking signal for all US equities.
    usd_cny: CurrencyLevel  # USD/CNY. China growth proxy; CNY weakness ripples into semis
    # (supply chain) and energy (demand expectations).

    # ── Commodity-currency crosses (optional, for energy correlation tracking) ──
    usd_cad: CurrencyLevel | None = (
        None  # USD/CAD. Energy-linked proxy; CAD weakness = commodity weakness
    )
    usd_nok: CurrencyLevel | None = None  # USD/NOK. Norwegian krone; energy-sensitive

    # ── Dollar move attribution ──
    dollar_move_attribution: DollarMoveAttribution = field(
        default_factory=lambda: DollarMoveAttribution(
            primary_driver="mixed",
            rate_differential_score=0.33,
            risk_sentiment_score=0.33,
            trade_flow_score=0.33,
            explanation="",
        )
    )

    # ── DXY-commodity correlation regime ──
    dxy_commodity_correlation: str = "normal"  # "positive" (DXY up = commodities down) |
    # "negative" (diverging) | "neutral" (low correlation)
    correlation_strength: float = 0.5  # Abs value of correlation coefficient (0.0-1.0)

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class MacroEventCalendar:
    """Q6:6g — Economic event calendar with consensus, surprise scoring, and proximity.

    Event dates (FOMC, CPI, NFP, ISM, GDP, etc.), consensus estimates,
    macro surprise index, event proximity flags (within N hours), historical
    event impact by sector.

    The scheduling and scoring layer that ties everything else together. The
    system needs to know not just what data is coming but when, and how recent
    releases have compared to expectations.

    Source: Finnhub (free tier)
    Cadence: Daily
    Feasibility: HIGH — economic calendar from Finnhub free tier
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Upcoming events in the next 7 days ──
    upcoming_events: list[MacroEvent] = field(default_factory=list)
    # Chronologically sorted. Major events: FOMC decisions, CPI/PPI/PCE, NFP,
    # ISM/PMI, GDP, Fed speaker appearances, Treasury auctions.

    # ── Imminent event proximity flag ──
    event_proximity_alerts: list[EventProximityFlag] = field(default_factory=list)
    # Events within N hours (distillation layer configurable, typically 24-48 hours).
    # The sector analysts and Portfolio manager need to know when event risk is imminent.

    # ── Running macro surprise index ──
    macro_surprise_index: float = 0.0  # Similar to Citi Economic Surprise Index (ESI).
    # Aggregate score of whether recent data has been beating
    # or missing expectations. Positive = data stronger than consensus;
    # negative = data weaker than consensus. Updated every data release.
    surprise_index_trend: Direction = (
        Direction.NEUTRAL
    )  # Is the surprise index rising (improving data flow)
    # or falling (deteriorating)?
    surprise_index_change_5d: float = (
        0.0  # 5-day change in the ESI (how fast perception is shifting)
    )

    # ── Historical event impact by sector (lookup table) ──
    event_impact_by_sector: dict[str, float] = field(default_factory=dict)
    # Map of event_type → avg sector-specific reaction (multiplier on absolute market reaction).
    # e.g., {"hot_cpi": {"tech": 1.8, "semis": 1.2, "financials": 0.8, "energy": 0.6}}
    # Helps the analyst size sector-specific theses around events.

    # ── Most recent surprise release ──
    latest_release: MacroEvent | None = None

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
