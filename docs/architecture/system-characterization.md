# System characterization

What kind of system AlphaMind is — before choosing how to build it.

---

## Summary

AlphaMind is a **scheduled batch pipeline** where most wall-clock time is spent waiting on LLM API calls, with a **real-time sidecar** (the continuous monitor consuming Alpaca's `trade_updates` websocket and watching price streams for options-stop and guardrail-breach detection), a **moderately complex state machine** (the OMS) managing position and order lifecycle, a **heavy data integration layer** talking to many external APIs, and a **numerical computation component** that is meaningful but not HPC-scale.

It is **not** a low-latency trading system, a streaming data pipeline, or a microservices architecture — the layers are sequential stages in a single pipeline. Closest shape: a **sophisticated ETL/workflow system with LLM agents in the middle and an event-driven execution sidecar**.

---

## Four runtime profiles

### 1. The pipeline (batch, scheduled, 8-10x/day)

The core loop. Triggered on schedule, runs sequentially through 5 layers. Each invocation is a single pass — no long-lived state within a run, no loops spanning invocations.

```
Trigger
  → Data collection      (I/O bound: burst of API calls to external sources + DB read of internal state)
  → Distillation          (CPU bound: numerical computation — indicators, anomaly detection, normalization)
  → Analysis              (LLM-API bound: multiple parallel agent calls + sequential synthesizer)
  → Decision              (LLM-API bound: sequential agent calls with tool use)
  → Execution             (CPU + I/O: validation, DB writes, order submission)
```

The dominant bottleneck is **LLM API latency**. Layers 1-2 and 5 are fast programmatic work; layers 3-4 wait on LLM responses. Total wall-clock per invocation is mostly LLM inference wait.

### 2. The continuous monitor (real-time, always-on)

A **separate process** from the pipeline — runs 24/7 during market hours, subscribed to Alpaca's `trade_updates` websocket for fill events and to underlying equity streams for options bracket-stop and greeks-refresh triggers.

```
Alpaca trade_updates  → buffer fills for next pipeline collect
Underlying price feed → options stop trigger evaluation → close order via broker adapter
                      → 2%-move greek refresh trigger
                      → guardrail breach detection
```

An **event-driven, real-time system**. Low latency matters (trigger detection should happen near real-time). Maintains state (pending orders, bracket lifecycle for options, position greeks). Shares the DB with the pipeline at different times (pipeline reads during Phase 1; monitor writes continuously).

### 3. The scheduler (control plane)

Manages the invocation schedule: market-hours cadence (2h), off-hours cadence (4h), anchored runs (pre-open, pre-close), overlap deduplication. Needs market calendar awareness, time zones, and schedule conflict resolution.

### 4. Data ingestion infrastructure (I/O, rate-limited)

External data collection across 12+ quantitative and 6+ qualitative data categories for 60-80 tickers. Hundreds of API calls per invocation, each with rate limits, authentication, error handling, and varying refresh cadences. Some sources are websocket-based (monitor); some are REST (batch pulls at invocation time).

---

## Six architectural concerns

These cut across the runtime profiles.

### A. Pipeline orchestration

Sequencing layers, managing parallelism *within* layers (3 sector analysts + portfolio analyst + qualitative research run in parallel), handling partial failures (one API down), passing structured data between stages.

### B. LLM agent management

7-8 distinct LLM agents per invocation (3 sector analysts, portfolio analyst, qualitative researcher, adaptive researcher, synthesizer, trader, PM), each with its own context window, system prompt, and tool access. Some parallel, some sequential. The adaptive researcher has a nested agentic loop (tool use within tool use); decision layer agents have retrieval tools that fetch from a brief store. The heart of the system's complexity.

### C. State management

Multiple state lifecycles:

- **Portfolio state** (persistent, authoritative): positions, theses, orders, cash, P/L — the system's memory across invocations
- **Fill buffer** (ephemeral, accumulating): fills from the continuous monitor, drained each pipeline invocation
- **Brief store** (per-invocation, read-heavy): analysis briefs keyed by reference ID for decision layer retrieval
- **Distillation state** (rolling): baselines, regime classifications, composite signals — updated each invocation, persisted between

### D. Numerical computation

The distillation layer: technical indicators, statistical anomaly detection, normalization, volatility regime classification, portfolio exposure calculations, P/L attribution. Requires strong numerical libraries.

### E. External data integration

Dozens of integrations with heterogeneous APIs, authentication schemes, rate limits, data formats. Market data vendors, news APIs, prediction market APIs. Each needs adapters, error handling, potentially caching.

### F. Observability and auditability

Every trading decision must be traceable: PM approved → trader recommended → synthesizer highlighted → analyst flagged → distillation computed → raw data showed. Structured logging, brief archival, decision audit trails.
