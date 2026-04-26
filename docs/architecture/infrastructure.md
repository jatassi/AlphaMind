# Infrastructure

Scheduling, deployment, observability, and process supervision.

---

## Scheduling

### Decision

**APScheduler v3** (`AsyncIOScheduler`) as an in-process scheduler within the pipeline process. The scheduler runs in the same event loop as the pipeline, firing triggers that invoke the pipeline's main entry point.

### Schedule definition

The design spec defines five trigger types, all in US Eastern time:

| Trigger | Schedule | APScheduler trigger type |
|---------|----------|------------------------|
| Market-hours rolling | Every 2h during 9:30 AM – 4:00 PM ET | `CronTrigger` (hour='9,11,13,15', minute='30') |
| Off-hours rolling | Every 4h during 4:00 PM – 9:30 AM ET | `CronTrigger` (hour='20,0,4,8') |
| Pre-open (anchored) | 9:00 AM ET | `CronTrigger` (hour=9, minute=0) |
| Pre-close (anchored) | 3:30 PM ET | `CronTrigger` (hour=15, minute=30) |
| Weekend (reduced) | Every 6-8h Sat/Sun | `CronTrigger` with day_of_week filter |

**Overlap deduplication:** When an anchored run coincides with a rolling run (e.g., pre-close at 3:30 overlaps the 2h rolling cadence), the anchored run takes precedence. Implemented by tagging each trigger with a run type and checking whether a run of any type completed within the last 30 minutes before firing a rolling trigger. APScheduler's `max_instances=1` prevents concurrent pipeline executions as a safety net.

**Market calendar:** Trading day detection (skip market holidays) via a lightweight calendar check before each trigger fires. The `exchange-calendars` or `pandas_market_calendars` library provides NYSE holiday schedules.

### Why APScheduler v3

- Stable, actively maintained (v3.11.2, Dec 2025)
- `AsyncIOScheduler` integrates with the pipeline's asyncio event loop
- `CronTrigger` with timezone support handles the ET-anchored schedule natively
- Missed-fire handling and coalescing for crash recovery (if the process was down during a scheduled run, it fires once on restart rather than queuing N missed runs)
- Battle-tested DST transition handling (v3.11.2 specifically fixes a DST edge case)

### Why not APScheduler v4

v4 has been in alpha for 3.5 years (4.0.0a1 Aug 2022 → 4.0.0a6 Apr 2025). The maintainer explicitly warns against production use. Known issues include schedule disappearance (data integrity) and a shutdown bug with orphaned jobs. No beta or RC in sight, no published timeline for stable release. v3 is the right choice.

---

## Deployment

### Decision

**Local Windows trading machine.** Both processes run directly on the operator's Windows trading machine. No remote server, no cloud VM, no containers. The command center (Phase 4, [command-center.md](../design/command-center.md)) joins as a third long-running process on the same machine.

### Process layout

```
Trading machine (Windows)
├── alphamind-pipeline     (NSSM-managed Windows Service)
│   ├── APScheduler         (in-process, fires triggers)
│   └── Pipeline execution   (data → distillation → analysis → decision → execution)
│
├── alphamind-monitor      (NSSM-managed Windows Service)
│   ├── Websocket client    (price feed connection)
│   └── Order resolution    (trigger detection, fill buffering)
│
└── %USERPROFILE%\AlphaMind\data\
    └── alphamind.db        (SQLite, shared)
```

During development, both processes are started manually from a PowerShell session. For unattended operation (paper trading evaluation runs), each process is registered as an NSSM-managed Windows Service that starts on boot and restarts on failure.

### Why a local trading machine

This is a paper trading system in its validation phase. There's no uptime SLA, no external users, and no reason to pay for or manage remote infrastructure. The resource requirements are trivial (< 200 MB RAM, < 1 second CPU per invocation, modest network). If the system demonstrates profitability and moves toward live trading, deployment to a dedicated server or VPS becomes relevant — but that's a future decision gated on paper trading results.

### Process supervision (when running unattended)

NSSM (the Non-Sucking Service Manager) wraps each long-running process — pipeline, continuous monitor, and command center — as a Windows Service. NSSM is preferred over Task Scheduler (designed for one-shot or periodic commands; lacks a supervisor model with auto-restart) and over implementing the Service Control Manager interface natively via `pywin32` (forces service-control boilerplate into application code; NSSM keeps the same lifecycle external to the app).

**Service configuration per process:**

- **Restart on failure.** `AppExit Default Restart` with a throttle delay of 60 seconds, so a tight crash loop backs off rather than burning CPU.
- **Start type.** Automatic (delayed start) so services come up after Windows finishes its own boot-time service work.
- **Log capture.** `AppStdout` and `AppStderr` redirect each process's stdout/stderr to `%USERPROFILE%\AlphaMind\logs\<service>.out.log` and `<service>.err.log`, alongside the application logs that the Python `logging` module writes to the same directory.
- **Service account.** Each service runs under the operator's user account (not LocalSystem) so that `%USERPROFILE%` resolves to the operator's profile, the SQLite database under `%USERPROFILE%\AlphaMind\data\` is reachable, and the service inherits the operator's outbound network credentials.

Standard Windows tooling applies once registered — `services.msc`, `sc start/stop`, or `nssm start/stop alphamind-pipeline`. Service start/stop transitions and crash-restart events are written to the Windows Event Log automatically.

During active development, NSSM is not needed — both processes run in PowerShell windows, with the same Python entry points the services would call.

---

## Observability

### Decision

Two complementary layers: **SQLite tables** for structured, queryable metrics and **a file-based invocation archive** for browsable agent outputs. No external monitoring stack.

### Layer 1: Structured metrics (SQLite)

The persistence layer's `invocations`, `process_lifetimes`, and `agent_calls` tables — fully specified in [state-persistence.md](../design/05-execution-layer/state-persistence.md) — capture queryable data about every pipeline run, the runtime that produced it, and every LLM agent call within it. The invocation record carries the trigger type, phase timestamps, fill collection summary, command execution summary, and the provenance fields (active profile, regime, mode, overlays, resolved config hash and snapshot reference, git SHA, data source freshness) that let the feedback loop join past behavior to the exact composition that produced it. Per-agent metrics (model ID, prompt hash, token usage, wall-clock latency, success/error class) live in `agent_calls`, one row per agent invocation, joined to the parent `invocations` row via `invocation_id`.

This is the layer you query: "how many invocations failed this week?", "what's the average analysis phase duration?", "which run types produce the most OMS commands?", "did this prompt edit shift our PM rejection rate?". Standard SQL, no special tooling needed.

The existing `activity_log` table (portfolio state category 5) handles trade-level audit — every state-changing event with timestamps. The `invocations` and `agent_calls` tables add pipeline-level and per-call audit on top.

### Layer 2: Invocation archive (files)

Each pipeline invocation writes its full agent output chain to disk as readable files:

```
%USERPROFILE%\AlphaMind\archive\
└── 2026-03-15/
    ├── 11-30-00_market_hours_rolling/
    │   ├── _summary.json              # Phase durations, status, commands issued
    │   ├── distillation/
    │   │   ├── tech_sector.md          # Distillation output as packaged for the tech analyst
    │   │   ├── financials_sector.md
    │   │   ├── energy_sector.md
    │   │   ├── portfolio_state.md
    │   │   └── regime.md               # Volatility regime classification
    │   ├── analysis/
    │   │   ├── tech_semis_brief.md     # Sector analyst output
    │   │   ├── financials_brief.md
    │   │   ├── energy_brief.md
    │   │   ├── portfolio_brief.md
    │   │   ├── qualitative_brief.md
    │   │   ├── adaptive_research.md    # Investigation threads and findings
    │   │   └── synthesis.md            # Synthesizer output (primary decision layer input)
    │   ├── decision/
    │   │   ├── trader_recommendations.md
    │   │   └── pm_evaluation.md        # PM's reasoning + final OMS commands
    │   └── execution/
    │       └── commands.json           # Actual OMS commands submitted
    ├── 13-30-00_market_hours_rolling/
    │   └── ...
    └── 15-30-00_pre_close/
        └── ...
```

**This is the primary debugging and development tool.** During prompt iteration, you open the archive directory in your editor and read through the chain: what did the distillation layer produce → what did the analyst make of it → what did the synthesizer highlight → what did the trader propose → what did the PM decide. Markdown files are readable, diffable, and greppable.

**Retention:** Archive indefinitely during paper trading evaluation (total volume is modest — a few MB of text per invocation, ~80 MB/day). Prune older archives when disk space becomes a concern.

**Relationship to the brief store:** The brief store in SQLite (from the data-and-state doc) is the *live* store used by the retrieval tool during an invocation. The archive is a *post-hoc* copy written at the end of each invocation. Both contain the same content; the archive is optimized for human browsing, the DB store for programmatic retrieval.

### Process logging

Separate from the invocation archive, standard process-level logging for operational health:

- `%USERPROFILE%\AlphaMind\logs\pipeline.log` — scheduler events, invocation start/end, errors, warnings
- `%USERPROFILE%\AlphaMind\logs\monitor.log` — websocket connection state, trigger detections, fill events

Python's `logging` module with `TimedRotatingFileHandler` (daily rotation). Retention: 30 days. These are operational logs, not analytical artifacts — you read them when something goes wrong, not routinely.

### Alerting

Not built initially. The pipeline logs errors and continues. If it crashes, NSSM restarts it and the next invocation runs normally — missed invocations are logged but not critical (the system is designed for the next run to catch up).

**Future:** Simple alerting (email or webhook) on: pipeline failure, monitor websocket disconnect >15 minutes, guardrail breach, or daily P/L exceeding a threshold. Easy to add later, no architectural decisions needed now.

---

## Cost summary

| Item | Monthly cost |
|------|-------------|
| Claude Max 5x subscription | $100 |
| Market data APIs | $30-100 (Polygon Starter $29 + supplementary) |
| Compute (existing machine or small VPS) | $0-20 |
| **Total** | **~$130-220/month** |

The dominant cost is the Claude Max subscription. Everything else is incidental. See [design/cost-and-rate-limit-modeling.md](../design/cost-and-rate-limit-modeling.md) for the workload fit against the Max 5x throughput envelope and the operating posture under cap pressure.
