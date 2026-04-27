# Qualitative research — baseline (always-on)

Floor-level awareness of what's happening in the world, every invocation regardless of quant findings. Fixed predictable scope keeps per-invocation cost low.

LLM judgment applied to qualitative data the ingestion layer ([qualitative.md](../01-data-layer/external/qualitative.md)) collects and structures.

---

## Inputs

Two categories: **bounded in-context data** loaded at invocation start, and **on-demand data** available via tools during reasoning.

### In-context data (~6,500–8,000 tokens)

Loaded before reasoning begins. All bounded and predictable regardless of market conditions.

**1. News digest (~1,000–1,200 tokens)**
Deterministic, ranked summary of headlines since the last invocation. See [news digest specification](#news-digest) below.

**2. Sentiment aggregates (~3,000 tokens)**
Pre-computed per-ticker sentiment from [qualitative 2a](../01-data-layer/external/qualitative.md) — directional score, magnitude, rate of change, volume, sentiment-price divergence flag. Percentiles against each ticker's own trailing distribution per [distillation external.md §3](../02-distillation-layer/external.md), not a universal scale.

> **Data layer dependency:** Distillation maintains trailing per-ticker sentiment distributions and delivers current readings as percentiles. See [external.md](../02-distillation-layer/external.md), sentiment calibration state.

**3. Prediction market snapshot (~800–1,200 tokens)**
All tracked contracts from [qualitative 3a–3c](../01-data-layer/external/qualitative.md) — current probability, delta since last invocation, delta since last 24h, volume, expiration. Contracts with delta >5pp are flagged per the distillation threshold.

> **Data layer dependency:** Distillation computes and persists inter-invocation deltas and flags threshold breaches. See [external.md](../02-distillation-layer/external.md), prediction market state.

**4. Event calendar — next 72 hours (~500–1,000 tokens)**
Scheduled events from [qualitative 5e](../01-data-layer/external/qualitative.md) and [quantitative 6g](../01-data-layer/external/quantitative.md) — FOMC, CPI/PPI, universe earnings, OPEC, congressional hearings, court dates, Treasury auctions. Each entry: event name, timestamp, sector relevance tags, consensus/expected outcome where applicable.

> **Data layer dependency:** Data layer merges regulatory/policy events (qualitative 5e) with macro data releases (quantitative 6g) into a unified feed tagged by sector relevance.

**5. Active thesis summaries (~500–1,000 tokens)**
Per-thesis: ticker, summary one-liner (from [thesis model](../05-execution-layer/thesis-model.md)), key catalyst, time expectation. Loaded in-context so the agent can cross-reference the event calendar against thesis catalysts — "FOMC tomorrow and we hold three rate-sensitive positions" — without a tool call. Same data the [synthesizer](synthesizer.md) accesses via `get_active_theses_summary`; loaded here because catalyst-proximity assessment requires the theses always-visible.

Scales ~50 tokens × 10–20 concurrent positions = ~500–1,000 tokens. Bounded at the system's target portfolio size.

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

> **Data layer dependency:** The three shared tools require the data layer to support parameterized queries against its qualitative data stores — not just bulk delivery. Their input/output contracts are defined in [adaptive-research.md](adaptive-research.md). The `earnings_commentary` tool is new and its contract is defined below.

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

**Implementation phasing:** Initially `transcript_available` is `false` for most calls until transcript ingestion (Motley Fool scraping or Quartr API) and NLP extraction are built. Tier 1 is available day one. The tool degrades gracefully — Tier 1 with `quality: "partial_no_transcript"` — rather than failing when transcripts are unavailable.

> **Data layer dependencies:**
> - Earnings result data: yfinance + Finnhub (confirmed available, API keys configured)
> - Price reaction data: Polygon quote data (confirmed available, API key configured)
> - Transcript ingestion: Motley Fool scraping (free, <24hr latency) or Quartr API (status TBD per [earnings_commentary.yaml](../01-data-layer/mappings/earnings_commentary.yaml))
> - Transcript NLP pipeline: not yet specified — tone classification, Q&A clustering, non-answer detection, forward-looking statement extraction all marked "derived from TBD" in mappings
> - ManagementToneAnalysis and AnalystQADynamics schemas: defined in [schema/earnings_commentary.py](../01-data-layer/schema/earnings_commentary.py); computation pipeline not yet built

**Tool call budget:** No hard cap. The agent's closed-scope mandate (assess the five sweep categories) bounds usage — single pass across known categories, not an open-ended loop. System prompt includes soft advisory guidance: "typically 5–15 tool calls per invocation; fewer on quiet days, more on busy days." A hard cap can be added if testing reveals runaway behavior.

Differs from [adaptive research](adaptive-research.md)'s hard 25-call limit, which exists because that agent's open-ended investigation loop can spiral. The baseline qualitative agent is inherently self-limiting.

---

## News digest

Deterministic, pre-computed summary of news activity since the last invocation. The agent's primary awareness of "what happened" — guides tool calls and narrative threads. Produced by the data layer's news ingestion pipeline with no LLM involvement.

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

**Sector buckets (MACRO, TECH/SEMIS, FINANCIALS, ENERGY):** Top 5 per bucket. Sector is determined by primary ticker or topic; cross-sector headlines (e.g., Fed decision) go in MACRO; headlines mentioning multiple sectors go in the sector of the first-mentioned universe ticker with cross-sector relevance in the tags.

**Earnings calls:** Per-ticker event results, structurally different from headlines. Present only when universe names reported since last invocation. Numeric result vs. consensus only (EPS and revenue) — tone classification and topic extraction are the qualitative research agent's job via `earnings_commentary` when warranted.

> **Data layer dependency:** Numeric data only — actual EPS/revenue vs. consensus from quantitative category 5a–5b ([quantitative.md](../01-data-layer/external/quantitative.md)). Data layer flags universe names that reported since last invocation (from quant 5a's earnings calendar) and delivers actual-vs-estimate. No LLM needed for this section.

**High-priority flags:** Items visible regardless of sector-bucket slots. Capped at 3. Data layer designates these at ingestion: M&A rumors, activist short reports, surprise regulatory actions, activist involvement, geopolitical escalation (per [qualitative.md](../01-data-layer/external/qualitative.md) categories 1b and 5). A high-priority item also appears in its sector bucket if top-5 — duplication is intentional so the agent sees it even when skimming sectors.

> **Data layer dependency:** High-priority tagging at ingestion. The qualitative data spec defines triggers in prose: M&A headlines mentioning universe names ([qualitative.md](../01-data-layer/external/qualitative.md), 1b), short-report publications (1b), surprise regulatory/geopolitical events (5e). Must be a first-class field on ingested items, not inferred downstream.

### Reference IDs

Each digest item carries a reference ID (`ND-M1`, `ND-T3`, `ND-HP2`, etc.) the agent cites when investigating or producing output. The agent's brief can reference "investigated [ND-T3] via news_search" and the synthesizer traces the chain to the original headline.

Reference scheme:
- `ND-M{N}` — macro/cross-sector headlines
- `ND-T{N}` — tech/semis headlines
- `ND-F{N}` — financials headlines
- `ND-E{N}` — energy headlines
- `ND-EC{N}` — earnings call summaries
- `ND-HP{N}` — high-priority flags

### Ranking algorithm

Deterministic composite-score ranking per sector bucket — no LLM involvement.

**Step 1 — Deduplication:** Cluster same-event headlines (headline similarity + co-occurring ticker mentions within a short window). Keep the highest-credibility source per cluster. Record cluster size — 10 articles on one event signals higher attention than one article.

> **Data layer dependency:** Headline clustering at ingestion. The qualitative data spec describes "cross-ticker news clustering" ([qualitative.md](../01-data-layer/external/qualitative.md), 1a) as a concept; algorithm is not specified. Needs: similarity metric (text overlap, shared tickers, time proximity), representative selection (highest credibility tier, then most recent), cluster size as metadata.

**Step 2 — Scoring:** Each deduplicated headline is scored:

| Factor | Weight | Logic |
|--------|--------|-------|
| Source credibility | High | Tier 1 = 3, Tier 2 = 2, Tier 3 = 1 |
| Recency | High | Linear decay from invocation time to last invocation time |
| Universe ticker mention | Medium | Headline directly mentions a universe ticker = 1.5× boost |
| Cluster size | Medium | More articles covering the same event = higher score (log-scaled to prevent runaway dominance) |
| High-priority flag | Override | High-priority items always appear in the HP section regardless of score; they also compete for sector slots on score |

> **Data layer dependency:** Source credibility tiers are already defined in the qualitative data spec ([qualitative.md](../01-data-layer/external/qualitative.md), 1a — Tier 1/2/3 classification). The data layer must persist the credibility tier as a field on each ingested headline. Universe ticker mention detection requires the data layer to tag headlines with mentioned universe tickers at ingestion time — this is already specified ("tags by ticker, sector, and topic") but must be implemented as structured fields, not free text.

**Step 3 — Selection:** Take the top 5 per sector bucket by composite score. If fewer than 5 headlines exist for a sector, show what's available (no padding with low-relevance items).

### Type tags

Each headline carries one or more type tags from a fixed taxonomy:

`breaking` · `earnings_related` · `M&A` · `analyst_action` · `regulatory` · `geopolitical` · `macro_data` · `insider_activity` · `short_report` · `activist` · `product_launch` · `supply_chain` · `guidance` · `sector_rotation`

Tags are assigned at ingestion time by the data layer based on headline content and source classification. Multiple tags per headline are allowed (e.g., a headline about an M&A rumor from a Tier 1 source would carry `M&A` + `breaking`).

> **Data layer dependency:** The data layer must implement headline type tagging at ingestion time. The qualitative data spec describes many of these event types in prose but does not define a formal tag taxonomy or a tagging mechanism. The taxonomy above should be added to the data layer spec as a required field on ingested news items.

### Token budget

The news digest is designed to fit within **~1,000–1,200 tokens** under all market conditions:
- 25 headlines × ~40 tokens each = ~1,000 tokens (sector buckets + macro)
- Earnings section: ~0 tokens (non-season) to ~200 tokens (peak season, 3–4 names)
- High-priority section: ~0–150 tokens (0–3 items)

The **total available vs. shown count** at the top of the digest tells the agent how much it's not seeing. "312 collected, 25 shown" signals a high-activity period where tool pulls are likely needed; "28 collected, 25 shown" signals a quiet period where the digest captures nearly everything.

---

## Sweep scope

The baseline sweep draws from all cross-sector qualitative categories, using the in-context data as a starting point and tools for deeper investigation:

- **News digest triage** — scan the digest for headlines that need deeper investigation. Use `news_search` to pull full articles or search for related coverage. On quiet days, the digest may be sufficient without tool pulls. On busy days, several digest items may warrant follow-up.
- **Macro event calendar assessment** — from the in-context event calendar: what's coming in the next 24–72 hours that could change the thesis landscape? FOMC decisions, CPI releases, earnings for universe names, OPEC meetings. The output isn't the calendar itself — it's the qualitative assessment of what each upcoming event means for current positions and potential new theses.
- **Sentiment regime assessment** — from the in-context sentiment aggregates: are any tickers or sectors at extreme readings? Are there sentiment-price divergences? The agent reports the current sentiment state; investigation of *why* sentiment shifted can use the `social_sentiment` tool.
- **Prediction market interpretation** — from the in-context prediction market snapshot: what do the probability shifts mean for the system's sectors? A 10-point shift in FOMC hold probability overnight needs a qualitative read even if no quant anomaly was flagged. The agent can use the `prediction_markets` tool to query contracts beyond the tracked set.
- **Portfolio catalyst proximity** — cross-referencing the in-context event calendar with the in-context active thesis summaries. Are any open theses approaching their named catalyst? Is any upcoming event likely to accelerate or invalidate an existing thesis? This is why thesis summaries are loaded in context rather than behind a tool — the agent needs both the calendar and the theses visible simultaneously to spot catalyst proximity.

---

## Output

A baseline qualitative brief — the "what's happening in the world" context layer. Delivered to the [synthesizer](synthesizer.md) alongside sector researcher briefs. Uses reference prefix `QR` for the synthesizer's typed reference system.

The brief is structured around **narrative threads** — coherent stories about the world that draw from multiple input sources. This is fundamentally different from the domain researcher output (ticker-level findings with signal types) and the adaptive research output (investigation conclusions). The synthesizer uses this brief as contextual backdrop that reframes how sector-level findings should be interpreted: "SA-FIN-2 flags unusual financials flow" means something different when QR-1 says "rate expectations shifted hawkish overnight."

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

**Narrative threads as the core unit.** The qualitative brief's value is in connecting signals across sources into a coherent story. A prediction market shift, a cluster of news headlines, and a sentiment extreme that all point the same direction are one thread, not three separate findings. Organizing by thread lets the synthesizer see the convergence directly rather than having to reconstruct it.

**Minimum two input sources per thread.** A thread must draw from at least two distinct input sources (news, sentiment, prediction markets, event calendar, earnings, portfolio catalysts). A single-source observation doesn't belong here — a single headline is just a headline; a headline corroborated by a prediction market shift and a sentiment move is a narrative. This rule prevents the agent from padding the brief with restatements of individual data points.

**Relevance tagging (sectors and/or tickers).** Each thread explicitly names which sectors and tickers it's relevant to. The synthesizer uses this for cross-referencing: "QR-1 is relevant to financials → check against SA-FIN findings." Relevance can be broad ("all sectors" for a macro thread like a VIX spike) or narrow ("NVDA, AMD" for a supply chain thread).

**Direction + subject.** "Bearish for rate-sensitive tech" is more useful than "bearish." The direction is the thread's bottom-line implication; the subject scopes what it applies to. The synthesizer needs both to match against domain findings.

**Time horizon.** Distinguishes between threads that require immediate attention (FOMC in 4 hours), near-term context (earnings cluster this week), and developing narratives (regulatory posture shifting over weeks). Decision-layer agents weight immediate threads more heavily for current-invocation actions.

**Evidence with source attribution.** Each evidence line traces to either a news digest reference (`[ND-T3]`), a tool pull (`news_search: "chip export controls"`), or an in-context data point (`prediction markets: FOMC hold +12pp`). This creates an audit trail from the synthesizer's output back through the qualitative brief to the underlying data.

**Catalyst watch as a separate section.** This is structurally different from narrative threads — it's per-ticker, per-thesis, tied to the event calendar, not a narrative observation. Keeping it separate lets the synthesizer scan it quickly for position-relevant timing without parsing through narrative threads. The thesis impact sentence connects the upcoming event to the specific active thesis, giving the synthesizer (and downstream decision-layer agents) immediate context on what's at stake.

**Sentiment snapshot as a fixed-size footer.** Three fields, always present, always compact. The synthesizer reads this as a quick positioning check — are crowds extreme anywhere, is sentiment diverging from price anywhere, what's the overall mood? Not a narrative thread; a state summary. On quiet days all three fields may be "none" / "none" / "neutral, no notable shifts."

**What's deliberately absent:**
- **Anomaly flags** — anomaly detection and investigation is the [adaptive research agent's](adaptive-research.md) territory, not the baseline qualitative sweep
- **Thesis candidates** — the qualitative agent observes the world, it doesn't propose trades. Thesis candidate generation is the domain researchers' job (and ultimately the [analyst's](../04-decision-layer/analyst.md))
- **Signal type taxonomy** — the domain researcher contract uses `price_action`, `flow`, `options`, etc. These categories don't fit narrative threads, which by definition span multiple signal types. Forcing a taxonomy here would fragment the narrative
- **Per-ticker findings** — the qualitative brief operates at the narrative level, not the ticker level. Individual ticker observations belong in domain researcher briefs

### Token budget

**300–600 tokens**, bounded regardless of market conditions.

- 1–5 narrative threads × ~60–80 tokens each = ~60–400 tokens
- Catalyst watch: 0–5 items × ~30 tokens each = ~0–150 tokens
- Sentiment snapshot: ~30–50 tokens (fixed)

On quiet days the brief is short (~300 tokens: 1–2 threads, no catalysts, neutral sentiment). On eventful days the agent triages harder — 5 threads at 5 evidence lines each would exceed the budget, so the agent must prioritize the highest-signal threads and compress evidence. The budget constraint enforces triage, which is the agent's primary value-add.

---

## Dependencies summary

| Dependency | Owner | Status | Description |
|------------|-------|--------|-------------|
| **In-context data** | | | |
| Per-ticker sentiment calibration | Distillation layer | Specified ([external.md](../02-distillation-layer/external.md)) | Trailing sentiment distributions, percentile delivery |
| Prediction market delta computation | Distillation layer | Specified ([external.md](../02-distillation-layer/external.md)) | Inter-invocation deltas, threshold flags |
| Unified event calendar | Data layer | Not specified | Merge qualitative 5e + quantitative 6g into single feed with sector tags |
| Active thesis summaries delivery | Execution layer | Specified ([thesis-model.md](../05-execution-layer/thesis-model.md)) | Per-thesis: ticker, summary, key catalyst, time expectation — loaded in context every invocation |
| **News digest** | | | |
| Headline credibility tier tagging | Data layer | Specified in prose ([qualitative.md](../01-data-layer/external/qualitative.md), 1a) | Must be implemented as structured field on ingested items |
| Headline universe ticker tagging | Data layer | Specified in prose ([qualitative.md](../01-data-layer/external/qualitative.md), 1a) | Must be implemented as structured field |
| Headline type tagging | Data layer | Not specified | New taxonomy defined in this doc; needs addition to data layer spec |
| Headline clustering / deduplication | Data layer | Partially specified ([qualitative.md](../01-data-layer/external/qualitative.md), 1a) | Concept described; algorithm and cluster metadata not specified |
| High-priority flag tagging | Data layer | Specified in prose ([qualitative.md](../01-data-layer/external/qualitative.md), 1b, 5e) | Must be implemented as first-class field |
| Earnings report detection | Data layer | Specified (quant 5a, 5b) | Flag when universe name reports since last invocation; deliver actual vs. consensus EPS/revenue |
| **Shared tools** (contracts defined in [adaptive-research.md](adaptive-research.md)) | | | |
| `news_search` | Data layer | Specified | Parameterized query: ticker, topic, keyword, lookback, max results |
| `social_sentiment` | Data layer | Specified | Parameterized query: tickers, lookback |
| `prediction_markets` | Data layer | Specified | Parameterized query: query string, categories |
| **New tool** | | | |
| `earnings_commentary` Tier 1 (numeric) | Data layer | Available | yfinance + Finnhub (result), Polygon (price reaction) — API keys configured, tested |
| `earnings_commentary` Tier 2 (transcript) | Data layer | Not yet built | Requires transcript ingestion (Motley Fool scraping or Quartr API — status TBD) + NLP pipeline (tone, Q&A clustering, non-answer detection — not specified) |
