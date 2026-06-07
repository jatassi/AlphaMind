# Component boundaries

Two processes sharing a database. No further decomposition.

---

## Decision

**Two processes:**

1. **The pipeline process** — invocation-driven. Wakes on schedule, runs the five-layer pipeline (data collection → distillation → analysis → decision → execution command submission), then exits or sleeps.

2. **The continuous monitor** — long-running. Watches real-time price feeds via websocket, resolves pending orders on trigger conditions, buffers fills for the next pipeline invocation's collect phase.

A shared database between them: pipeline writes orders and reads fills; monitor reads orders and writes fills. Portfolio state, distillation state, and brief storage also live there.

The **scheduler** is embedded in the pipeline process (in-process timer) or an OS-level cron — not a separate runtime. Scheduling logic (market-hours cadence, anchored runs, `max_instances=1` with emergency triggers pre-empting in-progress rolling invocations) is simple enough to live in-process. See [breach-behavior.md § Emergency invocation trigger](../design/06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) for the emergency-vs-rolling interaction contract.

---

## Rationale

### Why not a monolith?

The pipeline is **episodic** (3-10 minutes, 8-10x/day, then idles); the monitor is **always-on** (websocket connections, continuous trigger evaluation). In one process each lifecycle constrains the other. Separation also gives independent failure modes — a pipeline crash doesn't stop order resolution; a monitor restart doesn't disrupt the next invocation.

### Why not more than two processes?

No computational pressure to decompose further:

- **Pre-LLM pipeline** (data collection + distillation): 7-20s wall-clock, < 1s CPU, < 200 MB RAM. I/O-bound on external APIs.
- **LLM pipeline** (analysis + decision): 3-8 min wall-clock, near-zero local CPU. Parallelism via concurrent async API calls in one process.
- **Execution** (OMS commands): milliseconds.
- **Continuous monitor**: lightweight event loop watching 5-20 pending orders. Negligible CPU.

The system runs comfortably on 4 cores / 8 GB RAM. Splitting further adds operational complexity (IPC, deployment coordination, failure cascading) with no benefit.

### Why a shared database instead of IPC?

Processes interact at low-frequency boundaries: pipeline → monitor sends new orders (end of command execution, 8-10x/day); monitor → pipeline sends fills (start of fill collection, 8-10x/day). A natural write-rows / read-rows pattern, durable across either process's restart.

---

## Process interaction diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Pipeline Process                         │
│                                                              │
│  Scheduler → Data Collection → Distillation → Analysis →    │
│              Decision → Execution (OMS command submission)   │
│                                                              │
│  Fill collection: read fills     Command execution: write orders │
└──────────────┬──────────────────────────────┬────────────────┘
               │ read fills                   │ write orders
               ▼                              ▼
         ┌──────────────────────────────────────────┐
         │              Shared Database              │
         │                                           │
         │  portfolio state · orders · fills ·       │
         │  theses · distillation state · briefs     │
         └──────────────┬───────────────────────────┘
               │ read orders        │ write fills
               ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│                   Continuous Monitor                          │
│                                                              │
│  Websocket price feed → trigger detection → fill modeling → │
│  buffer fills to DB                                          │
│                                                              │
│  Runs 24/7. Watches pending orders. Resolves on trigger.    │
└─────────────────────────────────────────────────────────────┘
```

---

## Resource estimates

Per-invocation pipeline resource usage:

| Phase | Wall-clock | CPU | Memory | Bottleneck |
|-------|-----------|-----|--------|------------|
| Data collection | 5-15s | Negligible | ~50-100 MB | External API latency |
| Distillation | 0.5-3s | < 1s burst | ~50-100 MB | DB reads for trailing data |
| Analysis (LLM) | 1-4 min | Near zero | Minimal | LLM API latency |
| Decision (LLM) | 1-4 min | Near zero | Minimal | LLM API latency |
| Execution | < 1s | Negligible | Minimal | DB writes |
| **Total** | **~3-10 min** | **~1s actual CPU** | **~200 MB peak** | **LLM API latency** |

Continuous monitor: negligible CPU and memory, one persistent websocket connection.
