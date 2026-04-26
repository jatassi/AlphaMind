# Synthesizer agent

Reads all analysis briefs and produces a unified market snapshot. The synthesizer's job is NOT to re-analyze — it's to find intersections and contradictions across domains, and to present them with typed source references so that [decision layer](../04-decision-layer/README.md) agents can drill into underlying detail on demand.

---

## Inputs

The synthesizer receives briefs from every analysis component, the volatility regime label as universal context, and on-demand access to portfolio state. Each brief source is assigned a reference prefix used throughout the output:

| Source | Prefix | Document |
|--------|--------|----------|
| Tech & semis domain researcher | `SA-TECH` | [tech-semis.md](domain-researchers/tech-semis.md) |
| Financials domain researcher | `SA-FIN` | [financials.md](domain-researchers/financials.md) |
| Energy domain researcher | `SA-ENERGY` | [energy.md](domain-researchers/energy.md) |
| Correlation and regime brief | `CR` | From the [distillation layer](../02-distillation-layer/external.md) |
| Baseline qualitative research | `QR` | [qualitative-research.md](qualitative-research.md) |
| Adaptive research threads | `AR` | [adaptive-research.md](adaptive-research.md) |

**Domain researcher briefs** (`SA-TECH`, `SA-FIN`, `SA-ENERGY`):
One from each sector agent covering intra-sector conditions, anomalies, and thesis candidates.

**Correlation and regime brief** (`CR`, from the [distillation layer](../02-distillation-layer/external.md)):
Dedicated brief computed from category 7 (cross-asset and correlation) data — covers intra-sector divergences, cross-sector rotation, intermarket regime signals, lead-lag gaps, and correlation regime changes. This brief is the synthesizer's native territory and should be processed as a first-class input alongside the sector briefs, not embedded within them.

**Baseline qualitative brief** (`QR`, from [qualitative-research.md](qualitative-research.md)):
The always-on context layer — headlines, macro calendar, sentiment, prediction market shifts, portfolio catalyst proximity.

**Adaptive research findings** (`AR`, from [adaptive-research.md](adaptive-research.md)):
One brief per investigation thread — anomaly-driven findings with signal-or-noise assessments and thesis implications. Individual threads are referenced as `AR-1`, `AR-2`, etc.

**Volatility regime label** (from the [distillation layer](../02-distillation-layer/external.md), section 4):
The current regime classification (low-vol compression, vol expansion, crisis/spike, vol normalization) plus the regime-transition flag. Universal context delivered to every analysis-layer agent.

---

## Portfolio state tools

The synthesizer has access to three lightweight read-only tools for on-demand portfolio context. These replace the former portfolio state researcher agent — rather than receiving a pre-summarized LLM brief about the portfolio, the synthesizer queries structured data directly when market signals are relevant to existing positions.

| Tool ID | Description | Returns |
|---------|-------------|---------|
| `get_positions_summary` | Current holdings at a glance | Per-position: ticker, direction, sector, size (% of portfolio), position age (hours) |
| `get_active_theses_summary` | Active thesis snapshots | Per-thesis: ticker, thesis one-liner (summary field from [thesis model](../05-execution-layer/thesis-model.md)), key catalyst, time expectation |
| `get_exposure_snapshot` | Portfolio exposure profile | Sector exposure breakdown (% per sector), net directional exposure (%), gross exposure (%) |

**Usage pattern:** The synthesizer calls these tools when cross-referencing market signals against the current portfolio — e.g., "multiple sector briefs flag tech momentum, and the portfolio is already 22% tech" or "SA-TECH-3 identifies a catalyst that directly affects an active thesis on NVDA." On quiet days with no portfolio-relevant signals, the synthesizer may not call any of these tools.

**What these tools do NOT provide:** P/L details, thesis component-level detail, capital efficiency metrics, system health diagnostics, activity log, or derived metrics (categories 7–11). Those are decision-layer concerns consumed directly by the [strategist](../04-decision-layer/strategist.md) and [portfolio manager](../04-decision-layer/portfolio-manager.md) via their own context packages and tools.

---

## Purpose

The synthesizer adds value by finding connections that no individual agent can see from its scoped vantage point. Each sector researcher sees its sector deeply but narrowly. The qualitative research layer sees the narrative landscape. The synthesizer sees where these perspectives converge, diverge, or create compound signals — and can pull portfolio context on demand (via its portfolio state tools) to flag when market signals are relevant to existing positions or exposure.

**Key value-add examples:**
- "Sentiment is bullish but flow data shows distribution" — more valuable than either signal alone
- "Prediction markets shifted on regulatory outcome but spot hasn't reacted" — potential opportunity
- "Technical setup is constructive but earnings calendar creates near-term event risk" — thesis qualification
- "Credit spreads widened 15bp this week but equities haven't responded — lead-lag model flags this as overdue repricing" — cross-asset divergence thesis
- "Intra-sector correlation in semis has broken down, NVDA diverging from AMD/AVGO — while implied correlation remains high, suggesting the market hasn't priced in the dispersion" — correlation regime insight

---

## Source reference mechanism

Every finding in the synthesizer output carries a typed source reference linking back to the specific finding in the originating brief. The format is `[<prefix>-<index>]`, where the prefix identifies the source agent and the index identifies the specific finding within that brief.

**Examples:**
- `[SA-TECH-3]` — third key finding from the tech & semis researcher
- `[QR-4]` — fourth item from the baseline qualitative research
- `[AR-2]` — second adaptive research investigation thread
- `[CR-1]` — first finding from the correlation/regime brief

These references serve two purposes:

1. **Traceability:** Every synthesizer claim can be traced back to its source, making the synthesis auditable.
2. **Retrieval:** [Decision layer](../04-decision-layer/README.md) agents have a retrieval tool that accepts these reference IDs and returns the corresponding source section. This allows drill-down into underlying detail without loading all source material into context by default.

---

## Contradiction and uncertainty handling

When source briefs conflict, the synthesizer does NOT attempt to resolve the contradiction. Instead, it:

1. **States both positions** with their source references
2. **Flags the contradiction explicitly** as an item requiring decision-layer attention
3. **Notes the implications** of each position being correct

This is a deliberate design choice. Contradictions between sources are often where the best trading opportunities hide — a bullish sector researcher clashing with bearish flow data, or prediction markets diverging from spot reaction. The analyst should see these contradictions in their raw form and make its own judgment, with the ability to retrieve the underlying detail via source references.

Similarly, when the synthesizer's confidence in a finding is low — insufficient data, ambiguous signals, or sources that neither confirm nor deny — it flags the uncertainty explicitly rather than omitting the finding or presenting it with false confidence.

---

## Output

A prose synthesis brief that captures the full state of the world as relevant to trading decisions. The brief is consumed by all three decision-layer agents — the [analyst](../04-decision-layer/analyst.md), the [strategist](../04-decision-layer/strategist.md), and the [portfolio manager](../04-decision-layer/portfolio-manager.md) — each loading it into their fresh context window at invocation start.

The synthesizer does not make trade recommendations and produces no reference IDs of its own — it presents the state of the world by surfacing intersections and contradictions across the upstream briefs, embedding upstream source references (`[SA-TECH-3]`, `[QR-4]`, `[AR-2]`, `[CR-1]`, etc.) where each claim originates. The [analyst](../04-decision-layer/analyst.md) generates theses from this picture; the [strategist](../04-decision-layer/strategist.md) re-evaluates existing theses against it; the [portfolio manager](../04-decision-layer/portfolio-manager.md) evaluates the resulting recommendations.

The brief is prose, not structured data. There is no producer-side schema — the form is whatever shape best serves the three decision-layer LLM consumers under current market conditions. The single load-bearing constraint is reference-ID embedding: every claim that traces back to an upstream brief must cite the corresponding `[<prefix>-<index>]` so downstream agents can drill in. Invented references propagate as failure surfaces at the consumer's referential-integrity check (see [llm-output-validation.md — Layer 3](../testing/llm-output-validation.md#layer-3--referential-integrity)) — keeping the synthesizer's contract minimal pushes structural enforcement to the agents that actually depend on the references.

### Retrieval store side-effect

In addition to emitting the prose brief, the synthesizer stage assembles a **retrieval store** keyed by reference ID. The store contains the upstream brief sections (domain researcher findings, qualitative narrative threads, adaptive research threads, correlation/regime findings) indexed by their producer-side IDs. Decision-layer agents pull sections from this store on demand via the source-brief retrieval tool — see [analyst.md — Source brief retrieval](../04-decision-layer/analyst.md#source-brief-retrieval), [strategist.md — Source brief retrieval](../04-decision-layer/strategist.md#source-brief-retrieval), and [portfolio-manager.md — Retrieval tools](../04-decision-layer/portfolio-manager.md#retrieval-tools).

The store is populated from already-validated upstream briefs (the structural validators in [llm-output-validation.md](../testing/llm-output-validation.md) enforce sequential indexing and prefix correctness on the producer side). The synthesizer does not invent or rename reference IDs; it indexes the upstream briefs as-emitted. On quiet days with broad consensus, retrieval may not be needed. On volatile days with many contradictions, agents naturally pull more source material — token spend scales with market complexity.
