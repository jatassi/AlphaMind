"""Reference Data — structural entities that scope and classify the universe.

These entities don't map to a quant/qual spec ID. They provide the
foundational reference data that every other domain depends on: which
tickers are in the universe, what sector they belong to, and how they
relate to indices and ETFs.

Unlike market data entities (refreshed every invocation), reference data
changes infrequently — typically on corporate actions, index rebalances,
or manual universe updates.

Design decisions:
    - asset_id as stable identifier: Ticker symbols can change (FB → META,
      TWTR → X). The asset_id is a UUID assigned when a name enters the
      universe and never changes. All cross-references between entities use
      ticker (for readability in LLM context) but asset_id is the durable
      foreign key for storage.
    - Denormalized sector on AssetUniverse: The AlphaMind sector is stored
      on both AssetUniverse and SectorClassification. This is intentional —
      AssetUniverse needs it for filtering without a join, and
      SectorClassification is the authoritative source with full detail.
    - ETF weights as a snapshot: ETF constituent weights change continuously
      as prices move. We store the weight as of the last rebalance date,
      not the real-time weight. Close enough for the system's purposes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ._common import (
    AlphaMindSector,
    DataConfidence,
    Exchange,
    InvocationMetadata,
    MarketCapBucket,
    Ticker,
)


# ── Supporting types ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TickerChange:
    """Record of a ticker symbol change for a universe name. Maintains the
    audit trail so historical data can be linked across symbol changes."""
    previous_ticker: str         # Old symbol (e.g., "FB")
    new_ticker: str              # New symbol (e.g., "META")
    effective_date: date         # When the change took effect
    reason: str                  # "rebrand" | "merger" | "spin_off" | "other"


@dataclass(frozen=True)
class ETFMembership:
    """A ticker's membership in a specific ETF, with weight and rebalance context.
    Used by SectorClassification to track how each name is represented in passive
    vehicle flows (relevant for Q12:12f ETF flow impact estimation)."""
    etf_ticker: str              # ETF symbol (e.g., "XLK", "SPY", "QQQ", "SMH")
    etf_name: str                # Full ETF name for readability
    weight_pct: float            # Weight in ETF as of last rebalance (0.0–100.0)
    weight_as_of: date           # Date the weight was last confirmed
    is_top_10_holding: bool      # Whether this name is a top-10 holding in the ETF


@dataclass(frozen=True)
class ReclassificationEvent:
    """Record of a sector or industry reclassification. Rare but consequential —
    a GICS reclassification can trigger forced selling/buying from sector-focused
    funds, creating mechanical flows (similar to index rebalance in Q12:12a)."""
    effective_date: date
    previous_sector: AlphaMindSector
    new_sector: AlphaMindSector
    previous_gics_sub_industry: str
    new_gics_sub_industry: str
    reason: str                  # Why the reclassification happened
    expected_flow_impact: Optional[str]  # Brief description of expected passive flow impact


# ── Primary entities ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AssetUniverse:
    """REF:UNIVERSE — Canonical ticker list with metadata for the ~65-name universe.

    Defines the complete set of tradeable instruments AlphaMind monitors.
    Every data ingestion pipeline, analysis agent, and risk guardrail is
    scoped to this universe. Changes trigger re-initialization of dependent
    caches (correlation matrices, sector rankings, etc.).

    Source: Manual curation (asset-universe.md) + Polygon reference API (free)
    Fallback: SEC EDGAR company search + Finnhub company profile
    Cadence: On change (index rebalance, M&A, universe review)
    Feasibility: HIGH — static reference data, one-time build + maintenance on events

    Consumers:
        - Every data ingestion pipeline (scoping what to collect)
        - Risk guardrails (position sizing uses ADV, market cap)
        - Execution layer (fill modeling uses ADV, float)
        - Distillation layer (peer grouping, normalization baselines)
    """

    # ── Identity ──
    asset_id: str                    # Stable UUID — survives ticker changes, never reused
    ticker: Ticker                   # Current ticker symbol (e.g., "AAPL"). Primary human-readable key.
    full_name: str                   # Legal entity name (e.g., "Apple Inc.")
    asset_class: str = "equity"      # Always "equity" in v1. Field exists for future expansion
                                     # (e.g., if the system adds crypto or commodity futures).

    # ── Listing and identifiers ──
    exchange: Exchange = Exchange.NASDAQ  # Primary listing exchange
    cik: str = ""                    # SEC Central Index Key (10-digit, zero-padded). Required for
                                     # EDGAR lookups (Q5:5c, Q5:5g, Q12:12c). Every US public company
                                     # has one. Example: "0000320193" (Apple).
    figi: Optional[str] = None       # OpenFIGI identifier. More universal than ticker but less
                                     # readable. Used for cross-referencing with institutional data
                                     # sources that don't use ticker symbols.
    isin: Optional[str] = None       # International Securities Identification Number (12-char).
                                     # Useful if the system ever expands to non-US markets.

    # ── Size and liquidity — selection criteria from asset-universe.md ──
    market_cap_bucket: MarketCapBucket = MarketCapBucket.LARGE_CAP
    market_cap_usd: float = 0.0      # Current market cap in USD. Updated daily from price × shares
                                     # outstanding. Used by risk guardrails for position sizing floors.
    avg_daily_volume_shares: int = 0  # 20-day trailing ADV in shares. Selection criterion: >2M shares/day.
                                     # Used by execution layer for fill modeling and slippage estimation.
    avg_daily_volume_notional_usd: float = 0.0  # 20-day trailing ADV in USD notional. Selection
                                     # criterion: >$50M/day. More useful than share count for comparing
                                     # liquidity across names at different price levels.
    shares_outstanding: int = 0      # Total shares outstanding. Source: Polygon reference API or
                                     # most recent 10-Q/10-K filing. Used for short interest calculations
                                     # (Q4:4a) and index weight estimation (Q12:12a).
    float_shares: int = 0            # Public float (shares outstanding minus restricted/insider-held).
                                     # More relevant than total shares for: short interest as % of float
                                     # (Q4:4a), squeeze analysis (Q4:4e), and realistic fill modeling.
                                     # Source: Polygon reference API. Some names (e.g., TSLA, META) have
                                     # float significantly below shares outstanding due to insider holdings.
    beta_spy: float = 1.0            # 1-year beta relative to SPY. Selection criterion: >0.8.
                                     # Updated monthly. Used by risk guardrails for beta-adjusted
                                     # exposure calculation and by the distillation layer for
                                     # normalizing price moves.

    # ── Coverage and signal quality ──
    analyst_count: int = 0           # Number of sell-side analysts covering this name. Selection
                                     # criterion: 10+. Higher coverage = richer estimate revision
                                     # signal (Q5:5a), more rating change events (Q5:5f), and better
                                     # news density. Names below 10 analysts tend to have sparse
                                     # estimate data and unreliable consensus.
    options_chain_liquid: bool = True  # Whether the options chain meets the liquidity threshold for
                                     # reliable flow signal ingestion (Q3). Assessed by: number of
                                     # strikes with non-zero OI, average bid-ask spread on ATM options,
                                     # and daily options volume. Names with illiquid chains produce
                                     # noisy derivatives signals.

    # ── Lifecycle ──
    ipo_date: Optional[date] = None  # IPO date, if the company went public within a trackable window.
                                     # Relevant for: lock-up expiration calendar (Q12:12e), post-IPO
                                     # volatility regime, and insider selling patterns (Q5:5g).
                                     # None for companies that went public before our tracking window.
    is_active: bool = True           # Whether this name is currently in the active universe. Inactive
                                     # names are retained for historical reference but excluded from
                                     # all ingestion, analysis, and trading pipelines.
    added_date: date = date(2026, 1, 1)  # When this name was added to the AlphaMind universe.
    removed_date: Optional[date] = None  # When this name was deactivated. None if still active.
    removal_reason: Optional[str] = None  # Free text: "acquired_by_XYZ", "delisted",
                                     # "below_liquidity_threshold", "universe_review", etc.

    # ── History ──
    ticker_changes: list[TickerChange] = field(default_factory=list)
        # Ordered list of historical ticker symbol changes. Most names have none.
        # Example: [TickerChange("FB", "META", date(2022, 6, 9), "rebrand")]

    # ── Metadata ──
    last_updated: datetime = field(default_factory=datetime.utcnow)
        # When this record was last refreshed from source data. Reference data
        # doesn't change often, but market_cap_usd, avg_daily_volume_*, beta_spy,
        # and analyst_count need periodic updates (daily for cap/ADV, monthly for
        # beta/analyst count).


@dataclass(frozen=True)
class SectorClassification:
    """REF:SECTOR — Ticker-to-sector mapping with index membership and ETF linkage.

    Maps each universe ticker to its sector, industry group, and relevant
    index/ETF memberships. This is the structural backbone for:
        - Relative performance computation (Q1:1e) — needs sector ETF mapping
        - Intra-sector correlation (Q7:7a) — needs peer group membership
        - Cross-sector rotation (Q7:7b) — needs sector ETF performance comparison
        - Market breadth (Q7:7c) — needs sector membership for advance/decline
        - Sector concentration guardrails (risk layer) — needs sector assignment
        - Domain researcher routing (analysis layer) — needs sector assignment

    Source: Polygon reference API (free) + index provider announcements
    Fallback: Finnhub company profile + manual curation
    Cadence: On change (quarterly rebalance, reclassification events)
    Feasibility: HIGH — well-defined reference data, available from multiple free sources

    Design note on peer groups: The peer_group field creates sub-sector groupings
    within the AlphaMind sectors for tighter correlation analysis. "Mega-cap tech"
    names (AAPL, MSFT, GOOG, AMZN, META) correlate with each other more tightly
    than with "high-growth tech" (PLTR, SNOW, SHOP) — the peer group lets the
    correlation analysis (Q7:7a) identify meaningful divergences within the right
    comparison set.
    """

    # ── Identity (links to AssetUniverse) ──
    ticker: Ticker                   # Must match an active AssetUniverse.ticker
    asset_id: str                    # Must match the corresponding AssetUniverse.asset_id

    # ── AlphaMind sector assignment ──
    alphamind_sector: AlphaMindSector  # The system's 4-sector grouping. This is the authoritative
                                     # sector assignment for all routing, guardrails, and analysis.
                                     # Diverges from GICS intentionally — GICS puts semis under
                                     # "Information Technology" but AlphaMind separates them because
                                     # semis have distinct catalyst structures (supply chain, export
                                     # controls, fab utilization) that tech doesn't share.
    domain_researcher: str = ""      # Which analysis layer agent receives this ticker's data.
                                     # Values: "tech_semis" | "financials" | "energy"
                                     # Note: tech and semis share a researcher agent per the design
                                     # (03-analysis-layer/domain-researchers/tech-semis.md).

    # ── GICS classification (industry standard) ──
    gics_sector: str = ""            # GICS Level 1 (e.g., "Information Technology", "Financials")
    gics_industry_group: str = ""    # GICS Level 2 (e.g., "Semiconductors & Semiconductor Equipment")
    gics_industry: str = ""          # GICS Level 3 (e.g., "Semiconductors")
    gics_sub_industry: str = ""      # GICS Level 4 (e.g., "Semiconductors") — most granular

    # ── ETF and index relationships ──
    sector_etf: Ticker = ""          # Primary sector ETF used for relative performance (Q1:1e).
                                     # XLK for tech, SMH for semis, XLF for financials, XLE for energy.
                                     # This is the benchmark the distillation layer uses for RS ratios.
    index_memberships: list[str] = field(default_factory=list)
        # Major index memberships: ["SPY", "QQQ", "DIA", etc.]. Used for:
        # - Index rebalance impact estimation (Q12:12a)
        # - Market breadth computation (Q7:7c)
        # - Understanding passive flow exposure
    etf_memberships: list[ETFMembership] = field(default_factory=list)
        # Detailed ETF membership with weights. Includes sector ETFs, broad market
        # ETFs, and thematic ETFs. Used for:
        # - ETF flow impact estimation (Q12:12f)
        # - Understanding how ETF creation/redemption affects this name's volume
        # - Cross-referencing ETF-level flow signals with single-name flow (Q3:3h)

    # ── Peer group ──
    peer_group: str = ""             # Within-sector peer assignment for tighter correlation analysis.
                                     # Tech: "mega_cap_tech", "high_growth_tech", "ai_adjacent"
                                     # Semis: "gpu_compute", "memory", "equipment", "analog_mixed",
                                     #         "eda_ip", "foundry"
                                     # Financials: "money_center_banks", "investment_banks",
                                     #             "payments_networks", "fintech"
                                     # Energy: "integrated_majors", "e_and_p", "midstream_lng",
                                     #          "services"
                                     # Peer groups are used by Q7:7a (intra-sector correlation) to
                                     # define the comparison set for divergence detection. NVDA
                                     # diverging from AMD (same peer group: gpu_compute) is a stronger
                                     # signal than NVDA diverging from ADI (different peer group).
    peer_tickers: list[Ticker] = field(default_factory=list)
        # Ordered list of peer tickers for this name's peer group. Pre-computed
        # so correlation analysis doesn't need to derive it each invocation.
        # Example for NVDA: ["AMD", "AVGO", "TSM"]

    # ── Reclassification history ──
    reclassification_history: list[ReclassificationEvent] = field(default_factory=list)
        # Ordered list of sector/industry reclassifications. Most names have none.
        # When a reclassification occurs, it can trigger:
        # - Forced buying/selling from sector-focused funds (structural flow)
        # - Changes to which analyst agent receives this ticker
        # - Re-initialization of correlation baselines

    # ── Metadata ──
    last_updated: datetime = field(default_factory=datetime.utcnow)
    classification_source: str = "polygon"  # "polygon" | "manual" | "finnhub"
