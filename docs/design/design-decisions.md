# Key design decisions

### Why fresh context windows between stages

Prevents cognitive anchoring. If the analyst agent has access to raw data, it might anchor on a specific data point rather than reasoning from the synthesized picture. Each agent should only see what it needs to do its job — this mirrors how institutional trading desks separate research from execution.

### Why quant-first sequencing

Establishes "what" before "why." The quant layer produces factual, verifiable observations. The qualitative layer then contextualizes those observations. Running them in reverse would risk the qualitative layer generating narratives that the quant data doesn't support.

### Why adaptive research over fixed research

Most trading systems ask the same questions every cycle and look for different answers. This system asks *different questions* based on what the numbers are doing. The questions themselves are dynamic and context-dependent. This is where the LLM's reasoning capability is most defensible as an edge — no traditional systematic strategy can do this.

### Why sector-scoped researcher agents over data-type-scoped agents

The earlier design had agents scoped by data type (price action agent, flow agent, portfolio agent). The revised design scopes by sector (tech/semis agent, financials agent, energy agent) plus a cross-sector portfolio agent. Sector scoping is better because: (a) each agent develops domain-specific pattern recognition — a financials researcher learns rate sensitivity, an energy researcher learns commodity linkage — which produces higher-quality briefs; (b) it mirrors how real trading desks are organized; (c) at ~65 tickers, sector scoping keeps each agent's context manageable (15–35 names) while data-type scoping would force every agent to process all 65.

### Why thesis-based position management over time-based

A rigid "close all positions by EOD" rule would force exits at suboptimal times and prevent the system from capturing multi-day moves. Thesis-based management — close when invalidated or realized — lets the system adapt to how catalysts actually play out.

### Why no compound commands (ROLL, HEDGE) in the OMS vocabulary

The OMS command vocabulary was deliberately limited to five atomic commands (OPEN, CLOSE, ADJUST, CANCEL, ADD) with no compound commands. Two compound commands were designed, evaluated, and removed: ROLL (atomic close + open for position replacement) and HEDGE (open a protective position with lifecycle binding to a parent).

**Why ROLL was removed:** During design, we discovered that ROLL creates a cognitive shortcut for avoiding honest thesis evaluation. A ROLL frames a position exit as a "continuation" rather than a close — which lets the portfolio manager agent avoid classifying the exit as a thesis invalidation. When walked through concretely: if a catalyst fires and the expected price move doesn't materialize, that's a thesis failure, not a "timing extension." But a ROLL command makes it psychologically easy to reframe the failure as "the thesis evolved." Even with structural constraints (requiring a roll justification type), the concern is that LLM agents — like human traders — will rationalize failed theses as continuations rather than confronting the invalidation.

The solution: position replacement is expressed as CLOSE (with an explicit invalidation reason that feeds the feedback loop) followed by OPEN (with a full independent thesis). Each action has full accountability. The sequential processing model within an invocation handles the capital accounting correctly.

**Why HEDGE was removed:** The same accountability concern applies. A HEDGE command with lifecycle binding creates a path for the portfolio manager to avoid closing a losing position — instead of admitting a thesis is failing and closing, the portfolio manager can "manage risk" by hedging, keeping the losing position open while appearing disciplined. The hedge command also introduces implicit dependencies (auto-close cascades, partial close scaling questions) that add structural complexity.

The solution: a hedge is just an OPEN whose thesis explains the hedging rationale. It's an independent position with its own bracket (including a time-based hard backstop to prevent it from outliving its purpose). The portfolio manager manages all positions with the same tools and the same accountability. No special treatment for protective positions.

### Why outside-in spec ordering for the decision layer

The analyst and portfolio manager agents are the last components to be fully specified, deliberately. The approach is to finish the surrounding contracts first — guardrail values, breach behavior, regime adaptation parameters, state delivery payloads, domain researcher output schemas — and then design the agents against a fully defined constraint surface.

The reasoning: LLM agents are the most malleable component in the system. A prompt can be rewritten in an afternoon; an OMS command schema or a guardrail enforcement interface is structural. The surrounding layers define the agents' input space (what they see) and action space (what they can do), and the tighter those boundaries are drawn before writing the agent specs, the less open-ended the design problem becomes. The analyst spec isn't "what should this agent do in the abstract?" — it's "given these inputs and these available actions, what's the decision procedure?" That's a much more tractable question.

This is already partially true in the current state of the docs: the OMS command set is fixed, the fill report contract is locked, the thesis lifecycle has defined state transitions, and the distillation output categories are enumerated. Finishing the remaining outside-in work (primarily risk guardrails and state delivery) completes the picture.

**The one exception:** before locking any remaining surrounding contracts, do a lightweight "tracer bullet" pass on the decision layer — draft a sample analyst output and portfolio manager evaluation for one concrete scenario, walk it through the full pipeline, and verify that the surrounding contracts are shaped correctly. This isn't a full agent spec; it's a validation exercise that catches missing fields or misaligned formats before they're finalized.

### Why the portfolio state researcher agent was eliminated

The original design included a dedicated LLM agent (the "portfolio state researcher" or PSA) in the analysis layer that consumed all 11 categories of internal portfolio state and produced two outputs: (1) a portfolio-level brief for the synthesizer and (2) per-position thesis status assessments for the strategist.

Both outputs were eliminated because they introduced an unnecessary LLM intermediary:

**Output 1 (portfolio-level brief)** had an LLM summarizing strictly quantitative data — beta-adjusted exposure, correlation profiles, P/L attribution, capital efficiency metrics — into prose for consumption by another LLM. This is a lossy round-trip. The synthesizer is better served by querying structured data directly when it needs portfolio context.

**Output 2 (per-position thesis status assessments)** duplicated the strategist's core job. The strategist already receives full thesis records, position details, and the synthesizer's market context. Having a separate agent pre-classify thesis health meant the strategist was layering its judgment on top of another agent's opinion rather than reasoning from primary data. This also created an artificial "agree/disagree" interaction pattern that added complexity without improving decision quality.

**What replaced it:**
- The synthesizer gained three lightweight read-only tools (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) for on-demand portfolio context. It calls them when market signals are relevant to existing positions — naturally scaling token spend with portfolio relevance.
- The strategist assumed full responsibility for thesis status classification (on-track, partially-realized, at-risk, stale, invalidated) as part of its per-position evaluation pass.
- Portfolio-derived metrics (categories 7–11) are exposed as structured data for decision-layer agents rather than pre-summarized by an LLM.

The principle: don't route quantitative data through an LLM summarization step when the consumer is another LLM that can read the structured data directly. LLM agents add value through judgment and reasoning, not through summarizing numbers.

### Why the execution engine is mode-agnostic (paper vs. live)

The aspiration is to move beyond paper trading when the system demonstrates profitability. The engine architecture separates the pluggable part (how an order becomes a fill — the execution gateway) from the permanent part (everything else — order management, position tracking, thesis lifecycle, guardrail enforcement). This means the transition to live trading is a configuration change (swap the gateway implementation), not an architectural change. The OMS, guardrail layer, portfolio state database, and thesis feedback loop are identical in both modes.
