# Adaptive research (anomaly-driven)

An LLM reads the quant anomaly flags (from the [programmatic distillation layer](../02-distillation-layer/external.md)) and the sector researcher flagged anomalies, then *decides what to investigate further*. This is the system's "curiosity" layer — where it asks different questions each cycle based on what the numbers are doing.

This is where the LLM's reasoning capability is most defensible as an edge. No traditional systematic strategy can dynamically generate research questions based on what the data is doing right now. See [design-decisions.md](../design-decisions.md) for the rationale on adaptive vs. fixed research.

---

## Inputs

The adaptive research agent receives two streams of anomaly signals plus the universal volatility regime label:

**Distillation layer anomaly flags** (from [external.md](../02-distillation-layer/external.md), section 3):
Purely quantitative detections — volume spikes, price-flow divergences, correlation breakdowns, lead-lag gaps, short interest anomalies, earnings revision clusters, macro surprises, funding stress alerts. These are binary flags with magnitude, not interpretations.

**Sector researcher flagged anomalies** (from [domain-researchers](domain-researchers/)):
Items the sector researchers marked as warranting further investigation during their analysis pass. These may overlap with the distillation flags but can also surface patterns the distillation layer's quantitative detection missed — a sector researcher might flag "the energy sector is responding to the EIA report differently than the last three weeks" based on contextual pattern recognition that isn't captured by a statistical anomaly threshold.

**Volatility regime label** (from [distillation external.md](../02-distillation-layer/external.md), section 4):
Universal context broadcast — current regime classification (low-vol compression, vol expansion, crisis/spike, vol normalization) plus the regime-transition flag. Used to weight triage decisions: anomalies that warrant immediate investigation differ across regimes (a funding-stress flag is far more actionable in vol-expansion or crisis than in low-vol compression).

---

## Query generation

The LLM generating research questions needs good triage instincts — distinguishing interesting anomalies from noise. Not every flagged anomaly warrants an investigation thread. The triage decision is itself a high-value LLM judgment call.

**Triage criteria:**
- **Actionability:** Would the answer change a trading decision in the next 4–72 hours? An anomaly that's interesting but wouldn't inform a thesis isn't worth investigating.
- **Resolvability:** Can the question be answered with available research tools (news searches, API pulls, prediction market data)? "Why is the market irrational?" isn't resolvable; "What news drove NVDA's volume spike?" is.
- **Signal density:** Anomalies that combine multiple flags are more likely to be signal than noise. A volume spike alone is weakly interesting; a volume spike with no price movement, divergent options flow, and a sector researcher flag is strongly interesting.
- **Portfolio relevance:** Anomalies affecting names the system currently holds or is considering for theses get priority.

**Few-shot examples** of signal vs. noise anomalies should be included in the prompt to calibrate the triage instinct. These examples should be updated as the system accumulates a track record of which investigation threads produced actionable findings and which were dead ends.

**Example research threads:**
- "NVDA volume is 4x average with no price movement — is there news or is this institutional repositioning?"
- "XOM and CVX diverging despite both being integrated majors — what's driving the split?"
- "Gold and 10Y yields moving in the same direction — what macro narrative explains this, and how does it affect our financials exposure?"
- "Prediction market odds on next FOMC hold shifted 15% overnight — what drove this and has the market priced it in?"
- "Lead-lag model flags credit spread widening without equity response — is this the start of a repricing or noise from a single issuer?"

---

## Research execution

Once questions are generated, an agentic tool-use loop searches for answers. The qualitative ingestion sources (qualitative 1–6 from [qualitative.md](../01-data-layer/external/qualitative.md)) are the research infrastructure — the [baseline sweep](qualitative-research.md) covers the surface, and adaptive research makes targeted pulls when something specific needs investigation.

**Available research tools:**

The following tools are registered with the agent SDK and available to the adaptive research agent during its agentic loop. Each tool has a defined input/output contract so the agent can compose multi-step investigations.

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

**Input validation:** Each tool validates its inputs before making API calls. Invalid inputs (unknown ticker, unsupported category) return an error immediately rather than consuming a rate limit slot.

**Output format:** All tools return structured data that the LLM can reason about directly. Outputs include a `data_freshness` timestamp and a `quality` flag (`complete`, `partial`, `stale`, `unavailable`) so the agent can assess reliability.

**Rate limits per invocation:** The per-tool call limits ensure bounded cost and latency. Across all tools combined, the adaptive research agent is limited to **25 total tool calls per invocation** (configurable). This prevents runaway investigation loops while allowing meaningful multi-step research (e.g., news_search → ticker_deep_pull → options_flow for a single investigation thread).

**Cumulative token budget:** All tool outputs across all investigation threads must fit within a **4,000 token budget** (configurable). If a tool returns a large result set, it's truncated to the most relevant items by relevance score. This keeps the adaptive research output compact enough for the synthesizer's context.

### On-demand vs. routine data

Several tools provide data that is not routinely ingested for the full universe but is available on-demand for specific names. The `sec_lending` and `short_interest` tools are the primary examples — the data is available via API but not worth the cost/rate-limit budget to pull for 65+ tickers every invocation. The adaptive research agent pulls this data selectively when a squeeze or short-selling anomaly warrants investigation.

**Per-thread execution model:** Each investigation thread operates as an independent agentic loop with its own tool-use context. The thread starts with a question, makes targeted data pulls, and synthesizes an answer. If the initial pull is inconclusive, the agent can make follow-up queries — but within bounded limits.

---

## Bounded search

**Max 3–5 investigation threads per cycle,** each with a capped number of tool calls. This constraint is critical — the adaptive research layer could easily consume unbounded time and tokens chasing interesting anomalies while the market moves and the pipeline stalls.

The bound creates a natural prioritization pressure: the triage step must select the highest-value questions, knowing that most anomalies will go uninvestigated this cycle. Uninvestigated anomalies that persist will be flagged again next cycle, naturally creating a persistence-weighted priority queue where sustained anomalies eventually get investigated even if they're not the most urgent in any single cycle.

**Cost implications:** Adaptive research is the most variable-cost component of the pipeline. The always-on baseline research has a predictable token budget; adaptive research can range from near-zero (a quiet cycle with few anomalies) to the full 3–5 thread budget (a volatile cycle with multiple simultaneous signals). The per-run scoping TODO in the [README](README.md) should address how the adaptive research budget varies across run types — pre-open runs likely warrant more adaptive budget (overnight developments to investigate), while after-hours runs may warrant less.

---

## Output

Adaptive research findings delivered to the [synthesizer](synthesizer.md) alongside the [baseline qualitative brief](qualitative-research.md) and sector researcher briefs. Uses reference prefix `AR` for the synthesizer's typed reference system. Individual threads are referenced as `AR-1`, `AR-2`, etc.

The output is structured around **investigation threads** — each a self-contained question → evidence → verdict for a single anomaly. This is fundamentally different from the [qualitative brief](qualitative-research.md) (narrative threads spanning multiple sources) and the [domain researcher briefs](domain-researchers/tech-semis.md) (ticker-level findings with signal types). The synthesizer uses AR findings to **add or remove certainty** from domain researcher observations and qualitative narrative threads.

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

**`Trigger` with explicit reference — the cross-referencing key.** Each investigation links back to the anomaly that spawned it. When the trigger is a sector researcher anomaly, it carries the exact reference ID (e.g., `[SA-TECH-ANOM-1]`). When it's a distillation layer anomaly flag, it carries the spec ID and detection details (e.g., `Distillation: Q2 volume spike, NVDA, 3.2σ`). The synthesizer can mechanically link AR findings back to the domain researcher briefs that flagged the anomaly — this is critical for consolidation. Without it, the synthesizer would have to infer which AR thread relates to which domain finding by reading and matching on content.

**`Strengthens` / `Weakens` cross-references.** The AR agent's findings often have implications beyond the triggering anomaly. "I investigated the NVDA volume spike and found pre-earnings institutional positioning — this strengthens `[SA-TECH-3]` (unusual call buying) and weakens `[SA-TECH-TC-2]` (the short thesis candidate)." These explicit directional links give the synthesizer pre-computed cross-references rather than forcing it to reason about how each AR finding affects every other brief. References can point to any ID in the invocation's briefs: domain researcher findings (`SA-*`), anomalies (`SA-*-ANOM-*`), thesis candidates (`SA-*-TC-*`), qualitative threads (`QR-*`), or correlation/regime findings (`CR-*`).

**Three-way assessment (signal / noise / inconclusive).** Binary signal/noise loses information. "Inconclusive" with a `Missing` field feeds the next invocation's triage — the anomaly persists in the pipeline's awareness, and the adaptive research agent can see "this anomaly was investigated last cycle but couldn't be resolved because X." Over multiple invocations, persistent inconclusive anomalies accumulate triage priority, creating the natural persistence-weighted queue described in the [bounded search](#bounded-search) section.

**`Anomalies deferred` header.** Transparency about what was NOT investigated. The synthesizer (and downstream agents) see that `[SA-ENERGY-ANOM-1]` was flagged but uninvestigated — it should be treated with the original severity from the domain researcher brief, not assumed resolved. On quiet days this field is "none"; on busy days it shows which anomalies lost the triage prioritization.

**Per-investigation structure, not narrative threading.** Each thread is self-contained: one trigger → one question → evidence → one verdict. The AR agent doesn't synthesize across its own threads — that's the synthesizer's job. If two AR threads produce findings that interact ("AR-1 found institutional repositioning in NVDA" + "AR-2 found the same pattern in AMD"), the synthesizer connects them using the sector and ticker fields.

**What's deliberately absent:**
- **Narrative threads** — the qualitative brief handles narrative context; AR handles anomaly resolution
- **Thesis candidates** — AR assesses anomalies, it doesn't propose trades. Thesis generation is the domain researchers' and [analyst's](../04-decision-layer/analyst.md) job
- **Signal type taxonomy** — the domain researcher signal types (`price_action`, `flow`, `options`, etc.) don't apply here. AR threads are classified by their trigger and assessment, not by signal category

### Token budget

**400–800 tokens**, bounded by the existing constraints:

- 0–5 investigation threads × ~100–150 tokens each = ~0–750 tokens
- Header (threads investigated, anomalies deferred) = ~30–50 tokens
- The 4,000 token cumulative tool output budget (defined in [research execution](#research-execution)) bounds the evidence the agent can gather; the output schema bounds the synthesis of that evidence into findings

On quiet cycles with no anomalies worth investigating, the output is minimal (~50 tokens: header with "0 of 0" and empty thread section). On volatile cycles, 5 threads at the upper end of the range approaches the budget cap, which enforces triage quality — the agent must be concise in its findings and precise in its assessments.
