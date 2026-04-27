"""Qual 6: Sector-Specific Qualitative Catalysts — entity definitions.

3 entities covering tech/semiconductor, energy, and financials sector-specific
catalysts. Each entity captures the unique qualitative signals and trade press
intelligence relevant to its sector within our ~65-ticker universe.

Design principles:
    - Each entity is SECTOR-SCOPED: TechSemiCatalyst flows to tech/semis analyst,
      EnergyCatalyst to energy analyst, FinancialsCatalyst to financials analyst.
    - Supporting types precede main entities.
    - All entities are frozen dataclasses (immutable snapshots).
    - Every entity has metadata: InvocationMetadata and anomalies: list[AnomalyFlag].
    - Extensive docstrings explain sourcing, cadence, and interpretation guidance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ._common import (
    AlphaMindSector,
    AnomalyFlag,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
)

__all__ = [
    "CapexSignal",
    "CompetitiveShift",
    "EnergyCatalyst",
    "FinancialsCatalyst",
    "OPECStatement",
    "ProductCycle",
    "RegulatoryShift",
    "TechSemiCatalyst",
]


# ── TECHSEMICATALYST supporting types ────────────────────────────────────────


@dataclass(frozen=True)
class CapexSignal:
    """Per-company AI/capex commentary from earnings calls, investor presentations,
    or trade press interviews. Captures the direction and key evidence."""

    ticker: Ticker  # Company ticker (e.g., "MSFT", "GOOG", "META")
    commentary_direction: Direction  # BULLISH/BEARISH/NEUTRAL on AI capex trajectory
    key_statement: str  # Direct quote or paraphrased evidence
    capex_change_direction: Direction  # BULLISH/BEARISH/NEUTRAL on capex delta vs. prior period


@dataclass(frozen=True)
class ProductCycle:
    """Active semiconductor or AI infrastructure product cycle stage and market impact."""

    ticker: Ticker  # Company ticker
    product_name: str  # Human-readable product name (e.g., "Blackwell", "Maia 2")
    cycle_stage: str  # "pre_launch" | "launch" | "ramp" | "maturity" | "decline"
    significance: SignalStrength  # STRONG/MODERATE/WEAK impact on sector narrative
    description: str | None = None  # Additional context


@dataclass(frozen=True)
class CompetitiveShift:
    """Market share or competitive position change in a narrowly-scoped market segment."""

    ticker: Ticker  # Company gaining or losing share
    description: str  # Qualitative description of the shift
    market_share_direction: Direction  # BULLISH/BEARISH/NEUTRAL on this company's share
    significance: SignalStrength  # STRONG/MODERATE/WEAK market impact
    affected_segment: str | None = None  # Market segment (e.g., "AI inference chips", "DPU market")


# ── ENERGYCATALYST supporting types ──────────────────────────────────────────


@dataclass(frozen=True)
class OPECStatement:
    """Individual OPEC member or group statement on production intentions, compliance,
    or market expectations. Sourced from official meetings, press releases, or
    statements by key leaders."""

    speaker: str  # Person or entity (e.g., "Saudi Energy Minister")
    role: str  # Official role or affiliation
    statement_date: datetime  # UTC timestamp of statement
    tone: Direction  # BULLISH (supportive of cuts) | BEARISH (willing to increase) | NEUTRAL
    key_statement: str  # Direct quote or paraphrased message
    confidence: SignalStrength = SignalStrength.STRONG  # How authoritative this statement is


# ── FINANCIALSCATALYST supporting types ────────────────────────────────────


@dataclass(frozen=True)
class RegulatoryShift:
    """Discrete regulatory change: new rule, enforcement action, appointee impact,
    or policy shift. Sourced from SEC notices, Fed announcements, or agency filings."""

    description: str  # Human-readable regulatory change
    effective_date: datetime | None = None  # UTC timestamp when change takes effect
    affected_sectors: list[str] = field(
        default_factory=list
    )  # Which subsectors (e.g., "fintech", "crypto")
    expected_impact: str | None = None  # Qualitative impact assessment


# ── MAIN ENTITIES ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TechSemiCatalyst:
    """Qual 6:6a — AI/semiconductor narrative, supply chain, and product cycle signals.

    Sector-scoped for TECH and SEMIS analysts. Captures:
      - AI spending signals (hyperscaler capex, startup funding, enterprise adoption)
      - Supply chain intelligence (lead times, fab utilization, inventory)
      - Product cycle dynamics (launches, platform shifts)
      - Competitive dynamics (market share moves)
      - Export controls (regulatory status, affected companies)

    This entity flows exclusively to the tech/semis analyst agent, enabling
    domain-specific interpretation of AI spending narrative (the primary driver
    of current AI trade thesis).

    Source: SIA monthly sales (free) + SEMI equipment data (free) + DigiTimes
            (mostly free) + BIS export controls (free gov) + HPCwire/Tom's Hardware RSS
    Fallback: SemiAnalysis (~$30/mo), TechInsights (expensive)
    Cadence: Every invocation (hourly)
    Feasibility: MEDIUM — structured free data pipeline from SIA, SEMI, DigiTimes, BIS

    Interpretation notes for analyst:
      - AI_narrative_arc is the single highest-weight narrative axis. "accelerating"
        vs. "plateau" vs. "bubble_concerns" entirely reframes the capex conversation.
      - Hyperscaler capex sentiment is the leading indicator. Individual company
        statements matter more than analyst consensus estimates.
      - Supply chain tightness directly constrains upside AI capex forecasts.
        A 6-month lead time on memory or compute limits near-term expansion.
      - Export controls create a two-tier market (US-compliant vs. China-focused).
        Affected ticker revenue impact is asymmetric.
    """

    sector: AlphaMindSector  # Always TECH or SEMIS

    # ── AI Spending Signals ──────────────────────────────────────────────────
    hyperscaler_capex_sentiment: Direction
    # Aggregate direction of hyperscaler AI capex commentary across MSFT, GOOG,
    # AMZN, META. Synthesizes earnings call rhetoric, investor day statements,
    # and trade press interviews.
    ai_startup_funding_trend: Direction
    # Directional assessment of venture-stage AI company funding activity.
    # BULLISH: elevated funding rounds, higher valuations. BEARISH: drying up.
    enterprise_ai_adoption_trend: Direction
    # Assessment of non-hyperscaler enterprise AI adoption pace.
    # Based on SaaS earnings commentary, CRO guidance, and analyst reports.
    ai_narrative_arc: str
    # Qualitative framing of the AI narrative cycle. One of:
    #   "sustainable_growth" — AI as structural shift in compute spending
    #   "bubble_concerns" — Valuations exceed capex run-rate expectations
    #   "accelerating" — Upside inflection, capex guidance raising
    #   "plateau" — Growth moderating, saturation signals emerging
    #   "mixed" — No consensus narrative (conflicting signals)

    # ── Supply Chain Intelligence ────────────────────────────────────────────
    semiconductor_lead_times_trend: Direction
    # Directional assessment of wafer/memory lead times. BULLISH: tightening
    # (constrained supply, favorable pricing). BEARISH: extending (glut).
    fab_utilization_rate_trend: Direction
    # Directional assessment of fab utilization across key producers (TSMC,
    # Samsung, Intel). BULLISH: high utilization, pricing power. BEARISH: slack.
    inventory_level_assessment: str
    # Qualitative inventory state across channel (distributors, OEMs, system
    # integrators). One of:
    #   "lean" — Low inventory, supply-constrained, upside capex risk
    #   "balanced" — Healthy levels, no structural pressure
    #   "elevated" — Above historical average, risk of build cycle peak
    #   "glut" — Excess inventory, pricing pressure, demand destruction risk
    supply_chain_tightness: SignalStrength
    # Overall tightness of semiconductor supply chain (STRONG/MODERATE/WEAK).

    # ── Export Controls ──────────────────────────────────────────────────────
    export_control_status: str
    # Current state of US semiconductor export controls on advanced AI chips.
    # One of:
    #   "stable" — Current regime unchanged
    #   "tightening" — New restrictions announced/pending
    #   "loosening" — Exceptions granted or rules eased
    #   "uncertain" — Policy under review, outcome unclear
    revenue_impact_assessment: str
    # Qualitative assessment of export control revenue impact on affected
    # companies. Examples:
    #   - NVDA: ~20% China data center revenue at risk if restrictions tighten
    #   - AMD: Moderate impact via XILINX Virtex portfolio
    #   - QCOM: Limited exposure, smartphone exemptions secure

    # ── Metadata and Anomalies ───────────────────────────────────────────────
    metadata: InvocationMetadata
    # Invocation metadata: when collected, confidence, volatility regime.

    # ── Detailed lists (fields with defaults) ──────────────────────────────────
    hyperscaler_capex_details: list[CapexSignal] = field(default_factory=list)
    # Per-company AI capex signals with key statements and trajectory assessment.
    # This is the working evidence behind hyperscaler_capex_sentiment.
    key_supply_chain_signals: list[str] = field(default_factory=list)
    # Notable supply chain developments: wafer shortage announcements, new fab
    # capacity openings, geopolitical bottlenecks (Taiwan contingency), raw
    # material constraints (rare earths, neon).
    active_product_cycles: list[ProductCycle] = field(default_factory=list)
    # Semiconductor and AI infrastructure products in active market stages
    # (launch, ramp, maturity). Captures product name, stage, and market impact.
    platform_shift_signals: list[str] = field(default_factory=list)
    # Structural platform shifts in compute architecture:
    #   - cloud → AI inference at edge
    #   - GPU-dominant → custom ASIC specialization
    #   - x86 → ARM in data center
    #   - single-precision → lower precision (FP8, int8) training
    competitive_shift_signals: list[CompetitiveShift] = field(default_factory=list)
    # Notable market share moves or competitive position changes. Captures
    # which company, the nature of the shift, and its market significance.
    export_control_recent_developments: list[str] = field(default_factory=list)
    # Recent changes, pending rule changes, or new guidance. Examples:
    #   - BIS narrowed H100 carve-out for China
    #   - Taiwan semiconductor export license exceptions expanded
    #   - Commerce Dept reviewing advanced AI chip controls
    affected_tickers: list[Ticker] = field(default_factory=list)
    # Ticker symbols of companies with material revenue exposure to affected
    # markets or controls (e.g., NVDA, AMD, QCOM, INTC).
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected statistical anomalies flagged by the distillation layer.


@dataclass(frozen=True)
class EnergyCatalyst:
    """Qual 6:6b — OPEC rhetoric, inventory narrative, weather, and infrastructure.

    Sector-scoped for ENERGY analysts. Captures:
      - OPEC rhetoric and compliance (production intentions, member signals)
      - Inventory/supply narrative (interpretation of demand vs. import flows)
      - Weather/seasonal patterns (hurricanes, extreme temperatures)
      - Infrastructure developments (pipelines, LNG terminals, refinery capacity)

    This entity flows exclusively to the energy analyst agent, enabling
    interpretation of OPEC rhetoric as a leading indicator and inventory
    narrative framing as a directional bias amplifier.

    Source: EIA (free) + news APIs + OPEC website + NOAA (weather)
    Fallback: Argus, Platts (expensive)
    Cadence: Every invocation (hourly)
    Feasibility: MEDIUM — EIA data free, OPEC decisions public, NOAA weather free

    Interpretation notes for analyst:
      - OPEC rhetoric between meetings (intersessional statements) is a leading
        indicator of willingness to comply or leak production. Monitor key members
        (Saudi Arabia, UAE, Iraq, Russia) for tone shifts.
      - Inventory narrative framing is everything. Same 5M barrel draw can be
        BULLISH (demand-driven) or BEARISH (import-driven, demand weak). The
        analyst must reconcile EIA data with broader demand signals.
      - Hurricane season (June-November) creates binary tail risk. Even low
        probability events warrant inventory adjustments.
      - Pipeline/LNG infrastructure changes affect marginal supply cost and
        geopolitical supply diversification (e.g., new LNG terminal → potential
        volume swing from Russia to Australia/Qatar).
    """

    sector: AlphaMindSector  # Always ENERGY

    # ── OPEC Rhetoric and Compliance ─────────────────────────────────────────
    opec_rhetoric_tone: Direction
    # Aggregate tone of OPEC member statements on production. One of:
    #   BULLISH — Members publicly supportive of production cuts or quota discipline
    #   BEARISH — Members signaling willingness to increase, or skepticism on cuts
    #   NEUTRAL — Mixed messaging or no consensus
    member_compliance_assessment: str
    # Assessment of OPEC member compliance with announced quotas. One of:
    #   "strong_compliance" — Members broadly respecting production caps
    #   "slipping" — Leakage evident; some members above quota
    #   "fragmented" — Compliance highly variable by member; group discipline breaking
    next_meeting_expectation: str
    # Expectation for the next OPEC meeting decision. One of:
    #   "cut" — Additional production cuts likely
    #   "hold" — Status quo maintained
    #   "increase" — Production increase announced
    #   "uncertain" — Outcome unclear or split signals

    # ── Inventory/Supply Narrative ───────────────────────────────────────────
    inventory_narrative_framing: str
    # Qualitative interpretation of inventory draws/builds. Same data can be
    # interpreted multiple ways depending on demand context. One of:
    #   "demand_driven_draw" — Inventory falling because demand is strong
    #   "import_driven_draw" — Inventory falling due to import disruption; demand weak
    #   "weak_demand_build" — Inventory building due to soft demand
    #   "seasonal_build" — Expected seasonal inventory builds (fall/winter prep)
    #   "hedge_unwind" — Flows driven by spec liquidation, not supply/demand
    #   "mixed" — No clear narrative dominance
    supply_demand_balance_assessment: Direction
    # Overall supply/demand balance direction. Synthesizes EIA crude imports,
    # refinery runs, product imports, and demand estimates.
    #   BULLISH — Supply tightening or demand exceeding expectations
    #   BEARISH — Supply ample or demand faltering
    #   NEUTRAL — Balanced

    # ── Weather and Seasonal ─────────────────────────────────────────────────
    weather_risk_active: bool
    # True if weather risk is currently material to supply/demand. False
    # during low-risk periods (Dec-May outside hurricane zone).
    weather_production_impact: SignalStrength
    # Expected impact on oil/natural gas production from current/imminent
    # weather. STRONG: >500k bbl/d crude or >1 Bcf/d gas; MODERATE: 200-500k;
    # WEAK: <200k.
    hurricane_season_risk_level: SignalStrength
    # Elevated during June-November (Atlantic hurricane season). Assessed
    # monthly by NOAA. STRONG: above-normal storm activity forecast;
    # MODERATE: normal season; WEAK/NONE: low-activity forecast.
    weather_description: str
    # Qualitative description of weather conditions and supply impact.
    # Example: "Gulf production curtailments ~400k bbl/d due to Beryl;
    # repairs ongoing, expected 7-10 day recovery."

    # ── Infrastructure Developments ──────────────────────────────────────────
    infrastructure_catalyst_direction: Direction
    # Net direction of infrastructure supply impact over next 3-6 months.
    #   BULLISH — Meaningful new export capacity coming online
    #   BEARISH — Maintenance downtime or capacity exits
    #   NEUTRAL — Offsetting impacts or no material changes

    # ── Metadata and Anomalies ───────────────────────────────────────────────
    metadata: InvocationMetadata
    # Invocation metadata: when collected, confidence, volatility regime.

    # ── Detailed lists (fields with defaults) ──────────────────────────────────
    inter_meeting_statements: list[OPECStatement] = field(default_factory=list)
    # Discrete OPEC member or group statements collected between official
    # meetings. These are leading indicators of compliance and future decisions.
    key_inventory_narrative_signals: list[str] = field(default_factory=list)
    # Specific datapoints supporting the narrative framing. Examples:
    #   - Crude imports fell 800k bbl/d → supply tightening
    #   - Refinery runs steady but product demand soft → import risk
    #   - SPR drawdown commentary → potential floor support
    #   - Seasonal gasoline build expected → summer demand preparation
    weather_event_type: str | None = None
    # Current or imminent weather event type, if any. One of:
    #   "hurricane" — Atlantic/Gulf hurricane season (June-November)
    #   "extreme_cold" — Extreme cold snap; refinery/pipeline freeze risk (Nov-Feb)
    #   "extreme_heat" — Extreme heat; power demand spike, refinery constraints (Jun-Aug)
    #   "drought" — Water constraints; hydroelectric/nuclear power reduction
    #   None if no active material weather risk
    pipeline_developments: list[str] = field(default_factory=list)
    # Recent or pending pipeline infrastructure changes. Examples:
    #   - Permian Express 2 final investment decision pushed to Q4
    #   - Arctic LNG 2 sanctions impact: capacity offline
    #   - Keystone XL environmental approval challenges persist
    lng_terminal_developments: list[str] = field(default_factory=list)
    # LNG export/import terminal changes. Examples:
    #   - Golden Pass LNG startup timeline: Q4 2024
    #   - Tellurian Driftwood LNG financing secured; construction accelerating
    #   - Mexico LNG terminal permits under review; geopolitical sensitivity
    refinery_capacity_changes: list[str] = field(default_factory=list)
    # Refinery maintenance, turnarounds, or permanent closures. Examples:
    #   - Valero Port Arthur coking unit offline for 3 months; throughput impact ~50k bbl/d
    #   - China refinery utilization: 85% (elevated seasonal)
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected statistical anomalies flagged by the distillation layer.


@dataclass(frozen=True)
class FinancialsCatalyst:
    """Qual 6:6c — Credit narrative, rate environment, M&A pipeline, and fintech trends.

    Sector-scoped for FINANCIALS analysts. Captures:
      - Credit conditions (lending standards, SLOOS, consumer/commercial credit)
      - Rate environment (NIM expectations, rate sensitivity)
      - M&A pipeline (deal flow, investment bank commentary)
      - Regulatory posture (new rules, enforcement, capital requirements)
      - Consumer/payment trends (spending, e-commerce, digital adoption)
      - Crypto/digital assets (regulatory, flow, affected tickers)

    This entity flows exclusively to the financials analyst agent, enabling
    domain-specific interpretation of credit conditions (SLOOS is the single
    most authoritative source) and rate environment sentiment as a performance
    driver for net interest margins.

    Source: FRED (free) + news APIs + SEC EDGAR + SLOOS (quarterly)
    Fallback: American Banker (paid)
    Cadence: Every invocation (hourly for intraday; quarterly cadence for SLOOS)
    Feasibility: MEDIUM — credit conditions from FRED, regulatory filings from
                 EDGAR, SLOOS released quarterly by Federal Reserve

    Interpretation notes for analyst:
      - SLOOS (Senior Loan Officer Survey) is released quarterly (after FOMC
        meetings). It is the single highest-weight credit indicator. A tightening
        shift has ~90-day lag to loan growth slowdown.
      - Credit narrative should reconcile SLOOS trends with real-time charge-off
        and delinquency data from regulatory filings (10-K, 10-Q).
      - NIM expansion is the most direct earnings driver for banks. Rate environment
        sentiment captures market positioning (steep curve = favorable; flat/inverted
        = margin compression risk).
      - M&A pipeline is a secondary earnings lever; aggregate deal flow value
        indicates investment banking fee pools.
      - Crypto narrative is INDEPENDENT of broader financials dynamics. COIN,
        MSTR, and SQ carry distinct crypto-specific risk/return, decoupled from
        credit and rate cycles. Bitcoin ETF flows are a leading indicator of
        retail/institutional inflows into digital assets.
      - Regulatory shifts (capital rules, stress test criteria) have long lead
        times but material impacts on ROE, capital allocation, and dividend policy.
    """

    sector: AlphaMindSector  # Always FINANCIALS

    # ── Credit Conditions ────────────────────────────────────────────────────
    credit_conditions_narrative: Direction
    # Overall credit conditions direction. Synthesizes lending standards,
    # charge-offs, delinquencies, and origination trends.
    #   BULLISH — Credit standards easing, demand strong, quality intact
    #   BEARISH — Standards tightening, charge-offs rising, demand slowing
    #   NEUTRAL — Stable conditions
    consumer_credit_quality: Direction
    # Assessment of consumer credit quality (charge-offs, delinquencies,
    # utilization rates). Sourced from regulatory filings and credit bureau data.
    commercial_credit_quality: Direction
    # Assessment of commercial real estate and C&I credit quality. Includes
    # CRE stress indicators (occupancy, cap rates, refi roll-over risk).

    # ── Rate Environment ─────────────────────────────────────────────────────
    rate_environment_sentiment: Direction
    # How the current and expected rate environment is perceived by banks.
    #   BULLISH — Steep yield curve (favorable NIM); expectations of cuts
    #               generate asset repricing upside (duration gains)
    #   BEARISH — Flat/inverted curve (NIM compression); expectations of
    #             rate hikes create asset repricing losses (duration losses)
    #   NEUTRAL — Curve balanced; stable rate expectations
    nim_expectations: Direction
    # Net interest margin trend expectations over next 2-4 quarters.
    #   BULLISH — NIMs expanding (deposit costs stable, yields rising, or spreads widening)
    #   BEARISH — NIMs compressing (deposit costs rising, yields flat/falling, or spreads narrowing)
    #   NEUTRAL — Stable NIM trajectory
    rate_sensitivity_commentary: str
    # Qualitative assessment from bank earnings calls or investor day commentary
    # on rate sensitivity. Examples:
    #   - JPM: 1% rate hike → +$3.2B NII over 12 months
    #   - WFC: Deposit flows stable; core deposit beta improving
    #   - GS: Investment banking fees sensitive to deal flow; rate environment indirect

    # ── M&A Pipeline ────────────────────────────────────────────────────────
    ma_pipeline_activity: Direction
    # Directional assessment of M&A deal flow and pipeline. Sourced from
    # investment bank commentary, announced deals, and industry chatter.
    #   BULLISH — Deal activity increasing; valuations attractive; financing available
    #   BEARISH — Deal pipeline cooling; valuations elevated; financing constraints
    #   NEUTRAL — Stable deal flow
    investment_bank_commentary: Direction
    # How major investment banks (GS, MS, JPM, BLK) characterize deal pipeline.
    #   BULLISH — Commentary on "robust pipeline", rising valuations, pent-up demand
    #   BEARISH — Commentary on "muted activity", valuation concerns, limited appetite
    #   NEUTRAL — No material pipeline shifts described

    # ── Regulatory Posture ───────────────────────────────────────────────────
    regulatory_posture_direction: Direction
    # Directional shift in regulatory environment (more or less restrictive).
    #   BULLISH — Regulatory easing (deregulation, supportive appointees)
    #   BEARISH — Regulatory tightening (new restrictions, enforcement focus)
    #   NEUTRAL — Status quo or mixed signals
    capital_requirement_outlook: str
    # Qualitative outlook on capital requirement changes expected in next
    # 12-24 months. Examples:
    #   - Basel IV endgame: likely 3-5 year phase-in; regional banks exempt
    #   - Stress test severity stable; expected pass-fail distribution unchanged
    #   - Dividend policy constrained; buybacks likely to remain muted

    # ── Consumer/Payment Trends ──────────────────────────────────────────────
    consumer_spending_trend: Direction
    # Direction of consumer spending. Synthesizes credit card data, retail
    # sales, and payment processor commentary.
    #   BULLISH — Spending accelerating; volumes up; delinquencies stable
    #   BEARISH — Spending slowing; volumes flat/negative; delinquencies rising
    #   NEUTRAL — Stable spending
    ecommerce_growth_trend: Direction
    # E-commerce growth trajectory. Sourced from V, MA, PYPL, SQ earnings
    # commentary and industry data.
    #   BULLISH — E-commerce penetration rising; volumes accelerating
    #   BEARISH — E-commerce growth stalling; shift to offline
    #   NEUTRAL — Stable e-commerce trajectory
    cross_border_payment_trend: Direction
    # Cross-border payment volume direction. Signals global trade and travel
    # recovery/contraction.
    #   BULLISH — Cross-border volumes accelerating; tourism/trade recovering
    #   BEARISH — Cross-border volumes declining; geopolitical/travel concerns
    #   NEUTRAL — Stable cross-border flow
    digital_payment_adoption: Direction
    # Adoption of digital/contactless payment methods. Sourced from V/MA
    # commentary and industry data.
    #   BULLISH — Cash displacement accelerating; digital wallet adoption rising
    #   BEARISH — Adoption slowing; cash recovery in some geographies
    #   NEUTRAL — Stable adoption trajectory

    # ── Crypto and Digital Assets ────────────────────────────────────────────
    crypto_narrative_direction: Direction
    # Direction of the crypto/digital assets narrative independent of broader
    # financials. One of:
    #   BULLISH — Bitcoin appreciation, regulatory clarity, institutional inflows
    #   BEARISH — Bitcoin decline, regulatory concerns, retail outflows
    #   NEUTRAL — Mixed signals; no dominant narrative
    crypto_regulatory_development: str
    # Current regulatory environment for crypto. Examples:
    #   - Spot Bitcoin ETF approved; regulatory clarity improving
    #   - SEC vs. Ripple decision: XRP clarity; stablecoin rules pending
    #   - FIT21 legislative path uncertain; state-by-state approach fragmented
    bitcoin_etf_flow_direction: Direction
    # Direction of Bitcoin ETF inflows/outflows (primarily BLK iShares Bitcoin ETF,
    # MicroStrategy as a leverage proxy).
    #   BULLISH — Net inflows; institutional adoption accelerating
    #   BEARISH — Net outflows; capitulation or hedge unwinding
    #   NEUTRAL — Flows balanced or immaterial

    # ── Metadata and Anomalies ───────────────────────────────────────────────
    metadata: InvocationMetadata
    # Invocation metadata: when collected, confidence, volatility regime.

    # ── Detailed fields (with defaults) ─────────────────────────────────────
    sloos_latest_signal: Direction | None = None
    # Latest Senior Loan Officer Survey signal on credit standards. Released
    # quarterly. One of:
    #   BULLISH — Standards easing (net % banks easing > tightening)
    #   BEARISH — Standards tightening (net % banks tightening > easing)
    #   NEUTRAL — Balanced
    #   None if SLOOS not yet released this quarter
    credit_narrative_details: list[str] = field(default_factory=list)
    # Specific credit signals supporting the narrative. Examples:
    #   - Credit card delinquencies: 2.1% (vs. 1.8% pre-COVID baseline)
    #   - CRE distress: multifamily cap rates at 6.5%; refi pipeline at risk
    #   - C&I loan loss provisions stable; underwriting standards tightening on LLPs
    notable_deals: list[str] = field(default_factory=list)
    # Significant M&A transactions, announced deals, or credible rumors with
    # material financing needs. Examples:
    #   - Apollo/Blackstone $100B+ consortium bid for GE Aerospace
    #   - Microsoft $20B OpenAI funding commitment
    #   - Banking sector consolidation talks: potential regional bank M&A
    key_regulatory_shifts: list[str] = field(default_factory=list)
    # Notable regulatory changes, enforcement actions, or policy shifts.
    # Examples:
    #   - SEC approves spot Bitcoin ETF: COIN/MSTR regulatory clarity
    #   - OCC prioritizes CRE stress testing in 2024 stress test
    #   - CFPB rulemaking on fintech lending standards
    #   - Basel IV endgame proposal impacts capital requirements
    payment_volume_signals: list[str] = field(default_factory=list)
    # Specific payment volume signals and trends. Examples:
    #   - Visa: US e-commerce growth +8% YoY (vs. +12% prior year)
    #   - Square: Cash App users +15% YoY; payment velocity stable
    #   - PayPal: Cross-border volumes down 5% YoY; macro softening
    affected_tickers: list[Ticker] = field(default_factory=list)
    # Ticker symbols of companies with material crypto/digital asset exposure:
    #   COIN — Crypto exchange, regulatory leverage
    #   SQ — Cash App Bitcoin monetization
    #   MSTR — Bitcoin treasury (leveraged crypto play)
    #   GBTC/IBIT — Bitcoin ETF/trust vehicles
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected statistical anomalies flagged by the distillation layer.
