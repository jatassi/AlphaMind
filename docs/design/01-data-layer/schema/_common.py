"""Common types, enums, and base definitions shared across all schema modules.

This module establishes the foundational vocabulary for the AlphaMind data layer.
Every schema module imports from here to ensure consistency in how tickers are
referenced, time is represented, and categorical values are constrained.

Design decisions:
    - Enums over string literals: Prevents typos and makes valid values discoverable.
      Every categorical field that has a known, finite set of values uses an enum.
    - datetime in UTC: All timestamps are UTC. Conversion to ET happens at the
      presentation/distillation layer, not in storage.
    - date for calendar dates: Pure dates (earnings date, IPO date) use date, not
      datetime, because the time component is meaningless.
    - Optional for nullable: Fields that may legitimately be absent (e.g., IPO date
      for a company that went public decades ago) use Optional[T]. Fields that
      should always be present are non-optional — a missing value is a bug, not
      a valid state.
    - Frozen dataclasses: All entity dataclasses are frozen (immutable) because
      they represent snapshots of state at a point in time. Mutation should produce
      a new instance, not modify an existing one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

# ── Identifiers ──────────────────────────────────────────────────────────────

# Ticker symbols are plain strings (e.g., "AAPL", "NVDA"). We don't wrap them
# in a newtype because they're used everywhere and the overhead isn't worth it.
# But we alias the type for documentation purposes.
Ticker = str


# ── Temporal ─────────────────────────────────────────────────────────────────

__all__ = [
    "AlphaMindSector",
    "AnomalyFlag",
    "DataConfidence",
    "Direction",
    "Exchange",
    "HeadlineType",
    "InflationRegime",
    "InvocationMetadata",
    "MarketCapBucket",
    "MarketSession",
    "SignalStrength",
    "SourceCredibilityTier",
    "Timeframe",
    "TrendState",
    "VolatilityRegime",
    "YieldCurveRegime",
]


class Timeframe(Enum):
    """Candle/indicator timeframe. The system uses 5 standard timeframes for
    multi-timeframe analysis, plus tick-level for derived computations."""

    MIN_1 = "1min"  # Raw input for volume profile, VWAP — not surfaced to agents
    MIN_15 = "15min"  # Shortest agent-visible timeframe — intraday structure
    HOUR_1 = "1h"  # Intraday momentum and flow patterns
    HOUR_4 = "4h"  # Primary swing-trade timeframe for the 4-72hr horizon
    DAILY = "1d"  # Daily context and reference levels
    WEEKLY = "1w"  # Trend context — where are we in the bigger picture


class MarketSession(Enum):
    """Trading session classification. Extended-hours data carries a confidence
    discount (thinner liquidity, wider spreads) applied by the distillation layer."""

    PRE_MARKET = "pre_market"  # 4:00-9:30 AM ET
    REGULAR = "regular"  # 9:30 AM-4:00 PM ET
    AFTER_HOURS = "after_hours"  # 4:00-8:00 PM ET
    OVERNIGHT = "overnight"  # 8:00 PM-4:00 AM ET (no US equity trading)


# ── Sector and classification ────────────────────────────────────────────────


class AlphaMindSector(Enum):
    """The system's four sector groupings. These drive analyst routing, risk
    guardrail concentration limits, and correlation analysis scoping. They
    intentionally diverge from GICS — semis are separated from tech because
    they have fundamentally different catalyst structures."""

    TECH = "tech"  # Mega-cap + high-growth tech (~20 names)
    SEMIS = "semis"  # Semiconductor supply chain (~15 names)
    FINANCIALS = "financials"  # Banks, payments, fintech (~15 names)
    ENERGY = "energy"  # Integrated, E&P, midstream, LNG (~15 names)


class Exchange(Enum):
    """Primary listing exchanges for universe names."""

    NYSE = "NYSE"
    NASDAQ = "NASDAQ"
    NYSE_ARCA = "NYSE_ARCA"  # Some ETFs


class MarketCapBucket(Enum):
    """Market cap classification. Bucket boundaries are approximate and used
    for peer grouping, not precise filtering. The universe floor is $10B."""

    MEGA_CAP = "mega_cap"  # >$200B — AAPL, MSFT, NVDA, etc.
    LARGE_CAP = "large_cap"  # $50B-$200B — CRM, PANW, etc.
    MID_CAP = "mid_cap"  # $10B-$50B — NET, ZS, etc.


# ── Trend and regime ─────────────────────────────────────────────────────────


class TrendState(Enum):
    """Per-timeframe trend classification. Applied at each of the 5 agent-visible
    timeframes (15min through weekly). The composite multi-timeframe trend score
    is a separate derived field, not an enum value."""

    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE_BOUND = "range_bound"


class VolatilityRegime(Enum):
    """Market-wide volatility regime, maintained by the distillation layer from
    Q11:11a-11e inputs. Broadcast to every agent as universal context. See
    Q11:11f spec and distillation layer § 4 for classification criteria."""

    LOW_VOL_COMPRESSION = "low_vol_compression"  # Squeeze building, mean-reversion works
    VOL_EXPANSION = "vol_expansion"  # Momentum/breakout, mean-reversion dangerous
    CRISIS_SPIKE = "crisis_spike"  # Risk reduction, widen stops, bias to close
    VOL_NORMALIZATION = "vol_normalization"  # Post-spike recovery, cautious re-entry


class YieldCurveRegime(Enum):
    """Yield curve shape classification from Q6:6a. Regime transitions are
    flagged by the distillation layer as anomalies."""

    NORMAL = "normal"  # Upward sloping (near < far)
    FLAT = "flat"  # Near ≈ far
    INVERTED = "inverted"  # Near > far
    STEEPENING = "steepening"  # Transitioning toward normal
    FLATTENING = "flattening"  # Transitioning toward flat/inverted


class InflationRegime(Enum):
    """Inflation regime classification from Q6:6c."""

    HOT = "hot"
    COOLING = "cooling"
    STABLE = "stable"
    DEFLATION_RISK = "deflation_risk"


# ── Signal confidence and data quality ───────────────────────────────────────


class DataConfidence(Enum):
    """Reliability tier for data points. The distillation layer tags every output
    with a confidence level so downstream agents know how much weight to give it.
    Extended-hours, interpolated, and delayed data are explicitly marked."""

    HIGH = "high"  # Regular-session data from primary source
    MEDIUM = "medium"  # Extended-hours, or primary source with known gaps
    LOW = "low"  # Interpolated, estimated, or significantly delayed
    STALE = "stale"  # Data older than expected refresh cadence


class SourceCredibilityTier(Enum):
    """News source credibility for qualitative data (Qual 1:1a, 1b). Determines
    how the qualitative research layer weights information."""

    TIER_1 = "tier_1"  # Wire services (Reuters, Bloomberg, DJ), WSJ/FT exclusives
    TIER_2 = "tier_2"  # Major financial outlets, established reporters
    TIER_3 = "tier_3"  # Financial blogs, aggregators, social-media-first outlets


class HeadlineType(Enum):
    """Canonical headline-type taxonomy used by Qual 1:1a (BreakingHeadline) and the
    news digest. See `qualitative-research.md § Type tags` (in `docs/design/03-analysis-layer/`).

    Vendor tag vocabularies (Marketaux, Finnhub, SEC EDGAR 8-K item codes, RSS topics)
    are normalized into this set at the collector boundary via
    `config/headline_tag_mapping.yaml`. Multiple values per headline are allowed —
    an M&A rumor from a Tier 1 source carries both ``M_AND_A`` and ``BREAKING``.
    """

    BREAKING = "breaking"
    EARNINGS_RELATED = "earnings_related"
    M_AND_A = "m_and_a"
    ANALYST_ACTION = "analyst_action"
    REGULATORY = "regulatory"
    GEOPOLITICAL = "geopolitical"
    MACRO_DATA = "macro_data"
    INSIDER_ACTIVITY = "insider_activity"
    SHORT_REPORT = "short_report"
    ACTIVIST = "activist"
    PRODUCT_LAUNCH = "product_launch"
    SUPPLY_CHAIN = "supply_chain"
    GUIDANCE = "guidance"
    SECTOR_ROTATION = "sector_rotation"


# ── Direction and sentiment ──────────────────────────────────────────────────


class Direction(Enum):
    """Simple directional classification used across many entities."""

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    MIXED = "mixed"


class SignalStrength(Enum):
    """Qualitative signal strength for flags and composite assessments."""

    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    NONE = "none"


# ── Anomaly classification ───────────────────────────────────────────────────


@dataclass(frozen=True)
class AnomalyFlag:
    """A detected statistical anomaly. The distillation layer produces these as
    trigger inputs for the adaptive research layer. Anomalies are flagged, not
    interpreted — interpretation is the analysis pipeline's job.

    The z_score field expresses how many standard deviations the observation is
    from the 20-day trailing baseline. Higher absolute values indicate more
    extreme anomalies. The direction field indicates which way the anomaly goes.
    """

    anomaly_type: (
        str  # Machine-readable anomaly category (e.g., "volume_spike", "correlation_breakdown")
    )
    description: str  # Human-readable description for LLM consumption
    z_score: float  # Standard deviations from 20-day trailing baseline
    direction: Direction  # Which way the anomaly goes
    severity: SignalStrength  # How actionable this anomaly is
    related_tickers: list[Ticker] = field(default_factory=list)  # Tickers involved
    related_spec_ids: list[str] = field(default_factory=list)  # Which data categories flagged this


# ── Common metadata ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvocationMetadata:
    """Metadata attached to every entity instance, identifying when and how the
    data was produced. Every entity in the schema carries this as a field."""

    invocation_id: str  # Unique pipeline run identifier (UUID)
    invocation_type: str  # "pre_open" | "intraday" | "pre_close" | "off_hours"
    collected_at: datetime  # UTC timestamp when data was collected/computed
    data_confidence: DataConfidence  # Overall reliability of this data point
    vol_regime: VolatilityRegime  # Current volatility regime (universal context)
