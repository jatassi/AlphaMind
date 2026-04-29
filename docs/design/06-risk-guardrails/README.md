# Risk guardrails

Hard-coded deterministic constraints enforced by code, regardless of LLM agent confidence or thesis quality.

*Design principle — progressive validation, not single checkpoint:* A trade that violates a position size limit is caught *before* the PM spends tokens evaluating it. The guardrail system operates at multiple pipeline layers, each applying the rules relevant to its context.

---

## Multi-layer enforcement model

Guardrails are checked at four points in the pipeline, progressively narrowing valid actions. The first three correspond to the [decision layer](../04-decision-layer/README.md); the fourth is the engine backstop. See [rules-and-limits.md](rules-and-limits.md) for the per-rule mapping to enforcement tiers.

| Tier | Layer | Agent(s) | Guardrail role | Enforcement style |
|------|-------|----------|----------------|-------------------|
| T1 | Recommendation | [Analyst](../04-decision-layer/analyst.md) + [Strategist](../04-decision-layer/strategist.md) | Both agents receive guardrail headroom in their state headers and self-constrain during proposal generation. Before finalizing, each calls the shared **guardrail validation tool**, which deterministically checks every proposal against all applicable rules — including delta-adjusted exposure for options — and tracks cumulative impact across proposals within an invocation. | **Advisory.** Agents produce compliant proposals; violations that slip through are caught downstream. |
| — | Cross-agent annotation | [Proposal pre-processor](../04-decision-layer/proposal-pre-processor.md) | Deterministic computation between analyst/strategist outputs and the PM. Detects cumulative breaches arising from the combined proposal set (individually-compliant proposals that together breach a sector limit). Also computes capital requirements, same-underlying conflicts, and sector exposure shifts. | **Informational.** Pre-processor annotates; the PM uses annotations to resolve cross-proposal interactions. |
| T2 | Approval | [Portfolio manager](../04-decision-layer/portfolio-manager.md) | PM receives the annotated package alongside guardrail headroom. PM modifications (sizing changes, instrument substitutions) are validated via the same tool. The PM makes sizing tradeoffs informed by headroom but cannot override engine-authoritative limits. | **Judgment-informed.** PM sees guardrail state alongside thesis quality and portfolio coherence. |
| T3 | Execution | Engine guardrail layer | Final check before orders reach the gateway. Authoritative because portfolio state can shift between T1/T2 and execution (market movement, fill resolution, regime changes). Rejections return **synchronously** to the PM within the same invocation. | **Authoritative.** Deterministic, no override. Catches anything the PM approved against stale state, LLM error, or race condition. |

The execution layer is the **single source of truth** for constraint enforcement. Upstream layers reduce wasted work by catching violations early and providing pre-computed context; the system remains safe even if they all fail.

Between pipeline invocations, the **[continuous monitor](../05-execution-layer/architecture.md#4-continuous-monitor)** — running persistently during market hours — performs breach detection against live market data for rules breachable by market movement. On breach detection it may issue protective CLOSE commands via engine-originated [command envelopes](../04-decision-layer/portfolio-manager.md), per the per-rule classification in [breach-behavior.md](breach-behavior.md), and may trigger **emergency pipeline invocations** ([breach-behavior.md](breach-behavior.md#emergency-invocation-trigger)). Monitor authority is bounded to closing positions and triggering invocations; constructive actions require PM judgment.

---

## Sub-documents

| Document | Status | Description |
|----------|--------|-------------|
| [Rules & limits](rules-and-limits.md) | Complete | Concrete default values and rationale for all 17 constraint rules, three-tier enforcement mapping per rule, per-rule enforcement summary with breach detection classification, and dual portfolio profiles ($1,500 primary + $100K full-system) with feature flags. [Implementation plan](../../implementation/06-risk-guardrails/rules-and-limits/). |
| [Regime adaptation](regime-adaptation.md) | Complete | Parameter multiplier table for all rules across four regimes (low-vol, normal, elevated, crisis), asymmetric transition mechanics (immediate tightening, gradual 3-invocation loosening), position handling on tightening, pre-event tightening overlay, and stress overlay. [Implementation plan](../../implementation/06-risk-guardrails/regime-adaptation/). |
| [Breach behavior](breach-behavior.md) | Complete | Four-zone escalation model (70/85/95 with per-rule overrides), per-rule forced reduction classification (immediate vs. deferred), position selection logic, secondary breach checking, daily halt mode with agent behavior modifications (watchlist/defensive/risk-reduction modes), progressive cumulative drawdown response, margin cascade handling, emergency invocation trigger conditions, hard rejection semantics, and engine-originated traceability. [Implementation plan](../../implementation/06-risk-guardrails/breach-behavior/). |
| [Guardrail state delivery](state-delivery.md) | Complete | Concrete guardrail state headers for analyst (200–400 tokens), strategist (300–600 tokens), and PM (400–800 tokens) with structured field layouts, halt-mode and emergency invocation header modifications, guardrail validation tool I/O contract with cumulative tracking, portfolio state ingestion integration, and cross-constraint impact visibility. [Implementation plan](../../implementation/06-risk-guardrails/state-delivery/). |
| [Guardrail-evaluation library](guardrail-evaluation.md) | Complete | Deterministic math primitives shared by all three guardrail-check call sites (validation tool, proposal pre-processor combined-set check, engine T3 check): Black-Scholes delta computation with conservative buffer, per-rule projection against `(current_state, proposed_deltas)`, active regime parameter resolution, feature-flag early-exit, and the canonical per-rule output object reused by every caller. [Implementation plan](../../implementation/06-risk-guardrails/guardrail-evaluation/). |

---

## Relationship to other specs

| Spec | Relationship |
|------|-------------|
| [Execution layer](../05-execution-layer/architecture.md) | The engine's guardrail enforcement layer (component 3) implements the T3 authoritative check. The [continuous monitor](../05-execution-layer/architecture.md#4-continuous-monitor) (component 4) implements between-invocation breach detection, protective CLOSE authority, and emergency invocation triggering. The enforcement *interface* is defined in the engine spec; the *rules* are defined here |
| [Analyst](../04-decision-layer/analyst.md) | T1 consumer. Receives guardrail headroom as context, self-constrains during proposal generation, validates each proposal via the shared guardrail validation tool before finalization |
| [Strategist](../04-decision-layer/strategist.md) | T1 consumer. Same guardrail validation workflow as the analyst, applied to position action recommendations (ADD, CLOSE, REDUCE). Receives position-level constraint proximity in its guardrail state header |
| [Proposal pre-processor](../04-decision-layer/proposal-pre-processor.md) | Cross-agent annotation layer between T1 and T2. Computes cumulative exposure impact across the combined analyst + strategist proposal set, detecting breaches that individual agent validation can't catch |
| [Portfolio manager](../04-decision-layer/portfolio-manager.md) | T2 consumer. Receives pre-processor annotations alongside guardrail headroom. Validates its own sizing modifications via the guardrail validation tool. Receives synchronous T3 rejection feedback from the engine and can adjust within the same invocation |
| [Portfolio state / raw state](../01-data-layer/internal/portfolio-state.md) | Category 4c (risk budget consumption) and 4d (active risk parameter set) are the read interface for guardrail state. Defined in portfolio state, sourced from this system |
| [Programmatic distillation](../02-distillation-layer/external.md) | Volatility regime classification (section 4) is the input to regime-dependent guardrail parameters. Funding stress and liquidity scores drive the stress overlay |
