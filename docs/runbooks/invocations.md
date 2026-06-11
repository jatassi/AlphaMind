# Running invocations

Manual (`--once`) invocations and the scheduler daemon's run-type schedule. Watching a
run in flight is [monitoring.md](monitoring.md).

## 3. Running a manual invocation

Use cases: ad-hoc smoke after a config edit, off-schedule analysis pass,
characterizing a specific run-type's behavior.

A manual invocation runs in your shell — it does NOT go through the
scheduler service and does NOT honor the schedule. The scheduler service can
keep running in parallel; the two processes share the DB but APScheduler's
overlap-dedup keys off scheduled run-types only, so a manual `--once` won't
collide with the daemon's scheduled fire.

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python -m alphamind.scheduler run \
        --once <run-type> \
        --reason "<short text>"
```

Where `<run-type>` is one of:
`market_open`, `market_hours_rolling`, `pre_close`, `off_hours_rolling`,
`weekend_saturday`, `weekend_sunday`, `emergency`.

The process exits when the invocation completes (~25–50 min steady-state in
prod — the full agent roster runs on every run-type; see § 4). All
artifacts land in the same archive layout as scheduled runs — under
`%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\`.

**agent_calls telemetry capture is active in production (ALP-907).** Every LLM
agent call (the 9 analysis + decision agents, run inside the SDK subprocess
worker) persists one `agent_calls` table row plus a four-file provenance tree
under
`%USERPROFILE%\AlphaMind\data\provenance\invocations\<invocation_id>\agent_calls\<agent_call_id>\`
(`system_prompt.md`, `output_schema.json`, `tools_definition.json`,
`output.json`; the row's `output_artifact_ref` points at that directory). The
worker opens one telemetry SQLite session per agent call and commits it before
returning. Telemetry persistence is **logged-not-fatal**: a capture/commit error
is logged (`agent_calls telemetry commit failed in subprocess worker` /
`agent_calls capture failed …`) and never fails the agent call or the
invocation. Delivery requires the `agent_calls` table to exist — the combined
feedback-loop migration (ALP-879) must be applied on the prod box first;
until then the insert silently no-ops (swallowed) and the table stays empty.

**Post-deploy spot-check** (after the migration is applied): run one `--once`
invocation, then confirm the table populated and the tree exists:

```bash
uv run python -c "import os, sqlite3; \
db = sqlite3.connect(os.path.expandvars(r'%USERPROFILE%\AlphaMind\data\alphamind.db')); \
print(db.execute('SELECT count(*), sum(success) FROM agent_calls').fetchone())"
```

```powershell
Get-ChildItem "$env:USERPROFILE\AlphaMind\data\provenance\invocations" -Recurse -Filter output.json | Select-Object -First 5
```

**Do not pass `--fresh-start`.** That flag is reserved for the one-time
bootstrap in bootstrap.md § 2.4; it hard-fails when the singletons already exist.

**A manual `--once` run is NOT observable on the SSE streams.** The
`--once` code path (`_run_once` in `src/alphamind/scheduler/__main__.py`)
runs the invocation in your shell as its own process; it never constructs
the `SSEEventEmitter` or the `/control` + `/events` HTTP server — those exist
only in the daemon path (`_run_daemon`). So while a manual run executes, the
scheduler daemon's `127.0.0.1:8765/events` stream (monitoring.md § 5.1) and the command
center's Live Run dashboard (monitoring.md § 5.3) — which consumes that same stream — show
only heartbeats. They reflect the daemon, and the daemon is idle; your run is
elsewhere. (Don't be fooled into thinking the run stalled: collect → distill
then SSE silence is exactly what a healthy out-of-process run looks like on
those surfaces.)

To watch a manual `--once` invocation live, use:

- **The process's own stdout/stderr** — the terminal you launched it in. This
  is the richest live view: per-phase `distillation.orchestrator` completions,
  per-agent `analysis.*.runner` lines, command execution dispatch, and the final exit
  code all stream here.
- **The structured `pipeline.log`** (monitoring.md § 5.4) — `_run_once` does call
  `configure_pipeline_logging()`, so records land in the rotating log
  alongside the daemon's (filter by the invocation id).
- **The `invocations` DB row** (monitoring.md § 5.5) and **per-invocation archive** (monitoring.md § 5.6),
  which are written identically to a scheduled run.

---

## 4. Scheduled invocations (the daemon's job)

Once `alphamind-scheduler` is running as an NSSM service, APScheduler drives
all scheduled invocations automatically against the NYSE calendar. The
production schedule (timezone `US/Eastern`, defined in
`config/scheduler.yaml`):

The schedule is **Tier B** (ALP-745): every trigger fires at a distinct
minute. The `as_of` is second-resolution and cron fires land on `:00`, so a
distinct minute means a distinct `as_of` — no two scheduled runs ever share
one (the condition that previously crashed one of two coinciding distillation
passes on the `distillation_ticker_baseline` UNIQUE constraint).

| Run type               | Cron (US/Eastern)          | Fires (ET)                                | Notes                                        |
|------------------------|----------------------------|-------------------------------------------|----------------------------------------------|
| `market_open`             | `35 9 * * mon-fri`         | 09:35 weekdays                            | NYSE-gated; fires just after the 09:30 open for fresh live quotes |
| `market_hours_rolling` | `0 13 * * mon-fri`         | 13:00 weekdays (single mid-day read)      | NYSE-gated; dedup-gated within 30 min        |
| `pre_close`            | `0 15 * * mon-fri`         | 15:00 weekdays                            | NYSE-gated; sole owner of the close slot     |
| `weekend_sunday`       | `0 18 * * sun`             | 18:00 Sunday                              | Unconditional                                |
| `off_hours_rolling`    | (unscheduled)              | Manual / emergency only                   | Valid run type; overlay retained, not on cron |
| `weekend_saturday`     | (unscheduled)              | Manual / emergency only                   | Valid run type; overlay retained, not on cron |
| `emergency`            | (not scheduled)            | On-demand, operator-triggered (command-center control → scheduler `trigger_emergency_invocation`) | Cooldown-gated per `config/breach_behavior.yaml`; auto breach-cascade is **not** wired in this build (monitoring.md § 5.7) |

~3 scheduled invocations per trading weekday (`market_open` + `market_hours_rolling`
+ `pre_close`), ~16 per full trading week including the Sunday run. There is no
longer any same-minute collision for the dedup window to "collapse" — every slot
is distinct by construction. **Typical steady-state wall-clock per invocation is
~25–50 minutes**: deterministic distillation takes ~5–6 min, and the 9 SDK agents
(~1–8 min each, domain researchers serial within their phase) dominate the rest;
the adaptive researcher consuming its full budget pushes a run toward the upper
band. Observed: 29.7 / 40.8 min `market_open`, ~49 min `market_hours_rolling`
(2026-06-09/10). The "~60–180 seconds" figure previously documented here
predates the full agent roster — a sub-5-minute completion now means the run
aborted early, not that it was fast.
An `emergency` invocation is **not** a fast path: it runs the full agent roster —
the same roster as `market_open`, with the heaviest adaptive-researcher budget
(`config/run_types/emergency.yaml`) — so it lands comparable to or slower than a
scheduled run.

The cadence and time windows live in `config/scheduler.yaml`. Per-trigger
agent budgets live in `config/run_types/*.yaml`. Edits to either take effect
on the next scheduler restart — APScheduler reads them at boot. To apply a
schedule change:

```powershell
nssm restart alphamind-scheduler
```

No DB migration, no data churn — the service restart re-reads configs and
re-arms the jobs.

**To pause scheduled invocations without taking the service down**, use the
command center's pause control (Dashboard → Controls → Pause), or hit the
loopback verb directly:

```bash
curl -X POST http://127.0.0.1:8765/control/pause \
    -H "Content-Type: application/json" \
    -d '{"reason":"operator pause via CLI"}'
```

Resume:

```bash
curl -X POST http://127.0.0.1:8765/control/resume \
    -H "Content-Type: application/json" \
    -d '{}'
```

Pause/resume mutates an in-process flag; APScheduler skips fires while
paused, and the flag survives the service running but does NOT survive a
restart (a restarted scheduler is unpaused by default). Use the
command-center UI for the audited path — it writes an `OPERATOR_CONSOLE` row
to `activity_log` for each pause/resume.

