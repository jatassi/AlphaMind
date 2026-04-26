# Decision layer

The layer where trading decisions are made. Three LLM agents operate with fresh context windows, connected by a deterministic pre-processing step: the **analyst** proposes new trades, the **strategist** evaluates existing positions, the **proposal pre-processor** merges and annotates their structured outputs, and the **portfolio manager** critically evaluates the annotated proposals and either rejects or executes them.

The analyst and strategist run in parallel — both receive the synthesizer output independently and produce structured recommendations concurrently. Their outputs pass through the proposal pre-processor before reaching the portfolio manager.

| Component | Document | Type | Status |
|-----------|----------|------|--------|
| [Analyst](analyst.md) | Identifies highest-conviction asymmetric setups, constructs trade theses for new entries | LLM agent | Stub |
| [Strategist](strategist.md) | Evaluates every open position against current conditions, recommends portfolio actions | LLM agent | Stub |
| [Proposal pre-processor](proposal-pre-processor.md) | Merges analyst and strategist outputs, computes cross-proposal annotations | Deterministic | Stub |
| [Portfolio manager](portfolio-manager.md) | Critically evaluates annotated proposals, manages portfolio risk, approves/rejects, executes | LLM agent | Stub |

---

## Information flow

The decision layer consumes the output of the [analysis layer](../03-analysis-layer/README.md) through a hybrid model designed to balance token efficiency with access to detail:

**Primary input: Synthesizer brief.** The [synthesizer](../03-analysis-layer/synthesizer.md) produces a unified market snapshot that is loaded into context by default for all three agents. This brief contains cross-domain findings with typed source references (e.g., `[SA-TECH-3]`, `[QR-4]`, `[AR-2]`) and explicitly flags contradictions and areas of uncertainty rather than resolving them.

**On-demand retrieval: Source briefs.** All three agents have access to a retrieval tool that accepts source reference IDs and returns the corresponding section from the original analysis brief. This allows drill-down into underlying detail without loading all source material into context by default.

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
1. Agent reads the synthesizer brief in full
2. Forms initial views based on the unified snapshot
3. For findings needing deeper investigation — especially flagged contradictions or uncertainty — retrieves the underlying source brief sections by reference ID
4. On quiet days with broad consensus, retrieval may not be needed at all; on volatile days with many contradictions, the agent pulls more source material, naturally scaling token spend with market complexity

---

## Parallel execution: analyst and strategist

The analyst and strategist run concurrently in separate context windows. They receive the same synthesizer output but have different mandates and input bundles:

**Analyst context** is opportunity-focused: synthesizer brief, the [analyst guardrail state header](../06-risk-guardrails/state-delivery.md#analyst-guardrail-state-header) (available capital, per-sector delta-adjusted exposure room, directional/gross exposure room, per-position size limits under the active regime, and a slim held-positions block for dedup). The analyst does not receive full thesis records — its job is to find new setups, not evaluate existing ones. The analyst also has access to a guardrail validation tool that deterministically checks each proposal before finalization — see [analyst.md](analyst.md) for the full workflow.

**Strategist context** is portfolio-focused: synthesizer brief, full thesis records at component level (including prior thesis status — strategist is the only consumer that receives components inline; see [portfolio-state.md §3a](../01-data-layer/internal/portfolio-state.md)), position details, activity log, pending orders, and the [strategist guardrail state header](../06-risk-guardrails/state-delivery.md#strategist-guardrail-state-header) (per-sector delta-adjusted headroom, directional/gross exposure room, position-level constraint proximity, drawdown state, regime-transition breaches). The strategist's job is to classify thesis health, connect new information from the synthesizer to existing thesis narratives, and recommend specific position actions. Like the analyst, the strategist has access to a guardrail validation tool for pre-submission checking of exposure-changing proposals — see [strategist.md](strategist.md).

This separation ensures each agent operates with a context optimized for its task, rather than splitting attention between opportunity scanning and position management.

---

## Proposal pre-processor

A deterministic processing step between the analyst/strategist outputs and the portfolio manager. Reads the **structured fields** from both agents' outputs, computes cross-proposal annotations, and delivers the combined annotated package to the PM. No LLM reasoning — pure computation on typed data. The pre-processor never modifies, filters, or reorders the underlying proposals — it only adds annotations.

**Computed annotations:** same-underlying conflict detection, cumulative capital requirement, cumulative exposure impact, entry window priority ordering, conviction distribution, book health summary, and sector exposure shift preview.

See [proposal-pre-processor.md](proposal-pre-processor.md) for the full specification.

---

## Convergence at the portfolio manager

The portfolio manager receives the pre-processor's annotated output alongside the synthesizer brief and portfolio state. It evaluates proposals holistically, with the pre-processor's annotations reducing the mechanical reasoning burden:

- **New entries** (from analyst): thesis quality, sizing, portfolio fit, risk/reward
- **Position actions** (from strategist): whether the recommended holds, reduces, closes, adjustments, and adds are well-justified
- **Cross-proposal interactions** (flagged by pre-processor): same-name conflicts, cumulative exposure breaches, and sector shift implications are pre-computed — the PM makes judgment calls on how to resolve them rather than detecting them
- **Capital sequencing**: if multiple trades are approved, the portfolio manager determines execution order and ensures cumulative capital allocation stays within constraints. The pre-processor's capital requirement and entry window priority annotations inform the starting point

The portfolio manager issues [OMS commands](../05-execution-layer/oms-commands.md) (OPEN, CLOSE, ADJUST, CANCEL, ADD) for approved actions.

---

## Relationship to risk guardrails

The [risk guardrails](../06-risk-guardrails/README.md) operate at multiple levels in the decision layer, with the goal of pushing guardrail compliance as far upstream as possible so the portfolio manager can focus on thesis quality and portfolio coherence:

1. **Analyst and strategist pre-submission validation:** Both agents receive guardrail headroom in their [guardrail state headers](../06-risk-guardrails/state-delivery.md) (available capital, per-sector delta-adjusted exposure room, directional/gross exposure room, per-position limits under the active regime) and use it to self-constrain during proposal generation. Before finalizing, each agent calls a deterministic guardrail validation tool that checks every proposal against all applicable rules — including delta-adjusted exposure for options — and revises proposals that fail. Proposals that reach the portfolio manager are guardrail-compliant at the time they were checked. See [analyst.md](analyst.md) and [strategist.md](strategist.md) for the full workflow.
2. **Portfolio manager modification validation:** When the PM adjusts parameters (e.g., sizing changes), those modifications are validated against guardrails before becoming commands. The PM has access to the same validation tool.
3. **Engine authoritative (synchronous):** The [execution layer](../05-execution-layer/README.md) performs a final guardrail check on every OMS command at execution time. Because portfolio state can shift between upstream validation and command execution (market movement, fill resolution, regime changes), this check is the authoritative backstop. Rejections are returned synchronously to the PM within the same invocation, allowing the PM to adjust and retry. See [portfolio-manager.md](portfolio-manager.md) for the synchronous feedback model.
