# Thesis model

Every position has exactly one thesis — a single record, not a hierarchy. The thesis has internal structure: typed, individually-addressable components linked to specific orders and bracket legs. Enables efficient consumption at three granularity levels, critical for token management as historical thesis data accumulates.

*Design rationale — structured components over flat narrative:* Diagnostic and self-improvement functions need to evaluate individual aspects of a thesis ("was the short-leg rationale correct?") without ingesting the entire narrative. Structuring the thesis into addressable components enables programmatic filtering before LLM evaluation, dramatically reducing token consumption at scale. A flat narrative evaluated holistically every time scales poorly — a system with 300 resolved theses analyzing "hit rate on short-side entry rationales across pairs trades" extracts those components programmatically, not by feeding 300 full theses to an LLM.

*Design rationale — flat record over thesis hierarchy:* Diagnostic richness comes from thesis *content discipline*, not structural complexity. A single thesis per position with mandatory content sections covering every bracket component achieves the same analytical power as hierarchical parent-child thesis records, without the schema complexity. Components are addressable by reference, not separate database entities.

---

## Thesis structure

**Summary.** The overall trade rationale in one or two sentences. Used for at-a-glance portfolio review and for portfolio state snapshots delivered to the analysis pipeline. Self-contained — a reader who sees only the summary should understand the core bet.

**Thesis components.** Each linked to a specific order or bracket leg, containing:

- **Component type:** entry rationale, target rationale, or invalidation rationale
- **Linked order/leg:** which order or bracket leg this addresses — entry, take-profit, price stop, time expiration, event-based invalidation, or a specific leg of a multi-leg strategy
- **Instrument reference:** which ticker or leg of a multi-leg strategy this pertains to
- **Narrative:** the specific reasoning — which signals support it, the expected causal chain, why this threshold was chosen
- **Key assumptions:** discrete falsifiable claims this depends on, expressed as testable statements (e.g., "AI capex will exceed consensus," "gaming revenue disappoints," "implied volatility contracts post-earnings")

### Mandatory coverage

The engine validates that every bracket leg has a corresponding thesis component. A bracket with a price-based stop, time-based expiration, and event-based invalidation must have three invalidation rationale components — one per threshold. A multi-leg strategy must have entry rationale components addressing each leg's contribution. Enforced structurally by the schema.

Every thesis must include at minimum:

- One entry rationale component per order leg (one for a simple equity entry, one per leg for a multi-leg strategy)
- One target rationale component explaining the expected exit level and the catalyst that should drive the price there
- One invalidation rationale component per hard bracket leg (price- or time-based) explaining the threshold
- One invalidation rationale component per soft bracket leg (event-based), if present

---

## Thesis status classifications

Each active thesis carries a discrete status assessment, updated at each pipeline invocation by the [strategist](../04-decision-layer/strategist.md) as part of its per-position evaluation. The strategist cross-references thesis narrative and key assumptions against the [synthesizer's](../03-analysis-layer/synthesizer.md) current market context:

- **On track:** the catalyst hasn't fired yet but conditions remain supportive, or the position is moving toward the target for the right reasons. No action required.
- **Partially realized:** the catalyst has partially played out — the expected move is underway but hasn't reached the target. The PM should consider partial profits or tightening invalidation.
- **At risk:** conditions have weakened the thesis without outright invalidating it — a supporting signal faded, a correlated position moved against the expected pattern, or new information creates ambiguity. Warrants increased monitoring and potentially reduced size.
- **Stale:** the thesis has outlived its expected time horizon without the catalyst firing. Original reasoning may still be valid, but the opportunity cost of an unresolving thesis is real. The PM should re-evaluate whether to maintain, reduce, or exit.
- **Invalidated:** the specific invalidation condition has been met — the falsifiable claim has been proven wrong. The position should be closed. Highest-urgency status, flagged prominently to the PM.

---

## Resolution: component-level then thesis-level

At position close, each thesis component receives an outcome assessment — **validated**, **wrong**, or **inconclusive** — before thesis-level resolution. Enables granular diagnostics: "The NVDA entry rationale was validated, the AMD short-leg rationale was wrong, net result was a loss" is more useful for system tuning than "the pairs trade lost money."

Component-level resolution is performed by the analysis pipeline (LLM-evaluated for qualitative components, programmatic for falsifiable quantitative assumptions). Thesis-level resolution integrates component assessments with P/L outcome.

**Thesis-level resolution categories:**

- **Validated — correct for the right reasons:** thesis played out as predicted, catalyst fired, position hit the target. Most or all components validated. The ideal outcome — reasoning was sound.
- **Profitable but wrong:** position made money, but not because the thesis was right — the market moved in the right direction for different reasons. Positive P/L but key components wrong or inconclusive. Most important category for system tuning — a lucky outcome that shouldn't reinforce the reasoning patterns that produced it.
- **Invalidated — stopped out correctly:** invalidation condition met, position closed, original thesis proven wrong. Negative P/L outcome but positive process outcome — the system correctly identified when it was wrong.
- **Invalidated — wrong on thesis, wrong on exit:** thesis wrong and system held too long, either because the invalidation condition was poorly specified or the PM was slow to act. A process failure — component assessments reveal which invalidation rationale was inadequate.

---

## Three consumption modes

Three distinct consumption patterns with different token efficiency profiles:

**Programmatic (zero LLM tokens).** Filter components by type, instrument, or outcome. "What's our hit rate on entry rationales for short equity legs?" is a database query across component records. "Which invalidation rationale components were assessed 'wrong' in the last 20 resolved theses?" is a filter-and-aggregate. No LLM involved.

**Targeted LLM evaluation (minimal tokens).** Pull a specific component plus relevant market data into a focused context window. "Evaluate whether the AMD short-leg rationale still holds given today's gaming revenue data" sends only the AMD component narrative, its key assumptions, and relevant market data — not the full thesis, not the NVDA leg, not bracket parameters. Small context, better reasoning, lower cost.

**Full thesis evaluation (full tokens, rare).** Ingest the complete thesis with all components for holistic assessment. Only needed for thesis-level resolution, when components interact such that seeing the whole picture matters, or for PM review of complex strategy positions. The least common mode.

The ingestion layer ([01-data-layer internal](../01-data-layer/internal/README.md)) delivers active theses at summary level by default, with component-level detail on request. The analysis pipeline pulls components selectively. Historical thesis resolution queries operate primarily in programmatic mode with targeted LLM evaluation for ambiguous cases.
