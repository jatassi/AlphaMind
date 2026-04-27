# Adaptive research (anomaly-driven)

An LLM reads quant anomaly flags from the [programmatic distillation layer](../02-distillation-layer/external.md) and sector-researcher flagged anomalies, then *decides what to investigate*. The system's "curiosity" layer — different questions each cycle based on what the data is doing.

This is where the LLM's reasoning capability is most defensible as an edge: no traditional systematic strategy can dynamically generate research questions based on what the data is doing right now. See [design-decisions.md](../design-decisions.md).

---

## Inputs

Two anomaly streams plus the universal volatility regime label:

**Distillation layer anomaly flags** (from [external.md](../02-distillation-layer/external.md) §3): Purely quantitative detections — volume spikes, price-flow divergences, correlation breakdowns, lead-lag gaps, short-interest anomalies, earnings revision clusters, macro surprises, funding-stress alerts. Binary flags with magnitude, not interpretations.

**Sector-researcher flagged anomalies** (from [domain-researchers](domain-researchers/)): Items sector researchers marked for further investigation. May overlap with distillation flags but can also surface patterns statistical detection missed — e.g., "energy is responding to the EIA report differently than the last three weeks," contextual pattern recognition not captured by a z-score threshold.

**Volatility regime label** (from [distillation external.md §4](../02-distillation-layer/external.md)): Universal context — current regime (low-vol compression, vol expansion, crisis/spike, vol normalization) plus transition flag. Weights triage: a funding-stress flag is far more actionable in vol-expansion or crisis than in low-vol compression.

---

## Query generation

Good triage instincts — signal vs. noise — are the agent's primary value-add. Not every flag warrants a thread.

**Triage criteria:**
- **Actionability:** Would the answer change a trading decision in the next 4–72 hours? Interesting but inert anomalies aren't worth investigating.
- **Resolvability:** Can it be answered with available research tools (news searches, API pulls, prediction-market data)? "Why is the market irrational?" isn't resolvable; "What news drove NVDA's volume spike?" is.
- **Signal density:** Multi-flag anomalies are more likely signal than noise. A volume spike alone is weakly interesting; a volume spike with no price movement, divergent options flow, and a sector-researcher flag is strongly interesting.
- **Portfolio relevance:** Anomalies affecting held names or thesis candidates get priority.

**Few-shot examples** of signal vs. noise calibrate triage in the prompt; updated as the system accumulates a track record of which threads produced actionable findings.

**Example research threads:**
- "NVDA volume is 4x average with no price movement — news or institutional repositioning?"
- "XOM and CVX diverging despite both being integrated majors — what's driving the split?"
- "Gold and 10Y yields moving the same direction — what macro narrative, and how does it affect financials exposure?"
- "Prediction market odds on next FOMC hold shifted 15% overnight — what drove this and has the market priced it in?"
- "Lead-lag model flags credit spread widening without equity response — start of a repricing or noise from a single issuer?"

---

## Research execution

An agentic tool-use loop searches for answers to generated questions. Qualitative ingestion sources (qualitative 1–6 in [qualitative.md](../01-data-layer/external/qualitative.md)) are the research infrastructure — the [baseline sweep](qualitative-research.md) covers the surface; adaptive research makes targeted pulls.

Tools below are registered with the agent SDK with defined input/output contracts so the agent can compose multi-step investigations.


### Tool inventory

| Tool ID | Description | Input | Output | Rate limit |
|---------|-------------|-------|--------|------------|
| `news_search` | Targeted news search by ticker, topic, or keyword | `{query: str, tickers: [str], lookback_hours: int, max_results: int}` | `{articles: [{headline, source, timestamp, summary, url, relevance_score}]}` | 10 calls/invocation |
| `ticker_deep_pull` | Extended data pull for a single ticker — more granular than routine ingestion | `{ticker: str, categories: [str]}` | Category-specific data per [quantitative.md](../01-data-layer/external/quantitative.md) schema | 5 calls/invocation |
| `social_sentiment` | Social media sentiment for specific tickers (X, Reddit, StockTwits) | `{tickers: [str], lookback_hours: int}` | `{per_ticker: [{ticker, sentiment_score, volume, trending_topics, notable_posts: [str]}]}` | 5 calls/invocation |
| `prediction_markets` | Query specific prediction market contracts beyond the routine tracked set | `{query: str, categories: [str]}` | `{contracts: [{title, market, current_prob, prob_24h_ago, volume, expiration}]}` | 5 calls/invocation |
| `options_flow` | Unusual options activity detail for specific tickers | `{tickers: [str], lookback_hours: int, min_premium: float}` | `{flows: [{ticker, contract, direction, size, premium, timestamp, unusual_score}]}` | 5 calls/invocation |
| `sec_lending` | Securities lending market data — borrow rates, availability, utilization | `{tickers: [str]}` | `{per_ticker: [{ticker, borrow_rate, available_shares, utilization_pct, days_to_cover, cost_trend}]}` | 3 calls/invocation |
| `short_interest` | Short interest and squeeze composite for specific names | `{tickers: [str]}` | `{per_ticker: [{ticker, short_interest_pct, squeeze_score, days_to_cover, cost_to_borrow}]}` | 3 calls/invocation |
| `earnings_calendar` | Upcoming earnings dates, consensus estimates, and recent revisions | `{tickers: [str], lookback_days: int}` | `{per_ticker: [{ticker, report_date, consensus_eps, revision_trend, whisper_number}]}` | 5 calls/invocation |
| `macro_data` | Specific macro data point lookup (treasury yields, credit spreads, etc.) | `{indicator: str, lookback_days: int}` | `{series: [{date, value, change_1d, change_5d, percentile_1y}]}` | 5 calls/invocation |

### Tool usage contracts

**Input validation:** Inputs validated before API calls. Invalid inputs (unknown ticker, unsupported category) return an error immediately without consuming a rate-limit slot.

**Output format:** Structured data with `data_freshness` timestamp and `quality` flag (`complete`, `partial`, `stale`, `unavailable`).

**Rate limits per invocation:** Per-tool limits above plus an aggregate cap of **25 total tool calls per invocation** (configurable). Allows meaningful multi-step research (news_search → ticker_deep_pull → options_flow) without runaway loops.

**Cumulative token budget:** All tool outputs across all threads must fit a **4,000 token budget** (configurable). Large result sets are truncated by relevance score.

### On-demand vs. routine data

Some tools provide data not routinely ingested for the full universe but available on-demand. `sec_lending` and `short_interest` are the primary examples — available via API but not worth the cost/rate-limit budget across 65+ tickers every invocation; pulled selectively when a squeeze or short-selling anomaly warrants investigation.

**Per-thread execution model:** Each thread is an independent agentic loop with its own tool-use context — question, targeted pulls, synthesized answer. Inconclusive initial pulls allow bounded follow-ups.

---

## Bounded search

**Max 3–5 investigation threads per cycle,** each with capped tool calls. Without the bound, adaptive research consumes unbounded time and tokens while the market moves and the pipeline stalls.

The bound creates prioritization pressure: triage selects the highest-value questions, knowing most anomalies go uninvestigated this cycle. Persistent anomalies re-flag next cycle, producing a natural persistence-weighted queue — sustained anomalies eventually get investigated.

**Cost implications:** Most variable-cost component of the pipeline. Range: near-zero on quiet cycles to the full 3–5 thread budget on volatile ones. The per-run scoping TODO in the [README](README.md) addresses how the adaptive budget varies — pre-open runs likely warrant more (overnight developments); after-hours runs less.

---

## Output

Findings delivered to the [synthesizer](synthesizer.md) alongside the [baseline qualitative brief](qualitative-research.md) and sector-researcher briefs. Reference prefix `AR`; individual threads `AR-1`, `AR-2`, etc.

Structured around **investigation threads** — each self-contained question → evidence → verdict for a single anomaly. Differs from the [qualitative brief](qualitative-research.md) (narrative threads spanning multiple sources) and [domain researcher briefs](domain-researchers/tech-semis.md) (ticker-level findings with signal types). The synthesizer uses AR findings to **add or remove certainty** from domain-researcher observations and qualitative threads.

### Output schema

```
ADAPTIVE RESEARCH FINDINGS
Invocation: {invocation_id}
Threads investigated: {N} of {M} anomalies triaged
Anomalies deferred: {anomaly refs not investigated this cycle, or "none"}

=== INVESTIGATION THREADS ===
[AR-1]
  Trigger: {reference to originating anomaly}
    e.g., "[SA-TECH-ANOM-1]" or "Distillation: Q2 volume spike, NVDA, 3.2σ"
  Question: {the specific research question investigated}
  Tickers: {affected tickers}
  Sector: {primary sector}
  Tools used: {list of tool IDs called during investigation}
  Findings:
    - {factual finding with source attribution}
    - {factual finding}
    - ...
  Assessment: {signal | noise | inconclusive}
  Confidence: {high | moderate | low}
  If signal:
    Implication: {1-2 sentences — what this means for the triggering anomaly's tickers}
    Strengthens: {refs this finding corroborates, e.g., "[SA-TECH-3]", or "none"}
    Weakens: {refs this finding contradicts, e.g., "[SA-FIN-TC-1]", or "none"}
  If noise:
    Dismissal reason: {1 sentence — why this anomaly doesn't warrant action}
  If inconclusive:
    Missing: {what data would resolve this — guides next invocation's triage}

[AR-2] ...

[AR-N] ...
  (0–5 threads per invocation. Zero on quiet cycles with no anomalies
   worth investigating.)
```

### Schema design rationale

**`Trigger` with explicit reference — the cross-referencing key.** Each investigation links back to its spawning anomaly. Sector-researcher anomalies carry the exact reference ID (e.g., `[SA-TECH-ANOM-1]`); distillation flags carry spec ID and detection details (e.g., `Distillation: Q2 volume spike, NVDA, 3.2σ`). The synthesizer mechanically links AR findings back to the briefs that flagged the anomaly — without this it would have to infer relationships by content matching.

**`Strengthens` / `Weakens` cross-references.** AR findings often have implications beyond the triggering anomaly. "Investigated the NVDA volume spike, found pre-earnings institutional positioning — strengthens `[SA-TECH-3]` (unusual call buying), weakens `[SA-TECH-TC-2]` (short thesis candidate)." Explicit directional links pre-compute cross-references rather than forcing the synthesizer to reason about every pairwise interaction. References can point to any ID: domain findings (`SA-*`), anomalies (`SA-*-ANOM-*`), thesis candidates (`SA-*-TC-*`), qualitative threads (`QR-*`), or correlation/regime findings (`CR-*`).

**Three-way assessment (signal / noise / inconclusive).** Binary loses information. "Inconclusive" with a `Missing` field feeds next invocation's triage — the anomaly stays in pipeline awareness with "investigated last cycle but couldn't resolve because X." Persistent inconclusive anomalies accumulate triage priority across invocations — the persistence-weighted queue from [bounded search](#bounded-search).

**`Anomalies deferred` header.** Transparency about what was NOT investigated. Downstream agents see that `[SA-ENERGY-ANOM-1]` was flagged but uninvestigated and treat it at the original domain-researcher severity rather than assuming resolved. Quiet days: "none"; busy days: shows which anomalies lost triage.

**Per-investigation structure, not narrative threading.** Each thread is self-contained — one trigger, one question, evidence, one verdict. AR doesn't synthesize across its own threads; that's the synthesizer's job. If AR-1 finds NVDA institutional repositioning and AR-2 finds the same pattern in AMD, the synthesizer connects them via sector and ticker fields.

**Deliberately absent:**
- **Narrative threads** — qualitative brief's territory
- **Thesis candidates** — AR assesses anomalies; thesis generation is the domain researchers' and [analyst's](../04-decision-layer/analyst.md) job
- **Signal type taxonomy** — domain researcher signal types (`price_action`, `flow`, `options`, etc.) don't apply; AR threads are classified by trigger and assessment

### Token budget

**400–800 tokens**, bounded by:

- 0–5 threads × ~100–150 tokens each = ~0–750 tokens
- Header (threads investigated, anomalies deferred) = ~30–50 tokens
- The 4,000-token cumulative tool-output budget ([research execution](#research-execution)) bounds evidence; the schema bounds synthesis

Quiet cycles: ~50 tokens (header "0 of 0", empty thread section). Volatile cycles: 5 threads near the upper range approach the cap, enforcing concise findings and precise assessments.
