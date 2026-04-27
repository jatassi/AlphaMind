"""Q8: Commodities — entity definitions.

6 entities covering crude oil, natural gas, industrial metals, precious metals,
crack spreads, and commodity positioning/flows. EIA (free) is the primary
government source for energy data.

Design note on commodity vs. ticker:
    Commodity entities are NOT per-ticker. They represent market-wide or per-commodity
    benchmarks (WTI, Brent, Henry Hub, copper, gold, etc.). Each carries InvocationMetadata
    for timestamp and confidence context, but uses a commodity_symbol field instead of
    a ticker field to identify the specific commodity being measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ._common import (
    AnomalyFlag,
    DataConfidence,
    InvocationMetadata,
    SignalStrength,
)

__all__ = [
    "CommodityPositioning",
    "CrackSpreads",
    "CrudeOil",
    "ETFFlowData",
    "FuturesCurvePoint",
    "IndustrialMetals",
    "NaturalGas",
    "PreciousMetals",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FuturesCurvePoint:
    """A single point on a commodity futures curve (e.g., front month, 3-month out)."""

    expiry_label: str  # "front" | "2m" | "3m" | "6m" | "12m" or contract month
    price: float  # Futures contract price ($/bbl, $/MMBtu, etc.)
    days_to_expiry: int  # Trading days until expiration


@dataclass(frozen=True)
class ETFFlowData:
    """Inflow/outflow data for a specific commodity ETF."""

    ticker: str  # ETF symbol (e.g., "USO", "GLD", "SLV", "UNG")
    flow_usd: float  # Net flow in USD. Positive = inflow, negative = outflow
    flow_change_pct: float  # % change in fund AUM from prior period
    accumulated_flow_7d: float  # 7-day cumulative flow in USD
    data_confidence: DataConfidence


# ── Primary entities ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CrudeOil:
    """Q8:8a — WTI/Brent prices, futures curve, spreads, and EIA inventory.

    WTI and Brent spot (level, change, trend), futures curve shape
    (backwardation vs. contango), WTI-Brent spread, EIA weekly petroleum
    report (inventory vs. consensus), crude IV.

    For the energy sector, crude is the direct driver. For other sectors,
    crude spikes signal inflation/growth shocks that ripple through credit
    and capital allocation.

    Source: EIA (free) + Polygon Stocks Starter or Yahoo Finance
    Fallback: FRED for some series
    Cadence: Weekly (EIA Wed) + daily prices
    Feasibility: HIGH — EIA weekly reports free, prices widely available
    """

    # ── Identity and metadata ──
    metadata: InvocationMetadata

    # ── WTI spot ──
    wti_spot_price: float  # Current WTI spot price ($/barrel)
    wti_prior_close: float  # Prior day close ($/barrel)
    wti_change_pct: float  # Daily change percentage
    wti_change_atr: float  # Daily change in ATR-equivalent multiples (for
    # cross-commodity normalization)
    wti_5d_change_pct: float  # 5-day cumulative change
    wti_20d_change_pct: float  # 20-day cumulative change

    # ── Brent spot ──
    brent_spot_price: float  # Current Brent spot price ($/barrel)
    brent_prior_close: float  # Prior day close
    brent_change_pct: float  # Daily change
    brent_5d_change_pct: float  # 5-day change

    # ── WTI-Brent spread ──
    wti_brent_spread: float  # Brent - WTI ($/barrel). Positive = Brent premium.
    # Widening spread signals US-specific oversupply or
    # export constraints. Typical range: -$3 to +$5/barrel.
    wti_brent_spread_pct: float  # Spread as % of Brent price
    spread_trend: str = ""  # "widening" | "narrowing" | "stable"
    # 5-day trend of the spread

    # ── Futures curve shape (backwardation vs. contango) ──
    futures_curve: list[FuturesCurvePoint] = field(default_factory=list)
    # WTI futures curve points: front month, 2M, 3M, 6M, 12M. Enables computation
    # of curve slope. Front month > 3M signals backwardation (supply tightness).
    # 3M > 12M signals contango (surplus/storage carry).
    curve_slope_1m_3m: float = 0.0  # (3M price - front price) / front price x 100.
    # Positive = contango (far months premium). Negative =
    # backwardation (near-term premium). Extreme curves
    # (slope > 15% or < -10%) signal structural stress.
    curve_shape_regime: str = ""  # "backwardation" | "contango" | "flat"
    # Classified curve regime

    # ── EIA weekly petroleum inventory ──
    eia_crude_inventory_mbbl: float = 0.0  # EIA crude oil inventory (million barrels)
    eia_inventory_prior: float = 0.0  # Prior week inventory
    eia_inventory_change_mbbl: float = 0.0  # Week-over-week change (positive = build)
    eia_inventory_consensus_mbbl: float = 0.0  # Market consensus for the change (pre-report)
    eia_inventory_surprise_pct: float = 0.0  # (actual - consensus) / |consensus| x 100.
    # >10% surprise is high signal. EIA reports Wed 10:30 AM ET.
    eia_refinery_utilization_pct: float = 0.0  # % of US refinery capacity in use
    eia_product_demand_change_pct: float = 0.0  # Gasoline + distillate demand YoY %

    # ── Crude volatility ──
    crude_iv_pct: float = 0.0  # Implied volatility on crude futures (30-day).
    # Spikes signal event risk (OPEC meetings, geopolitical).
    # Normal range: 15-25%. >35% = elevated risk.
    iv_regime: str = ""  # "low" | "normal" | "elevated" | "spike"

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class NaturalGas:
    """Q8:8b — Henry Hub pricing, futures curve, EIA storage, and weather context.

    Henry Hub spot and front-month futures, seasonal futures curve, EIA storage
    report (vs. expectations), weather forecasts (HDD/CDD), LNG export volumes.

    Natural gas is more seasonal and weather-sensitive than crude. Winter weather
    (HDD spikes) and summer heat waves (CDD spikes) create directional setups.

    Source: EIA (free) + Yahoo Finance / Polygon
    Fallback: FRED
    Cadence: Weekly (EIA Thu) + daily prices
    Feasibility: HIGH — EIA reports free, prices widely available
    """

    # ── Identity and metadata ──
    metadata: InvocationMetadata

    # ── Henry Hub spot and futures ──
    henry_hub_spot_price: float  # Current Henry Hub spot price ($/MMBtu)
    henry_hub_prior_close: float  # Prior day close
    henry_hub_change_pct: float  # Daily % change
    henry_hub_5d_change_pct: float  # 5-day cumulative change
    front_month_futures_price: float  # Front-month futures contract ($/MMBtu)
    front_month_change_pct: float  # Daily change in front month

    # ── Futures curve (seasonal shape) ──
    futures_curve: list[FuturesCurvePoint] = field(default_factory=list)
    # Natural gas curve: front, 2M, 3M, 6M, 12M. Seasonal patterns: winter (Dec/Jan)
    # premium over summer (Jul/Aug) is typical. Inversion signals structural tightness.
    curve_slope_1m_3m: float = 0.0  # (3M - front) / front x 100. Positive = typical
    # summer contango. Negative = winter backwardation.
    curve_seasonality_deviation: float = 0.0  # % deviation from typical seasonal curve
    # Normal range: -20% to +20%. Extremes flag supply stress.

    # ── EIA natural gas storage ──
    eia_storage_bcf: float = 0.0  # EIA working gas in underground storage (Bcf)
    eia_storage_prior: float = 0.0  # Prior week storage level
    eia_storage_change_bcf: float = 0.0  # Weekly net change (positive = injection)
    eia_storage_consensus_bcf: float = 0.0  # Pre-report consensus
    eia_storage_surprise_pct: float = 0.0  # (actual - consensus) / |consensus| x 100.
    # EIA report Thu morning. >15% surprise = high signal.
    eia_storage_vs_5y_avg_pct: float = 0.0  # Current storage vs. 5-year average for this
    # week. Negative = tight storage, tightens supply.

    # ── Weather context (demand drivers) ──
    hdd_forecast_7d: float = 0.0  # Heating degree days forecast (7-day). Cold snap =
    # high HDD = increased heating demand = bullish gas.
    cdd_forecast_7d: float = 0.0  # Cooling degree days forecast. Heat wave = bullish gas.
    weather_demand_signal: str = ""  # "bullish" | "bearish" | "neutral" based on HDD/CDD
    weather_surprise_vs_normal: float = 0.0  # % deviation of HDD/CDD from seasonal normal

    # ── LNG export context ──
    lng_export_volume_mtpa: float = 0.0  # Annualized LNG export capacity from US (million
    # tonnes per annum). Relevant for LNG, ET positioning.
    lng_spot_price_jkm: float = 0.0  # Japan Korea Marker (global LNG benchmark, $/MMBtu).
    # High JKM = strong global demand, supports US exports.
    lng_spread_henry_hub: float = 0.0  # (JKM - Henry Hub) x liquefaction costs. Positive =
    # export arbitrage profitable.

    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class IndustrialMetals:
    """Q8:8c — Copper, aluminum prices with copper-to-gold ratio and inventory.

    Copper/aluminum spot and futures, LME warehouse inventory, copper-to-gold
    ratio, industrial metals vs. equity divergence.

    Copper is the "Doctor Copper" growth barometer. Copper rallying while equities
    lag signals underpriced growth. Copper falling while equities hold signals
    unpriced growth slowdown.

    Source: Yahoo Finance (free) or Polygon Stocks Starter
    Fallback: FRED (some series)
    Cadence: Daily
    Feasibility: MEDIUM — spot prices available, LME inventory harder
    """

    # ── Identity and metadata ──
    metadata: InvocationMetadata

    # ── Copper ──
    copper_spot_price: float  # Current copper spot price ($/lb or $/kg, specify)
    copper_prior_close: float  # Prior day close
    copper_change_pct: float  # Daily % change
    copper_5d_change_pct: float  # 5-day cumulative change
    copper_20d_change_pct: float  # 20-day cumulative change (trend context)
    copper_lme_inventory_kt: float  # LME warehouse inventory (kilotonnes). Rising inventory
    # = oversupply. Falling inventory = tightness.
    copper_inventory_change_pct: float  # % change in LME inventory from prior week

    # ── Aluminum ──
    aluminum_spot_price: float  # Aluminum spot price ($/metric tonne)
    aluminum_prior_close: float  # Prior day close
    aluminum_change_pct: float  # Daily % change

    # ── Copper-to-gold ratio and divergence ──
    copper_gold_ratio: float  # Copper price / gold price (both in $/oz equivalent
    # for comparability). Rising = growth confidence,
    # falling = defensive positioning.
    industrial_metals_5d_return_pct: float  # Composite return (copper + aluminum)
    equities_5d_return_pct: float  # SPY 5-day return for divergence comparison.
    # Filled by distillation layer.

    # ── Optional and defaulted fields ──
    aluminum_production_ytd_pct: float = 0.0  # YTD production vs. prior year (trend)
    copper_gold_ratio_20d_ma: float = 0.0  # 20-day moving average of the ratio (trend)
    copper_gold_trend: str = ""  # "rising" | "falling" | "stable"
    # 5-day trend of the ratio
    divergence_signal: str = ""  # "copper_strength_equity_weakness" |
    # "copper_weakness_equity_strength" | "in_sync" | "neutral"
    divergence_severity: SignalStrength = SignalStrength.NONE
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class PreciousMetals:
    """Q8:8d — Gold, silver prices with ratios and ETF flow signals.

    Gold/silver spot (level, change), gold/silver ratio, gold ETF flows
    (GLD, IAU), gold vs. real yields positioning, central bank buying signals.

    Gold's relationship with real yields is one of the most stable intermarket
    correlations. Breaks from that relationship signal safe-haven demand or
    central bank buying not yet visible in equities.

    Source: Yahoo Finance (free) or FRED
    Fallback: Polygon
    Cadence: Daily
    Feasibility: HIGH — gold and silver prices widely available
    """

    # ── Identity and metadata ──
    metadata: InvocationMetadata

    # ── Gold ──
    gold_spot_price: float  # Current gold spot price ($/oz)
    gold_prior_close: float  # Prior day close
    gold_change_pct: float  # Daily % change
    gold_5d_change_pct: float  # 5-day cumulative change
    gold_20d_change_pct: float  # 20-day cumulative change

    # ── Silver ──
    silver_spot_price: float  # Current silver spot price ($/oz)
    silver_prior_close: float  # Prior day close
    silver_change_pct: float  # Daily % change

    # ── Gold/silver ratio (risk appetite) ──
    gold_silver_ratio: float  # Gold price / silver price ($/oz basis). Higher ratio
    # = gold premium (risk-off), lower ratio = silver premium
    # (risk-on). Extremes flag positioning shifts.
    gold_silver_ratio_20d_ma: float = 0.0  # 20-day MA of the ratio
    gs_ratio_regime: str = ""  # "gold_premium" (ratio > 20yr MA + 1 std dev) |
    # "silver_premium" (ratio < 20yr MA - 1 std dev) |
    # "neutral"

    # ── Optional and defaulted fields ──
    real_yield_2y: float = 0.0  # 2-year real yield (from Q6:6a). Filled by distillation
    gold_real_yield_divergence: float = 0.0  # Deviation from typical gold = -real_yields
    # relationship. Positive = gold over-extended relative
    # to real yields. Negative = gold cheap vs. rates.
    gold_yield_relationship: str = ""  # "normal" | "gold_extended" | "gold_cheap"
    # Classification of the divergence
    gld_flow_usd: ETFFlowData | None = None  # SPDR Gold Shares (largest gold ETF)
    iau_flow_usd: ETFFlowData | None = None  # iShares Gold Trust
    gold_etf_flow_7d_usd: float = 0.0  # Combined 7-day flow (GLD + IAU) in USD
    gold_etf_flow_signal: str = ""  # "inflow_accumulation" (sustained inflows during calm)
    # | "outflow_liquidation" | "neutral"
    cb_buying_trend: str = ""  # "increasing" | "decreasing" | "stable"
    # Monthly/quarterly trend from WGC data
    cb_buying_last_month_oz: float = 0.0  # Approximate central bank purchases last month (oz)
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CrackSpreads:
    """Q8:8e — Gasoline and diesel crack spreads with demand patterns.

    Gasoline crack spread, heating oil/diesel crack, 3-2-1 crack spread,
    gasoline demand patterns, crack spread vs. energy stock performance.

    The refined product layer that connects crude (Q8:8a) to refiner profitability
    for integrated and refining names in the energy universe. Widening cracks =
    refiner margin expansion. Narrowing cracks = margin compression.

    Source: Derived from Q8:8a + EIA product prices
    Cadence: Weekly
    Feasibility: HIGH — computable from crude + product prices
    """

    # ── Identity and metadata ──
    metadata: InvocationMetadata

    # ── Gasoline crack spread (crude → gasoline) ──
    gasoline_crack_spread: float  # $/bbl. Derived: gasoline_price - WTI_price. Widening
    # spread signals refiner profitability improvement.
    # Normal range: $8-$25/bbl.
    gasoline_crack_prior: float  # Prior week spread
    gasoline_crack_change_pct: float  # Weekly % change in spread

    # ── Heating oil / diesel crack spread ──
    diesel_crack_spread: float  # $/bbl. Heating oil / diesel crack spread.
    diesel_crack_prior: float  # Prior week spread
    diesel_crack_change_pct: float  # Weekly % change

    # ── 3-2-1 crack spread (composite refining margin) ──
    crack_3_2_1_spread: float  # $/bbl. Composite: 3 barrels crude → 2 gasoline +
    # 1 distillate. The industry's refining margin benchmark.
    crack_3_2_1_prior: float  # Prior week
    crack_3_2_1_change_pct: float  # Weekly % change

    # ── Demand patterns and divergence ──
    eia_gasoline_demand_change_pct: float  # Weekly gasoline demand vs. prior year
    eia_diesel_demand_change_pct: float  # Weekly diesel demand vs. prior year
    energy_stock_5d_return_pct: float  # XLE 5-day return (filled by distillation)
    crack_spread_5d_change_pct: float  # 5-day % change in 3-2-1 crack

    # ── Optional and defaulted fields ──
    gasoline_crack_5w_ma: float = 0.0  # 5-week moving average (trend)
    diesel_crack_seasonal_pattern: str = ""  # "peak_season" | "off_season" | "neutral"
    # Winter drives heating oil demand
    crack_3_2_1_vs_20w_ma: float = 0.0  # % deviation from 20-week MA. Extreme deviations
    # (>30% from MA) flag margin regimes.
    driving_season_phase: str = ""  # "peak" (Memorial Day-Labor Day) | "shoulder" | "off_season"
    # Affects gasoline demand seasonality
    divergence_signal: str = ""  # "crack_up_stocks_down" | "crack_down_stocks_up" |
    # "in_sync" | "neutral"
    divergence_severity: SignalStrength = SignalStrength.NONE
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class CommodityPositioning:
    """Q8:8f — COT commercial/speculative positioning and commodity ETF flows.

    COT commercial vs. speculative positioning, speculative positioning percentile,
    commodity ETF flows (USO, GLD, SLV, UNG), managed money positioning trend.

    Extreme positioning is a contrarian signal. When speculators are max-long
    (or max-short), the next move is more likely to reverse.

    Source: CFTC (free) — API now available via PRE
    Fallback: Quandl/Nasdaq Data Link
    Cadence: Weekly (COT Fri)
    Feasibility: HIGH — COT reports free, CFTC PRE API available
    """

    # ── Identity and metadata ──
    metadata: InvocationMetadata

    # ── COT positioning (commitments of traders) ──
    # Crude oil COT ──
    cot_crude_commercial_net: int  # Net long or short position (contracts)
    cot_crude_speculative_net: int  # Net speculative position
    cot_crude_total_open_interest: int  # Total open interest for reference

    # Natural gas COT ──
    cot_gas_commercial_net: int  # Net commercial position
    cot_gas_speculative_net: int  # Net speculative position
    cot_gas_total_open_interest: int  # Total open interest

    # Gold COT ──
    cot_gold_commercial_net: int  # Net commercial position
    cot_gold_speculative_net: int  # Net speculative position
    cot_gold_total_open_interest: int  # Total open interest

    # Copper COT ──
    cot_copper_commercial_net: int  # Net commercial position
    cot_copper_speculative_net: int  # Net speculative position
    cot_copper_total_open_interest: int  # Total open interest

    # ── Optional and defaulted fields ──
    cot_crude_spec_percentile: float = 0.0  # Where speculative positioning sits vs. trailing
    # 2-year history (0-100). >90 = max long (contrarian
    # bearish). <10 = max short (contrarian bullish).
    cot_crude_trend: str = ""  # "specs_accumulating_longs" |
    # "specs_reducing_longs" | "mixed"
    cot_gas_spec_percentile: float = 0.0
    cot_gas_trend: str = ""
    cot_gold_spec_percentile: float = 0.0
    cot_gold_trend: str = ""
    cot_copper_spec_percentile: float = 0.0
    cot_copper_trend: str = ""

    # ── Optional and defaulted fields ──
    cot_report_date: datetime | None = (
        None  # The Tuesday date the COT report covers (published Fri)
    )
    uso_flow: ETFFlowData | None = None  # United States Oil Fund (crude proxy)
    ung_flow: ETFFlowData | None = None  # United States Natural Gas Fund
    gld_flow: ETFFlowData | None = None  # SPDR Gold Shares
    slv_flow: ETFFlowData | None = None  # iShares Silver Trust
    managed_money_trend: str = ""  # "accumulating" | "liquidating" | "mixed"
    # Are commodity hedge funds adding or reducing exposure?
    managed_money_trend_velocity: str = ""  # "fast" | "moderate" | "slow"
    # How rapidly are they changing positioning?
    aggregate_etf_flow_7d_usd: float = 0.0  # Combined flow across all commodity ETFs
    etf_positioning_signal: str = ""  # "institutional_buying" | "retail_selling" | "neutral"
    # Interpretation of ETF flow direction and size
    anomalies: list[AnomalyFlag] = field(default_factory=list)
