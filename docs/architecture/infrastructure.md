# Infrastructure

Scheduling, deployment, observability, and process supervision.

---

## Scheduling

### Decision

**APScheduler v3** (`AsyncIOScheduler`) as an in-process scheduler in the pipeline process, sharing the asyncio event loop and firing triggers that invoke the pipeline's entry point.

### Schedule definition

Four scheduled trigger types (the **Tier B** cadence, ALP-745), all in US Eastern time. Every trigger fires at a distinct minute, so no two scheduled runs ever share a minute-truncated `as_of`:

| Trigger | Schedule | APScheduler trigger type |
|---------|----------|------------------------|
| Pre-open (anchored) | 9:00 AM ET weekdays | `CronTrigger` (hour=9, minute=0) |
| Market-hours rolling | 1:00 PM ET weekdays (single mid-day read) | `CronTrigger` (hour=13, minute=0) |
| Pre-close (anchored) | 3:30 PM ET weekdays | `CronTrigger` (hour=15, minute=30) |
| Weekend (reduced) | Sun 6:00 PM ET | `CronTrigger` (day_of_week=sun, hour=18) |

`off_hours_rolling` and `weekend_saturday` remain valid run types (enum members + overlays retained) but are deliberately unscheduled — real-time risk on open positions is owned by the continuous monitor, not by an intraday pipeline cadence.

**Overlap deduplication:** Under Tier B the schedule itself guarantees no two scheduled triggers fire in the same minute, so the prior collision (two passes racing on the distillation `UNIQUE(ticker, baseline_kind, as_of)` constraint) is structurally unreachable on the scheduled path. The 30-minute dedup window is retained as a backstop: each rolling trigger is tagged with a run type, and before firing the scheduler checks whether a run completed within the last 30 minutes — relevant now only around manual / emergency runs near a scheduled slot. APScheduler's `max_instances=1` is a safety net against concurrent pipeline executions.

**Market calendar:** Trading day detection via `exchange-calendars` or `pandas_market_calendars` for NYSE schedules, checked before each trigger fires.

### Why APScheduler v3

- Stable, actively maintained (v3.11.2, Dec 2025)
- `AsyncIOScheduler` integrates with the pipeline's asyncio loop
- `CronTrigger` with timezone support handles ET-anchored schedules natively
- Missed-fire coalescing: a process down during a scheduled run fires once on restart, not N times
- Battle-tested DST handling

### Why not APScheduler v4

v4 has been in alpha 3.5 years (4.0.0a1 Aug 2022 → 4.0.0a6 Apr 2025). The maintainer warns against production use; known issues include schedule disappearance (data integrity) and a shutdown bug with orphaned jobs. No beta, RC, or stable timeline.

---

## Deployment

### Decision

**Local Windows trading machine.** Both processes run on the operator's machine — no remote server, cloud VM, or containers. The command center ([command-center.md](../design/command-center.md)) joins as a third long-running process.

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

During development, both processes start manually from PowerShell. For unattended operation (paper trading evaluation), each process registers as an NSSM-managed Windows Service that starts on boot and restarts on failure.

### Why a local trading machine

Paper trading in validation: no uptime SLA, no external users, trivial resource requirements (< 200 MB RAM, < 1s CPU per invocation, modest network). Live trading would relocate to a dedicated server or VPS, gated on paper trading results.

### Process supervision (when running unattended)

NSSM wraps each long-running process — pipeline, continuous monitor, command center — as a Windows Service. Preferred over Task Scheduler (no supervisor with auto-restart) and `pywin32` Service Control Manager integration (forces service-control boilerplate into application code).

**Service configuration per process:**

- **Restart on failure.** `AppExit Default Restart` with a 60-second throttle so tight crash loops back off.
- **Start type.** Automatic (delayed start), after Windows finishes boot-time service work.
- **Log capture.** `AppStdout`/`AppStderr` redirect to `%USERPROFILE%\AlphaMind\logs\<service>.out.log` and `<service>.err.log`, alongside Python `logging` output in the same directory.
- **Service account.** Operator's user account (not LocalSystem) so `%USERPROFILE%` resolves correctly, `%USERPROFILE%\AlphaMind\data\` is reachable, and outbound network credentials are inherited.

Standard Windows tooling applies once registered — `services.msc`, `sc start/stop`, `nssm start/stop alphamind-pipeline`. Service transitions and crash-restart events go to the Windows Event Log.

NSSM is not needed during active development — both processes run in PowerShell windows with the same Python entry points.

---

## Observability

### Decision

Two complementary layers: **SQLite tables** for structured, queryable metrics and **a file-based invocation archive** for browsable agent outputs. No external monitoring stack.

### Layer 1: Structured metrics (SQLite)

The persistence layer's `invocations`, `process_lifetimes`, and `agent_calls` tables — specified in [state-persistence.md](../design/05-execution-layer/state-persistence.md) — capture every pipeline run, its runtime, and every LLM agent call within it. The invocation record carries trigger type, phase timestamps, fill collection summary, command execution summary, and provenance (active profile, regime, mode, overlays, resolved config hash and snapshot reference, git SHA, data source freshness) so the feedback loop can join past behavior to the exact composition that produced it. Per-agent metrics (model ID, prompt hash, token usage, wall-clock latency, success/error class) live in `agent_calls`, one row per invocation, joined via `invocation_id`.

Standard SQL: "how many invocations failed this week?", "average analysis phase duration?", "which run types produce the most OMS commands?", "did this prompt edit shift PM rejection rate?".

`activity_log` (portfolio state category 5) handles trade-level audit; `invocations` and `agent_calls` add pipeline-level and per-call audit on top.

### Layer 2: Invocation archive (files)

Each pipeline invocation writes its full agent output chain to disk as readable files:

```
%USERPROFILE%\AlphaMind\archive\
└── 2026-03-15/
    ├── 11-30-00_market_hours_rolling/
    │   ├── _summary.json              # Phase durations, status, commands issued
    │   ├── distillation/
    │   │   ├── tech_sector.md          # Distillation output as packaged for the tech researcher
    │   │   ├── financials_sector.md
    │   │   ├── energy_sector.md
    │   │   ├── portfolio_state.md
    │   │   └── regime.md               # Volatility regime classification
    │   ├── analysis/
    │   │   ├── tech_semis_brief.md     # Sector researcher output
    │   │   ├── financials_brief.md
    │   │   ├── energy_brief.md
    │   │   ├── qualitative_brief.md
    │   │   ├── adaptive_research.md    # Investigation threads and findings
    │   │   └── synthesis.md            # Synthesizer output (primary decision layer input)
    │   ├── decision/
    │   │   ├── analyst_recommendations.md
    │   │   ├── strategist_assessments.md
    │   │   └── pm_evaluation.md        # PM's reasoning + final OMS commands
    │   └── execution/
    │       └── commands.json           # Actual OMS commands submitted
    ├── 13-30-00_market_hours_rolling/
    │   └── ...
    └── 15-30-00_pre_close/
        └── ...
```

**The primary debugging and development tool.** During prompt iteration, walk the archive top-to-bottom: distillation produced → sector researcher interpreted → synthesizer highlighted → analyst proposed → PM decided. Markdown is readable, diffable, greppable.

**Retention:** Archive indefinitely during paper trading evaluation (a few MB per invocation, ~80 MB/day). Prune when disk space becomes a concern.

**Relationship to the brief store:** The SQLite brief store is the *live* store used by the retrieval tool during an invocation; the archive is a *post-hoc* copy written at end of invocation. Same content — DB for programmatic retrieval, archive for human browsing.

### Process logging

Operational health logging, separate from the invocation archive:

- `%USERPROFILE%\AlphaMind\logs\pipeline.log` — scheduler events, invocation start/end, errors, warnings
- `%USERPROFILE%\AlphaMind\logs\monitor.log` — websocket connection state, trigger detections, fill events

Python's `logging` module with `TimedRotatingFileHandler` (daily rotation), 30-day retention.

### Alerting

Not built initially. The pipeline logs errors and continues; if it crashes, NSSM restarts it and the next invocation catches up.

**Future:** Email or webhook alerts on pipeline failure, monitor websocket disconnect >15 minutes, guardrail breach, or daily P/L exceeding a threshold.

---

## Cost summary

| Item | Monthly cost |
|------|-------------|
| Claude Max 5x subscription | $100 |
| Market data APIs | $30-100 (Polygon Starter $29 + supplementary) |
| Compute (existing machine or small VPS) | $0-20 |
| **Total** | **~$130-220/month** |

Claude Max dominates. See [design/cost-and-rate-limit-modeling.md](../design/cost-and-rate-limit-modeling.md) for workload fit against the Max 5x throughput envelope and operating posture under cap pressure.
