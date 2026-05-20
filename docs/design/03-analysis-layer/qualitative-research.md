# Qualitative research — baseline (always-on)

Floor-level awareness of what's happening in the world, every invocation regardless of quant findings. Fixed predictable scope keeps per-invocation cost low.

LLM judgment applied to qualitative data the ingestion layer ([qualitative.md](../01-data-layer/external/qualitative.md)) collects and structures.

---

## Inputs

Two categories: **bounded in-context data** loaded at invocation start, and **on-demand data** available via tools during reasoning.

### In-context data (~6,500–8,000 tokens)

Loaded before reasoning begins. Bounded and predictable regardless of market conditions.

**1. News digest (~1,000–1,200 tokens)**
Deterministic, ranked summary of headlines since the last invocation. See [news digest specification](#news-digest) below.

**2. Sentiment aggregates (~3,000 tokens)**
Pre-computed per-ticker sentiment from [qualitative 2a](../01-data-layer/external/qualitative.md) — directional score, magnitude, rate of change, volume, sentiment-price divergence flag. Percentiles against each ticker's own trailing distribution per [distillation external.md §2](../02-distillation-layer/external.md), not a universal scale.

**3. Prediction market snapshot (~800–1,200 tokens)**
All tracked contracts from [qualitative 3a–3c](../01-data-layer/external/qualitative.md) — current probability, delta since last invocation, delta since last 24h, volume, expiration. Contracts with delta >5pp are flagged per the distillation threshold.

**4. Event calendar — next 72 hours (~500–1,000 tokens)**
Scheduled events from [qualitative 5e](../01-data-layer/external/qualitative.md) and [quantitative 6g](../01-data-layer/external/quantitative.md) — FOMC, CPI/PPI, universe earnings, OPEC, congressional hearings, court dates, Treasury auctions. Each entry: event name, timestamp, sector relevance tags, consensus/expected outcome where applicable. Data layer merges regulatory/policy events (qualitative 5e) with macro data releases (quantitative 6g) into a unified feed.

**5. Active thesis summaries (~500–1,000 tokens)**
Per-thesis: ticker, summary one-liner (from [thesis model](../05-execution-layer/thesis-model.md)), key catalyst, time expectation. Loaded in-context so the agent can cross-reference the event calendar against thesis catalysts — "FOMC tomorrow and we hold three rate-sensitive positions" — without a tool call. Same data the [synthesizer](synthesizer.md) accesses via `get_active_theses_summary`; loaded here because catalyst-proximity assessment requires theses always-visible. Scales ~50 tokens × 10–20 concurrent positions = ~500–1,000 tokens.

**6. Volatility regime label**
Broadcast to all agents from [distillation external.md §4](../02-distillation-layer/external.md).

### On-demand tools

Four tools. Three shared with [adaptive research](adaptive-research.md) (same data infrastructure, same input/output contracts); `earnings_commentary` is new. Used for baseline investigation — following up digest items, pulling context for narrative threads — rather than anomaly-driven research.

| Tool ID | Description | Contract source | Use case for this agent |
|---------|-------------|----------------|------------------------|
| `news_search` | Targeted news search by ticker, topic, keyword | [Adaptive research](adaptive-research.md) | Pull full articles behind digest headlines; search for narrative context around calendar events |
| `social_sentiment` | Social media sentiment for specific tickers | [Adaptive research](adaptive-research.md) | Investigate sentiment extremes or divergences spotted in the aggregates |
| `prediction_markets` | Query specific prediction market contracts | [Adaptive research](adaptive-research.md) | Investigate prediction market moves flagged in the snapshot |
| `earnings_commentary` | Earnings result + commentary for a specific ticker | New — see [contract below](#earnings_commentary-tool-contract) | Deep pull on universe names that reported since last invocation |

The shared tools require the data layer to support parameterized queries (not just bulk delivery); contracts in [adaptive-research.md](adaptive-research.md). `earnings_commentary` contract below.

---

### `earnings_commentary` tool contract

Returns earnings results and, when available, transcript-derived commentary for a specific ticker. Two data tiers — numeric results available immediately after a report; transcript analysis depends on transcript ingestion (Motley Fool scraping <24hr latency, or Quartr API if available).

**Input:**

```
earnings_commentary(
  ticker: str,
  include_transcript_analysis: bool = true
)
```

Set `include_transcript_analysis: false` to skip the transcript pull when only numeric results are needed (cheaper, faster).

**Output:**

```
{
  ticker: str,
  earnings_date: str,                 // ISO date of the earnings report
  data_freshness: str,                // ISO timestamp of when this data was last updated

  // --- Tier 1: always present (immediate after report) ---

  result: {
    eps_actual: float,
    eps_consensus: float,
    eps_surprise_pct: float,          // (actual - consensus) / |consensus| × 100
    revenue_actual: float,            // in dollars
    revenue_consensus: float,
    revenue_surprise_pct: float
  },
  price_reaction: {
    close_to_close_pct: float,        // prior close → post-earnings close
    immediate_move_pct: float,        // post-report first 30min move
    vs_implied_move: float            // |actual move| / options-implied move
  },
  post_earnings_activity: {
    estimate_revisions_since: int,    // count of analyst estimate revisions since report
    revision_direction: str,          // "up" | "down" | "mixed" | "none"
    rating_changes_since: [
      {
        analyst_firm: str,
        prior_rating: str,
        new_rating: str,
        prior_target: float,
        new_target: float
      }
    ]
  },

  // --- Tier 2: present only when transcript has been ingested ---

  transcript_available: bool,
  transcript_analysis: {              // null when transcript_available == false
    overall_tone: str,                // "bullish" | "cautious" | "defensive" | "mixed"
    tone_vs_prior_quarter: str,       // "more_bullish" | "more_cautious" | "unchanged"
    dominant_qa_theme: str,           // what analysts focused on most
    forward_looking_statements: [
      {
        text: str,                    // the actual claim
        topic: str,                   // e.g., "AI capex", "credit quality"
        sentiment: str                // "bullish" | "bearish" | "neutral"
      }
    ],
    non_answer_flags: [
      {
        topic: str,                   // question topic management dodged
        evasion_type: str,            // "deflection" | "redirect" | "boilerplate" | "silence"
        severity: str                 // "high" | "medium" | "low"
      }
    ],
    guidance_vs_consensus: str        // "above" | "in_line" | "below" | "not_provided"
  },

  quality: str                        // "complete" | "partial_no_transcript" | "stale"
}
```

**Tier 1 data sources:**

| Field | Source | Feasibility | Notes |
|-------|--------|-------------|-------|
| `result` (EPS/revenue) | yfinance, Finnhub | HIGH | Tested 62/62 tickers ([api-key-checklist.md](../01-data-layer/api-key-checklist.md)) |
| `price_reaction` | Polygon quote data (quant 1a) | HIGH | Cross-layer dependency: quantitative price data |
| `post_earnings_activity.estimate_revisions_since` | yfinance `get_eps_revisions()` | HIGH | Tested |
| `post_earnings_activity.rating_changes_since` | Finnhub + yfinance | MEDIUM | Finnhub free tier has limited historical depth |

**Tier 2 data sources:**

| Field | Source | Feasibility | Notes |
|-------|--------|-------------|-------|
| `overall_tone`, `tone_vs_prior_quarter` | ManagementToneAnalysis entity ([earnings_commentary.yaml](../01-data-layer/mappings/earnings_commentary.yaml)) | MEDIUM | Requires transcript (Motley Fool <24hr delay, Quartr TBD) + NLP pipeline (not yet built) |
| `dominant_qa_theme`, `non_answer_flags` | AnalystQADynamics entity ([earnings_commentary.yaml](../01-data-layer/mappings/earnings_commentary.yaml)) | MEDIUM | Same transcript + NLP dependency |
| `forward_looking_statements` | ManagementToneAnalysis.forward_looking_statements | MEDIUM | NLP extraction from transcript |
| `guidance_vs_consensus` | EarningsEstimates entity (quant 5e) + transcript | HIGH (numeric) / MEDIUM (narrative) | Numeric guidance vs. consensus is straightforward; narrative guidance tone requires transcript |

**Implementation phasing:** Tier 1 is available day one; `transcript_available` is initially `false` for most calls until transcript ingestion (Motley Fool scraping or Quartr API) and NLP extraction are built. The tool degrades gracefully to Tier 1 with `quality: "partial_no_transcript"` rather than failing.

Outstanding data-layer work for Tier 2: transcript NLP pipeline (tone classification, Q&A clustering, non-answer detection, forward-looking statement extraction — all marked "derived from TBD" in mappings); ManagementToneAnalysis and AnalystQADynamics schemas defined in [schema/earnings_commentary.py](../01-data-layer/schema/earnings_commentary.py) but computation pipeline not yet built; transcript ingestion source TBD per [earnings_commentary.yaml](../01-data-layer/mappings/earnings_commentary.yaml).

**Tool call budget:** No hard cap. The agent's closed-scope mandate (assess the five sweep categories) bounds usage — single pass across known categories, not an open-ended loop. System prompt includes soft advisory: "typically 5–15 tool calls per invocation; fewer on quiet days, more on busy days." A hard cap can be added if testing reveals runaway behavior. Differs from [adaptive research](adaptive-research.md)'s hard 25-call limit, which exists because that agent's open-ended loop can spiral; the baseline qualitative agent is self-limiting.

---

## News digest

Deterministic, pre-computed summary of news activity since the last invocation — the agent's primary awareness of "what happened," guiding tool calls and narrative threads. Produced by the data layer's news ingestion pipeline with no LLM involvement.

### Format

```
=== NEWS DIGEST (invocation {id}, covering {last_invocation_time} → {now}) ===
Headlines: {total_available} collected, {shown} shown below

--- MACRO / CROSS-SECTOR (top 5) ---
[ND-M1] {timestamp} [{T1|T2|T3}] {headline}
  Source: {outlet} | Tickers: {mentioned universe tickers, or "none"} | Tags: {type_tags}
[ND-M2] ...

--- TECH / SEMIS (top 5) ---
[ND-T1] {timestamp} [{T1|T2|T3}] {headline}
  Source: {outlet} | Tickers: {mentioned} | Tags: {type_tags}
[ND-T2] ...

--- FINANCIALS (top 5) ---
[ND-F1] {timestamp} [{T1|T2|T3}] {headline}
  Source: {outlet} | Tickers: {mentioned} | Tags: {type_tags}
[ND-F1] ...

--- ENERGY (top 5) ---
[ND-E1] {timestamp} [{T1|T2|T3}] {headline}
  Source: {outlet} | Tickers: {mentioned} | Tags: {type_tags}
[ND-E1] ...

--- EARNINGS CALLS SINCE LAST INVOCATION (if any) ---
[ND-EC1] {ticker} reported {timestamp}: EPS ${actual} vs ${est} est, rev ${actual}b vs ${est}b est
[ND-EC2] ...

--- HIGH-PRIORITY FLAGS (0–3, regardless of sector) ---
[ND-HP1] {timestamp} [{T1|T2|T3}] {headline}
  Priority reason: {M&A_rumor | short_report | surprise_regulatory | activist_involvement | geopolitical_escalation}
  Source: {outlet} | Tickers: {mentioned}
[ND-HP2] ...
```

### Sections

**Sector buckets (MACRO, TECH/SEMIS, FINANCIALS, ENERGY):** Top 5 per bucket. Sector determined by primary ticker or topic; cross-sector headlines (e.g., Fed decision) go in MACRO; headlines mentioning multiple sectors go in the sector of the first-mentioned universe ticker with cross-sector relevance in the tags.

**Earnings calls:** Per-ticker event results, structurally different from headlines. Present only when universe names reported since last invocation. Numeric result vs. consensus only (EPS and revenue from quant 5a–5b) — tone classification and topic extraction are this agent's job via `earnings_commentary` when warranted.

**High-priority flags:** Items visible regardless of sector-bucket slots. Capped at 3. Data layer designates these at ingestion as a first-class field: M&A rumors, activist short reports, surprise regulatory actions, activist involvement, geopolitical escalation (per [qualitative.md](../01-data-layer/external/qualitative.md) categories 1b and 5). A high-priority item also appears in its sector bucket if top-5 — duplication is intentional so the agent sees it even when skimming sectors.

### Reference IDs

Each digest item carries a reference ID the agent cites when investigating or producing output (e.g., "investigated [ND-T3] via news_search"); the synthesizer traces the chain to the original headline.

Reference scheme:
- `ND-M{N}` — macro/cross-sector headlines
- `ND-T{N}` — tech/semis headlines
- `ND-F{N}` — financials headlines
- `ND-E{N}` — energy headlines
- `ND-EC{N}` — earnings call summaries
- `ND-HP{N}` — high-priority flags

### Headline clustering

Two-pass deterministic algorithm producing the cluster set Step 1 of the [ranking algorithm](#ranking-algorithm) consumes. Runs at ingestion in the news pipeline — no LLM involvement.

**Stage 1 — Source canonicalization.** Many duplicate headlines are syndicated wire copies — the same Reuters or AP item republished by Yahoo, MarketWatch, CNBC within minutes. Stage 1 collapses these so cluster size reflects independent reporting rather than syndication multiplier.

- Compare every pair of headlines published within 30 minutes of each other.
- Match condition: normalized-Levenshtein ratio of `headline_text` ≥ 0.90. Normalization: lowercase, strip punctuation, normalize whitespace, normalize ticker references (`$AAPL` → `AAPL`), drop outlet-specific lead tokens (`BREAKING:`, `UPDATE:`, `EXCLUSIVE:`).
- Result: matched headlines merge into a source-canonical group keyed on the earliest member's `article_id`. The earliest publication time wins; the highest-credibility-tier source on the group wins for `is_first_mover` attribution per [news_sentiment.py § BreakingHeadline](../01-data-layer/schema/news_sentiment.py).

**Stage 2 — Event clustering.** Source-canonical headlines covering the same event from different angles (independent reporting on one filing, one earnings print, one geopolitical development) cluster into a single event cluster.

- Compare every pair of source-canonical headlines published within 6 hours of each other.
- Match condition (all three required):
  1. SimHash Jaccard similarity over 64-bit fingerprints of `headline_text` ≥ 0.60. Fingerprint: token set after the same normalization Stage 1 uses, hashed via SimHash.
  2. At least one shared ticker between the headlines' `(primary_ticker, tickers_mentioned)` sets. Ticker-less headlines cluster only with other ticker-less headlines whose `topic_tags` ([`HeadlineType`](../01-data-layer/schema/_common.py)) intersect by at least one tag.
  3. Both members fall within 6 hours of the cluster's `first_seen_at`.
- Cluster ID: the `article_id` of the earliest source-canonical member. Single-link agglomeration on the matched-pair graph — a headline joins an existing cluster if it matches any current member.
- Cluster sealing: a cluster seals at `first_seen_at + 24h`. Headlines arriving past the seal that match the cluster's similarity profile start a new cluster — the digest's invocation-windowed view does not need cross-day continuity.

**Cluster metadata.** Persisted in [`news_article_clusters`](../01-data-layer/collector/storage.md#news_article_clusters) and represented at the schema level by [`HeadlineCluster`](../01-data-layer/schema/news_sentiment.py):

- `cluster_id` — the earliest source-canonical member's `article_id`.
- `primary_theme` — the modal `HeadlineType` across cluster members; ties resolve to the modal member's first `topic_tags` entry.
- `headline_count` — number of source-canonical headlines (post-syndication, distinct outlets only). The count Step 2 of the ranking algorithm scores on.
- `first_seen_at`, `last_seen_at` — UTC timestamps bounding cluster activity within its 24h window.
- `tickers_involved` — union of `(primary_ticker, tickers_mentioned)` across cluster members.

Per-headline linkage: [`BreakingHeadline.cross_ticker_cluster_id`](../01-data-layer/schema/news_sentiment.py) carries the cluster's `cluster_id` for every member; null for unclustered headlines.

**Why two passes.** Syndication is the dominant duplication source in news APIs — Reuters → Yahoo → MarketWatch → CNBC chains produce 4–8 near-identical headlines per breaking event. A single-pass similarity check conflates these with same-event-different-angle independent coverage, inflating `headline_count` 4–8× and breaking the attention-signal interpretation Step 2 relies on.

**Why no embeddings.** SimHash is deterministic, replayable, and dependency-free — no model artifact, no embedding store, no GPU. Headlines are short (most < 20 tokens) and lexical sufficiency is high; the lexical-method failure mode (paraphrase miss) is rare for wire-service-style headlines, which dominate the digest's input.

### Ranking algorithm

Deterministic composite-score ranking per sector bucket — no LLM involvement.

**Step 1 — Deduplication:** Each headline arrives carrying its `cross_ticker_cluster_id` and the cluster's `headline_count` from [headline clustering](#headline-clustering). Within a sector bucket, retain the highest-credibility-tier member of each cluster as that cluster's representative; lower-tier copies drop. Cluster size feeds Step 2's score — 10 distinct sources on one event signals higher attention than one.

**Step 2 — Scoring:** Each deduplicated headline is scored:

| Factor | Weight | Logic |
|--------|--------|-------|
| Source credibility | High | Tier 1 = 3, Tier 2 = 2, Tier 3 = 1 |
| Recency | High | Linear decay from invocation time to last invocation time |
| Universe ticker mention | Medium | Headline directly mentions a universe ticker = 1.5× boost |
| Cluster size | Medium | More articles covering the same event = higher score (log-scaled to prevent runaway dominance) |
| High-priority flag | Override | High-priority items always appear in the HP section regardless of score; they also compete for sector slots on score |

**Step 3 — Selection:** Take the top 5 per sector bucket by composite score. If fewer than 5 headlines exist for a sector, show what's available (no padding).

### Type tags

Each headline carries one or more type tags from the canonical [`HeadlineType`](../01-data-layer/schema/_common.py) enum:

`breaking` · `earnings_related` · `m_and_a` · `analyst_action` · `regulatory` · `geopolitical` · `macro_data` · `insider_activity` · `short_report` · `activist` · `product_launch` · `supply_chain` · `guidance` · `sector_rotation`

Tags are assigned at ingestion time by the data layer; vendor tag vocabularies (Marketaux, Finnhub, SEC EDGAR 8-K item codes, RSS topics) are normalized into the canonical set via [`config/headline_tag_mapping.yaml`](../../../config/headline_tag_mapping.yaml). Multiple tags per headline are allowed (e.g., an M&A rumor from a Tier 1 source carries `m_and_a` + `breaking`).

### Token budget

Designed to fit within **~1,000–1,200 tokens** under all market conditions:
- 25 headlines × ~40 tokens each = ~1,000 tokens (sector buckets + macro)
- Earnings section: ~0 tokens (non-season) to ~200 tokens (peak season, 3–4 names)
- High-priority section: ~0–150 tokens (0–3 items)

The **total available vs. shown count** at the top tells the agent how much it's not seeing — "312 collected, 25 shown" signals high activity where tool pulls are likely needed; "28 collected, 25 shown" signals a quiet period.

---

## Sweep scope

The baseline sweep draws from all cross-sector qualitative categories, using in-context data as a starting point and tools for deeper investigation:

- **News digest triage** — scan the digest for headlines needing deeper investigation. Use `news_search` for full articles or related coverage. Quiet days: digest may suffice without tool pulls; busy days: several items may warrant follow-up.
- **Macro event calendar assessment** — what's coming in the next 24–72 hours that could change the thesis landscape (FOMC, CPI, universe earnings, OPEC)? The output isn't the calendar — it's the qualitative assessment of what each event means for current positions and potential new theses.
- **Sentiment regime assessment** — are tickers or sectors at extreme readings? Sentiment-price divergences? The agent reports current state; `social_sentiment` investigates *why* sentiment shifted.
- **Prediction market interpretation** — what do probability shifts mean for the system's sectors? A 10-point overnight shift in FOMC hold probability needs a qualitative read even without a quant flag. `prediction_markets` queries contracts beyond the tracked set.
- **Portfolio catalyst proximity** — cross-references the in-context event calendar with active thesis summaries. Are any open theses approaching their named catalyst? Is any upcoming event likely to accelerate or invalidate an existing thesis? Thesis summaries are loaded in context (not behind a tool) precisely because catalyst proximity needs both visible simultaneously.

---

## Output

A baseline qualitative brief — the "what's happening in the world" context layer. Delivered to the [synthesizer](synthesizer.md) alongside sector researcher briefs. Reference prefix `QR`.

Structured around **narrative threads** — coherent stories drawing from multiple input sources. Differs from domain researcher output (ticker-level findings with signal types) and adaptive research output (investigation conclusions). The synthesizer uses this brief as contextual backdrop reframing how sector findings should be interpreted: "SA-FIN-2 flags unusual financials flow" means something different when QR-1 says "rate expectations shifted hawkish overnight."

### Output schema

```
QUALITATIVE BRIEF
Invocation: {invocation_id}
Signal quality: {HIGH | MODERATE | LOW | DEGRADED}
  [If DEGRADED: reason — e.g., "news API returned partial results, sentiment data stale"]

=== NARRATIVE THREADS ===
[QR-1] {one-sentence thread summary}
  Relevance: {sectors and/or tickers this thread touches}
  Direction: {bullish | bearish | mixed | uncertain} for {specific subject}
  Time horizon: {immediate (<24h) | near-term (24-72h) | developing (>72h)}
  Evidence:
    - {source type}: {specific observation} [from {digest ref or tool pull}]
    - {source type}: {specific observation}
    - ...
  Implication: {1-2 sentences — what this means for trading decisions}

[QR-2] ...

[QR-N] ...
  (1–5 threads per brief. Fewer on quiet days, more on eventful days,
   but never zero — a "nothing is happening" observation is itself
   a thread worth noting.)

=== CATALYST WATCH ===
[QR-CW-1] {ticker}: {catalyst name} in {hours}h
  Thesis impact: {1 sentence — how this event could affect the active thesis}

[QR-CW-N] ...
  (0–5 items. Only present when active theses have approaching catalysts.
   Empty section on invocations with no imminent catalysts.)

=== SENTIMENT SNAPSHOT ===
Extremes: {tickers at extreme readings with direction, or "none"}
Divergences: {tickers where sentiment contradicts price action, or "none"}
Regime: {overall market sentiment characterization in 1 sentence}
```

### Schema design rationale

**Narrative threads as the core unit.** The brief's value is connecting signals across sources into a coherent story. A prediction market shift, a cluster of news headlines, and a sentiment extreme all pointing the same direction are one thread, not three separate findings. Thread organization lets the synthesizer see convergence directly rather than reconstructing it.

**Minimum two input sources per thread.** A thread must draw from at least two distinct input sources (news, sentiment, prediction markets, event calendar, earnings, portfolio catalysts). A single headline is just a headline; a headline corroborated by a prediction market shift and a sentiment move is a narrative. This prevents padding with restatements of individual data points.

**Relevance tagging (sectors and/or tickers).** Each thread explicitly names which sectors and tickers it's relevant to. The synthesizer uses this for cross-referencing ("QR-1 is relevant to financials → check against SA-FIN findings"). Relevance can be broad ("all sectors" for a macro thread) or narrow ("NVDA, AMD" for a supply chain thread).

**Direction + subject.** "Bearish for rate-sensitive tech" is more useful than "bearish." Direction is the thread's bottom-line implication; subject scopes what it applies to. The synthesizer needs both to match against domain findings.

**Time horizon.** Distinguishes immediate (FOMC in 4 hours), near-term (earnings cluster this week), and developing (regulatory posture shifting over weeks) narratives. Decision-layer agents weight immediate threads more heavily for current-invocation actions.

**Evidence with source attribution.** Each evidence line traces to either a news digest reference (`[ND-T3]`), a tool pull (`news_search: "chip export controls"`), or an in-context data point (`prediction markets: FOMC hold +12pp`) — an audit trail from the synthesizer's output back through this brief to the underlying data.

**Catalyst watch as a separate section.** Per-ticker, per-thesis, tied to the event calendar — not a narrative observation. Keeping it separate lets the synthesizer scan quickly for position-relevant timing. The thesis impact sentence connects the upcoming event to the specific active thesis.

**Sentiment snapshot as a fixed-size footer.** Three always-present, compact fields — a quick positioning check (extremes? sentiment-price divergence? overall mood?). On quiet days all three fields may be "none" / "none" / "neutral, no notable shifts."

**Deliberately absent:**
- **Anomaly flags** — [adaptive research](adaptive-research.md)'s territory
- **Thesis candidates** — the qualitative agent observes the world; thesis generation is the domain researchers' and [analyst's](../04-decision-layer/analyst.md) job
- **Signal type taxonomy** — domain researcher categories (`price_action`, `flow`, `options`, etc.) don't fit narrative threads, which by definition span multiple signal types
- **Per-ticker findings** — narrative-level here; ticker-level findings belong in domain researcher briefs

### Token budget

**300–600 tokens**, bounded regardless of market conditions.

- 1–5 narrative threads × ~60–80 tokens each = ~60–400 tokens
- Catalyst watch: 0–5 items × ~30 tokens each = ~0–150 tokens
- Sentiment snapshot: ~30–50 tokens (fixed)

Quiet days: ~300 tokens (1–2 threads, no catalysts, neutral sentiment). Eventful days: the agent triages harder — 5 threads at 5 evidence lines each would exceed the budget, forcing prioritization of highest-signal threads and compressed evidence. The budget enforces triage, which is the agent's primary value-add.

---

## Dependencies summary

| Dependency | Owner | Status | Description |
|------------|-------|--------|-------------|
| **In-context data** | | | |
| Per-ticker sentiment calibration | Distillation layer | Specified ([external.md](../02-distillation-layer/external.md)) | Trailing sentiment distributions, percentile delivery |
| Prediction market delta computation | Distillation layer | Specified ([external.md](../02-distillation-layer/external.md)) | Inter-invocation deltas, threshold flags |
| Unified event calendar | Data layer | Specified ([events.py](../01-data-layer/schema/events.py)) | `EventCalendar` (Events:CAL) — single sector-tagged feed across macro releases and regulatory / policy / geopolitical events |
| Active thesis summaries delivery | Execution layer | Specified ([thesis-model.md](../05-execution-layer/thesis-model.md)) | Per-thesis: ticker, summary, key catalyst, time expectation — loaded in context every invocation |
| **News digest** | | | |
| Headline credibility tier tagging | Data layer | Specified in prose ([qualitative.md](../01-data-layer/external/qualitative.md), 1a) | Must be implemented as structured field on ingested items |
| Headline universe ticker tagging | Data layer | Specified in prose ([qualitative.md](../01-data-layer/external/qualitative.md), 1a) | Must be implemented as structured field |
| Headline type tagging | Data layer | Not specified | New taxonomy defined in this doc; needs addition to data layer spec |
| Headline clustering / deduplication | Data layer | Specified ([§ Headline clustering](#headline-clustering)) | Two-pass syndication + event clustering; cluster metadata persisted in `news_article_clusters` |
| High-priority flag tagging | Data layer | Specified in prose ([qualitative.md](../01-data-layer/external/qualitative.md), 1b, 5e) | Must be implemented as first-class field |
| Earnings report detection | Data layer | Specified (quant 5a, 5b) | Flag when universe name reports since last invocation; deliver actual vs. consensus EPS/revenue |
| **Shared tools** (contracts defined in [adaptive-research.md](adaptive-research.md)) | | | |
| `news_search` | Data layer | Specified | Parameterized query: ticker, topic, keyword, lookback, max results |
| `social_sentiment` | Data layer | Specified | Parameterized query: tickers, lookback |
| `prediction_markets` | Data layer | Specified | Parameterized query: query string, categories |
| **New tool** | | | |
| `earnings_commentary` Tier 1 (numeric) | Data layer | Available | yfinance + Finnhub (result), Polygon (price reaction) — API keys configured, tested |
| `earnings_commentary` Tier 2 (transcript) | Data layer | Not yet built | Requires transcript ingestion (Motley Fool scraping or Quartr API — status TBD) + NLP pipeline (tone, Q&A clustering, non-answer detection — not specified) |
