# Key design decisions

### Why fresh context windows between stages

Prevents cognitive anchoring. An analyst agent with raw data access might anchor on a specific data point rather than reasoning from the synthesized picture. Each agent sees only what it needs — mirroring how institutional desks separate research from execution.

### Why quant-first sequencing

Establishes "what" before "why." Quant produces factual, verifiable observations; qualitative contextualizes them. The reverse risks the qualitative layer generating narratives the quant data doesn't support.

### Why adaptive research over fixed research

Most trading systems ask the same questions every cycle and look for different answers. This system asks *different questions* based on what the numbers are doing — dynamic, context-dependent. The LLM's reasoning capability is most defensible as an edge here; no traditional systematic strategy can do this.

### Why sector-scoped researcher agents over data-type-scoped agents

Sector scoping (tech/semis, financials, energy) plus a cross-sector portfolio agent beats data-type scoping (price action, flow, portfolio) because: (a) each agent develops domain-specific pattern recognition — financials learns rate sensitivity, energy learns commodity linkage; (b) it mirrors real trading desk organization; (c) at ~65 tickers, sector scoping keeps each context manageable (15–35 names), while data-type scoping forces every agent to process all 65.

### Why thesis-based position management over time-based

A rigid "close by EOD" rule forces exits at suboptimal times and prevents capture of multi-day moves. Thesis-based — close when invalidated or realized — lets the system adapt to how catalysts actually play out.

### Why no compound commands (ROLL, HEDGE) in the OMS vocabulary

The OMS vocabulary is five atomic commands (OPEN, CLOSE, ADJUST, CANCEL, ADD). Compound commands create cognitive shortcuts that let the PM avoid honest thesis evaluation.

**ROLL** would frame a position exit as "continuation," letting the PM dodge classifying it as thesis invalidation. If a catalyst fires and the expected move doesn't materialize, that's a thesis failure, not a "timing extension." Position replacement is therefore CLOSE (with explicit invalidation reason feeding the feedback loop) followed by OPEN (full independent thesis). Sequential processing within an invocation handles capital accounting correctly.

**HEDGE** with lifecycle binding would let the PM "manage risk" on a losing position rather than admit thesis failure and close. Hedging also introduces implicit dependencies (auto-close cascades, partial-close scaling). A hedge is therefore just an OPEN whose thesis explains the hedging rationale, with its own bracket (including a time-based hard backstop). The PM manages all positions with the same tools and the same accountability.

### Why outside-in spec ordering for the decision layer

The analyst and PM agents are specified last. Surrounding contracts (guardrail values, breach behavior, regime adaptation, state delivery, domain researcher schemas) are finished first; the agents are then designed against a fully defined constraint surface.

LLM agents are the most malleable component — a prompt rewrite is an afternoon; an OMS schema or guardrail interface is structural. Drawing input and action boundaries tightly first reduces the agent design from "what should this agent do?" to "given these inputs and actions, what's the decision procedure?" — a much more tractable question.

**The one exception:** before locking remaining contracts, do a lightweight "tracer bullet" pass — draft a sample analyst output and PM evaluation for one concrete scenario, walk it through, verify the surrounding contracts are shaped correctly. Catches missing fields or misaligned formats before finalization.

### Why the portfolio state researcher agent was eliminated

A portfolio LLM agent in the analysis layer would summarize quantitative data (beta-adjusted exposure, correlation profiles, P/L attribution, capital efficiency) into prose for another LLM, and would pre-classify thesis health for the strategist. Both routes are unnecessary LLM intermediaries.

Quantitative data summarization is a lossy round-trip — the synthesizer reads structured data directly. Per-position thesis pre-classification duplicates the strategist's core job and creates an artificial agree/disagree interaction with no decision-quality improvement.

In the active design:
- The synthesizer has three read-only tools (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) for on-demand portfolio context — naturally scaling token spend with relevance.
- The strategist owns thesis status classification (on-track, partially-realized, at-risk, stale, invalidated) in its per-position pass.
- Portfolio-derived metrics (categories 7–11) are structured data for decision-layer agents.

The principle: don't route quantitative data through an LLM summarization step when the consumer is another LLM. LLMs add value through judgment, not number-summarizing.

### Why the execution engine is mode-agnostic (paper vs. live)

The aspiration is live trading when profitability is demonstrated. The engine separates the pluggable part (the execution gateway — how an order becomes a fill) from the permanent part (order management, position tracking, thesis lifecycle, guardrail enforcement). The paper-to-live transition is a configuration change (swap the gateway), not an architectural one. OMS, guardrails, portfolio state, and the thesis feedback loop are identical in both modes.
