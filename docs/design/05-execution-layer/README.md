# Execution layer

The system's operational core — responsible for order lifecycle management, position tracking, P/L accounting, thesis management, and risk guardrail enforcement.

AlphaMind uses **Alpaca** as its broker for both paper and live trading. The switch between modes is a base-URL change (`paper-api.alpaca.markets` ↔ `api.alpaca.markets`), not a code change. The OMS consumes Alpaca's fill stream identically in both modes; a thin [paper-evaluation harness](paper-evaluation-harness.md) sits on top of paper-mode fills to produce calibrated live-execution estimates for go/no-go evaluation.

*Design principle — parity over simulation.* A custom paper-trading simulator can never match a broker's API surface byte-for-byte, and any gap produces bugs that only appear at the paper-to-live transition — exactly when the cost of a bug is highest. Using Alpaca's paper environment trades some simulation fidelity for identical-API parity. On a 4–72h strategy where per-fill microstructure is not the edge, that is the right tradeoff.

*Design principle — calibrated, not pessimistic.* The paper-evaluation harness estimates the slippage, market impact, and regulatory fees Alpaca's paper environment doesn't model, so paper P/L can be reported two ways: raw (what Alpaca said) and live-adjusted (what we'd expect at a real exchange). The goal is accurate live-estimation. Over-deflating paper P/L would make the go-live threshold unreachable without actually improving the system; the harness is tuned to estimate, not punish.

---

## Component architecture

The engine has four components with distinct responsibilities, consumers, and failure modes:

| Component | Document | Description |
|-----------|----------|-------------|
| Order management system (OMS) | [architecture.md](architecture.md) | Core of the engine: receives PM commands, consumes Alpaca fill events, maintains the authoritative record of positions, theses, orders, cash, activity log, and historical resolutions. The entire [portfolio state specification](../01-data-layer/internal/portfolio-state.md) (categories 1–6) is a read interface into the OMS database |
| Broker adapter (Alpaca) | [broker-adapter.md](broker-adapter.md) | The narrow layer the OMS talks to. Translates OMS commands into Alpaca REST calls and subscribes to Alpaca's `trade_updates` websocket. Documents Alpaca's actual surface: order types, `order_class` values, `PATCH` replace semantics, fee reporting cadence, options support gaps |
| Guardrail enforcement layer | [architecture.md](architecture.md) | Sits between PM command intake and broker adapter. Validates every command against risk constraints before submission. Specific rules and limits defined in [risk guardrails](../06-risk-guardrails/README.md) |
| Continuous monitor | [architecture.md](architecture.md) | Long-running process that owns: (1) Alpaca `trade_updates` consumption into the fill buffer, (2) guardrail breach detection on live prices, (3) emergency invocation triggering, (4) greeks refresh orchestration, (5) options bracket-stop evaluation against the underlying equity stream (Alpaca does not support brackets on options — the monitor owns that logic regardless of mode) |

## Sub-documents

| Document | Status | Description |
|----------|--------|-------------|
| [Architecture](architecture.md) | Designed | Four components, async interaction model, two-phase invocation, fill report contract |
| [Broker adapter](broker-adapter.md) | Designed | Alpaca-specific surface: order types, order classes, modification semantics, fill stream, fee reconciliation, options constraints |
| [Paper-evaluation harness](paper-evaluation-harness.md) | Designed | The calibration layer: slippage + impact + regulatory-fee estimates applied to paper fills, raw vs. live-adjusted P/L reporting, calibration against live data |
| [Counterfactual replay engine](counterfactual-replay-engine.md) | Designed | Offline simulator that walks PM-rejected and PM-modified proposals through underlying price data to produce hypothetical realized P/L. Substrate for PM rejection accuracy, modification effectiveness, and per-anti-pattern detector accuracy in the feedback loop |
| [Orders & brackets](orders-and-brackets.md) | Designed | Full order vocabulary (atomic, contingent, instrument-specific), mandatory thesis-linked brackets, three invalidation types, bracket modification rules |
| [Position model](position-model.md) | Designed | Multi-instrument hierarchy (equity, options, strategy), delta-adjusted exposure calculations, Reg T margin model |
| [Thesis model](thesis-model.md) | Designed | Structured components within a flat record, mandatory bracket coverage, component-level resolution, three consumption modes for token efficiency |
| [OMS commands](oms-commands.md) | Designed | Five commands: OPEN, CLOSE, ADJUST, CANCEL, ADD. No compound commands (ROLL/HEDGE deliberately excluded — see [design decisions](../design-decisions.md)). Validation contracts, rejection handling, sequencing |
| [State persistence](state-persistence.md) | Designed | Three-tier persistence model (core / lifecycle / derived entities), Phase 1 collect + Phase 2 commit, atomic transactions, immediate fill persistence, single-writer invariant, activity log catalog |
| [Corporate actions](corporate-actions.md) | Designed | Position-layer mechanics for Alpaca-emitted CA events: per-action quantity/cost-basis/cash mutations, spin-off child positions, Phase 1 chronological merge with fills, idempotency ledger, activity log additions |
| [Venue configuration](venue-configuration.md) | Designed | Alpaca-specific venue rules: settlement cycle, market sessions, PDT, Reg T margin percentages |

**Note on data schemas:** The position model, thesis model, and orders & brackets documents serve double duty. They define execution behavior *and* the authoritative data schemas consumed by the [data layer](../01-data-layer/internal/README.md). The data layer's internal README cross-references these documents as the source of truth for how positions, theses, and orders are structured.

---

*Relationship to other specs:*

| Spec | Relationship |
|------|-------------|
| [Data layer — internal](../01-data-layer/internal/README.md) | Portfolio state categories 1–6 are read interfaces into the OMS database. The engine *writes* portfolio state; the data layer *reads* it. The OMS's authoritative source of positions, cash, and activities is reconciled against Alpaca's account / positions / orders / activities endpoints |
| [Decision layer — analyst](../04-decision-layer/analyst.md) | Analyst generates thesis recommendations. The thesis model here defines how those recommendations are structured and stored |
| [Decision layer — portfolio manager](../04-decision-layer/portfolio-manager.md) | Portfolio manager is the engine's primary write-path consumer — it issues commands via the OMS command vocabulary and receives portfolio state as input |
| [Risk guardrails](../06-risk-guardrails/README.md) | The guardrail enforcement layer in this engine defines the enforcement interface; the risk guardrail spec defines the specific rules and limits |
