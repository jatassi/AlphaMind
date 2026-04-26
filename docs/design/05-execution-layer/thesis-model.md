# Thesis model

Every position has exactly one thesis — a single record, not a hierarchy. However, the thesis has internal structure: typed, individually-addressable components linked to specific orders and bracket legs. This enables efficient consumption at three different granularity levels, which is critical for token management as the system accumulates historical thesis data.

*Design rationale — structured components over flat narrative:* Diagnostic and self-improvement functions need to evaluate individual aspects of a thesis ("was the short-leg rationale correct?") without ingesting the entire thesis narrative. Structuring the thesis into addressable components enables programmatic filtering before LLM evaluation, dramatically reducing token consumption for analysis and diagnostics at scale. The alternative — a flat narrative evaluated holistically every time — scales poorly. A system with 300 resolved theses that wants to analyze "hit rate on short-side entry rationales across pairs trades" should be able to extract those specific components programmatically, not feed 300 full thesis narratives to an LLM.

*Design rationale — flat record over thesis hierarchy:* The diagnostic richness comes from thesis *content discipline*, not from structural complexity in the data model. A single thesis per position with mandatory content sections that address every bracket component achieves the same analytical power as hierarchical parent-child thesis records, without the schema complexity. Individual components are addressable by reference, not by separate database entities.

---

## Thesis structure

**Summary.** The overall trade rationale in one or two sentences. Used for at-a-glance portfolio review by the portfolio manager and for portfolio state snapshots delivered to the analysis pipeline. Should be self-contained — a reader who sees only the summary should understand the core bet.

**Thesis components.** Each component is linked to a specific order or bracket leg and contains:

- **Component type:** entry rationale, target rationale, or invalidation rationale
- **Linked order/leg:** which specific order or bracket leg this component addresses — entry, take-profit, price stop, time expiration, event-based invalidation, or a specific leg of a multi-leg strategy
- **Instrument reference:** which ticker or leg of a multi-leg strategy this component pertains to
- **Narrative:** the specific reasoning for this component — which signals support it, what the expected causal chain is, why this threshold was chosen
- **Key assumptions:** the discrete falsifiable claims this component depends on — expressed as testable statements (e.g., "AI capex will exceed consensus," "gaming revenue disappoints," "implied volatility contracts post-earnings")

### Mandatory coverage

The engine validates that every bracket leg has a corresponding thesis component. A bracket with a price-based stop, a time-based expiration, and an event-based invalidation must have three invalidation rationale components — one explaining why each threshold was chosen. A multi-leg strategy must have entry rationale components addressing each leg's contribution to the strategy. This is enforced structurally by the schema, not by hoping the thesis author wrote a thorough narrative.

Specifically, every thesis must include at minimum:

- One entry rationale component per order leg (one for a simple equity entry, one per leg for a multi-leg strategy)
- One target rationale component explaining the expected exit level and the catalyst that should drive the price there
- One invalidation rationale component per hard bracket leg (price-based or time-based) explaining why that threshold was chosen
- One invalidation rationale component per soft bracket leg (event-based), if present

---

## Thesis status classifications

Each active thesis carries a discrete status assessment, updated at each pipeline invocation by the [strategist](../04-decision-layer/strategist.md) as part of its per-position evaluation. The strategist cross-references the thesis narrative and key assumptions against the [synthesizer's](../03-analysis-layer/synthesizer.md) current market context:

- **On track:** the catalyst hasn't fired yet but conditions remain supportive, or the position is moving toward the target for the right reasons. No action required.
- **Partially realized:** the thesis catalyst has partially played out — the expected move is underway but hasn't reached the target. The PM should consider taking partial profits or tightening the invalidation level.
- **At risk:** conditions have changed in a way that weakens the thesis without outright invalidating it — a supporting signal has faded, a correlated position has moved against the expected pattern, or new information creates ambiguity. Warrants increased monitoring and potentially reduced position size.
- **Stale:** the thesis has outlived its expected time horizon without the catalyst firing. The original reasoning may still be valid, but the opportunity cost of holding a position on a thesis that isn't resolving is real. The PM should re-evaluate whether to maintain, reduce, or exit.
- **Invalidated:** the specific invalidation condition has been met — the falsifiable claim in the thesis has been proven wrong. The position should be closed. This is the highest-urgency status and should be flagged prominently to the portfolio manager.

---

## Resolution: component-level then thesis-level

At position close, each thesis component receives its own outcome assessment — **validated**, **wrong**, or **inconclusive** — before the thesis-level resolution is determined. This enables granular diagnostics: "The NVDA entry rationale was validated, the AMD short-leg rationale was wrong, and the net result was a loss" is far more useful for system tuning than "the pairs trade lost money."

Component-level resolution is performed by the analysis pipeline (LLM-evaluated for qualitative components, programmatic for falsifiable quantitative assumptions). Thesis-level resolution integrates the component assessments with P/L outcome.

**Thesis-level resolution categories:**

- **Validated — correct for the right reasons:** the thesis played out as predicted, the named catalyst fired, and the position hit the target. The component assessments should show most or all components validated. The ideal outcome — the system's reasoning was sound.
- **Profitable but wrong:** the position made money, but not because the thesis was right — the market moved in the right direction for different reasons. P/L is positive but key thesis components were wrong or inconclusive. The most important category for system tuning — a lucky outcome that shouldn't reinforce the reasoning patterns that produced it.
- **Invalidated — stopped out correctly:** the invalidation condition was met, the position was closed, and the original thesis was proven wrong. A negative P/L outcome but a positive process outcome — the system correctly identified when it was wrong.
- **Invalidated — wrong on thesis, wrong on exit:** the thesis was wrong and the system held too long, either because the invalidation condition was poorly specified or the portfolio manager was slow to act. A process failure to diagnose — the component assessments should reveal which invalidation rationale was inadequate.

---

## Three consumption modes

The structured thesis model enables three distinct consumption patterns, each with different token efficiency profiles:

**Programmatic (zero LLM tokens).** Filter components by type, instrument, or outcome. "What's our hit rate on entry rationales for short equity legs?" is a database query across component records. "Which invalidation rationale components were assessed as 'wrong' in the last 20 resolved theses?" is a filter-and-aggregate operation. No LLM involved.

**Targeted LLM evaluation (minimal tokens).** Pull a specific component plus its relevant market data into a focused context window. "Evaluate whether the AMD short-leg rationale still holds given today's gaming revenue data" sends only the AMD component narrative, its key assumptions, and the relevant market data — not the full thesis, not the NVDA leg, not the bracket parameters. Small context, better reasoning quality, lower cost.

**Full thesis evaluation (full tokens, rare).** Ingest the complete thesis with all components for holistic assessment. Only needed for thesis-level resolution, when components interact in ways that require seeing the whole picture, or for portfolio manager review of complex strategy positions. The least common mode, reserved for cases where component-level evaluation is insufficient.

The ingestion layer ([01-data-layer internal](../01-data-layer/internal/README.md)) delivers active theses at the summary level by default, with component-level detail available on request. The analysis pipeline pulls components selectively based on what it's evaluating. Historical thesis resolution queries (for system tuning and feedback loops) operate primarily in programmatic mode with targeted LLM evaluation for ambiguous cases.
