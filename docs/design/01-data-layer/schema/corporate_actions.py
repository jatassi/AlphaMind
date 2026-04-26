"""Q12: Corporate Actions and Structural Flows — entity definitions.

6 entities covering index rebalance, buyback execution, regulatory filings,
investor event calendar, lock-up/secondary calendar, and ETF flow impact.
These are event-driven signals that create predictable structural flows.

Design note:
    All entities are per-ticker (they track events for specific universe names).
    Event-driven signals create mechanical or predictable flows: index additions
    force passive fund buying, buyback blackout lifts restore corporate bid,
    regulatory filings create asymmetric setups. Predictability of flow is what
    makes these valuable — they're closer to physics than psychology.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Optional

from ._common import (
    AnomalyFlag,
    DataConfidence,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
)


# ── Supporting enums ─────────────────────────────────────────────────────────

class IndexName(Enum):
    """Major indices tracked for rebalance/reconstitution announcements."""
    SP500 = "sp500"           # S&P 500
    NASDAQ100 = "nasdaq100"   # NASDAQ-100
    RUSSELL1000 = "russell1000"  # Russell 1000
    RUSSELL2000 = "russell2000"  # Russell 2000


class RegulatoryFilingType(Enum):
    """Classification of SEC regulatory filings."""
    SCHEDULE_13D = "schedule_13d"        # Activist investor 5%+ ownership disclosure
    FORM_8K = "form_8k"                  # Material event disclosure
    HSR_FILING = "hsr_filing"            # Hart-Scott-Rodino M&A pre-merger notification
    SHELF_S3 = "shelf_s3"                # S-3 shelf registration for future offerings
    PROXY_FIGHT = "proxy_fight"          # DFAN14A proxy contest filing


class EventType(Enum):
    """Classification of corporate events embedded in regulatory filings."""
    EXECUTIVE_DEPARTURE = "executive_departure"
    MERGER_ACQUISITION = "merger_acquisition"
    CREDIT_FACILITY = "credit_facility"
    MATERIAL_IMPAIRMENT = "material_impairment"
    STRATEGIC_PIVOT = "strategic_pivot"
    LITIGATION = "litigation"
    OTHER = "other"


class LockupStatus(Enum):
    """Status of IPO lock-up period."""
    ACTIVE = "active"          # Lock-up still in effect, insiders cannot sell
    EXPIRING_SOON = "expiring_soon"  # Expires within 30 days
    EXPIRED = "expired"        # Lock-up has expired


# ── Supporting types ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndexAddRemove:
    """An add or remove event from an index."""
    index: IndexName
    event_type: str            # "add" | "remove"
    announcement_date: date    # Date the change was announced
    effective_date: date       # Date the change becomes effective (rebalance date)
    prior_weight: Optional[float] = None  # Prior index weight %
    new_weight: Optional[float] = None    # New index weight %
    estimated_passive_flow_usd: Optional[float] = None  # Estimated $ flow from passive funds


@dataclass(frozen=True)
class BuybackBlackout:
    """A blackout period when corporate buybacks are restricted."""
    blackout_start: datetime   # Start of blackout (typically earnings announcement - 14 days)
    blackout_end: datetime     # End of blackout (typically +2 trading days after earnings release)
    reason: str                # "earnings_blackout" | "material_event" | "custom"
    description: str = ""      # Human-readable explanation


@dataclass(frozen=True)
class RegulatoryEvent:
    """A single regulatory filing event."""
    filing_type: RegulatoryFilingType
    filing_date: datetime      # When filed with SEC (UTC)
    announcement_date: Optional[datetime] = None  # When publicly announced (may differ from filing)
    event_type: Optional[EventType] = None  # Classification of the material event (if 8-K)
    description: str = ""      # Human-readable summary for LLM consumption
    url: str = ""              # Link to EDGAR filing or news source
    market_impact_estimate: SignalStrength = SignalStrength.MODERATE


@dataclass(frozen=True)
class InvestorEvent:
    """A scheduled corporate event for investor engagement."""
    event_name: str            # "Investor Day" | "JPMorgan Conference" | "Goldman Sachs Conference"
    event_date: date           # Date of the event
    event_type: str            # "investor_day" | "analyst_day" | "capital_markets_day" | "conference"
    is_virtual: bool = False   # True if virtual/hybrid format
    agenda_topics: list[str] = field(default_factory=list)  # Key topics (e.g., "cost reduction", "2025 guidance")
    novelty_flag: bool = False  # True if unusual scheduling (hasn't held event in 2+ years, etc.)
    historical_post_event_drift_1d: float = 0.0  # Historical avg return in 1 day post-event
    historical_post_event_drift_5d: float = 0.0  # Historical avg return in 5 days post-event


@dataclass(frozen=True)
class SecondaryOffering:
    """A secondary or follow-on offering of shares."""
    offering_type: str         # "secondary" | "follow_on" | "shelf_takedown"
    announcement_date: date
    estimated_shares: Optional[int] = None  # Number of shares being offered (if known)
    estimated_dilution_pct: Optional[float] = None  # Estimated dilution % to existing shareholders
    offering_price_range: Optional[tuple[float, float]] = None  # (low, high) price estimate


# ── Primary entities ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IndexRebalance:
    """Q12:12a — Index add/remove events with estimated passive flow magnitude.

    S&P 500, NASDAQ 100, Russell reconstitution announcements, estimated passive
    flow magnitude, quarterly weight changes, float/share count changes,
    rebalance date proximity.

    When an index adds a stock, passive funds MUST buy. When removed, they must sell.
    Flow magnitude is quantifiable (estimated from AUM), timing is known, direction
    is forced — mechanical and predictable.

    Source: Index provider announcements + Finnhub
    Fallback: SEC EDGAR, financial news APIs
    Cadence: Event-driven
    Feasibility: MEDIUM — S&P/NASDAQ announce publicly
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Index add/remove events ──
    current_indices: list[IndexName] = field(default_factory=list)
        # Which major indices (SP500, NASDAQ100, etc.) currently include this ticker.
        # Updated as adds/removes are announced and become effective.

    pending_adds: list[IndexAddRemove] = field(default_factory=list)
        # Announced (but not yet effective) additions to indices. These create
        # forward expectations of passive flow magnitude.

    pending_removes: list[IndexAddRemove] = field(default_factory=list)
        # Announced removals. Sellers have time to front-run; the stock often starts
        # declining before the effective date as forward-looking traders sell ahead.

    # ── Current weight context ──
    sp500_weight_pct: float = 0.0  # Current S&P 500 weight (for universe names in the index)
    nasdaq100_weight_pct: float = 0.0  # Current NASDAQ-100 weight
    russell_weight_pct: float = 0.0    # Current Russell 1000/2000 weight (whichever is applicable)

    # ── Quarterly reconstitution tracking ──
    next_quarterly_rebalance_date: Optional[date] = None
        # When is the next scheduled index reconstitution / quarterly rebalance that
        # might affect this ticker's weight. Used for flagging "rebalance date proximity."
    recent_weight_change_pct: Optional[float] = None
        # Percentage change in index weight from the most recent rebalance.
        # Positive = heavier, negative = lighter. Even without adds/removes,
        # market cap changes create rebalance flows.

    # ── Float and share-count events ──
    recent_stock_split: bool = False   # True if stock split occurred recently (affects index weight calc)
    recent_share_offering: bool = False  # True if secondary offering priced recently
    buyback_reducing_shares: bool = False  # True if active buyback is reducing float

    # ── Estimated passive flow magnitude ──
    estimated_flow_magnitude_usd: Optional[float] = None
        # Rough estimate of passive fund flow magnitude (in USD millions) when a pending
        # add becomes effective. Derived from: estimated AUM of index × weight change.
        # Larger flow magnitude = stronger and more sustained price impact.

    # ── Anomalies detected ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class BuybackProgram:
    """Q12:12b — Corporate buyback authorization, execution pace, and blackout windows.

    Active program status (authorization size, execution pace), blackout windows
    (~2 weeks before through ~2 days after earnings), buyback as % of daily
    volume, 10b5-1 plan indicators.

    The company as a buyer of its own stock. Active buyback programs create a steady
    bid, but it's gated by regulatory (blackout windows) and corporate calendar
    constraints. Blackout absence = corporate bid absence = vulnerability to selling.

    Source: SEC EDGAR (10-Q/10-K free)
    Fallback: Earnings transcript extraction
    Cadence: Quarterly
    Feasibility: MEDIUM — authorization from filings, blackout from earnings calendar
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Active program status ──
    program_active: bool = False      # True if an authorized buyback program exists
    authorization_remaining_usd: Optional[float] = None
        # Remaining authorization size (in USD millions). When authorization is nearly
        # exhausted, the company may announce a new program soon (forward signal).
    authorization_remaining_shares: Optional[int] = None
        # Remaining authorization in share count (alternative/complementary view).

    # ── Execution pace ──
    recent_buyback_pace_monthly_usd: Optional[float] = None
        # Average monthly buyback spend over the trailing 3 months (USD millions).
        # Combined with daily volume, this indicates buyback as % of volume.
    buyback_as_pct_daily_volume: Optional[float] = None
        # Estimated buyback execution as percentage of typical daily volume.
        # For mega-caps (AAPL, GOOG, META), this can be 5–15% of daily volume.
        # Presence/absence shifts supply/demand balance materially.

    # ── Blackout windows (earnings calendar-driven) ──
    current_blackout_window: Optional[BuybackBlackout] = None
        # If in a blackout period, details on when it started and when it ends.
        # Typically ~2 weeks before earnings through ~2 trading days after.
    next_blackout_window: Optional[BuybackBlackout] = None
        # Next known blackout (usually around next earnings announcement).
    blackout_lift_date: Optional[datetime] = None
        # Estimated datetime when the current blackout window closes and buybacks
        # can resume. Useful for near-term flow expectations.

    # ── 10b5-1 plan status ──
    has_10b5_1_plan: bool = False    # True if the company has pre-scheduled automatic buyback plans
    plan_expiration_date: Optional[date] = None  # When the current 10b5-1 plan expires
        # Pre-scheduled plans provide a more predictable bid than discretionary buybacks.
        # 10b5-1 execution is mechanical and not subject to blackout restrictions
        # (though companies often stop non-10b5-1 buybacks during blackouts).

    # ── Recent execution context ──
    last_buyback_date: Optional[datetime] = None  # Last recorded buyback execution
    days_since_last_buyback: int = 0   # Convenience field: today - last_buyback_date
    in_blackout_period: bool = False   # Cached convenience flag

    # ── Anomalies detected ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class RegulatoryFiling:
    """Q12:12c — SEC filings classified by type and impact (13D, 8-K, HSR, S-3, proxy).

    Schedule 13D (activist 5% threshold), 8-K (material events by type),
    HSR filings (M&A pre-merger), shelf registrations (S-3), proxy fight
    filings (DFAN14A).

    Sporadic but high-signal when they fire. SEC filings create asymmetric setups
    because the market takes time to digest them. EDGAR RSS feeds are near real-time.

    Source: SEC EDGAR RSS feeds (free)
    Fallback: Finnhub
    Cadence: Real-time (EDGAR RSS updates within seconds)
    Feasibility: HIGH — EDGAR RSS feeds are near real-time
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Recent filings ──
    recent_filings: list[RegulatoryEvent] = field(default_factory=list)
        # Recent regulatory filings (last 90 days). Each entry captures filing type,
        # date, material event classification (for 8-Ks), and estimated market impact.
        # Sorted by filing_date descending (most recent first).

    # ── Schedule 13D (activist filing) ──
    active_13d: Optional[RegulatoryEvent] = None
        # If a current Schedule 13D filing exists, this field captures the most recent.
        # 13D = activist investor crossing 5% ownership with intent to influence.
        # "Someone is coming for this company" signal. Triggers repricing over days
        # as market assesses the activist's thesis and likely actions.
    activist_ownership_pct: Optional[float] = None
        # Disclosed ownership % of the activist(s) filing 13D (if available).

    # ── Form 8-K (material events) ──
    recent_8k_filings: list[RegulatoryEvent] = field(default_factory=list)
        # Recent 8-K filings classified by event type. Material events include:
        # executive departures, M&A announcements, credit facility changes,
        # material impairments, strategic pivots. Each entry has event_type
        # and human-readable description.

    # ── HSR (M&A pre-merger notification) ──
    active_hsr_filing: Optional[RegulatoryEvent] = None
        # If an HSR filing is pending or in review, this field captures the filing.
        # HSR = Hart-Scott-Rodino pre-merger notification. Flags M&A intent and
        # timing uncertainty (review period varies: 30 days typical, can extend).

    # ── Shelf registration (S-3) ──
    active_shelf_registration: Optional[RegulatoryEvent] = None
        # If a shelf registration (S-3) is active, captures the filing. Shelf = quiet
        # warning that dilutive supply is coming. Gap between registration and actual
        # offering is a window of uncertainty. Likely timing is flagged by shelf_takedown_risk.
    shelf_registration_amount_usd: Optional[float] = None
        # Total $ amount registered (in USD millions) under the active shelf.
    shelf_registration_remaining_usd: Optional[float] = None
        # Remaining amount available to be drawn down (may differ if partial takedowns occurred).

    # ── Proxy fight filings ──
    active_proxy_fight: Optional[RegulatoryEvent] = None
        # If a proxy contest (DFAN14A or similar) is active, captures the filing.
        # Signals contested board elections or activist campaigns. Creates event-driven
        # volatility with a known resolution date (proxy vote date).
    proxy_vote_date: Optional[date] = None  # When the proxy vote/shareholder meeting occurs

    # ── Overall filing activity ──
    filing_frequency_last_90d: int = 0     # How many SEC filings in the last 90 days
    has_material_pending_disclosures: bool = False  # True if material events are in progress
                                     # (merger pending, litigation unresolved, etc.)

    # ── Anomalies detected ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class InvestorEventCalendar:
    """Q12:12d — Analyst days, conferences, and corporate event scheduling.

    Investor day / analyst day / capital markets day dates, industry conference
    presentations, event novelty signal (unusual scheduling), post-event drift
    tracking (1–5 day).

    Scheduled events where companies present to institutional investors outside the
    normal earnings cycle. Don't get the same attention as earnings but can move
    stocks 5–10% on strategy updates or guidance changes.

    Source: Finnhub (free tier)
    Fallback: Company IR pages (manual)
    Cadence: Weekly check
    Feasibility: MEDIUM — Finnhub has IPO/FDA calendars, full coverage needs manual maintenance
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Upcoming investor events ──
    upcoming_events: list[InvestorEvent] = field(default_factory=list)
        # Scheduled investor events (next 180 days). Includes investor days, analyst
        # days, capital markets days, and conference presentations.
        # Sorted by event_date ascending (nearest first).

    # ── Next event context ──
    next_event_date: Optional[date] = None
        # Date of the nearest upcoming investor event (convenience field).
    next_event_name: str = ""              # Name of next event
    days_until_next_event: int = 999       # Days until next event (high value if none scheduled)

    # ── Event novelty detection ──
    last_investor_day_date: Optional[date] = None
        # When was the last investor day held? Used to detect "unusual scheduling."
    days_since_last_investor_day: int = 0
    has_novelty_flag: bool = False         # True if company just scheduled an investor day
                                     # after not holding one for 2+ years (potential signal)

    # ── Historical post-event drift patterns ──
    post_event_drift_avg_1d: float = 0.0   # Average return in 1 day following investor events
    post_event_drift_avg_5d: float = 0.0   # Average return in 5 days following investor events
    post_event_drift_sample_size: int = 0  # How many historical events contributed to the average
        # Names with consistent positive post-event drift use events to deliver positive
        # surprises; names with negative drift use events to reset expectations downward.

    # ── Recent event outcomes ──
    most_recent_event_drift: Optional[float] = None
        # Return in the 5 days after the most recent completed investor event.
        # Used to assess whether recent events are tracking the historical pattern.

    # ── Anomalies detected ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class LockupSecondaryCalendar:
    """Q12:12e — IPO lock-up expirations and secondary offering tracking.

    IPO lock-up expiration dates (~18 months post-IPO), lock-up overhang
    (insider ownership × post-IPO performance), secondary/follow-on offering
    announcements, shelf takedown risk.

    Known supply events that create predictable selling pressure. Insiders holding
    unrealized gains at lock-up expiration are more likely to sell.

    Source: SEC EDGAR S-1/S-3 filings (free)
    Fallback: Finnhub IPO calendar
    Cadence: Event-driven
    Feasibility: MEDIUM — lock-up dates from S-1, secondaries from S-3
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── IPO lock-up status ──
    is_recently_ipo: bool = False           # True if ticker IPO'd < 24 months ago
    ipo_date: Optional[date] = None         # Date the company went public
    days_since_ipo: int = 0                 # Convenience: today - ipo_date
    current_lock_up_status: LockupStatus = LockupStatus.EXPIRED

    # ── Lock-up expiration tracking ──
    lock_up_expiration_date: Optional[date] = None
        # Date when insiders/early investors can first sell (typically 180 days post-IPO).
        # Known catalyst for downward pressure on some names.
    days_until_lock_up_expiration: int = 999  # Days remaining (high value if lock-up expired)

    # ── Lock-up overhang (supply pressure estimate) ──
    insider_ownership_pct: Optional[float] = None
        # Estimated % of shares held by insiders. At lock-up expiration, these shares
        # become freely sellable.
    stock_performance_since_ipo_pct: Optional[float] = None
        # Return since IPO (%). Large gains = large unrealized profits for insiders =
        # higher selling pressure at lock-up expiration.
    estimated_lock_up_overhang_pct: Optional[float] = None
        # Rough estimate of selling pressure at lock-up expiration.
        # Derived from: insider_ownership × stock performance. High overhang = higher
        # risk of downward pressure when insiders can sell.

    # ── Secondary and follow-on offerings ──
    pending_secondaries: list[SecondaryOffering] = field(default_factory=list)
        # Announced secondary or follow-on offerings. Dilutive supply that creates
        # direct selling pressure. The announcement itself often moves the stock
        # more than the actual pricing.

    # ── Shelf takedown risk ──
    has_active_shelf: bool = False          # True if active shelf registration exists
    shelf_takedown_risk: SignalStrength = SignalStrength.NONE
        # Qualitative assessment of likelihood of an imminent offering from active shelf.
        # Based on: stock price performance, capital needs (debt ratios, cash burn),
        # market conditions (is the market receptive to equity offerings?).
        # STRONG = likely offering coming; MODERATE = possible; WEAK = unlikely.

    # ── Anomalies detected ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)


@dataclass(frozen=True)
class ETFFlowImpact:
    """Q12:12f — Sector ETF creation/redemption flows and rebalance impact.

    Sector ETF flow magnitude (daily creation/redemption), ETF-driven volume
    estimate (% of ticker daily volume), thematic ETF rebalance (AI, cyber,
    clean energy), ETF flow vs. single-name divergence.

    Mechanical flows from ETF structure that can distort single-name signals.
    When a sector ETF sees large inflows, authorized participants must buy the
    underlying stocks in proportion — creating correlated buying pressure
    independent of any fundamental view.

    Source: Derived from ETF volume + NAV
    Fallback: Yahoo Finance ETF data
    Cadence: Daily
    Feasibility: MEDIUM — ETF flow estimation from creation/redemption units
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Sector ETF holdings and weight ──
    sector_etf_holdings: dict[str, float] = field(default_factory=dict)
        # Map of ETF ticker -> weight % in that ETF. Example: {"XLK": 2.5, "QQQ": 1.8}
        # Shows which major sector/thematic ETFs hold this ticker and at what weight.

    # ── Daily ETF creation/redemption activity ──
    sector_etf_net_flow_shares: int = 0    # Net shares created/redeemed by APs today
    sector_etf_net_flow_usd: float = 0.0   # Dollar value of net creation/redemption (USD millions)
    etf_flow_direction: Direction = Direction.NEUTRAL
        # BULLISH = net creation (inflows), BEARISH = net redemption (outflows), NEUTRAL = balanced

    # ── ETF-driven volume contribution ──
    etf_driven_volume_pct: Optional[float] = None
        # Estimated percentage of this ticker's daily volume attributable to ETF
        # creation/redemption. When elevated (>10%), single-name flow signals in
        # Q2 (order flow, dark pools) may be misleading. What looks like institutional
        # buying might actually be ETF-driven mechanical buying.

    # ── Thematic ETF rebalance tracking ──
    thematic_etf_holdings: dict[str, float] = field(default_factory=dict)
        # Map of thematic ETF ticker -> weight %. Examples: {"AI": 3.2, "CYBER": 1.8}
        # Thematic ETFs (AI, cybersecurity, clean energy, semiconductor, etc.) have
        # rebalance events independent of sector ETFs, creating flows not captured
        # by standard sector tracking.
    thematic_etf_recent_rebalance: bool = False
        # True if any thematic ETF holding this ticker rebalanced in the last 5 days
    next_thematic_etf_rebalance_date: Optional[date] = None
        # Estimated date of next thematic ETF rebalance

    # ── ETF flow vs. single-name flow divergence ──
    etf_vs_singlenameflow_divergence: bool = False
        # True if the distillation layer detected a conflict: ETF-level flows and
        # single-name flows sending opposite signals. Example: ETF outflows paired
        # with single-name institutional buying suggests someone using ETF sell as
        # cover to accumulate individual names — potential asymmetric setup.
    divergence_signal_strength: SignalStrength = SignalStrength.NONE
        # STRONG = clear, sustained divergence; MODERATE = noisy; WEAK/NONE = no signal

    # ── Daily flow summary ──
    total_etf_activity_summary: str = ""   # Human-readable summary for LLM consumption:
                                     # e.g., "XLK inflows ($500M) driving 12% of daily volume"

    # ── Anomalies detected ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
