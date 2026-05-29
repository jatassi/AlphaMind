# AlphaMind — agentic trading system architecture spec

**Status:** Implementation phase
**Last updated:** 2026-03-15

---

## 1. System overview

**AlphaMind** is an autonomous trading system that uses a multi-agent LLM pipeline to synthesize market data, generate trade theses, and execute positions. Concerns split across fresh context windows — mirroring institutional desks dividing research, strategy, and risk management — preventing cognitive anchoring between stages.

The core insight: LLMs add value not through faster price analysis (traditional algos win there) but through synthesizing *unstructured* information across domains — reading between the lines of news, catching narrative shifts, identifying cross-domain signals quantitative systems can't easily encode.

The initial phase paper trades with real market data to validate thesis accuracy and measure P/L before capital is deployed.


## 2. Design principles

### Time horizon: 4–72 hours

Swing-trade scale. Long enough that unstructured information synthesis matters (narrative shifts, regulatory signals, prediction market movements), short enough to avoid macro bets requiring deep fundamental analysis.

The constraint is thesis validity, not calendar time. A position opened at 9 AM might close at 3 PM if the catalyst plays out fast, or sit two days waiting to resolve. Close when the original thesis is invalidated or fully realized.

### Incremental gains

No moonshots. High volume of small, positive-expectancy trades, ending each day with more liquid cash than the opening balance. Aligns with mean-reversion and statistical arbitrage over momentum or trend-following.

### Information edge, not speed edge

Competitive advantage is cross-domain synthesis of unstructured data. The system never competes on latency or pure technical analysis. Every trade thesis involves at least one qualitative signal a traditional quant system would miss.

### Invocation schedule

The pipeline runs on a deliberately sparse schedule (Tier B) tied to US market hours (NYSE: 9:30 AM – 4:00 PM ET). The deliberative pipeline forms theses and makes position decisions — an inherently low-frequency job for a swing/position system — so it does not need an intraday polling cadence. Real-time risk on open positions is owned by the continuous monitor (breach checks, bracket stops, greeks, entry windows, borrow accrual), not by the pipeline.

Three fixed weekday invocations, each at a distinct minute:
- **Pre-open (9:00 AM ET):** Synthesizes overnight developments, pre-market price action, and thesis-relevant news; positions for the open — open new entries vs. adjust existing.
- **Mid-day (1:00 PM ET):** A single intraday read (`market_hours_rolling`) for new-opportunity discovery and thesis updates while price action, flow, and news are signal-dense.
- **Pre-close (3:30 PM ET):** Evaluates open positions against end-of-day dynamics: close before the overnight gap? Late-day setups? Bias toward risk reduction — overnight holds need strong conviction.

Between 9:00↔13:00 and 13:00↔15:30 there is no deliberative read. An intraday setup that appears and decays inside those gaps will not be acted on; the continuous monitor still manages risk on open positions throughout. This is the intentional Tier B bargain — what is given up is new-opportunity discovery in those gaps.

**Overlap handling:** The Tier B schedule assigns every trigger a distinct minute, so no two scheduled runs ever fire in the same minute or share a minute-truncated `as_of` — the prior design's collision (two passes racing on the distillation `UNIQUE(ticker, baseline_kind, as_of)` constraint) is structurally unreachable on the scheduled path. The 30-minute dedup window remains as a backstop: it suppresses a rolling fire only if some invocation completed phase 2 within the lookback (e.g., a manual or emergency run just before the mid-day slot).

**Per-run-type scoping.** Each invocation type carries a tailored pipeline configuration via the [`run_types/` overlay](configuration-management.md#run_typestriggeryaml): an agent roster (which of the nine LLM agents fire on this trigger) plus a budget envelope (per-agent latency and output-token caps, adaptive-research tool-call and token caps, qualitative news-digest depth). Pre-open and weekend-Sunday carry full rosters with expanded adaptive budget; pre-close and market-hours rolling carry full rosters at normal budget. The off-hours-rolling and weekend-Saturday overlays — which omit the adaptive researcher entirely — remain defined for manual / emergency use but are no longer scheduled under Tier B. Behavioral shaping flows from the synthesizer brief's data composition and the structural budget envelope — no agent prompt receives a run-type instruction.

**Total daily invocations:** ~3 per trading day (pre-open + mid-day + pre-close), ~16 per normal week including the Sunday run.

**Weekend invocations:** One anchored run. Sunday ~18:00 ET positions before Monday's pre-market open and the Sunday-evening earnings-preannouncement window. Strategist re-evaluates open positions against new weekend signal; analyst's inclusion threshold suppresses noise on quiet weekends. The Saturday run is unscheduled under Tier B. Off-schedule events (geopolitical breaks, prediction-market shifts beyond distillation thresholds) reach the system through the continuous monitor's emergency-invocation path.


---

## Architecture

The system is organized into five sequential layers, with risk guardrails as a cross-cutting concern:

```
Trigger → Data Layer → Distillation Layer → Analysis Layer → Decision Layer → Execution Layer
                                                                    ↑                ↑
                                                              Risk Guardrails ───────┘
```

### Reference documents

| Document | Description |
|----------|-------------|
| [Pipeline overview](pipeline-overview.md) | Stage diagram and stage descriptions |
| [Asset universe](asset-universe.md) | Ticker universe across four sectors, selection criteria |
| [Design decisions](design-decisions.md) | Key architectural rationale |
| [Command center](command-center.md) | Operator-facing webapp: monitoring, alerting, configuration editing, manual control, authentication |
| [Cost and rate-limit modeling](cost-and-rate-limit-modeling.md) | Workload fit against the Claude Max 5x subscription envelope; operating posture under cap pressure |
| [Feedback loop](feedback-loop.md) | Discovery, Prescription, Validation: how the system gets better month over month. Metric inventory across all layers, citation-chain flagship, counterfactual replay role, confounder management |
| [Configuration management](configuration-management.md) | YAML configuration tree, composition model, validation layers, snapshot persistence |

### 01 — Data layer

Raw data collection. External market data and the system's own internal state.

| Document | Description |
|----------|-------------|
| [README](01-data-layer/README.md) | Layer overview: external vs. internal data |
| **External** | |
| &emsp;[Quantitative data](01-data-layer/external/quantitative.md) | 12 categories of market data (core + supplementary) |
| &emsp;[Qualitative data](01-data-layer/external/qualitative.md) | News, sentiment, prediction markets, catalysts |
| **Internal** | |
| &emsp;[README](01-data-layer/internal/README.md) | Portfolio state overview, data flow, consumer map, cross-reference map |
| &emsp;[Portfolio state](01-data-layer/internal/portfolio-state.md) | Categories 1–6: positions, P/L, thesis registry, capital, activity log, thesis quality — direct reads from execution layer |

### 02 — Distillation layer

Deterministic transformation of raw data into LLM-consumable signals.

| Document | Description |
|----------|-------------|
| [README](02-distillation-layer/README.md) | Layer overview: external vs. internal distillation |
| [External distillation](02-distillation-layer/external.md) | Normalization, indicators, anomaly detection, persistent state and composites |
| [Internal distillation](02-distillation-layer/internal.md) | Categories 7–11: exposure analysis, P/L attribution, thesis dependency, capital efficiency, system health |

### 03 — Analysis layer

LLM agents interpret distilled data and produce structured briefs.

| Document | Description |
|----------|-------------|
| [README](03-analysis-layer/README.md) | Layer overview, sequencing, universal context broadcast |
| **Domain researchers** | |
| &emsp;[Tech & semis](03-analysis-layer/domain-researchers/tech-semis.md) | ~35 tickers: mega-cap + growth + semiconductor analysis |
| &emsp;[Financials](03-analysis-layer/domain-researchers/financials.md) | ~15 tickers: banks, payments, fintech, rate sensitivity |
| &emsp;[Energy](03-analysis-layer/domain-researchers/energy.md) | ~15 tickers: majors, E&P, midstream, commodity linkage |
| [Qualitative research](03-analysis-layer/qualitative-research.md) | Always-on sweep: headlines, calendar, sentiment, prediction markets |
| [Adaptive research](03-analysis-layer/adaptive-research.md) | Anomaly-driven investigation: the system's "curiosity" layer |
| [Synthesizer](03-analysis-layer/synthesizer.md) | Unified market snapshot with source references — primary input for decision layer |

### 04 — Decision layer

LLM agents reason about what to do with the information available.

| Document | Description |
|----------|-------------|
| [README](04-decision-layer/README.md) | Layer overview, retrieval tool concept, sequential flow |
| [Analyst](04-decision-layer/analyst.md) | Identifies asymmetric setups, constructs trade theses for new entries; signal weighting, conviction calibration, anti-ranking discipline |
| [Strategist](04-decision-layer/strategist.md) | Evaluates every open position against current conditions; thesis status classification, action decision logic, defensive-posture mode |
| [Proposal pre-processor](04-decision-layer/proposal-pre-processor.md) | Deterministic merge of analyst + strategist outputs with cross-proposal annotations: combined-set guardrail impact, conviction distribution, conflict cross-references |
| [Portfolio manager](04-decision-layer/portfolio-manager.md) | Critically evaluates annotated proposals against thesis-quality criteria; rejects, modifies, or executes |

### 05 — Execution layer

Executes trades and records results.

| Document | Description |
|----------|-------------|
| [README](05-execution-layer/README.md) | Engine overview, component architecture, relationship map |
| [Architecture](05-execution-layer/architecture.md) | Four components (OMS, broker adapter, guardrails, continuous monitor), async interaction model, two-phase invocation, fill report contract |
| [Broker adapter](05-execution-layer/broker-adapter.md) | Alpaca-specific surface: order types, order classes, modification semantics, fill stream, fee reconciliation, options constraints |
| [Paper-evaluation harness](05-execution-layer/paper-evaluation-harness.md) | Calibrated live-execution estimates (slippage, impact, fees) on top of Alpaca paper fills |
| [Counterfactual replay engine](05-execution-layer/counterfactual-replay-engine.md) | Offline simulator for PM-rejected and PM-modified proposals — substrate for PM rejection accuracy and modification effectiveness in the feedback loop |
| [Orders & brackets](05-execution-layer/orders-and-brackets.md) | Full order vocabulary, mandatory thesis-linked brackets, three invalidation types |
| [Position model](05-execution-layer/position-model.md) | Multi-instrument hierarchy (equity, options, strategy), delta-adjusted exposure, Reg T margin |
| [Thesis model](05-execution-layer/thesis-model.md) | Structured components within flat record, mandatory bracket coverage, component-level resolution |
| [OMS commands](05-execution-layer/oms-commands.md) | Five commands: OPEN, CLOSE, ADJUST, CANCEL, ADD — no compound commands (see [design decisions](design-decisions.md)) |
| [Corporate actions](05-execution-layer/corporate-actions.md) | Position-layer mechanics for Alpaca-emitted CA events: per-action quantity/cost-basis/cash mutations, spin-off child positions, Phase 1 chronological merge with fills, idempotency ledger |
| [State persistence](05-execution-layer/state-persistence.md) | Three-tier persistence model (core / lifecycle / derived entities), Phase 1 collect + Phase 2 commit, atomic transactions, immediate fill persistence, single-writer invariant, activity log catalog |
| [Venue configuration](05-execution-layer/venue-configuration.md) | Alpaca-specific venue rules: settlement, sessions, PDT, Reg T margin tiers |

### 06 — Risk guardrails (cross-cutting)

Hard-coded programmatic constraints — not LLM-mediated. Enforced at multiple layers: analyst advisory, PM judgment, execution engine hard stop.

| Document | Description |
|----------|-------------|
| [README](06-risk-guardrails/README.md) | Multi-layer enforcement model, relationship map |
| [Rules & limits](06-risk-guardrails/rules-and-limits.md) | Concrete rule values for position size, sector concentration, drawdown, exposure, correlation, Greeks; three-tier enforcement mapping; dual portfolio profiles |
| [Regime adaptation](06-risk-guardrails/regime-adaptation.md) | Per-regime multiplier table for all rules across four regimes (low-vol / normal / elevated / crisis); immediate tightening, gradual loosening over 3 invocations |
| [Breach behavior](06-risk-guardrails/breach-behavior.md) | Forced-reduction policy (per-rule classification), drawdown halt mode (daily + cumulative tiers), escalation thresholds, margin-call cascade priority |
| [State delivery](06-risk-guardrails/state-delivery.md) | Per-agent guardrail state headers (analyst / strategist / PM); guardrail validation tool contract; portfolio state ingestion payload |
| [Guardrail evaluation](06-risk-guardrails/guardrail-evaluation.md) | Shared math primitives: Black-Scholes delta with conservative buffer, per-rule projection, regime parameter resolution, feature-flag early-exit |
| [Scenario tests](06-risk-guardrails/scenario-tests.md) | Worked walkthroughs of design-validation scenarios across normal / elevated / crisis regimes |
