"""Events: Unified Event Calendar — entity definitions.

One primary entity (`EventCalendar`) covering scheduled and recently-released
events across macro releases (FOMC, CPI/PPI/PCE, NFP, ISM, GDP, Fed speakers,
Treasury auctions) and regulatory / policy / geopolitical events (hearings,
court dates, OPEC, regulatory deadlines). One sector-tagged feed for
analysis-layer consumers; producers (FRED / Finnhub / SEC EDGAR collectors)
write through the shared `event_calendar` storage table keyed on `event_id`
([storage.md § Events](../collector/storage.md)).

The `Event` supporting type carries `affected_sectors` as a required list
(empty for genuinely market-wide events; populated for sector-tilted
releases — e.g., a hot CPI print hits TECH harder than ENERGY). Optional
release-specific fields (`consensus_estimate`, `actual_result`,
`surprise_bps`, `surprise_direction`, `surprise_magnitude`) are populated
for data releases and null for hearings, deadlines, and similar
non-numeric events.
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
)

__all__ = [
    "Event",
    "EventCalendar",
    "EventProximityFlag",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """A single scheduled or recently released event on the unified calendar.

    Carries macro-release dynamics (consensus / actual / surprise) when applicable,
    and the sector-impact tagging that drives analyst sizing around the event.
    """

    event_id: str
    """Unique identifier — stable across updates and reprioritizations.
    Sourced from the vendor where available, else hashed at ingestion."""

    event_type: str
    """Union enumeration over macro releases and policy/regulatory events:
    'fomc' | 'cpi' | 'ppi' | 'pce' | 'nfp' | 'ism' | 'gdp' |
    'fed_speaker' | 'treasury_auction' | 'opec' |
    'congressional_hearing' | 'court_date' | 'regulatory_deadline' |
    'rate_decision_other_central_bank' | 'fda_advisory' |
    'earnings_cluster' | 'other'."""

    event_datetime: datetime
    """UTC timestamp of the event (decision time, release time, or scheduled start)."""

    description: str
    """Human-readable event description (e.g., 'FOMC Rate Decision', 'CPI Release')."""

    affected_sectors: list[AlphaMindSector]
    """Sectors most directly impacted. Empty list for genuinely market-wide events;
    populated for sector-tilted releases (e.g., a hot CPI hits TECH and SEMIS harder
    than ENERGY)."""

    expected_volatility_impact: SignalStrength
    """Pre-event volatility expectation: STRONG (high-impact, e.g., FOMC + CPI),
    MODERATE, WEAK, NONE."""

    consensus_estimate: str | None = None
    """Qualitative consensus or numeric estimate (e.g., 'CPI +0.3% MoM',
    'NFP +150k'). Null for non-release events (hearings, deadlines, court dates)."""

    actual_result: float | None = None
    """Posted result after release. Null pre-release and for non-release events."""

    surprise_bps: float | None = None
    """Actual minus estimate in basis points. Null when not applicable."""

    surprise_direction: Direction = Direction.NEUTRAL
    """BULLISH (beat) | BEARISH (miss) | NEUTRAL. NEUTRAL pre-release."""

    surprise_magnitude: SignalStrength = SignalStrength.NONE
    """Magnitude of the surprise. NONE pre-release."""


@dataclass(frozen=True)
class EventProximityFlag:
    """Alert that a market-moving event is imminent.

    Distillation-layer-configurable horizon (typically 24-48 hours). Sector
    analysts and the Portfolio manager use these to size around event risk.
    """

    next_event: str
    """Event name (e.g., 'CPI Release')."""

    hours_until_event: float
    """Time remaining until the event (hours, fractional for sub-hour resolution)."""

    event_risk_level: SignalStrength
    """STRONG (high-impact event) | MODERATE | WEAK | NONE."""


# ── Primary entity ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventCalendar:
    """Events:CAL — Unified event calendar with consensus, surprise scoring,
    sector tagging, clustering risk, and proximity alerts.

    Producers serialize into the shared `event_calendar` storage table;
    analysis-layer consumers (qualitative-research agent, synthesizer's
    catalyst-watch source) read this single entity.

    Source: Finnhub economic calendar (free) + FRED (macro releases) + SEC EDGAR
    (regulatory deadlines)
    Cadence: Daily
    Feasibility: HIGH — Finnhub economic calendar covers the bulk; manual
    maintenance for any gaps (FOMC dates, OPEC meetings).
    """

    # ── Identity ──
    metadata: InvocationMetadata

    # ── Upcoming events horizon ──
    upcoming_events: list[Event] = field(default_factory=list)
    """Chronologically sorted upcoming events over the next 30 days."""

    next_event: Event | None = None
    """The single most imminent event (None when calendar is empty)."""

    events_this_week: list[Event] = field(default_factory=list)
    """Events scheduled for the current calendar week through Friday close.
    Subset of `upcoming_events`."""

    # ── Imminent event proximity ──
    event_proximity_alerts: list[EventProximityFlag] = field(default_factory=list)
    """Events within the proximity-flag horizon (typically 24-48 hours)."""

    # ── Clustering risk ──
    event_clustering_risk: SignalStrength = SignalStrength.NONE
    """STRONG = 3+ high-impact events within 5 days; MODERATE = 2 high-impact;
    WEAK = 1 or scattered; NONE = no significant events."""

    clustering_description: str = ""
    """Narrative description of which events are clustering and why it matters
    (e.g., 'FOMC decision (Wed) + CPI release (Thu) + earnings cluster (Fri)
    concentrates volatility risk; recommend 50% position sizing reduction')."""

    unscheduled_event_risk_factors: list[str] = field(default_factory=list)
    """Conditions increasing the probability of surprise unscheduled events
    (e.g., 'elevated geopolitical tensions', 'credit spreads widening',
    'Fed speaker on calendar but not yet announced')."""

    # ── Macro surprise index ──
    macro_surprise_index: float = 0.0
    """Aggregate score of whether recent data has been beating or missing
    expectations (analogous to the Citi Economic Surprise Index). Positive =
    data stronger than consensus; negative = weaker. Updated every release."""

    surprise_index_trend: Direction = Direction.NEUTRAL
    """Is the surprise index rising (improving data flow) or falling (deteriorating)?"""

    surprise_index_change_5d: float = 0.0
    """5-day change in the surprise index (how fast perception is shifting)."""

    # ── Historical event impact by sector ──
    event_impact_by_sector: dict[str, float] = field(default_factory=dict)
    """Map of `event_type` → average sector-specific reaction (multiplier on
    absolute market reaction). E.g., {'hot_cpi': 1.8} for tech. Helps the
    analyst size sector-specific theses around events."""

    # ── Most recent surprise release ──
    latest_release: Event | None = None
    """Most recent completed release with surprise scoring populated."""

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., 'event density spike detected', 'unusual
    multi-event clustering pattern')."""
