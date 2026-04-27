# Component boundaries

Two processes sharing a database. No further decomposition.

---

## Decision

The system is split into **two processes**:

1. **The pipeline process** — invocation-driven. Wakes on schedule, runs the five-layer pipeline (data collection → distillation → analysis → decision → execution command submission), then exits or sleeps until the next invocation.

2. **The continuous monitor** — long-running. Watches real-time price feeds via websocket, resolves pending orders as trigger conditions are met, buffers fills for the next pipeline invocation's collect phase.

A shared database sits between them: the pipeline writes orders and reads fills; the monitor reads orders and writes fills. Portfolio state, distillation state, and brief storage also live in this database.

The **scheduler** is embedded in the pipeline process (in-process timer) or an OS-level cron that invokes the pipeline — not a separate process. Scheduling logic (market-hours cadence, anchored runs, `max_instances=1` enforcement with emergency triggers pre-empting any in-progress rolling invocation) is simple enough not to warrant its own runtime. See [breach-behavior.md § Emergency invocation trigger](../design/06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) for the emergency-vs-rolling interaction contract.

---

## Rationale

### Why not a monolith (single process)?

The pipeline and the continuous monitor have different runtime characteristics:

- The pipeline is **episodic** — runs for 3-10 minutes, 8-10 times per day, then idles.
- The monitor is **always-on** — must maintain websocket connections and evaluate triggers continuously.

In a single process the pipeline's lifecycle is constrained by the monitor's need to stay alive (or vice versa). Separate processes let each run with the lifecycle that fits: the monitor starts at boot and runs indefinitely; the pipeline is invoked on schedule and exits cleanly.

Separate processes also give independent failure modes: if the pipeline crashes mid-invocation, the monitor continues resolving orders. If the monitor restarts, the pipeline's next invocation still runs on schedule.

### Why not more than two processes?

No computational pressure for further decomposition:

- **Pre-LLM pipeline** (data collection + distillation): 7-20s wall-clock, < 1s CPU, < 200 MB RAM. I/O-bound on external APIs. Sequential, fast, share data in memory.
- **LLM pipeline** (analysis + decision): 3-8 min wall-clock, near-zero local CPU (waiting on LLM API responses). Parallelism is achieved via concurrent async API calls within a single process.
- **Execution** (OMS command processing): milliseconds. Negligible.
- **Continuous monitor**: lightweight event loop watching price feeds for 5-20 pending orders. Negligible CPU.

The entire system runs comfortably on one modest machine (4 cores, 8 GB RAM). Splitting further would add operational complexity (IPC, deployment coordination, failure cascading) with no performance benefit.

### Why a shared database instead of IPC?

The pipeline and monitor interact at well-defined, low-frequency boundaries:

- Pipeline → monitor: new orders to watch (end of Phase 2, 8-10x/day)
- Monitor → pipeline: fills since last time (start of Phase 1, 8-10x/day)

A natural database pattern: write rows, read rows. No message queues, RPC, or shared memory. The database also provides durability — if either process restarts, the other's state is safe.

---

## Process interaction diagram

```
┌─────────────────────────────────────────────────────────────┐
│                     Pipeline Process                         │
│                                                              │
│  Scheduler → Data Collection → Distillation → Analysis →    │
│              Decision → Execution (OMS command submission)   │
│                                                              │
│  Phase 1: read fills from DB    Phase 2: write orders to DB │
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

## Resource estimates (for reference)

Per-invocation pipeline resource usage:

| Phase | Wall-clock | CPU | Memory | Bottleneck |
|-------|-----------|-----|--------|------------|
| Data collection | 5-15s | Negligible | ~50-100 MB | External API latency |
| Distillation | 0.5-3s | < 1s burst | ~50-100 MB | DB reads for trailing data |
| Analysis (LLM) | 1-4 min | Near zero | Minimal | LLM API latency |
| Decision (LLM) | 1-4 min | Near zero | Minimal | LLM API latency |
| Execution | < 1s | Negligible | Minimal | DB writes |
| **Total** | **~3-10 min** | **~1s actual CPU** | **~200 MB peak** | **LLM API latency** |

Continuous monitor: negligible CPU, negligible memory, one persistent websocket connection.
