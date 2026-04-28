# System characterization

What kind of system AlphaMind is — before choosing how to build it.

---

## Summary

AlphaMind is a **scheduled batch pipeline** dominated by LLM API latency, with a **real-time sidecar** (continuous monitor on Alpaca's `trade_updates` websocket plus price streams for options-stop and guardrail-breach detection), a **moderately complex state machine** (the OMS) for position and order lifecycle, a **heavy data integration layer**, and a **meaningful but not HPC-scale numerical component**.

Layers are sequential stages in a single pipeline — not a low-latency trading system, streaming pipeline, or microservices architecture. Closest shape: a **sophisticated ETL/workflow system with LLM agents in the middle and an event-driven execution sidecar**.

---

## Four runtime profiles

### 1. The pipeline (batch, scheduled, 8-10x/day)

The core loop. Triggered on schedule, runs sequentially through 5 layers — single pass, no long-lived state within a run, no loops spanning invocations.

```
Trigger
  → Data collection      (I/O bound: burst of API calls to external sources + DB read of internal state)
  → Distillation          (CPU bound: numerical computation — indicators, anomaly detection, normalization)
  → Analysis              (LLM-API bound: multiple parallel agent calls + sequential synthesizer)
  → Decision              (LLM-API bound: sequential agent calls with tool use)
  → Execution             (CPU + I/O: validation, DB writes, order submission)
```

**LLM API latency dominates.** Layers 1-2 and 5 are fast programmatic work; 3-4 wait on LLM responses.

### 2. The continuous monitor (real-time, always-on)

A **separate process** running 24/7 during market hours, subscribed to Alpaca's `trade_updates` websocket and to underlying equity streams for options bracket-stop and greeks-refresh triggers.

```
Alpaca trade_updates  → buffer fills for next pipeline collect
Underlying price feed → options stop trigger evaluation → close order via broker adapter
                      → 2%-move greek refresh trigger
                      → guardrail breach detection
```

**Event-driven, real-time.** Low latency matters; maintains state (pending orders, bracket lifecycle for options, position greeks); shares the DB with the pipeline (pipeline reads during Phase 1; monitor writes continuously).

### 3. The scheduler (control plane)

Manages the invocation schedule: market-hours (2h), off-hours (4h), anchored runs (pre-open, pre-close), overlap deduplication. Requires market calendar awareness and time zones.

### 4. Data ingestion infrastructure (I/O, rate-limited)

12+ quantitative and 6+ qualitative data categories across 60-80 tickers — hundreds of API calls per invocation with varied rate limits, auth, error handling, and refresh cadences. Websocket (monitor) and REST (batch pulls at invocation time).

---

## Six architectural concerns

These cut across the runtime profiles.

### A. Pipeline orchestration

Sequencing layers, parallelism *within* layers (3 sector analysts + qualitative research run in parallel), partial failure handling, structured data between stages.

### B. LLM agent management

9 distinct agents per invocation (3 sector researchers, qualitative researcher, adaptive researcher, synthesizer, analyst, strategist, PM), each with its own context window, system prompt, and tool access. Mix of parallel and sequential. Adaptive researcher has a nested agentic loop; decision layer agents have retrieval tools fetching from a brief store. The heart of the system's complexity.

### C. State management

Multiple state lifecycles:

- **Portfolio state** (persistent, authoritative): positions, theses, orders, cash, P/L — the system's memory across invocations
- **Fill buffer** (ephemeral, accumulating): fills from the continuous monitor, drained each pipeline invocation
- **Brief store** (per-invocation, read-heavy): analysis briefs keyed by reference ID for decision layer retrieval
- **Distillation state** (rolling): baselines, regime classifications, composite signals — updated each invocation, persisted between

### D. Numerical computation

Distillation layer work: technical indicators, statistical anomaly detection, normalization, volatility regime classification, portfolio exposure calculations, P/L attribution.

### E. External data integration

Dozens of heterogeneous APIs (market data vendors, news, prediction markets) with varied auth schemes, rate limits, and formats — each an adapter with its own error handling.

### F. Observability and auditability

Every decision must be traceable: PM approved → analyst recommended → synthesizer highlighted → sector researcher flagged → distillation computed → raw data showed. Structured logging, brief archival, decision audit trails.
