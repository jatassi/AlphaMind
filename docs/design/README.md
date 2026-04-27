# AlphaMind — agentic trading system architecture spec

**Status:** Design phase (pre-implementation)
**Version:** 0.30 — layered architecture refactor
**Last updated:** 2026-03-15

---

## 1. System overview

**AlphaMind** is an autonomous trading system that uses a multi-agent LLM pipeline to synthesize market data, generate trade theses, and execute positions. The system separates concerns across fresh context windows — mirroring how institutional trading desks divide research, strategy, and risk management — to prevent cognitive anchoring between stages.

The core insight: LLMs add value not through faster price analysis (traditional algos win there) but through synthesizing *unstructured* information across domains — reading between the lines of news, catching narrative shifts, identifying cross-domain signals that quantitative systems can't easily encode.

In the initial phase, the system paper trades with real market data to validate thesis accuracy and measure P/L before any real capital is deployed.


## 2. Design principles

### Time horizon: 4–72 hours

Positions are swing-trade scale, not day-trade. Long enough that unstructured information synthesis matters — narrative shifts, regulatory signals, prediction market movements — but short enough to avoid macro bets that require deep fundamental analysis.

The constraint is thesis validity, not calendar time. A position opened at 9 AM might close at 3 PM because the catalyst played out fast, or it might sit for two days waiting for a thesis to resolve. The rule: close any position whose original thesis has been invalidated or fully realized.

### Incremental gains

No moonshots. The system targets a high volume of small, positive-expectancy trades. The overarching goal is to end each trading day with more liquid cash than the opening balance. This aligns with mean-reversion and statistical arbitrage approaches rather than momentum or trend-following.

### Information edge, not speed edge

The system's competitive advantage is cross-domain synthesis of unstructured data. It should never compete on latency or pure technical analysis — those are solved problems with faster, cheaper tools. Every trade thesis should involve at least one qualitative signal that a traditional quantitative system would miss.

### Invocation schedule

The pipeline runs on a fixed schedule tied to US market hours (NYSE: 9:30 AM – 4:00 PM ET).

**Market hours (9:30 AM – 4:00 PM ET):** Every 2 hours. Higher frequency because this is when price action, flow data, and news are most signal-dense. Approximate runs: 9:30, 11:30, 1:30, 3:30.

**Off hours (4:00 PM – 9:30 AM ET):** Every 4 hours. Lower frequency because signal density drops, but overnight developments (Asian/European markets, geopolitical events, earnings releases) still need monitoring. Approximate runs: 8:00 PM, 12:00 AM, 4:00 AM, 8:00 AM.

**Anchored runs:** Two fixed invocations regardless of the rolling schedule:
- **Pre-open (9:00 AM ET):** Runs 30 minutes before market open. Synthesizes overnight developments, pre-market price action, and any thesis-relevant news into a fresh snapshot before the opening bell. Critical for positioning decisions — this is when the system decides whether to enter new positions at open or adjust existing ones.
- **Pre-close (3:30 PM ET):** Runs 30 minutes before market close. Evaluates all open positions against end-of-day dynamics — do any theses need to be closed before the overnight gap? Are there late-day setups worth entering? This run has a bias toward risk reduction: positions held overnight should have strong conviction.

**Schedule overlap handling:** When an anchored run coincides with a rolling interval (e.g., pre-close at 3:30 overlaps with the 2-hour cadence), the anchored run takes precedence and the rolling run is skipped. No double-invocations within 30 minutes of each other.

**TODO — Refine scope per run type:** Each invocation type should have a tailored pipeline configuration — not every run needs the full agent pipeline at full depth. Pre-open likely warrants expanded adaptive research budget and a broader qualitative sweep (overnight developments, international markets, pre-market movers). Pre-close should prioritize portfolio review and thesis invalidation over new opportunity generation. Intraday runs are monitoring-heavy with selective deep dives on anomalies. After-hours runs can be lighter — checking for material news, earnings releases, and overnight catalysts without full technical re-analysis. Defining these scoped profiles will also drive the cost model.

**Total daily invocations:** Approximately 8–10 per trading day (4 market hours + 2 anchored + 3–4 off hours), plus weekday off-hours cycles. Weekend cadence TBD — likely reduced to every 6–8 hours since only futures and prediction markets provide signal.


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
| [Analyst](04-decision-layer/analyst.md) | *(stub)* Trade recommendation generation |
| [Portfolio manager](04-decision-layer/portfolio-manager.md) | *(stub)* Portfolio management and risk evaluation |

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
| [State persistence](05-execution-layer/state-persistence.md) | *(stub)* Storage, read model, schema design |
| [Venue configuration](05-execution-layer/venue-configuration.md) | Alpaca-specific venue rules: settlement, sessions, PDT, Reg T margin tiers |

### 06 — Risk guardrails (cross-cutting)

Hard-coded programmatic constraints — not LLM-mediated. Enforced at multiple layers: trader advisory, PM judgment, execution engine hard stop.

| Document | Description |
|----------|-------------|
| [README](06-risk-guardrails/README.md) | Multi-layer enforcement model, relationship map |
| [Rules & limits](06-risk-guardrails/rules-and-limits.md) | *(stub)* Position size, sector concentration, drawdown, exposure, correlation |
| [Regime adaptation](06-risk-guardrails/regime-adaptation.md) | *(stub)* Volatility-regime-dependent parameter sets and transition mechanics |
| [Breach behavior](06-risk-guardrails/breach-behavior.md) | *(stub)* Warning thresholds, hard rejection, forced reduction, drawdown halt |
| [State delivery](06-risk-guardrails/state-delivery.md) | *(stub)* How guardrail state is packaged for trader, PM, and portfolio state |
