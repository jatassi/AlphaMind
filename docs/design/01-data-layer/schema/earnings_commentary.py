"""Qual 4: Earnings and Management Commentary — entity definitions.

4 entities covering management tone analysis, analyst Q&A dynamics,
cross-company earnings intelligence (combining sector-level bellwether reads and
supply chain commentary), and conference/investor day events. The transcript
pipeline (Quartr → Motley Fool → YouTube+Whisper) feeds the first two; cross-
company intelligence derives from them; conference data comes from SEC 8-K and
IR calendars.

Key insights:
    - Management tone shifts are LEADING indicators of future guidance revisions.
      Linguistic shifts ("we expect" → "we hope", "strong" → "resilient") signal
      cautiousness before numbers are revised.
    - Non-answers are HIGH-signal bearish indicators. Evasion on topics the market
      cares about (capex plans, competitive position, demand visibility) indicates
      management concern.
    - First reporter's commentary in a earnings season sets sector expectations;
      peers' guidance is anchored to bellwether tone and beat/miss ratio.
    - Capex commentary from hyperscalers on AI spending is the single most important
      qualitative catalyst for tech/semis on a 4-72 hour horizon.
    - Conference presentations (GTC, re:Invent, AWS announcements) can reprice
      names significantly within the thesis horizon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from ._common import (
    AlphaMindSector,
    AnomalyFlag,
    DataConfidence,
    Direction,
    InvocationMetadata,
    Ticker,
)

__all__ = [
    "AnalystQADynamics",
    "BellwetherRead",
    "ConferencePresentation",
    "CrossCompanyEarningsIntel",
    "ForwardLookingStatement",
    "ManagementToneAnalysis",
    "NonAnswerFlag",
    "QAExchange",
    "QuestionTheme",
    "SupplyChainSignal",
    "TargetUpdate",
    "TopicEmphasis",
]


# ── Supporting types for ManagementToneAnalysis ──────────────────────────────


@dataclass(frozen=True)
class ForwardLookingStatement:
    """A forward-looking claim extracted from management prepared remarks.

    These statements (guidance, expectations, assumptions) are the raw material for
    assessing management confidence and identifying narrative regime changes.
    """

    text: str
    """The extracted statement verbatim or near-verbatim."""

    topic: str
    """Categorization: 'demand', 'margins', 'capex', 'competition', 'macro', 'product'."""

    sentiment: Direction
    """Is the statement optimistic (bullish), cautious (bearish), or neutral?"""

    confidence_level: float
    """Explicit or implicit confidence (0.0-1.0). High = "expect", Low = "hope/may"."""


@dataclass(frozen=True)
class TopicEmphasis:
    """Time allocation and emphasis signals from prepared remarks analysis.

    Topics management spends the most time on are signals of what they're most
    concerned about or excited about. Shifts in emphasis across quarters are
    regime-change indicators.
    """

    topic: str
    """Topic name: 'AI', 'margins', 'capex', 'supply_chain', 'competition', etc."""

    time_allocation_pct: float
    """Percentage of prepared remarks devoted to this topic (0.0-100.0)."""

    vs_prior_quarter_change: float
    """Change in time allocation vs. prior quarter (-100.0 to +100.0 pct points)."""


# ── Supporting types for AnalystQADynamics ───────────────────────────────────


@dataclass(frozen=True)
class QuestionTheme:
    """A clustered theme of analyst questions from the Q&A section.

    Clustering reveals what the institutional investor base collectively cares about
    most. Dominance in certain themes signals high uncertainty or skepticism.
    """

    theme: str
    """Cluster label: 'demand', 'competition', 'margins', 'capex', 'guidance_confidence'."""

    question_count: int
    """Number of questions in this theme."""

    pct_of_questions: float
    """Percentage of total questions (0.0-100.0)."""

    is_dominant_concern: bool
    """True if this is the #1 concern by question volume."""


@dataclass(frozen=True)
class NonAnswerFlag:
    """Detected evasion or non-answer in Q&A exchange.

    When management evades or declines to answer questions on topics investors care
    about (capex plans, competitive threats, demand visibility), it's a bearish signal.
    Severity indicates how much pressure the analyst applied.
    """

    question_topic: str
    """The topic management dodged: 'capex', 'competition', 'demand', 'margins'."""

    evasion_type: str
    """Type of evasion: 'deflection', 'redirect', 'silence', 'boilerplate'."""

    severity: str
    """How direct was the dodge: 'high' (clear avoidance), 'medium', 'low' (partial answer)."""


@dataclass(frozen=True)
class QAExchange:
    """A notable question-answer pair from the earnings call.

    High-signal exchanges are those where the analyst pressed hard or management
    revealed important information (or failed to provide it).
    """

    analyst_firm: str
    """The analyst's firm: 'Goldman Sachs', 'Morgan Stanley', etc."""

    question_topic: str
    """What the analyst asked about: 'capex', 'competition', 'guidance'."""

    was_direct_answer: bool
    """True if management provided a clear, direct answer; False if evasive."""

    significance: str
    """Why this exchange matters: 'guidance_change', 'competitive_threat', 'capex_surprise'."""


# ── Supporting types for CrossCompanyEarningsIntel ────────────────────────────


@dataclass(frozen=True)
class BellwetherRead:
    """Sector bellwether earnings commentary that sets expectations for peers.

    The first reporter in a season — often a mega-cap like NVDA or AAPL —
    establishes tone and beat/miss anchors. Peer guidance is unconsciously
    calibrated to bellwether remarks.
    """

    ticker: Ticker
    """The bellwether company's ticker."""

    reported_date: date
    """Date the bellwether reported earnings."""

    demand_commentary_direction: Direction
    """Management's framing of demand: bullish, bearish, neutral, mixed."""

    pricing_commentary: str | None
    """Any commentary on pricing power, ASP trends, or margin outlook."""

    competitive_commentary: str | None
    """How management framed competitive intensity and market share."""

    macro_commentary: str | None
    """Management's framing of macro environment and customer spending intentions."""

    implications_for_sector: str
    """Synthesis: how does this bellwether read affect sector peers' expected guidance?"""


@dataclass(frozen=True)
class SupplyChainSignal:
    """Customer/supplier commentary extracted from company earnings.

    When Ticker A mentions Ticker B (a customer or supplier) in remarks, it's
    supply-chain intel. If NVDA talks about rising data-center demand, that's
    bullish for ASML, QCOM, etc. If Intel mentions margin pressure from TSMC,
    that's competitive context.
    """

    reporting_ticker: Ticker
    """The company that made the remark."""

    mentioned_ticker: Ticker
    """The company being discussed."""

    relationship: str
    """'customer', 'supplier', or 'competitor'."""

    commentary_direction: Direction
    """Tone of the reference: bullish, bearish, neutral, mixed."""

    key_quote: str
    """Verbatim or near-verbatim excerpt for LLM context."""


# ── Supporting types for ConferencePresentation ───────────────────────────────


@dataclass(frozen=True)
class TargetUpdate:
    """Long-term target guidance announced at a conference or investor day.

    Analyst/investor day updates to long-term targets (5-year revenue, EPS growth
    rates, margin targets) are high-conviction signals of management's confidence
    in the strategy.
    """

    metric: str
    """The target metric: 'revenue_cagr', 'eps_growth', 'fcf_growth', 'margin_target'."""

    prior_target: float | None
    """Prior guidance or consensus on this metric (if available)."""

    new_target: float
    """The new target value (absolute or growth rate, depending on metric)."""

    target_year: int
    """Year the target applies to (e.g., 2030)."""


# ── Main entities ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManagementToneAnalysis:
    """Qual 4:4a — Tone shift detection and forward-looking statement extraction.

    Captures management's confidence level shift across quarters via linguistic
    analysis of prepared remarks. Tone shifts (e.g., "we expect strong demand"
    → "we expect resilient demand") are LEADING indicators of guidance revisions.

    Also tracks forward-looking statement extraction, emphasis topics (where
    management spent time), hedging language (qualification vs. certainty),
    and narrative regime changes (new topics introduced).

    Per-ticker entity. Source: Motley Fool transcripts (free, <24hr) + Quartr
    (~95% coverage) + YouTube/Whisper fallback. Fallback: FMP Ultimate ($149/mo).
    Cadence: Earnings season. Feasibility: MEDIUM — multi-source transcript
    pipeline with NLP tone classification.
    """

    ticker: Ticker
    """Company ticker."""

    earnings_date: date
    """Date the earnings were reported."""

    fiscal_quarter: str
    """Quarter identifier: 'Q1', 'Q2', 'Q3', 'Q4'."""

    fiscal_year: int
    """Fiscal year: e.g., 2025."""

    overall_tone: Direction
    """Aggregate tone of prepared remarks: bullish, bearish, neutral, mixed."""

    tone_vs_prior_quarter: Direction
    """Direction of tone shift vs. prior quarter: bullish (improved), bearish (worsened),
    neutral (stable)."""

    tone_shift_magnitude: float
    """How dramatic the shift was (0.0-1.0). 0 = no shift, 1.0 = extreme reversal."""

    hedging_language_score: float
    """Hedge score (0.0-1.0) in this quarter's remarks. Higher = more qualification
    language ('may', 'hope', 'resilient') vs. certainty ('expect', 'will')."""

    hedging_change_vs_prior: str
    """Did hedging language increase, decrease, or stay stable vs. prior quarter?"""

    transcript_available: bool = False
    """True if full transcript is available for adaptive research team to re-examine."""

    forward_looking_statements: list[ForwardLookingStatement] = field(default_factory=list)
    """Extracted forward-looking claims (guidance, expectations, assumptions)."""

    emphasis_topics: list[TopicEmphasis] = field(default_factory=list)
    """Topics management spent the most time discussing in prepared remarks."""

    new_topics_introduced: list[str] = field(default_factory=list)
    """Topics discussed for the first time or rarely discussed before.
    Narrative regime change signal."""

    key_phrases: list[str] = field(default_factory=list)
    """Notable direct quotes that moved the market or diverged significantly from
    prior language. Context-setting for adaptive research deep dives."""

    metadata: InvocationMetadata = field(
        default_factory=lambda: InvocationMetadata(
            invocation_id="",
            invocation_type="",
            collected_at=datetime.now(tz=UTC),
            data_confidence=DataConfidence.MEDIUM,
            vol_regime=None,
        )
    )
    """Pipeline metadata: invocation ID, collection timestamp, confidence level."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected statistical anomalies (extreme tone shifts, unusual hedging, etc.)."""


@dataclass(frozen=True)
class AnalystQADynamics:
    """Qual 4:4b — Analyst question clustering, non-answer detection, and tone analysis.

    Captures what the institutional investor base collectively cares about (via
    question clustering) and how management responded. Non-answers and evasions
    on high-uncertainty topics (capex, competition, demand visibility) are
    bearish signals. Analyst tone hostility and follow-up persistence indicate
    investor skepticism.

    Per-ticker entity. Source: Motley Fool transcripts (free) + Quartr +
    YouTube/Whisper fallback. Fallback: FMP Ultimate ($149/mo). Cadence:
    Earnings season. Feasibility: MEDIUM — NLP required for Q&A clustering
    and evasion detection.
    """

    ticker: Ticker
    """Company ticker."""

    earnings_date: date
    """Date the earnings call was held."""

    dominant_concern: str
    """The single topic analysts focused on most (by question volume or persistence)."""

    analyst_tone_overall: Direction
    """Aggregate analyst tone: bullish (softball questions), bearish (hostile/probing),
    neutral, mixed. Maps analyst hostility → investor skepticism."""

    analyst_tone_shift_vs_prior: Direction
    """Did analysts become more hostile, more friendly, or stay neutral vs. prior quarter?"""

    follow_up_persistence_count: int
    """Number of follow-up pushbacks (analysts re-asking the same question after
    unsatisfactory answer). Higher = more institutional pressure for clarity."""

    management_responsiveness_score: float
    """How directly management answered questions (0.0-1.0).
    0 = evasive throughout, 1.0 = direct answers to all substantive questions."""

    question_themes: list[QuestionTheme] = field(default_factory=list)
    """Clustered themes of analyst questions. Reveals collective investor concerns."""

    non_answer_flags: list[NonAnswerFlag] = field(default_factory=list)
    """Detected evasions or non-answers. High-signal bearish when on topics
    the market cares about (capex, competition, demand)."""

    high_signal_exchanges: list[QAExchange] = field(default_factory=list)
    """Notable Q&A exchanges that shifted expectations or revealed uncertainty."""

    metadata: InvocationMetadata = field(
        default_factory=lambda: InvocationMetadata(
            invocation_id="",
            invocation_type="",
            collected_at=datetime.now(tz=UTC),
            data_confidence=DataConfidence.MEDIUM,
            vol_regime=None,
        )
    )
    """Pipeline metadata: invocation ID, collection timestamp, confidence level."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (unusual evasion patterns, unexpected hostility, etc.)."""


@dataclass(frozen=True)
class CrossCompanyEarningsIntel:
    """Qual 4:4c + 4d — Bellwether reads, supply chain commentary, and season momentum.

    Sector-scoped intelligence derived from individual company earnings calls
    and synthesized into cross-company narratives. Tracks bellwether companies'
    commentary that anchors peer expectations, supply-chain signals (customer/
    supplier mentions), guidance consensus (beat rate, raise rate), season
    momentum (broad trends), and strategic pivot signals.

    Capex guidance from hyperscalers is the most critical qualitative input for
    the 4-72 hour thesis horizon in tech/semis.

    Market-wide/sector-wide entity (not per-ticker). Source: Derived from
    Qual 4:4a-4b across universe. Cadence: Earnings season (rolling update
    as companies report). Feasibility: MEDIUM — cross-company synthesis is
    analysis layer output, aggregating transcript intelligence.
    """

    sector: AlphaMindSector
    """Sector: TECH, SEMIS, FINANCIALS, ENERGY."""

    earnings_season: str
    """Season identifier: 'Q4_2025', 'Q1_2026', etc."""

    guidance_consensus_direction: Direction
    """Are companies broadly guiding up (bullish), down (bearish), in-line (neutral)?"""

    beat_rate_pct: float
    """Sector earnings beat rate so far this season (0.0-100.0)."""

    guidance_raise_rate_pct: float
    """Percentage of companies raising guidance (0.0-100.0)."""

    season_momentum: str
    """Qualitative season momentum: 'strong_beats' (broad positive surprises),
    'mixed' (heterogeneous), 'weak_misses' (broad disappointments), 'too_early'."""

    capex_guidance_trend: Direction
    """Are companies increasing or cutting capex? Critical for AI/semis sectors.
    bullish = increasing, bearish = cutting."""

    management_macro_sentiment: Direction
    """Collective management view of macro environment: bullish, bearish, neutral, mixed.
    Synthesized from individual company forward-looking statements."""

    bellwether_reads: list[BellwetherRead] = field(default_factory=list)
    """Earnings commentary from bellwether companies (often first reporters).
    Peers unconsciously calibrate guidance to these remarks."""

    supply_chain_signals: list[SupplyChainSignal] = field(default_factory=list)
    """Customer/supplier/competitor mentions from company earnings remarks.
    Creates supply-chain intelligence network across the sector."""

    strategic_pivot_signals: list[str] = field(default_factory=list)
    """Sector-wide narrative shifts from prepared remarks. E.g., 'AI capex pullback',
    'margin defense pivot', 'geographic diversification emphasis'."""

    metadata: InvocationMetadata = field(
        default_factory=lambda: InvocationMetadata(
            invocation_id="",
            invocation_type="",
            collected_at=datetime.now(tz=UTC),
            data_confidence=DataConfidence.MEDIUM,
            vol_regime=None,
        )
    )
    """Pipeline metadata: invocation ID, collection timestamp, confidence level."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (unusual season momentum, divergent guidance, etc.)."""


@dataclass(frozen=True)
class ConferencePresentation:
    """Qual 4:4e — Industry conference and investor day signal extraction.

    Captures events like GTC (NVIDIA), AWS re:Invent, Google I/O, Microsoft Build,
    earnings-day investor briefings, and analyst days. These presentations can
    announce long-term strategic shifts, major product/capex commitments, or
    competitive positioning changes that move the market 4-72 hours post-event.

    Per-ticker entity. Source: SEC EDGAR 8-K Item 7.01 + company IR event calendars
    + Finnhub. Fallback: Manual monitoring. Cadence: Event-driven (~2-3 major
    events per year per name). Feasibility: LOW-MEDIUM — ~70% automated coverage
    via 8-K monitoring; investor day slide decks often require manual extraction.
    """

    ticker: Ticker
    """Company ticker."""

    event_name: str
    """Event title: 'GTC 2025', 'AWS re:Invent 2025', 'Q1 2026 Investor Day'."""

    event_type: str
    """'industry_conference', 'investor_day', 'analyst_day', 'fireside_chat'."""

    event_date: date
    """Date of the presentation."""

    presenter_name: str
    """Name of the executive(s) presenting (e.g., 'Jensen Huang')."""

    presenter_title: str
    """Title of presenter(s): 'CEO', 'CFO', 'CTO'."""

    real_time_reaction_sentiment: Direction
    """Crowd/media reaction during or immediately after the presentation:
    bullish, bearish, neutral, mixed."""

    key_announcements: list[str] = field(default_factory=list)
    """Major announcements or product reveals. E.g., 'Announced new H200 GPU',
    'Expanded partnership with OpenAI'."""

    long_term_target_updates: list[TargetUpdate] = field(default_factory=list)
    """New or updated long-term targets (5-year guidance, margin targets, etc.)."""

    strategic_shift_signals: list[str] = field(default_factory=list)
    """Indicated changes in strategy: 'pivoting to AI infrastructure', 'expanding
    into automotive', 'shifting to subscription model'."""

    metadata: InvocationMetadata = field(
        default_factory=lambda: InvocationMetadata(
            invocation_id="",
            invocation_type="",
            collected_at=datetime.now(tz=UTC),
            data_confidence=DataConfidence.MEDIUM,
            vol_regime=None,
        )
    )
    """Pipeline metadata: invocation ID, collection timestamp, confidence level."""

    is_high_priority: bool = False
    """True for major catalyst events (GTC, re:Invent, I/O from mega-caps).
    Signals that this event is likely to move the market within the 4-72hr horizon."""

    cross_company_context: str = ""
    """How this presentation fits with concurrent or recent presentations at
    the same conference. E.g., 'MSFT's Azure AI announcements at Build suggest
    similar capex intensity to NVDA commentary at GTC'."""

    anomalies: list[AnomalyFlag] = field(default_factory=list)
    """Detected anomalies (unexpected strategic shift, market reaction surprise, etc.)."""
