# AlphaMind design gap analysis

**Date:** 2026-03-15
**Scope:** All documents in `architecture/` and `design/`

---

## Summary

The AlphaMind design documentation is extensive and well-structured across the architecture layer (7 complete docs) and the 6-layer design spec. The data layer, distillation layer, analysis layer, and execution layer are thoroughly specified. However, there are two major areas that remain stubs, several cross-cutting concerns with no documentation at all, and a handful of open questions that the existing `open-questions.md` already flags but that deserve elaboration.

This document organizes gaps into three tiers: **stubs** (docs that exist but lack substance), **missing docs** (topics with no coverage anywhere), and **underspecified areas** (topics partially covered but needing more detail within existing docs).

---

## Tier 1 — Stubs requiring full design

These documents exist in the repo, are referenced by other docs, but contain only scope outlines with no actual design content. They block implementation.

### 1.1 Decision layer: Analyst agent (`04-decision-layer/analyst.md`)

The analyst agent is one of the two most important LLM agents in the system and currently has only a high-level mandate and a bullet list of required output fields. Missing:

- **System prompt design.** The analyst's reasoning quality is the primary determinant of P/L. The prompt needs to encode: how to weigh conflicting signals, how to calibrate conviction levels, when to prefer inaction over a marginal trade, how to reason about time horizons vs. catalyst timing, and how to avoid common LLM failure modes (over-anchoring on the last data point, generating plausible-sounding but vacuous theses).
- **Output schema.** ~~The bullet list of required fields (thesis, direction, entry, target, invalidation, sizing, time) needs to be a concrete structured format — JSON schema, typed Python dataclass, or at minimum a worked example. The portfolio manager, execution layer, and thesis tracker all consume this output; the contract must be precise.~~ **Partially addressed (phase 0 tracer bullet).** The analyst output is now split into structured fields (machine-parseable: recommendation ID, instrument, underlying, sector, conviction, entry order, position size with delta-adjusted exposure, target, invalidation legs, entry window, time expectation, guardrail validation result) and narrative fields (thesis narrative, target rationale, invalidation rationale, size rationale, counterarguments). The strategist output follows the same structured/narrative split. Structured fields are consumed by a new deterministic proposal pre-processor; narrative fields are consumed by the PM. Precise JSON schema or dataclass definitions still pending Phase 2 agent spec work.
- **Opportunity ranking methodology.** When multiple setups are identified, how does the analyst prioritize? Conviction-weighted expected value? Maximum asymmetry? Sector balance? The current doc says "highest-conviction asymmetric setups" but doesn't define how conviction and asymmetry are measured or compared.
- **Position management recommendations.** The doc mentions the analyst "may also recommend maintaining current positions or closing existing positions," but doesn't specify the format for these recommendations or how they integrate with the new-trade output. The portfolio manager needs a unified proposal format covering new entries, thesis updates, and close recommendations.
- **Retrieval tool usage guidance.** When should the analyst drill into source briefs vs. trusting the synthesis? The doc describes a workflow but doesn't define decision criteria — e.g., always retrieve when the synthesizer flags a contradiction, or when conviction is above a threshold.
- **Guardrail awareness.** ~~The state-delivery stub describes an "analyst context package" with sector headroom and position limits. The analyst spec needs to define how this constrains recommendation generation — does the analyst self-filter based on guardrail state, or does it propose freely and let the portfolio manager handle constraints?~~ **Resolved (phase 0 tracer bullet).** The analyst both self-constrains using headroom loaded into context AND deterministically validates each proposal against guardrails via a validation tool before finalization. The strategist follows the same pattern. The PM validates its own modifications and receives synchronous rejection feedback from the execution layer. See the updated [analyst.md](04-decision-layer/analyst.md), [strategist.md](04-decision-layer/strategist.md), [portfolio-manager.md](04-decision-layer/portfolio-manager.md), and [state-delivery.md](06-risk-guardrails/state-delivery.md).
- **Maximum recommendations per invocation.** Is there a cap? An unconstrained analyst could propose 10 trades per cycle, overwhelming the portfolio manager and the capital budget.

### 1.2 Decision layer: Portfolio manager agent (`04-decision-layer/portfolio-manager.md`)

Same severity as the analyst — this is the final judgment gate before capital deployment.

- **Evaluation framework.** "Critically evaluate" is the mandate, but there's no rubric. What specific questions does the portfolio manager ask? Suggested areas: thesis falsifiability (is the invalidation condition actually testable?), sizing proportionality (does the size match stated conviction?), portfolio coherence (does this trade conflict with existing thesis narratives?), timing plausibility (can the catalyst really fire in the stated window?).
- **Rejection criteria and output format.** When the portfolio manager rejects a trade, what's the output? A reason code? Free-text rationale? This feeds the activity log and potentially the feedback loop.
- **Sizing adjustment logic.** The doc says the portfolio manager can "scale up high-conviction ideas, scale down marginal ones" — but within what range? Can the portfolio manager halve an analyst's proposed size? Double it? The adjustment authority and bounds need definition.
- **Execution command generation.** The portfolio manager issues OMS commands (OPEN, CLOSE, ADJUST, CANCEL, ADD) — but the doc doesn't specify how the portfolio manager translates an approved trade recommendation into the concrete command format defined in `05-execution-layer/oms-commands.md`. This is the critical handoff between LLM output and programmatic execution.
- **Multi-trade sequencing.** If the portfolio manager approves three trades, in what order are they executed? Does capital allocation for trade 2 account for capital committed to trade 1? The execution architecture doc mentions "cumulative impact of prior approvals" but the portfolio manager doc doesn't address sequencing strategy.
- **Existing position management.** The portfolio manager's role in managing positions whose theses are aging, approaching time limits, or showing partial invalidation is not defined. The thesis model in the execution layer has rich lifecycle states — the portfolio manager needs guidance on how to use them.
- **System prompt design.** Same concern as the analyst — the portfolio manager prompt needs to encode skepticism, position sizing discipline, and resistance to sunk-cost reasoning.

### 1.3 Risk guardrails: Rules & limits (`06-risk-guardrails/rules-and-limits.md`)

The scope section lists all the right constraint categories but provides zero actual values. Without concrete numbers, the guardrail enforcement layer and the state delivery package can't be implemented.

- **Default values for every rule.** Position size (% and $), sector concentration, directional exposure, gross exposure, daily drawdown, cumulative drawdown, correlation, options Greeks, short exposure.
- **Rationale for each value.** Why 5% max position size instead of 3% or 8%? The rationale should reference the portfolio size assumption, the asset universe characteristics, and the time horizon.
- **Enforcement layer mapping.** Which rules are trader-advisory (soft), PM-judgment (medium), and engine-authoritative (hard)? The README outlines this three-tier model, but the specific mapping per rule is missing.

### 1.4 Risk guardrails: Breach behavior (`06-risk-guardrails/breach-behavior.md`)

The scope is well-defined (warning thresholds, hard rejection, forced reduction, margin cascades, drawdown halt, escalation model) but none of it is designed. Key decisions:

- **Forced reduction: automatic vs. flagged.** ~~Does the engine mechanically reduce positions that breach limits due to market movement, or does it flag the breach for the PM to handle at the next invocation? Each approach has trade-offs (latency vs. judgment).~~ **Partially resolved (phase 0 tracer bullet).** The architectural contract now supports both: the command envelope model has been generalized to support `engine_guardrail` provenance, and the continuous monitor has authority to issue protective CLOSE commands between invocations. The remaining decision is the **per-rule classification** — which specific breaches trigger immediate engine action vs. deferral to the PM. This is a configuration decision in the breach behavior spec, not an architectural question. See updated [breach-behavior.md](06-risk-guardrails/breach-behavior.md) and [architecture.md](05-execution-layer/architecture.md).
- **Drawdown halt mode.** Full halt vs. reduced mode vs. PM discretion — this is a fundamental system personality decision that affects everything from thesis management to capital recovery.
- **Escalation threshold percentages.** The doc mentions 70%/85%/95%/100% but these are examples, not decisions.

### 1.5 Risk guardrails: Regime adaptation (`06-risk-guardrails/regime-adaptation.md`)

- **Parameter sets per regime.** The distillation layer classifies four regimes (low-vol compression, normal, elevated, crisis) — each needs a complete parameter set derived from the base rules.
- **Transition mechanics.** Immediate tightening vs. gradual loosening is mentioned as a principle but not specified. What's "gradual"? Linear over N invocations? Step-down over time?
- **Position handling on tightening.** If a position was opened at 5% size limit (normal regime) and the regime shifts to elevated (3% limit), is that position immediately in breach? If so, what happens? This interacts directly with breach behavior.

### 1.6 Risk guardrails: State delivery (`06-risk-guardrails/state-delivery.md`)

- **Concrete payload formats.** The trader context package, PM context package, and portfolio state ingestion are described conceptually but need actual schemas — what fields, what units, what format (text block? structured data? both?). **Partially addressed (phase 0 tracer bullet):** headroom field categories are now defined (available capital, per-position size limit, per-sector delta-adjusted headroom, directional/gross exposure headroom, options-specific headroom, active regime). The guardrail validation tool contract is defined conceptually. Detailed schemas (exact field names, units, format) still pending rules-and-limits finalization.
- **Token budget.** The doc mentions "compact (minimize tokens)" for the trader package. What's the target? 500 tokens? 2,000?

---

## Tier 2 — Missing documentation (no coverage anywhere)

### 2.1 Error handling and recovery

No document addresses what happens when things go wrong during pipeline execution. Key scenarios:

- **API failures during data collection.** The data layer collects from potentially dozens of APIs. What happens when one fails? Skip the data category? Use stale data? Abort the invocation? The `api-keys.md` doc mentions free-tier rate limits but doesn't define degradation behavior.
- **LLM agent failures.** What if a sector researcher times out, returns malformed output, or hits a context length limit? Does the pipeline continue without that sector's brief? Does the synthesizer handle missing inputs?
- **Broker outages.** What happens when Alpaca's REST or `trade_updates` websocket is unreachable? The [broker adapter](05-execution-layer/broker-adapter.md) specifies disconnect recovery and submission retry/abandon; the data layer's `api-failure-handling.md` covers upstream data outages.
- **Mid-pipeline failures.** If the pipeline fails after the analysis layer but before execution, are partial results persisted? Can the pipeline resume from a checkpoint?
- **Idempotency guarantees.** If a pipeline invocation runs twice (scheduler glitch, manual re-run), what happens? Are OMS commands idempotent?

### 2.2 Monitoring, alerting, and observability

The architecture docs mention structured logging (structlog + JSON) and an SQLite audit trail, but there's no design for operational monitoring:

- **Health metrics.** Pipeline execution time per layer, LLM token usage per agent, API success/failure rates, order fill rates, data freshness indicators.
- **Alerting.** What conditions trigger human notification? Repeated API failures, guardrail breaches, unusual drawdown, agent output quality degradation, system errors.
- **Dashboard / reporting.** How does a human operator understand what the system is doing? Daily P/L summary, thesis performance, agent quality metrics, risk utilization.

### 2.3 Testing strategy

No document covers how the system will be tested:

- **Unit testing.** Distillation layer computations (indicators, anomaly detection, regime classification), OMS command processing, guardrail enforcement logic, fill modeling.
- **Integration testing.** End-to-end pipeline with mocked data and mocked LLM responses, broker adapter contract tests against an Alpaca mock, portfolio state consistency checks.
- **LLM output validation.** How do you verify that agent outputs are structurally valid? Schema validation? Output parsers? What happens when an agent produces an output that doesn't conform to the expected format?
- **Backtesting.** The design references a 4–72 hour time horizon and thesis-based position management. Is there a plan to replay historical data through the pipeline to validate the system's behavior before paper trading begins?

### 2.4 Feedback loops and learning

`open-questions.md` mentions "How do thesis tracking results feed back into prompt tuning?" but there's no document exploring this. The execution layer has a rich thesis lifecycle model (pending → active → resolved, with outcome classification), which is a natural input to a learning system:

- **Thesis outcome analysis.** Aggregate statistics on thesis success/failure by sector, by signal type, by time horizon, by conviction level.
- **Prompt iteration methodology.** How are prompts versioned and tested? A/B testing across invocations? Offline evaluation against historical data?
- **Agent quality metrics.** Analyst hit rate, portfolio manager rejection rate, thesis invalidation frequency, average holding period vs. predicted time horizon.

### 2.5 Cost modeling

Flagged in open-questions but worth expanding. With Claude Max flat-rate billing, the cost concern shifts from token spend to rate limits and throughput:

- **Rate limit analysis.** 9 agents × 8–10 invocations/day. Does Max support this throughput? What are the concurrent session limits?
- **Token budget per agent.** The distillation layer produces large payloads (all 65 tickers × multiple data categories). What's the estimated input token count per agent? Does it fit within context windows?
- **Latency budget.** The pipeline runs 2–4 times during market hours. If each invocation takes 30+ minutes (9 sequential/parallel agents with tool use), does the system finish before the next trigger?

### 2.6 Paper-to-live transition criteria

`open-questions.md` asks "What paper trading performance justifies moving to live trading?" This deserves its own document:

- **Minimum paper trading duration.** How many market days/weeks before the results are statistically meaningful?
- **Performance thresholds.** Sharpe ratio, max drawdown, win rate, profit factor — what values are required?
- **Operational readiness.** Beyond P/L: system uptime, error rate, guardrail breach frequency, data quality consistency.

### 2.7 Configuration management

The system has many configurable parameters spread across multiple layers — API keys, ticker universe, distillation thresholds, guardrail limits, regime parameters, agent model assignments, prompt files, cron schedule, venue profiles. There's no centralized configuration design:

- **Configuration file format and location.** Single YAML/TOML? Per-layer configs? Environment variables?
- **Runtime vs. deploy-time configuration.** Which parameters can be changed without restarting the pipeline?
- **Configuration validation.** How are invalid configurations caught before they cause runtime failures?

---

## Tier 3 — Underspecified areas within existing docs

### 3.1 Data layer: API source selection

The external data docs (quantitative.md, qualitative.md) define 12+ data categories in excellent detail but are deliberately agnostic about which specific APIs provide the data. The source-to-target mapping spreadsheet and YAML files define the schema, but the actual provider selection (and associated cost, rate limit, and reliability characteristics) is deferred. This is the #1 item in `open-questions.md` and blocks data layer implementation.

### 3.2 Analysis layer: Domain researcher output format

The three domain researcher docs (tech-semis, financials, energy) define what each researcher should look for (sector-specific patterns, signals, and concerns) but don't fully specify the output schema. The synthesizer consumes these briefs and applies typed reference IDs (e.g., `[SA-TECH-3]`) — there needs to be a precise contract for how briefs are structured so reference IDs are consistent and retrieval works correctly.

### 3.3 Analysis layer: Adaptive research tool inventory

The adaptive research doc describes a powerful agentic loop (triage anomalies → generate questions → investigate → synthesize) but the tool inventory is only sketched. "Research tools (news API, prediction market queries, data pulls)" needs to become a concrete list of available tools with input/output contracts, so the prompt can reference them and the SDK can register them.

### 3.4 Execution layer: Continuous monitor process

The architecture docs describe two processes (pipeline + continuous monitor). The monitor's five responsibilities (Alpaca fill-stream consumption, guardrail breach detection, emergency invocation triggering, greeks refresh orchestration, options bracket-stop evaluation) are now consolidated in [architecture.md § 4](05-execution-layer/architecture.md). Remaining underspecification: lifecycle management (startup, shutdown, failure recovery), interaction with the pipeline process during invocations, and real-time alerting beyond guardrail breaches.

### 3.5 Distillation layer: Threshold calibration

The external distillation doc defines anomaly detection (z-score thresholds, historical context requirements, composite scoring) but acknowledges that threshold values need tuning. There's no methodology for how these thresholds will be initially calibrated or updated over time. **Resolved.** Methodology, initial values, cold-start bootstrap policy, update process, and validation invariants are specified in [02-distillation-layer/threshold-calibration.md](02-distillation-layer/threshold-calibration.md). Schema location (`config/distillation.yaml`) is wired into [configuration-management.md](configuration-management.md).

### 3.6 Asset universe: Validation methodology

`asset-universe.md` provides starting ticker lists for 4 sectors (~60–80 names) with selection criteria (liquidity, coverage, beta, options availability). `open-questions.md` flags the need to validate each name against live data. **Resolved.** Methodology, per-criterion data source / lookback / computation / threshold, validation procedure, re-evaluation cadence, add/remove process (including held-position interaction), and config-load invariants are specified in [asset-universe-validation.md](asset-universe-validation.md). The authoritative ticker list now lives at `config/universe.yaml` (universe-wide, no profile composition); `asset-universe.md` is retained as the descriptive companion.

---

## Relationship to `open-questions.md`

The existing open questions file identifies 7 items. This gap analysis covers all of them and adds significant additional scope. Here's the mapping:

| Open question | Coverage in this analysis |
|---|---|
| Data source specifics | §3.1 |
| Risk guardrail values | §1.3, §1.4, §1.5, §1.6 |
| Prompt engineering | §1.1, §1.2 (system prompt design) |
| Cost modeling | §2.5 |
| Success criteria | §2.6 |
| Feedback loops | §2.4 |
| Ticker refinement | §3.6 |

---

## Suggested prioritization

The gaps above roughly sort into three implementation horizons:

**Before writing any code:** Analyst spec (§1.1), Portfolio manager agent spec (§1.2), rules & limits values (§1.3), API source selection (§3.1), domain researcher output schema (§3.2). These define the contracts that the rest of the system implements against.

**Before starting paper trading:** Breach behavior (§1.4), regime adaptation (§1.5), state delivery (§1.6), error handling (§2.1), testing strategy (§2.3), configuration management (§2.7), continuous monitor scope (§3.4).

**Before transitioning to live:** Monitoring and alerting (§2.2), feedback loops (§2.4), cost/rate-limit modeling (§2.5), paper-to-live transition criteria (§2.6), threshold calibration methodology (§3.5), asset universe validation (§3.6).
