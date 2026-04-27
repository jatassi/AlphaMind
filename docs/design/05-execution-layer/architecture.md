# Architecture

Four components with distinct responsibilities, interaction patterns, and failure modes.

---

## 1. Order management system (OMS)

The core of the engine. Receives commands from the portfolio manager (during pipeline invocations) and from the continuous monitor (between invocations, for guardrail breach responses — see [oms-commands.md](oms-commands.md) for command origins). Consumes fill events from Alpaca via the continuous monitor's websocket subscription and maintains the authoritative record of positions, theses, orders, cash, activity log, and historical resolutions. The raw state specification ([categories 1–6](../01-data-layer/internal/portfolio-state.md)) is a read interface into the OMS database.

The OMS behaves identically in paper and live modes — the only difference is which Alpaca base URL the [broker adapter](broker-adapter.md) is configured for, and whether the [paper-evaluation harness](paper-evaluation-harness.md) annotates fills with live-execution estimates. Position updates, P/L accounting, and thesis tracking run off the raw fill stream regardless of mode.

The OMS also owns venue-level configuration: settlement cycles, pre-settlement credit rules, market session boundaries, and Alpaca's regulatory constraints (PDT rule, Reg T margin percentages). See [venue-configuration.md](venue-configuration.md).

## 2. Broker adapter

The layer the OMS calls to execute orders and the layer the monitor subscribes to for fill events. Accepts a validated order, issues the corresponding Alpaca REST call, and returns a submission acknowledgment. Translates Alpaca's `trade_updates` websocket events into fill reports for the OMS. See [broker-adapter.md](broker-adapter.md) for the full Alpaca surface — order types, order classes, modification semantics, fee reporting cadence, options support constraints.

The adapter contains no business logic. Validation and risk enforcement happen in the guardrail layer before a command reaches the adapter; position and thesis state live in the OMS.

## 3. Guardrail enforcement layer

Sits between portfolio manager command intake and broker adapter routing. Validates every command against risk constraints before execution. Mode-agnostic. The engine spec defines the enforcement interface; specific rules, limit values, and regime-dependent parameters are defined separately.

The guardrail layer enforces rules during invocations via T3 validation; the continuous monitor (component 4) enforces them between invocations.

The layer has veto power: it can reject or modify any command that violates a constraint. The portfolio manager is the *judgment* layer for risk; the engine is the *hard stop* layer. Rejections return **synchronously to the portfolio manager within the same invocation** so the PM can adjust and retry, and are logged in the activity log ([raw state category 5b](../01-data-layer/internal/portfolio-state.md)).

Execution-time rejections should be infrequent: the analyst and strategist pre-validate proposals before they reach the PM, and the PM validates its own sizing modifications. The engine's check is an authoritative backstop catching state drift between upstream validation and command execution. See [oms-commands.md](oms-commands.md) for the rejection handling contract and [analyst.md](../04-decision-layer/analyst.md) for upstream validation.

**Greek computation for options validation:** For OPEN and ADD commands on options or strategy positions, the guardrail layer computes greeks internally rather than requiring them as command parameters — the portfolio manager is an LLM and would hallucinate values. The math is delegated to the [guardrail-evaluation library](../06-risk-guardrails/guardrail-evaluation.md), which is also the source of the Black-Scholes model used by the continuous monitor's greek refresh. Computed greeks are returned in the validation response and persisted as the position's initial greeks. See [oms-commands.md](oms-commands.md) for the engine-side orchestration.

---

## Interaction model: fully asynchronous

The broker adapter is always asynchronous from the OMS's perspective. Every order submission returns an acknowledgment, never an immediate fill. Fill reports arrive via Alpaca's `trade_updates` websocket and are collected at the start of the next pipeline invocation. Paper and live modes use the same interaction shape — only the Alpaca base URL differs.

### Two-phase invocation model

Each pipeline invocation has two phases:

**Phase 1 — Collect.** At the start of each invocation, the OMS drains the fill buffer written by the continuous monitor since the last invocation: fill reports, partial fills, stop triggers, order expirations, cancellations. This produces the activity changelog ([raw state category 5a](../01-data-layer/internal/portfolio-state.md)) and updates positions, P/L, cash, and thesis status. Collection happens *before* the ingestion layer snapshots portfolio state, so the pipeline operates on settled state with no in-flight orders.

**Phase 2 — Execute.** At the end of each invocation, the portfolio manager issues commands. The OMS validates them against guardrails, routes them through the broker adapter to Alpaca, and receives acknowledgments (not fills). These become pending orders ([raw state category 4b](../01-data-layer/internal/portfolio-state.md)) that resolve in a future invocation's collect phase.

*Fill timestamps reflect actual execution, not collection.* Fills arrive on `trade_updates` with Alpaca's event timestamps; the monitor writes them to the buffer as they arrive. Phase 1 preserves the Alpaca timestamp through to activity logs and position-age calculations.

---

## 4. Continuous monitor

A first-class system component running persistently during market hours — peer of the OMS, adapter, and guardrail layer. Five responsibilities span both paper and live trading.

### 4a. Alpaca fill-stream consumption

The monitor subscribes to Alpaca's `trade_updates` websocket (via the [broker adapter](broker-adapter.md)) and writes every fill event into the fill buffer as it arrives. Paper and live mode work identically — same websocket shape, same event types; only the base URL differs. The OMS drains the buffer during Phase 1. Bracket lifecycle for equities — entry fill → protective leg activation → stop/target resolution → OCO cancellation — is handled natively by Alpaca and arrives as separate events the monitor passes through.

**Disconnect recovery.** If the websocket disconnects, the monitor reconnects and queries `GET /v2/orders` with a `since` parameter to recover missed events. Alpaca's order state is authoritative; the OMS reconciles toward it and logs deltas.

### 4b. Guardrail breach detection and protective response

The monitor periodically evaluates portfolio state against guardrail limits using live market data. When market movement, a regime change, or a margin event causes a guardrail breach on an existing position, the monitor determines whether immediate action is required based on the per-rule classification in [breach-behavior.md](../06-risk-guardrails/breach-behavior.md).

**Response authority:** The monitor may issue CLOSE commands (only CLOSE — never OPEN, ADD, ADJUST, or CANCEL) to cure detected breaches. Each CLOSE is wrapped in an engine-originated command envelope with a guardrail trigger record providing full traceability. See [portfolio-manager.md](../04-decision-layer/portfolio-manager.md) for the envelope specification.

**Secondary breach checking:** Before issuing a protective CLOSE, the monitor verifies that the close wouldn't create a new breach (e.g., closing a short that was providing directional balance). If a secondary breach would result, the monitor logs the conflict and either selects an alternative position or defers to the strategist/PM.

**Activity log integration:** Engine-originated envelopes are written to the activity log with the same structure as PM-originated envelopes. The PM, strategist, and feedback loop see a uniform audit trail regardless of command origin.

### 4c. Emergency invocation triggering

When market conditions deteriorate beyond what mechanical between-invocation actions can handle, the monitor can trigger an emergency pipeline invocation to bring the full agent pipeline (analyst, strategist, PM) online immediately rather than waiting for the next scheduled run. Trigger conditions include regime jumps, multi-rule breaches, rapid drawdown acceleration, and margin calls. See [breach-behavior.md](../06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) for the trigger thresholds, cooldown rules, and relationship to the scheduled invocation cadence.

### 4d. Greeks refresh orchestration

For open option positions, the monitor maintains freshness of the greeks used for derived pricing and continuous guardrail evaluation. It runs a scheduled 15-minute refresh timer per underlying with open options and additionally triggers a refresh when the underlying has moved more than 2% since the last refresh. The monitor already subscribes to underlying price streams for bracket stop trigger detection, so the move-based trigger requires no new data subscription.

On each refresh, the monitor fetches the latest IV from the data pipeline, recomputes greeks via the same Black-Scholes model the guardrail layer uses for OPEN/ADD validation, and writes the refreshed values to the position record consumed by both its own derived-pricing path and the next invocation's guardrail checks. On refresh failure, the monitor falls back to a widened uncertainty buffer on the last known greeks; if the resulting ambiguity prevents a confident breach determination, the monitor escalates to emergency invocation under 4c rather than act on stale data.

This responsibility supports 4e (options bracket derived-price evaluation and P/L-target firing) and 4b (continuous exposure monitoring for options positions). Options-fill execution itself is Alpaca's responsibility — see [broker-adapter.md § Supported instruments](broker-adapter.md).

**Refresh triggers (whichever fires first):**

- *Scheduled:* every 15 minutes during market hours, per underlying with open option positions.
- *Move-based:* when the underlying has moved more than 2% cumulative from the price at the last refresh. Measured continuously against the underlying stream the monitor already consumes for 4e.

**Refresh action:** fetch the latest IV from the data pipeline's surface snapshot, recompute greeks via Black-Scholes using the underlying price in hand, and write the refreshed values to the position record consumed by both the monitor's derived-pricing path (4e) and the next invocation's guardrail checks.

**Failure semantics:** brief retry on IV fetch failure matching the adapter's submission retry pattern; on exhaustion, the monitor continues with the last successful greeks under a widened derivation uncertainty buffer and records `greeks_refresh_failed` in the activity log. If the stale greeks subsequently leave a breach determination ambiguous, the monitor escalates to emergency invocation (4c) rather than act on uncertain data.

**Off-hours:** the scheduled timer pauses outside market hours; the underlying stream stops producing ticks, so neither refresh trigger fires. Greeks hold at the last in-market-hours refresh until the next session open. Theta decay still applies via the derivation formula (Δt is known from the clock); delta, gamma, and vega do not drift because the underlying isn't trading.

### 4e. Options bracket-stop evaluation and P/L-target firing

Alpaca does not support bracket, OCO, or OTO order classes on options ([broker-adapter.md § Order classes](broker-adapter.md)). AlphaMind's mandatory thesis-linked bracket requirement ([orders-and-brackets.md](orders-and-brackets.md)) is therefore satisfied for options at the monitor level, regardless of paper vs. live mode.

**Price-based invalidation (thesis stop).** The stop trigger evaluates against the **underlying equity's real-time stream** — the same clean, high-fidelity feed used for equity stops. No derived pricing is involved in the trigger decision. When the trigger fires, the monitor submits a closing market order on the option position via the broker adapter. The actual fill price depends on Alpaca's options fill at the moment of submission; derivation error does not enter the trigger.

**P/L-based targets.** Take-profit legs defined in P/L terms ("close at 80% profit on premium") use the derived options price (from 4d) for trigger evaluation, with the derivation uncertainty buffer applied. A configurable margin on top of the target (default: 5% of estimated spread) prevents false firings from derivation noise.

**Guardrail P/L monitoring.** Position-level max-loss guardrails on options positions use the same derived pricing. This is the capital-protection layer that catches greek-driven value erosion independently of underlying price movement. Under refresh failure, the monitor widens the uncertainty buffer; if breach determination remains ambiguous, it escalates to emergency invocation (4c).

**Strategy positions.** Multi-leg strategies evaluate stops on the underlying and close the entire strategy via market orders. Alpaca's `mleg` order class does not support contingent submission, so the monitor submits a fresh `mleg` closing order (or per-leg market orders if Alpaca rejects a combined close) when the stop fires.

### Why first-class?

The continuous monitor is mode-agnostic: its guardrail enforcement, emergency invocation, greeks refresh, and options bracket-stop responsibilities apply identically in paper and live trading. Even 4a (fill-stream consumption) is identical between modes — same websocket shape, only the URL differs. The monitor is persistent system infrastructure, not a mode-specific detail.

---

## Fill report contract

The fill report is the OMS-facing projection of Alpaca's `trade_updates` event. The adapter translates raw Alpaca events into this shape before the monitor writes them to the fill buffer.

Each fill report contains:

- **Order ID:** the OMS's stable `client_order_id`, correlating back to the original submission
- **Alpaca order ID:** Alpaca's internal identifier at the time of the fill — may differ from the original if the order has been replaced via PATCH. Preserved for audit
- **Fill timestamp:** Alpaca's event timestamp, preserved through the pipeline — not the invocation collection time
- **Fill price:** raw execution price as reported by Alpaca
- **Fill quantity:** shares or contracts filled; may be less than order quantity for partial fills
- **Remaining quantity:** unfilled portion, if any — zero for complete fills
- **Order status:** mapped from Alpaca's event type — `filled`, `partially_filled`, `canceled`, `expired`, `rejected`, `stopped`, `done_for_day`
- **Execution venue:** which exchange filled the order (populated in live mode; absent or `paper` in paper mode)
- **`live_execution_estimate`** (paper mode only): metadata attached by the [paper-evaluation harness](paper-evaluation-harness.md) — estimated spread, impact, regulatory-fee drag, and a live-adjusted fill price. Absent in live mode, where execution costs are real

Regulatory fees are not present in the per-fill report — Alpaca reports them at EOD via the account activities endpoint. The OMS records fills pre-fee and applies a daily `fee_reconciliation` event against raw activity data; the paper-evaluation harness separately estimates the expected fee drag at fill time for live-execution estimation. See [broker-adapter.md § Fee reporting](broker-adapter.md) for details.
