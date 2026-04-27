"""Qual 5: Regulatory, Policy, and Geopolitical Events — entity definitions.

4 entities covering Fed communications, regulatory actions/rulings,
executive action, and geopolitical events. SEC EDGAR, Federal Register,
and FRED are the primary free sources. Scheduled regulatory and policy
events feed the unified [`EventCalendar`](events.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ._common import (
    AlphaMindSector,
    AnomalyFlag,
    Direction,
    InvocationMetadata,
    SignalStrength,
    Ticker,
)

__all__ = [
    "ExecutiveAction",
    "FedCommunication",
    "GeopoliticalEvent",
    "RegulatoryAction",
    "StatementChange",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StatementChange:
    """Word-level or section-level changes in FOMC statements over time.

    Used by FedCommunication to track evolution of Fed language, which
    is often a leading indicator of policy intent before rate decisions.
    """

    section: str
    """Section of statement (e.g., 'economic_outlook', 'inflation', 'labor_market',
    'monetary_policy_stance', 'forward_guidance')."""

    change_type: str
    """Type of change: 'added' | 'removed' | 'modified'."""

    old_text: str | None
    """Previous wording (None if section was added)."""

    new_text: str | None
    """New wording (None if section was removed)."""

    hawkish_dovish_impact: Direction
    """Directional impact of change: BULLISH (dovish rate cuts/accommodation),
    BEARISH (hawkish tightening/restrictive), NEUTRAL, or MIXED."""


# ── Main entities ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FedCommunication:
    """Qual 5:5a — FOMC statement analysis, press conference tone, and speaker commentary.

    FOMC statement text analysis (word-by-word diffs), press conference Q&A tone,
    minutes debate dynamics (dissent, new topics), Beige Book anecdotal conditions,
    individual speaker commentary (influence-tiered, voting status), non-Fed
    central bank decisions (ECB, BOJ).

    Market impact: Fed language often shifts rate expectations before actual moves.
    Press conference tone and chair body language can repruce sentiment and repricing.
    Dissent in minutes signals growing dovish/hawkish splits; new debate topics
    identify emerging concerns.

    Source: FRED (rate data) + Fed website + Finnhub news
    Fallback: Financial media sweep
    Cadence: FOMC cycle + ad hoc
    Feasibility: MEDIUM — rate decisions from FRED, statement text from Fed website
    """

    communication_type: str
    """Type of communication: 'fomc_statement' | 'press_conference' | 'minutes' |
    'beige_book' | 'speaker_appearance' | 'non_fed_central_bank'."""

    communication_date: datetime
    """UTC datetime of communication (e.g., FOMC statement release, press conference start)."""

    overall_hawkish_dovish_signal: Direction
    """Synthesis of all above: BULLISH (dovish), BEARISH (hawkish), NEUTRAL, MIXED."""

    rate_expectation_impact: str
    """Qualitative description of how this communication shifted rate path expectations
    (e.g., 'markets now pricing 25bp cut at March meeting', 'terminal rate pushed higher')."""

    metadata: InvocationMetadata
    """Pipeline invocation metadata."""

    # ── FOMC Statement fields ────
    statement_changes: list[StatementChange] = field(default_factory=list)
    """Changes from the prior statement. Empty if no statement comparison available."""

    # ── Press Conference fields ────
    chair_tone: Direction | None = None
    """Chair's overall tone in press conference: BULLISH, BEARISH, NEUTRAL, MIXED."""

    key_phrases: list[str] = field(default_factory=list)
    """Notable phrases or statements from the chair (e.g., 'patient on rate cuts',
    'data-dependent', 'inflation sticky')."""

    tone_shift_vs_prior: Direction | None = None
    """Did the chair sound more dovish or hawkish than last communication?
    BULLISH = more dovish, BEARISH = more hawkish, NEUTRAL = same, MIXED if unclear."""

    # ── Minutes fields ────
    dissent_count: int | None = None
    """Number of voting members who dissented or voiced minority views in minutes."""

    new_debate_topics: list[str] = field(default_factory=list)
    """New policy topics debated (e.g., 'bank stress test adequacy', 'climate risk')."""

    hawkish_lean_pct: float | None = None
    """Percentage of members whose language leaned hawkish (0.0-1.0)."""

    # ── Beige Book fields ────
    economic_conditions_summary: str | None = None
    """High-level summary of economic conditions reported by districts."""

    labor_market_assessment: str | None = None
    """Anecdotal labor market commentary (wage pressures, hiring, turnover)."""

    inflation_commentary: str | None = None
    """Anecdotal inflation pressures and pricing dynamics."""

    # ── Individual Speaker fields ────
    speaker_name: str | None = None
    """Name of speaker (e.g., 'Jerome Powell', 'Beth Hammack')."""

    speaker_role: str | None = None
    """Role: 'Chair' | 'Vice Chair' | 'Voting Member' | 'Non-Voting Member'."""

    influence_tier: int | None = None
    """Influence tier: 1=Chair (highest), 2=Vice Chairs, 3=voting members,
    4=non-voting members."""

    is_voting_member: bool | None = None
    """Whether the speaker is a current voting member of the FOMC."""

    hawkish_dovish_shift: Direction | None = None
    """Speaker's hawkish/dovish position relative to consensus: BULLISH, BEARISH, NEUTRAL."""

    key_statement: str | None = None
    """Most important or surprising statement from this speaker."""

    # ── Non-Fed Central Bank fields ────
    central_bank: str | None = None
    """If communication_type is 'non_fed_central_bank': 'ECB' | 'BOJ' | 'BOE' | 'PBOC' | 'SNB'."""

    decision_type: str | None = None
    """Type of decision/announcement: 'rate_decision' | 'qe_change' | 'policy_shift'."""

    was_surprise: bool | None = None
    """Was the decision/announcement unexpected by markets?"""

    # ── Market impact synthesis ────
    equity_sector_implications: dict[str, Direction] = field(default_factory=dict)
    """Per-sector impact inferred from this communication. Keys are sector names
    (e.g., 'tech', 'semis', 'financials', 'energy'); values are Direction."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., 'unprecedented hawkish shift', 'chair tone differs
    sharply from statement')."""


@dataclass(frozen=True)
class RegulatoryAction:
    """Qual 5:5b — SEC, FTC/DOJ, CFPB, EPA, and international regulatory events.

    SEC enforcement/rulemaking, FTC/DOJ antitrust (investigations, lawsuits),
    CFPB/banking regulators (enforcement, stress tests), EPA/energy regulators,
    international regulators (EU DMA, China tech, export controls).

    Market impact: Enforcement actions can repricing a target stock 5-20%. Antitrust
    investigations often trigger 6-12 month repricing horizons as court outcomes
    become clearer. Capital requirement changes directly affect banking sector
    buyback capacity and profitability.

    Source: SEC EDGAR (free) + Federal Register (free) + news APIs
    Fallback: Finnhub
    Cadence: Event-driven
    Feasibility: MEDIUM — SEC and federal regulatory actions from EDGAR/Federal Register
    """

    agency: str
    """Issuing regulatory agency: 'SEC' | 'FTC' | 'DOJ' | 'CFPB' | 'OCC' | 'FDIC' |
    'EPA' | 'EU_DMA' | 'CHINA_TECH' | 'BIS_EXPORT_CONTROLS' | 'OTHER'."""

    action_type: str
    """Type of action: 'enforcement' | 'rulemaking' | 'investigation' | 'lawsuit' |
    'settlement' | 'ruling' | 'guidance'."""

    action_stage: str
    """Stage in regulatory process: 'preliminary' | 'formal_investigation' |
    'complaint_filed' | 'trial' | 'ruling' | 'appeal'. Tracks progression."""

    action_date: date
    """Date action was filed/announced."""

    headline: str
    """Headline of the action (e.g., 'SEC charges Tesla and Elon Musk with fraud')."""

    description: str
    """Detailed description of the action and allegations."""

    severity: SignalStrength
    """Severity of the action for market price impact: STRONG, MODERATE, WEAK, NONE."""

    expected_price_impact_direction: Direction
    """Expected direction of price impact on affected tickers: BULLISH, BEARISH, NEUTRAL."""

    is_surprise: bool
    """Was this action expected or unexpected? True = surprise."""

    metadata: InvocationMetadata
    """Pipeline invocation metadata."""

    affected_tickers: list[Ticker] = field(default_factory=list)
    """List of directly affected ticker symbols."""

    affected_sectors: list[AlphaMindSector] = field(default_factory=list)
    """Affected sectors (e.g., regulatory cap on bank lending limits Financials sector)."""

    primary_ticker: Ticker | None = None
    """If targeting a specific company, its ticker symbol."""

    prediction_market_probability: float | None = None
    """Cross-reference with Qual 3:3b prediction market odds if applicable.
    E.g., 'conviction in FTC win against Big Tech was 65%'."""

    compliance_cost_estimate: str | None = None
    """Qualitative assessment of financial impact
    (e.g., '$500M-$1B in legal fees and settlements', 'Minimal impact')."""

    precedent_setting: bool = False
    """Could this ruling set precedent for similar actions against other companies?"""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., 'unexpected enforcement pivot',
    'broader scope than anticipated')."""


@dataclass(frozen=True)
class ExecutiveAction:
    """Qual 5:5c — Executive orders and administrative actions on trade, tech, and energy.

    Executive orders (trade, technology policy, energy, financial regulation),
    administrative actions and orders.

    Market impact: Trade tariffs repricing typically happens over 1-4 weeks as
    supply chain implications become clear. Tech policy (AI regulation, chip export
    controls) can reprize semiconductors and tech cap stocks 5-15% intraday.
    Energy policy reprices crude/natgas and integrated oil/E&P companies.

    Source: Federal Register (free) + news APIs
    Cadence: Event-driven
    Feasibility: MEDIUM
    """

    action_type: str
    """Type of action: 'executive_order' | 'administrative_action' | 'presidential_memorandum'."""

    action_date: date
    """Date action was signed/announced."""

    title: str
    """Title of executive order or action."""

    description: str
    """Detailed description of the action and intended impact."""

    policy_domain: str
    """Policy area: 'trade' | 'technology' | 'energy' | 'financial_regulation' |
    'ai_regulation' | 'semiconductor_policy'."""

    affected_sectors: list[AlphaMindSector]
    """Affected sectors (e.g., tariffs on semiconductors affects Semis and Tech)."""

    market_impact_direction: Direction
    """Expected direction of broad market impact: BULLISH, BEARISH, NEUTRAL."""

    severity: SignalStrength
    """Market impact severity: STRONG, MODERATE, WEAK, NONE."""

    is_surprise: bool
    """Was this action unexpected? True = surprise announcement."""

    implementation_timeline: str
    """When does this take effect?
    (e.g., 'Immediately', '30 days', '6 months', 'Upon subsequent rulemaking')."""

    reversal_risk: str
    """Could this be reversed by courts or future administration?
    (e.g., 'Low (executive order)', 'Moderate (likely challenged)', 'High (unpopular)')."""

    metadata: InvocationMetadata
    """Pipeline invocation metadata."""

    affected_tickers: list[Ticker] = field(default_factory=list)
    """Directly affected ticker symbols."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., 'unexpected reversal of prior policy', 'market repricing
    faster than historical norm')."""


@dataclass(frozen=True)
class GeopoliticalEvent:
    """Qual 5:5d — Geopolitical developments across key theaters affecting markets.

    Taiwan Strait (rhetoric, military, diplomacy), Middle East/energy supply
    (conflict, sanctions, production), Russia-Ukraine/European energy, China
    economic policy (stimulus, property, trade, PBOC), OPEC+ dynamics,
    trade agreements/disputes (tariffs, WTO rulings).

    Market impact: Taiwan Strait escalation reprices semis 10-30% within hours
    due to supply chain concentration risk. Middle East conflicts reprices energy
    5-20% and broad equities via risk-off. China property/stimulus news reprices
    commodity exporters and semiconductors. OPEC+ production changes reprices
    crude and integrated oils over days/weeks.

    Source: News APIs + prediction markets (Qual 3:3c)
    Cadence: Event-driven
    Feasibility: MEDIUM — systematic monitoring via news pipeline + prediction market odds
    """

    theater: str
    """Geopolitical theater: 'taiwan_strait' | 'middle_east' | 'russia_ukraine' |
    'china_economic' | 'opec_plus' | 'trade_disputes' | 'other'."""

    event_date: date
    """Date event occurred/announced."""

    headline: str
    """Headline of the event (e.g., 'PLA Conducts Military Drills Near Taiwan')."""

    description: str
    """Detailed description of the event and context."""

    escalation_direction: Direction
    """Is this escalating or de-escalating? BEARISH = escalation (bad for risk appetite),
    BULLISH = de-escalation (good for risk appetite), NEUTRAL, MIXED."""

    escalation_level: int
    """Current escalation level (1-5): 1=rhetorical/diplomatic, 2=minor military activity,
    3=significant military maneuvers/sanctions, 4=limited conflict/blockade,
    5=active warfare/full crisis."""

    prior_escalation_level: int
    """Prior escalation level for comparison."""

    affected_sectors: list[AlphaMindSector]
    """Sectors most exposed: semis for Taiwan, energy for Middle East, etc."""

    supply_chain_disruption_risk: SignalStrength
    """Risk of supply chain disruption: STRONG, MODERATE, WEAK, NONE."""

    risk_appetite_impact: Direction
    """Broad market risk sentiment impact: BULLISH (risk-on), BEARISH (risk-off), NEUTRAL."""

    metadata: InvocationMetadata
    """Pipeline invocation metadata."""

    affected_tickers: list[Ticker] = field(default_factory=list)
    """Specific tickers expected to be repriced."""

    primary_commodity_impact: str | None = None
    """Which commodity is most directly affected: 'crude' | 'natgas' | 'copper' |
    'semiconductors' | 'other'."""

    prediction_market_cross_ref: float | None = None
    """Probability estimate from Qual 3:3c prediction markets
    (e.g., '15% probability of Taiwan military action within 90 days')."""

    is_unscheduled: bool = False
    """Surprise event (unscheduled)? True triggers priority routing to Portfolio manager."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (e.g., 'unexpected escalation', 'market repricing slower than
    geopolitical severity')."""
