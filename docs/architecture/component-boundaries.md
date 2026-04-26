# Component boundaries

Two processes sharing a database. No further decomposition.

---

## Decision

The system is split into **two processes**:

1. **The pipeline process** — invocation-driven. Wakes up on schedule, runs the five-layer pipeline (data collection → distillation → analysis → decision → execution command submission), then exits or sleeps until the next invocation.

2. **The continuous monitor** — long-running. Watches real-time price feeds via websocket, resolves pending orders as trigger conditions are met, buffers fills for the next pipeline invocation's collect phase.

A shared database sits between them: the pipeline writes orders and reads fills; the monitor reads orders and writes fills. Portfolio state, distillation state, and brief storage also live in this database.

The **scheduler** is not a separate process. It's either embedded in the pipeline process (in-process timer) or an OS-level cron that invokes the pipeline. The scheduling logic (market-hours cadence, anchored runs, `max_instances=1` enforcement with emergency triggers pre-empting any in-progress rolling invocation) is simple enough that it doesn't warrant its own runtime. See [breach-behavior.md § Emergency invocation trigger](../design/06-risk-guardrails/breach-behavior.md#emergency-invocation-trigger) for the emergency-vs-rolling interaction contract.

---

## Rationale

### Why not a monolith (single process)?

The pipeline and the continuous monitor have fundamentally different runtime characteristics:

- The pipeline is **episodic** — it runs for 3-10 minutes, 8-10 times per day, then has nothing to do.
- The monitor is **always-on** — it must maintain websocket connections and evaluate triggers continuously, even when no pipeline is running.

Coupling them in a single process means the pipeline's lifecycle is constrained by the monitor's need to stay alive (or vice versa). Separate processes let each run with the lifecycle that fits: the monitor starts at system boot and runs indefinitely; the pipeline is invoked on schedule and exits cleanly.

Separate processes also give independent failure modes: if the pipeline crashes mid-invocation, the monitor continues resolving orders. If the monitor loses its websocket connection and restarts, the pipeline's next invocation still runs on schedule.

### Why not more than two processes?

The resource analysis shows no computational pressure for further decomposition:

- **Pre-LLM pipeline** (data collection + distillation): 7-20 seconds wall-clock, < 1 second CPU, < 200 MB RAM. I/O-bound on external APIs. No reason to split data collection from distillation — they're sequential, fast, and share data in memory.
- **LLM pipeline** (analysis + decision): 3-8 minutes wall-clock, near-zero local CPU (waiting on LLM API responses). No reason to run agents in separate processes — parallelism is achieved via concurrent async API calls within a single process.
- **Execution** (OMS command processing): milliseconds. Negligible.
- **Continuous monitor**: lightweight event loop watching price feeds for 5-20 pending orders. Negligible CPU.

The entire system runs comfortably on a single modest machine (4 cores, 8 GB RAM) with overhead to spare. Splitting further would add operational complexity (IPC, deployment coordination, failure cascading) with no performance benefit.

### Why a shared database instead of IPC?

The pipeline and monitor interact at well-defined, low-frequency boundaries:

- Pipeline → monitor: "Here are new orders to watch" (at the end of Phase 2, 8-10x/day)
- Monitor → pipeline: "Here are fills that happened since last time" (at the start of Phase 1, 8-10x/day)

This is a natural database pattern: write rows, read rows. No need for message queues, RPC, or shared memory. The database also provides durability — if either process restarts, the other's state is safe.

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
