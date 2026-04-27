"""Qual 1-2: News, Financial Media, and Sentiment — entity definitions.

5 entities covering breaking headlines, long-form analysis, aggregated social
sentiment, alternative sentiment proxies, and options flow narrative. This
module combines two qualitative categories since they share the sentiment
analysis pipeline.

Design note on qualitative data:
    All entities in this module are inherently qualitative — they require LLM
    interpretation from the start. Raw payloads (article text, social posts,
    options commentary) are ingested and minimally structured; the distillation
    layer's qualitative research pipeline applies domain expertise to extract
    meaning, contextualize, and flag divergences. No meaningful programmatic
    signal exists at the raw payload level.

Design note on sentiment calibration:
    Social sentiment is per-ticker relative to that ticker's rolling distribution,
    not absolute. TSLA's "neutral" sentiment at +0.2 is louder than SCHW's "bullish"
    at +0.6 — baseline emotional volatility differs dramatically by name. Every
    sentiment field includes percentile_vs_history to enable comparison across names.

Design note on source credibility:
    Tier 1 = wire services (Reuters, Bloomberg, DJ, WSJ/FT exclusives) — fast,
    broad reach, editorial rigor. Tier 2 = major financial outlets (CNBC, MarketWatch,
    Seeking Alpha established analysts) — slower, curated. Tier 3 = blogs, social-
    media-first outlets, aggregators — fastest dissemination but highest noise.
    Headline novelty (is_first_mover) marks agenda-setters.

Design note on news-price divergence:
    The system's highest-signal detection: negative news + flat/rising price
    ("priced in"), positive news + flat/falling price ("hidden problem"). This
    drives adaptive research layer investigation. Requires real-time price context
    from Q1:1a (synchronized timestamps).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ._common import (
    AnomalyFlag,
    Direction,
    HeadlineType,
    InvocationMetadata,
    SignalStrength,
    SourceCredibilityTier,
    Ticker,
)

__all__ = [
    "AlternativeSentimentProxy",
    "BreakingHeadline",
    "HeadlineCluster",
    "KeyArgument",
    "LongFormAnalysis",
    "NewsVolume",
    "OptionsFlowNarrative",
    "PlatformSentimentBreakdown",
    "SocialSentimentScore",
    "UnusualOptionsActivityReport",
]


# ── Supporting types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HeadlineCluster:
    """A group of related headlines detected via NLP clustering. Used to
    identify correlated news across different tickers or themes."""

    cluster_id: str  # Unique identifier for this cluster
    headline_count: int  # Number of headlines in the cluster
    primary_theme: str  # Human-readable theme (e.g., "earnings_miss", "regulatory_action")
    tickers_involved: list[Ticker] = field(default_factory=list)  # All tickers mentioned in cluster


@dataclass(frozen=True)
class NewsVolume:
    """Point-in-time news volume for a ticker across multiple rolling windows.
    Used to detect abnormal news flow frequency."""

    volume_2hr: int  # Article count in last 2 hours
    volume_6hr: int  # Article count in last 6 hours
    volume_24hr: int  # Article count in last 24 hours
    volume_vs_avg_20d: float  # Ratio of current 24hr volume to 20-day trailing average
    # E.g., 2.5 = 150% more articles than typical


@dataclass(frozen=True)
class KeyArgument:
    """An extracted argument or claim from a long-form analysis, tagged with
    sentiment direction. Used by the research layer to construct thesis narratives."""

    argument_text: str  # The claim or argument (e.g., "chip shortages easing in Q4")
    argument_direction: Direction  # Is this bullish, bearish, or neutral for the thesis?
    confidence: float  # How confident the extraction is (0.0-1.0)


@dataclass(frozen=True)
class PlatformSentimentBreakdown:
    """Per-platform sentiment scores for social media, showing divergence
    between communities (stocktwits vs. reddit vs. twitter have distinct personalities)."""

    platform: str  # "stocktwits" | "reddit" | "twitter" | "seeking_alpha"
    sentiment_score: float  # Platform-specific score (-1.0 to 1.0)
    mention_count_24hr: int  # Volume on this platform in last 24 hours
    sentiment_direction: Direction  # Aggregated direction from the platform


@dataclass(frozen=True)
class UnusualOptionsActivityReport:
    """A single unusual options activity report, providing qualitative narrative
    about what sophisticated options traders are positioning for."""

    report_timestamp: datetime  # When this activity was detected/reported
    activity_type: str  # "call_accumulation" | "put_accumulation" | "volatility_crush_trade" | etc.
    description: str  # Human-readable narrative of the activity
    implied_thesis_direction: Direction  # What does this positioning suggest?
    expiration_focus: str | None = (
        None  # Which expiration is focal ("weekly" | "monthly" | "leaps" | None if mixed)
    )
    confidence_signal_strength: SignalStrength = (
        SignalStrength.MODERATE
    )  # How high-signal is this activity?


# ── Primary entities ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BreakingHeadline:
    """Qual 1:1a — Wire service and financial news headlines with sentiment tagging.

    Headlines with timestamps, per-ticker news volume (2hr/6hr/24hr), cross-ticker
    clustering, news-price divergence flags, source credibility metadata (Tier 1-3),
    first-mover source attribution. Each headline is ingested in isolation; the
    clustering and divergence analysis layer cross-references with simultaneous
    price action from Q1:1a.

    Source: Finnhub News (free) + Marketaux (free, 100 req/day with pre-computed sentiment)
            + RSS feeds (CNBC/Reuters/MarketWatch) + SEC EDGAR 8-K RSS
    Fallback: Benzinga ($50-200/mo for wire-speed)
    Cadence: Continuous (as headlines are published)
    Feasibility: MEDIUM — multi-source pipeline, slower than wire services but broad coverage
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Headline content ──
    headline_text: str
    # The full headline text. E.g., "Apple Misses Q3 Revenue Estimates by 5%".
    # Minimal processing: formatting cleaned, source-specific metadata stripped.
    # This is the raw input to the sentiment analysis pipeline.

    # ── Source and credibility ──
    source: str
    # Source outlet name (e.g., "Reuters", "CNBC", "Seeking Alpha"). Used for
    # attribution and audit trail. Important for understanding which outlets
    # typically break news vs. react to competitors' scoops.
    source_credibility_tier: SourceCredibilityTier
    # Tier 1 (wire services, Bloomberg exclusives), Tier 2 (major outlets),
    # or Tier 3 (blogs, social-media-first). Used by research layer to weight
    # the headline's initial signal strength. Tier 1 moves the needle more.
    is_first_mover: bool
    # True if this source appears to be the first to publish this news (based on
    # timestamp clustering and known source behavior). First movers often command
    # market reaction; derivatives trading follows. Helps identify which outlet
    # broke the story.

    # ── Timing ──
    published_at: datetime
    # UTC timestamp when the headline was published. Used to correlate with
    # price/volume action in Q1:1a within a 5-minute window (high-frequency
    # correlation requires sub-minute precision; 5 min is the lowest reliable
    # granularity for free sources).

    # ── Tickers mentioned ──
    primary_ticker: Ticker
    # The ticker this headline is specifically about. This entity is per-ticker.
    headline_sentiment: Direction
    # Directional sentiment tagging (bullish, bearish, neutral, mixed) from
    # the LLM distillation layer. Per-ticker contextualized: "Apple says supply
    # chain improving" is BULLISH for AAPL, potentially neutral for XOM.
    # This is the primary signal for news-price divergence detection.
    sentiment_confidence: float
    # How confident the LLM is in the sentiment assignment (0.0-1.0). Ambiguous
    # headlines (e.g., "Apple Faces Scrutiny Over Tax Practices") may score 0.5-0.6.
    # Clear headlines (e.g., "Apple Crushes Earnings") score 0.85+. Research layer
    # uses this to gate divergence investigations (only pursue if confidence > 0.7).

    # ── News volume context ──
    news_volume: NewsVolume
    # Article counts for this ticker in rolling windows (2hr, 6hr, 24hr) and
    # ratio vs. 20-day average. Used to detect news bursts (sentiment_volume_ratio
    # > 3.0 = exceptional news flow) and contextualize single headlines within
    # broader news cycles.

    # ── Topic classification ──
    tickers_mentioned: list[Ticker] = field(default_factory=list)
    # All other tickers mentioned in the headline. E.g., headline about NVIDIA
    # may also mention competitors AMD, QCOM, or customer/supplier relationships.
    # Research layer uses this to propagate signals across correlated names.
    topic_tags: list[HeadlineType] = field(default_factory=list)
    # Canonical headline-type tags from `HeadlineType` (see schema/_common.py).
    # Vendor tag vocabularies are normalized into this set at the collector
    # boundary via `config/headline_tag_mapping.yaml`. Multiple values allowed —
    # an M&A rumor from a Tier 1 source carries both `M_AND_A` and `BREAKING`.
    # Enables downstream filtering and routing to specialist research agents;
    # earnings-coordinated analysis, M&A thesis routing, and short-report
    # detection consume this field directly via `HeadlineType` membership.

    # ── Cross-ticker clustering ──
    cross_ticker_cluster_id: str | None = None
    # If this headline was grouped with others via NLP clustering (e.g., all
    # headlines about a semiconductor supply-chain issue), the cluster ID.
    # Used to identify correlated multi-ticker narratives and propagate signals
    # (e.g., one semi's bad guidance may cluster with others' guidance warnings).

    # ── News-price divergence (high-signal flag) ──
    news_price_divergence_flag: bool = False
    # True if the headline sentiment significantly diverges from concurrent price
    # action (5-min window post-publication). E.g., negative headline + rising price,
    # or positive headline + falling price. Computed by comparing sentiment
    # (headline_sentiment) to price direction in Q1:1a. This is one of the system's
    # highest-signal detections — divergence often precedes reversals or indicates
    # hidden information.
    divergence_description: str | None = None
    # Human-readable narrative of the divergence, if present. E.g., "Bearish
    # guidance but price +3% — suggests institutional accumulation or priced-in
    # nature of the guidance." Research layer uses this for rapid context.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected anomalies: headline sentiment extreme confidence (>0.95),
    # unusual news volume spikes (>5 std dev from baseline), cross-ticker
    # clustering (correlated multi-ticker event), or divergence flags.


@dataclass(frozen=True)
class LongFormAnalysis:
    """Qual 1:1b — Sell-side research summaries, opinion/editorial, and short reports.

    Research note summaries (conclusion, key arguments), opinion/editorial tone,
    investigative articles, short reports (Hindenburg, Muddy Waters, etc.),
    M&A rumors and deal speculation. Distinct from BreakingHeadline in length,
    depth, and temporal cadence. Long-form pieces are rarer but carry more
    conviction weight. Short reports are especially high-signal.

    Source: Finnhub + Zacks free research reports + finance Substacks + OpenBB Terminal
    Fallback: SemiAnalysis (~$30/mo for semis), other sector-specific research
    Cadence: Every invocation (collected across available APIs; new pieces not on a timer)
    Feasibility: LOW-MEDIUM — sell-side paywalled, free alternatives + LLM distillation
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Content identity ──
    title: str
    # Title of the research piece / opinion article / short report.
    # E.g., "Why Tesla is Heading for a Crash" (short report) or
    # "Nvidia: Still the Best Semiconductor Franchise" (research note).
    source: str
    # Source outlet (e.g., "Goldman Sachs", "Hindenburg Research", "Substack/SemiAnalysis").
    # Critical for weighting: GS equity research has institutional reach, Hindenburg
    # is activist short-seller (high conviction but adversarial), Substack is
    # independent analyst (variable credibility but often prescient).
    source_credibility_tier: SourceCredibilityTier
    # Tier assessment: GS/JPM/Morgan Stanley = Tier 1. Established sell-side
    # (Jefferies, Wedbush) = Tier 2. Independent research / shorts = Tier 2-3
    # depending on track record. Research layer weights accordingly.
    published_at: datetime
    # UTC timestamp of publication. Used to sequence analysis against price
    # history and to flag stale research (>30 days old is historical context only).

    # ── Content taxonomy ──
    content_type: str
    # "research_note" | "editorial" | "investigative" | "short_report" | "ma_rumor".
    # Determines how the research layer interprets conviction and time horizon.
    # Short reports and MA rumors are event-driven (days-weeks horizon); research
    # notes are structural (months). Editorials are opinions (useful for narrative
    # context, lower conviction weight).
    primary_ticker: Ticker
    # The main subject ticker.
    directional_call: Direction
    # Overall thesis direction: BULLISH | BEARISH | NEUTRAL | MIXED.
    # Extracted from the piece conclusion or rating if explicit. "MIXED" for
    # balanced ("outperform vs. peers but headwinds remain") or ambiguous pieces.
    is_sell_side: bool
    # True if source is institutional sell-side (GS, JPM, Citi, etc.).
    # Sell-side has institutional reach and editorial process; short-sellers
    # have adversarial incentive alignment (may overstate negatives) but high
    # conviction. Independent research (Substacks, bloggers) varies widely.
    urgency: SignalStrength
    # How time-sensitive is this piece for trading? STRONG = time-critical catalyst
    # (deal deadline, earnings preview, recall risk). MODERATE = important but
    # medium-term (structural thesis). WEAK = historical analysis. Used to prioritize
    # research layer processing and gate alert velocity.

    # ── Author credibility ──
    author: str | None = None
    # Name of the analyst/author if available (e.g., "Katy Huberty from Morgan Stanley").
    # Used for track record lookups and continuity (does this analyst have a history
    # of prescience or contrarianism?).
    author_prominence: str | None = None
    # "high" | "medium" | "low" | None. High = well-known, quoted frequently,
    # institutional weight. Low = junior analyst or first-time writer. Used to
    # weight conviction.

    # ── Firm context (if sell-side) ──
    firm_name: str | None = None
    # If sell-side research, the firm name (e.g., "Goldman Sachs", "Morgan Stanley").
    prior_rating: str | None = None
    # Prior rating (if this is an update): "buy" | "hold" | "sell" | "neutral" | etc.
    # Format varies by firm; stored as-is.
    new_rating: str | None = None
    # New rating if the piece changes the analyst's stance. Rating changes
    # (downgrade from Buy to Hold, upgrade from Sell to Buy) carry high signal.
    # Used by research layer to flag conviction shifts and consensus moves.

    # ── Short report context (if applicable) ──
    is_activist_short_report: bool = False
    # True if this is a dedicated short-seller research piece (Hindenburg, Muddy Waters,
    # J Capital, etc.). These are high-conviction, thesis-heavy, and often catalyst-
    # driven. Requires cross-reference with price action and short interest trending
    # from Q4 to assess if shorts are building before publication (evidence of
    # conviction vs. p.r. play).
    short_seller_name: str | None = None
    # If short report, the name of the activist (e.g., "Hindenburg Research").

    # ── Secondary tickers and arguments ──
    tickers_mentioned: list[Ticker] = field(default_factory=list)
    # Secondary tickers (peers, suppliers, competitors mentioned in the analysis).
    # Enables cross-ticker thesis propagation.
    key_arguments: list[KeyArgument] = field(default_factory=list)
    # Extracted key claims/arguments with per-argument sentiment direction.
    # E.g., [KeyArgument("AI growth TAM expanding", BULLISH, 0.9),
    #        KeyArgument("Competition from new entrants", BEARISH, 0.8)].
    # Research layer uses these to build nuanced thesis narratives and identify
    # which sub-arguments are most contentious vs. consensus.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected anomalies: research rating change (upgrade/downgrade from firm),
    # activist short report publication, major conviction shift, or contradiction
    # with recent related headlines (editorial conflict detection).


@dataclass(frozen=True)
class SocialSentimentScore:
    """Qual 2:2a — Aggregated per-ticker social sentiment with velocity and divergence.

    Per-ticker sentiment score (bullish/bearish, magnitude, confidence), sentiment
    rate of change, sentiment volume (rolling mentions), sentiment-price divergence,
    per-ticker calibrated percentile. Social sentiment is contrarian at extremes
    (>+0.8 or <-0.8 often precedes reversals) and confirming in the middle.

    Design note: sentiment is calibrated per-ticker against rolling distribution.
    TSLA's +0.2 is louder than SCHW's +0.6 due to baseline volatility differences.
    percentile_vs_history enables cross-ticker comparison.

    Source: StockTwits API (free)
    Fallback: Finnhub sentiment (free, lower granularity)
    Cadence: Every invocation (most platforms update sentiment scores continuously
             or hourly; refresh on analysis cycle not on a schedule)
    Feasibility: MEDIUM — aggregated sentiment data readily available, but per-ticker
                 calibration is distillation logic (LLM-assisted)
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Sentiment score ──
    sentiment_score: float
    # Aggregate sentiment on -1.0 to +1.0 scale (-1.0 = 100% bearish,
    # 0.0 = neutral, +1.0 = 100% bullish). Computed from social platforms
    # (StockTwits posts, Reddit threads, Twitter sentiment where available).
    # Raw score is unfiltered (includes bots, retail noise, but also crowd
    # conviction). Extreme values (>+0.8 or <-0.8) are often contrarian signals.
    sentiment_magnitude: float
    # Absolute strength/conviction of the sentiment, independent of direction.
    # High magnitude (>0.7) suggests consensus; low magnitude (<0.3) suggests
    # divided opinion. Computed from vote ratios, post volume, and engagement.
    # Used to gate contrarian alerts (extremes with high magnitude > low magnitude).
    sentiment_confidence: float
    # LLM/distillation assessment of confidence in this score (0.0-1.0).
    # Low confidence (0.4-0.5) when platforms show contradictory signals or
    # low volume. High confidence (0.8+) when platforms agree and volume is
    # substantial. Research layer uses this to weight divergence investigations.

    # ── Directional aggregation ──
    sentiment_direction: Direction
    # Simple classification from sentiment_score: BULLISH (>+0.3), BEARISH (<-0.3),
    # NEUTRAL (±0.3), MIXED (if platform breakdown shows conflicting signals).
    # MIXED is common when retail (Twitter) is bullish but institutional sentiment
    # (Seeking Alpha comments) is bearish.

    # ── Per-ticker calibration ──
    sentiment_percentile_vs_history: float
    # Percentile rank of current sentiment within this ticker's rolling distribution
    # (e.g., 20-day or 60-day history). 0.0 = historical minimum (least bullish this
    # name has been), 100.0 = historical maximum (most bullish). Enables comparison:
    # TSLA at +0.2 (percentile 85) is more extreme than SCHW at +0.6 (percentile 45).
    # Critical for contrarian signals: TSLA at percentile 95+ is extreme bullish
    # (potential reversal).

    # ── Sentiment velocity ──
    sentiment_rate_of_change: float
    # How fast sentiment is shifting, in standard deviations per hour.
    # E.g., +0.15 = sentiment shifting bullish by 0.15 std dev per hour.
    # Used to detect sentiment cascades (cumulative retail FOMO, panic capitulation).
    # Rapid changes (>0.10 per hour) are high-signal for imminent reversals.
    mention_volume_2hr: int
    # Number of social posts/mentions in the last 2 hours mentioning this ticker.
    # Baseline varies dramatically by name (TSLA > GME > most others).
    mention_volume_6hr: int
    # 6-hour rolling mention count.
    mention_volume_24hr: int
    # 24-hour rolling mention count.
    mention_volume_vs_avg: float
    # Ratio of current 24hr mention volume to 20-day trailing average.
    # E.g., 5.0 = 5x normal mention volume (social buzz/FOMO spike).
    # Used to detect retail attention spikes independent of sentiment direction.

    # ── Sentiment-price divergence ──
    sentiment_price_divergence: bool
    # True if social sentiment significantly diverges from recent price action
    # (last 4 hours). E.g., extreme bullish sentiment (+0.85) but price falling
    # intraday, or extreme bearish sentiment (-0.75) but price rallying. Divergence
    # is contrarian signal: sentiment extremes often precede reversals.
    divergence_description: str | None = None
    # Human-readable narrative if divergence detected. E.g., "Extreme bullish
    # sentiment (percentile 98) but -3% intraday — potential reversal signal."
    roc_window_hours: int = 4
    # Window over which rate_of_change is computed (typically 4 hours for intraday).
    # Included for reproducibility.

    # ── Platform breakdown (optional, for transparency) ──
    platform_breakdown: list[PlatformSentimentBreakdown] = field(default_factory=list)
    # Per-platform sentiment scores (StockTwits, Reddit, Twitter, etc.).
    # Enables research layer to identify which communities are driving the aggregate
    # signal. E.g., if StockTwits is +0.9 but Reddit is -0.1, retail hype
    # vs. informed skepticism divergence is visible.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected anomalies: extreme sentiment percentile (>95 or <5), high rate of
    # change (cascade/panic), sentiment-price divergence, or platform disagreement
    # (platform_breakdown scatter).


@dataclass(frozen=True)
class AlternativeSentimentProxy:
    """Qual 2:2b — Google Trends search interest and retail brokerage flow signals.

    Google Trends search volume (ticker symbols, company names), trends spike detection,
    retail brokerage flow data (most-bought/most-sold lists, net flow direction), and
    retail-vs-institutional divergence. These are behavioral proxies for interest and
    positioning. Google Trends captures retail attention shifts; retail brokerage flow
    shows order imbalance. Divergence from institutional positioning (Q2:2c options flow,
    Q4 short interest) is high-signal.

    Source: Google Trends official API (free, 1,500 req/day since 2025)
    Fallback: pytrends (unofficial, fragile) or Finnhub retail flow (if available)
    Cadence: Daily (Google Trends and brokerage flow both have 24-hour minimum granularity)
    Feasibility: MEDIUM — official Google Trends API is production-grade as of 2025;
                 retail flow requires aggregation from multiple brokerages
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Google Trends search interest ──
    google_trends_index: float
    # Google Trends interest score (0-100 scale, relative to peak in query period).
    # 100 = peak interest; 0 = no meaningful search activity. Not absolute (can't
    # compare across queries directly; relative within a single ticker's history).
    # Used to detect retail attention spikes (e.g., 80+ is exceptional interest).
    trends_vs_avg_30d: float
    # Ratio of current Google Trends index to 30-day rolling average.
    # E.g., 2.5 = current interest is 2.5x normal level (attention spike).
    google_trends_ticker_symbol: bool = True
    # Was the ticker symbol (e.g., "AAPL") the primary search term?
    # True = ticker searches dominate. False = company name ("Apple Inc.") dominates.
    # Ticker dominance suggests financial/trader interest; company name suggests
    # mainstream media/consumer interest.
    google_trends_company_name: bool = False
    # Was the company name (e.g., "Apple") the primary search term?
    trends_spike_detected: bool = False
    # True if current Trends index > 1.5 standard deviations above 30-day baseline.
    # High-signal for retail attention cascade, often precedes retail order flow surge.

    # ── Retail brokerage flow ──
    retail_most_bought_rank: int | None = None
    # Position on the "most bought" list from retail platforms (e.g., Fidelity,
    # E*TRADE, Robinhood most-bought). E.g., rank=3 = 3rd most-bought name today.
    # None if not in top 50 or not available. Lower rank = stronger retail buying
    # interest.
    retail_most_sold_rank: int | None = None
    # Position on the "most sold" list. Used to identify retail panic selling.
    retail_net_flow_direction: Direction | None = None
    # Net direction of retail order flow: BULLISH = more buys than sells,
    # BEARISH = more sells than buys, NEUTRAL = balanced. Computed from aggregate
    # retail platform data. None if insufficient data.
    retail_institutional_divergence: bool = False
    # True if retail flow direction diverges from institutional positioning
    # (inferred from options narrative Q2:2c and short interest Q4).
    # E.g., retail heavily buying while institutional shorting (from Q4:4b borrow
    # cost trends) = divergence. High-signal: retail often leads institutional,
    # but at extremes, divergence reveals hidden imbalances.

    # ── Freshness metadata ──
    data_as_of: datetime | None = None
    # Timestamp of the most recent Google Trends and retail flow data. Google
    # Trends updates daily; retail flow updates intraday (varies by platform).
    # Included for reproducibility.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected anomalies: Google Trends spike (>2 std dev), retail rank appearance
    # (name enters top-50 most bought/sold), retail-institutional divergence,
    # or tick-to-tick flow direction reversal.


@dataclass(frozen=True)
class OptionsFlowNarrative:
    """Qual 2:2c — Qualitative options flow narrative from activity reports and media.

    Unusual options activity reports (from monitoring systems), options narrative
    divergence from equity narrative (high-signal: options market attracts
    sophisticated participants), earnings options positioning commentary,
    implied move assessment, and smart money signal direction. Options flow is
    informational (smart money hedges/positions ahead of moves); equity narrative
    is what the sell-side says. Divergence is high-conviction mismatch.

    Source: Derived from Q3:3b local detection (volume spikes, IV crush skew) + news API sweep
    Fallback: Financial media options commentary (Benzinga, tastytrade, CBOE reports)
    Cadence: Every invocation (unusual activity is continuous; narrative derived on-demand)
    Feasibility: LOW-MEDIUM — pre-computed narrative requires UW ($125/mo), local derivation
                 from flow spikes is fragile but feasible; media sweep requires LLM extraction
    """

    # ── Identity ──
    ticker: Ticker
    metadata: InvocationMetadata

    # ── Options narrative divergence ──
    options_equity_narrative_divergence: bool
    # True if options positioning (implied from unusual_activity_reports and
    # Q3:3b IV skew/term structure) significantly diverges from equity narrative
    # (sell-side consensus from Q1:1b, news sentiment from Q1:1a). E.g., options
    # market pricing in high volatility and bearish outcome while sell-side research
    # remains constructive = divergence. Options market is more sophisticated
    # (professional hedgers, volatility specialists); divergence = information
    # mismatch. VERY high-signal.

    # ── Unusual activity reports ──
    unusual_activity_reports: list[UnusualOptionsActivityReport] = field(default_factory=list)
    # List of detected unusual options activities (call accumulation, put buying,
    # volatility crush trades, etc.) with timestamps and descriptions. Each entry
    # includes implied thesis direction (bullish, bearish, neutral). Sorted by
    # recency (most recent first). Empty list if no unusual activity detected.
    # Used by research layer to gauge smart money convictions and timing.

    # ── Earnings positioning (if near earnings) ──
    earnings_positioning_commentary: str | None = None
    # Qualitative narrative about options positioning ahead of earnings (if the
    # name is <7 days to earnings). E.g., "Call buyers concentrated in $180-190
    # strikes (bullish thesis), put writers are thin (low bearish conviction)".
    # Used by analyst to understand consensus earnings expectations and
    # positioning asymmetries (if calls have much higher volume than puts,
    # market is biased bullish).

    # ── Implied move vs. historical volatility ──
    implied_move_vs_historical: str | None = None
    # Qualitative assessment of how current implied move (from ATM straddle or
    # strangle) compares to historical realized volatility. E.g., "Implied move
    # $5.00 vs. historical avg $3.50 — market pricing elevated volatility". Used
    # to contextualize the magnitude of option positioning and gauge whether
    # volatility expectations are stretched (potential crush opportunity) or
    # reasonable.

    # ── Divergence description ──
    divergence_description: str | None = None
    # Human-readable narrative of the divergence. E.g., "Options flow is pricing
    # +15% implied move (from IV term structure) but equity narrative is stable.
    # Suggests market expects earnings surprise or event risk not yet reflected
    # in consensus research."

    # ── Smart money signals ──
    smart_money_signal_direction: Direction | None = None
    # Aggregate direction inferred from sophisticated options positioning
    # (institutional call/put accumulation, dealer gamma positioning inferred from
    # options flow, etc.). None if no clear smart money signal. BULLISH = large
    # institutional calls being accumulated or dealers short gamma (market maker
    # positions imply demand for upside hedges). BEARISH = puts accumulating.
    # NEUTRAL = balanced. Requires careful interpretation: dealer gamma flips
    # intraday and are tactical, not strategic.

    # ── Anomalies ──
    anomalies: list[AnomalyFlag] = field(default_factory=list)
    # Detected anomalies: unusual options activity spike, extreme IV (>2 std dev),
    # options-equity narrative divergence, or IV term structure inversion
    # (front-loaded > longer-dated = event risk expected).
