# Decision layer

Three LLM agents operate in fresh context windows, connected by a deterministic pre-processing step: the **analyst** proposes new trades, the **strategist** evaluates existing positions, the **proposal pre-processor** merges and annotates their structured outputs, and the **portfolio manager** critically evaluates the annotated proposals and either rejects or executes them.

The analyst and strategist run in parallel — both receive the synthesizer output independently and produce recommendations concurrently. Their outputs pass through the proposal pre-processor before reaching the portfolio manager.

| Component | Document | Type | Status |
|-----------|----------|------|--------|
| [Analyst](analyst.md) | Identifies highest-conviction asymmetric setups, constructs trade theses for new entries | LLM agent | Designed |
| [Strategist](strategist.md) | Evaluates every open position against current conditions, recommends portfolio actions | LLM agent | Designed |
| [Proposal pre-processor](proposal-pre-processor.md) | Merges analyst and strategist outputs, computes cross-proposal annotations | Deterministic | Designed |
| [Portfolio manager](portfolio-manager.md) | Critically evaluates annotated proposals, manages portfolio risk, approves/rejects, executes | LLM agent | Designed |

---

## Information flow

The decision layer consumes the [analysis layer](../03-analysis-layer/README.md) output through a hybrid model balancing token efficiency with access to detail:

**Primary input: Synthesizer brief.** The [synthesizer](../03-analysis-layer/synthesizer.md) produces a unified market snapshot loaded into context for all three agents. The brief carries cross-domain findings with typed source references (e.g., `[SA-TECH-3]`, `[QR-4]`, `[AR-2]`) and flags contradictions and uncertainty rather than resolving them.

**On-demand retrieval: Source briefs.** All three agents have a retrieval tool that accepts reference IDs and returns the corresponding section from the original analysis brief — drill-down without loading all source material into context.

**Reference ID format:**

| Prefix | Source |
|--------|--------|
| `SA-TECH` | Tech & semiconductors domain researcher |
| `SA-FIN` | Financials domain researcher |
| `SA-ENERGY` | Energy domain researcher |
| `QR` | Baseline qualitative research |
| `AR` | Adaptive research threads |
| `CR` | Correlation and regime brief |

**Expected workflow (all agents):**
1. Read the synthesizer brief in full
2. Form initial views from the unified snapshot
3. Retrieve underlying source brief sections by reference ID for findings needing deeper investigation — especially flagged contradictions or uncertainty
4. On quiet days retrieval may not be needed; on volatile days the agent pulls more source material, scaling token spend with market complexity

---

## Parallel execution: analyst and strategist

The analyst and strategist run concurrently in separate context windows. Same synthesizer output, different mandates and input bundles:

**Analyst context** is opportunity-focused: synthesizer brief and the [analyst guardrail state header](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) (available capital, per-sector delta-adjusted exposure room, directional/gross exposure room, per-position size limits under the active regime, slim held-positions block for dedup). The analyst finds new setups and does not receive full thesis records. A guardrail validation tool deterministically checks each proposal before finalization — see [analyst.md](analyst.md).

**Strategist context** is portfolio-focused: synthesizer brief, full thesis records at component level including prior status (strategist is the sole consumer that receives components inline; see [portfolio-state.md §3a](../01-data-layer/internal/portfolio-state.md)), position details, activity log, pending orders, and the [strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) (per-sector delta-adjusted headroom, directional/gross exposure room, position-level constraint proximity, drawdown state, regime-transition breaches). The strategist classifies thesis health, connects new information to existing thesis narratives, and recommends specific actions. The same guardrail validation tool is available for pre-submission checking of exposure-changing proposals — see [strategist.md](strategist.md).

Each agent's context is optimized for its task rather than splitting attention between opportunity scanning and position management.

---

## Proposal pre-processor

A deterministic step between the analyst/strategist outputs and the portfolio manager. Reads the **structured fields** from both outputs, computes cross-proposal annotations, and delivers the combined package to the PM. Pure computation on typed data — the pre-processor only adds annotations and preserves underlying proposals verbatim.

**Computed annotations:** same-underlying conflict detection, cumulative capital requirement, cumulative exposure impact, entry window priority ordering, conviction distribution, book health summary, sector exposure shift preview.

See [proposal-pre-processor.md](proposal-pre-processor.md) for the full specification.

---

## Convergence at the portfolio manager

The PM receives the pre-processor's annotated output alongside the synthesizer brief and portfolio state. Annotations reduce mechanical reasoning load:

- **New entries** (from analyst): thesis quality, sizing, portfolio fit, risk/reward
- **Position actions** (from strategist): whether holds, reduces, closes, adjustments, and adds are well-justified
- **Cross-proposal interactions** (flagged by pre-processor): same-name conflicts, cumulative exposure breaches, sector shift implications — pre-computed so the PM makes judgment calls rather than detecting them
- **Capital sequencing**: when multiple trades are approved, the PM determines execution order and ensures cumulative capital allocation stays within constraints. The pre-processor's capital and entry-window-priority annotations inform the starting point

The PM issues [OMS commands](../05-execution-layer/oms-commands.md) (OPEN, CLOSE, ADJUST, CANCEL, ADD) for approved actions.

---

## Relationship to risk guardrails

The [risk guardrails](../06-risk-guardrails/README.md) operate at multiple levels, pushing compliance upstream so the PM can focus on thesis quality and portfolio coherence:

1. **Analyst and strategist pre-submission validation.** Both agents receive headroom in their [guardrail state headers](../06-risk-guardrails/state-delivery.md) and self-constrain during proposal generation. Before finalizing, each calls a deterministic guardrail validation tool that checks every proposal against all applicable rules (including delta-adjusted exposure for options) and revises failures. Proposals reaching the PM are guardrail-compliant at check time. See [analyst.md](analyst.md) and [strategist.md](strategist.md).
2. **PM modification validation.** When the PM adjusts parameters (e.g., sizing), modifications are validated against guardrails using the same tool before becoming commands.
3. **Engine authoritative (synchronous).** The [execution layer](../05-execution-layer/README.md) performs a final guardrail check on every OMS command at execution time — the authoritative backstop, since portfolio state can shift between upstream validation and execution (market movement, fill resolution, regime changes). Rejections return synchronously to the PM within the same invocation; see [portfolio-manager.md](portfolio-manager.md).
