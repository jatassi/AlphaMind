# Monitoring invocations

Real-time and after-the-fact observation of pipeline invocations: SSE streams, the
structured log, the `invocations` table, archives, and the agent-watch patterns.

## 5. Monitoring scheduled invocations

**Important: production scheduled invocations do NOT emit a `progress.jsonl`
file.** The scheduler uses a no-op progress emitter in production; the
JSONL-stream variant is part of a separate harness that's not on the daemon
code path. Don't try to tail a file under the archive directory hoping to
see phase events — there is none. Use the surfaces below instead.

The right surface depends on whether you're watching in real time or
investigating after the fact, and whether you want a structured event stream
or free-form log records.

### 5.1 Real-time: the scheduler's `/events` SSE stream (canonical)

This is the primary live-monitoring surface for agents and operators —
**for daemon-driven invocations only** (scheduled fires + emergency
cascades). A manual `--once` CLI run is a separate process and does not
publish here; watch it via its process stdout / `pipeline.log` instead
(invocations.md § 3).

```bash
curl -N http://127.0.0.1:8765/events
```

Emits SSE frames for each of these event types:

| Event                   | When                                                     |
|-------------------------|----------------------------------------------------------|
| `invocation_started`    | A daemon-driven invocation begins (scheduled / emergency; NOT a manual `--once` CLI run) |
| `phase_transition`      | The orchestrator advances to a new pipeline phase        |
| `agent_started`         | An SDK call begins (analyst, strategist, PM, etc.)       |
| `agent_succeeded`       | The SDK call returned a valid response                   |
| `agent_retrying`        | Parse/validation failure; the harness is retrying        |
| `agent_failed`          | All retries exhausted; the invocation will abort         |
| `invocation_ended`      | The invocation reached a terminal state (success/failure)|
| `next_trigger_changed`  | APScheduler recomputed its next fire                     |
| `heartbeat`             | 15 s idle keep-alive                                     |

**For agents:** call this stream via the `Monitor` tool so each emitted SSE
frame becomes a notification. Filter with grep/jq for the events you care
about. Silence beyond 15 s indicates the SSE stream is wedged (scheduler
process crashed, network filter on the loopback interface, etc.) — not that
"nothing is happening." Heartbeats are the proof-of-life signal.

Cross-field invariants worth knowing:

- `phase_transition` is forward-only — an invocation never goes backward
  through phases.
- `agent_succeeded` and `agent_failed` are mutually exclusive per
  `(invocation_id, agent_name)` — exactly one fires per agent per
  invocation.
- `invocation_started` for an emergency-triggered invocation carries
  `run_type: emergency` and the `invocation_id` returned by the
  `trigger_emergency_invocation` control verb.

### 5.2 Real-time: the monitor's `/events` SSE stream

The continuous monitor publishes its own SSE stream on `127.0.0.1:8766`. In the
**current build** it emits fills plus an idle keep-alive:

```bash
curl -N http://127.0.0.1:8766/events
```

| Event           | When                                          |
|-----------------|-----------------------------------------------|
| `fill_received` | A fill arrived from the trade-updates stream  |
| `heartbeat`     | 15 s idle keep-alive                          |

> **`fill_received` does not fire in practice (observed 2026-06-12).** The emit
> *is* composed (the fill-stream consumer's and backfill task's enrichment
> callables are both wrapped, `continuous_monitor/control/wiring.py`), but the
> wrapper's guard silently skips unless `order_id` + `position_id` +
> `fill_price` + `fill_quantity` are all resolved on the record at that seam —
> and across six fills on 2026-06-12 (a subscriber attached the whole time)
> zero frames were published, while every fill persisted to `fill_records`
> within ~100 ms. Until that's fixed, do **not** rely on this stream to watch
> fills; use the § 5.9 watcher's `FILLS` leg (polls `fill_records`) or query
> `fill_records` directly. The fill-append log lines are DEBUG-level, so
> `monitor.log` silence proves nothing either — only the terminal order-status
> events log at INFO.

**What is *not* on this stream (and why).** The monitor's SSE emitter still
*defines* `websocket_connected` / `websocket_disconnected` / `breach_detected` /
`emergency_invocation_triggered` / `greeks_refreshed`, but their producers are
not composed into the running monitor, so none of them are emitted:

- **`breach_detected` / `emergency_invocation_triggered`** — breach detection +
  price-staleness moved out of the monitor into the isolated, **log-only safety
  core** (ADR-0004 / ALP-857). The safety core has no SSE surface and writes
  nothing; it logs to `safety_core.log` (troubleshooting.md § 8.9, its breach + price-staleness signals subsection). The monitor proper no longer
  runs a breach loop.
- **`greeks_refreshed`** — the greeks-refresh task runs, but its SSE emit adapter
  is not wired; confirm greeks via `monitor.log` / the per-position diagnostics.
- **`websocket_connected` / `websocket_disconnected`** — defined but never
  emitted anywhere in the current tree; track websocket health via
  `monitor.log` / `monitor.err.log` instead.

### 5.3 Real-time: the command center Live Run dashboard

`http://localhost:8090/` → log in → Live Run. The dashboard merges both
daemons' SSE streams + decorates them with activity-log rows and per-agent
status. Best surface when an operator is sitting at the console.

### 5.4 After-the-fact: the structured pipeline log

```powershell
Get-Content "$env:USERPROFILE\AlphaMind\logs\pipeline.log" -Tail 200
```

Per-line structured records. The `.log` file is the structured Python-logger
emit (one JSON-ish record per line including `invocation_id`, `phase`,
`agent`, and any exception with full traceback). The `.out.log` and
`.err.log` siblings are NSSM's raw stdout/stderr capture — useful only when
the daemon crashed before the structured logger could write.

To filter to a single invocation across the three log files:

```powershell
Select-String -Path "$env:USERPROFILE\AlphaMind\logs\pipeline.*.log" -Pattern "inv-20260527T143000Z-"
```

### 5.5 After-the-fact: the `invocations` DB table

Each invocation gets exactly one row recording its lifecycle:

```bash
uv run python -c "
import sqlite3, os
db = sqlite3.connect(os.path.expandvars(r'%USERPROFILE%\AlphaMind\data\alphamind.db'))
db.row_factory = sqlite3.Row
for row in db.execute('SELECT invocation_id, trigger_type, trigger_source, trigger_reason, start_at, fill_collection_completed_at, command_execution_completed_at, git_sha_at_invocation, staleness_flag FROM invocations ORDER BY start_at DESC LIMIT 20').fetchall():
    print(dict(row))
"
```

The table does **not** carry a single `status` column. Completion is read
from the phase timestamps: a row with a non-null `command_execution_completed_at`
succeeded through command execution; a row with `start_at` set but
`command_execution_completed_at` NULL either is still in-flight or aborted mid-pipeline
(cross-check the SSE stream / `pipeline.log`). Useful columns:
`trigger_type` (how the invocation was launched — `scheduled`, `manual`,
or `emergency`), `trigger_source` (the run-type / origin label — for
scheduled fires `market_open`, `market_hours_rolling`, `pre_close`,
`weekend_sunday`, or `off_hours_rolling`; `cli` for a
manual `--once` run; `operator_console` for a command-center action;
`continuous_monitor` for an emergency invocation),
`trigger_reason` (free-form), `fill_collection_completed_at` / `command_execution_completed_at`
(lifecycle), `git_sha_at_invocation` (the repo HEAD when the invocation
ran — useful for confirming which code version a run executed under),
`command_execution_summary_json` (PM command-submission detail),
`staleness_flag`, and `resolved_config_snapshot_path` (points into the
archive directory).

### 5.6 Per-invocation archive directory

Every invocation writes a directory under
`%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\` (UTC date,
not ET). The invocation ID format is `inv-YYYYMMDDTHHMMSSZ-<8-hex>`.

Contents include `resolved_config.json` (the full resolved config tree as it
existed for that invocation — useful when reconciling "what config did this
run actually use") and per-agent diagnostic subdirs under
`analysis/<agent>/` and `decision/<agent>/` (each carries the assembled
input bundle, the prompt sent to the SDK, the SDK response, and a
`metadata.json` with token + wall-clock + tool-call counts).

This is the most expensive surface to read but the highest fidelity. Use
it when § 5.4 / § 5.5 don't tell you why an agent produced a malformed
brief.

### 5.7 What to watch for

**Stalled agent.** An `agent_started` event with no matching
`agent_succeeded` / `agent_failed` after that agent's configured
`latency_budget_seconds` (in `config/run_types/<trigger>.yaml`). The orchestrator
will eventually time out and emit `agent_failed`, but if you're watching
live and the budget is generous, the stall is visible first as
unexplained silence in the SSE stream.

**Failed invocation.** An `invocation_ended` event reporting a non-success
terminal state. The `invocations` row has **no `status` or `exit_reason`
column** — a failed run is identified by `command_execution_completed_at` being NULL
(often `fill_collection_completed_at` too). Tail `pipeline.log` (and the daemon's
`pipeline.err.log`) for the traceback that names the failing layer.

**Thesis-resolution skip warnings (expected, not a fault).** Every deliberative
invocation runs a thesis-resolution step (`phase: thesis_resolution`) between
fill collection and snapshot assembly: it moves each closed-position thesis
`ACTIVE → RESOLVED`, authoring the component outcomes + resolution category the
snapshot's recent-resolutions feed and thesis-quality aggregates read. The step
resolves each eligible thesis **independently** — a per-thesis data gap logs a
`WARNING` in `pipeline.log` (naming the `thesis_id` + `position_id` + the
specific gap) and **skips** that thesis, leaving it `ACTIVE`, then continues
resolving the rest. It never aborts the invocation. A skip-warning is therefore
**expected operator-visible behavior, not an error** — most commonly an
option-lifecycle-closed thesis (expiry / assignment / exercise) that carries no
`POSITION_CLOSED` activity-log entry yet, so the exit method can't be read. The
skipped thesis stays `ACTIVE` and is retried next invocation. Investigate only
if the *same* thesis logs the skip on every invocation indefinitely (a stuck
thesis, not a transient gap).

**Skipped invocations.** When you expected a scheduled fire (per invocations.md § 4) and
no `invocation_started` event arrived:

- **NYSE holiday or early close** — weekday fires are NYSE-calendar-gated.
  No fire on a market holiday is expected, not a bug.
- **Daemon paused** — check the command center Controls panel. Pause flag
  doesn't survive a restart.
- **Dedup suppressed it** — the 30-minute dedup window suppresses a
  `market_hours_rolling` fire if some invocation completed phase 2 within the
  lookback (e.g., a manual or emergency run just before the 13:00 slot). Under
  Tier B no two scheduled triggers share a minute, so this only happens around
  off-schedule runs, never between two scheduled fires. The
  `next_trigger_changed` events on the SSE stream show what APScheduler
  thinks comes next.

**Emergency invocations.** In the current build these are **operator-initiated**,
not auto-cascaded from a breach. The expected sequence:

1. Operator: command center → Controls → Trigger emergency invocation (or
   `POST` the scheduler's `trigger_emergency_invocation` verb). This writes an
   `EMERGENCY_INVOCATION_REQUESTED` row to `activity_log`.
2. Scheduler: the `emergency_receiver` task polls `activity_log`, picks up the
   request, and — unless the 30-minute cooldown
   (`config/breach_behavior.yaml` § `emergency_invocation_cooldown_minutes`;
   a `margin_call` request bypasses it) suppresses it — dispatches one emergency
   invocation.
3. Scheduler `/events`: `invocation_started` with `run_type: emergency`, then the
   usual phase + agent events through `invocation_ended`. The `invocations` row
   records `trigger_type: emergency` / `trigger_source: continuous_monitor`.

If you requested an emergency and no `invocation_started` follows within a few
seconds, the cooldown gate suppressed it — check the cooldown window and the
recent `activity_log` rows for the prior emergency.

> **Auto breach→emergency cascade is not wired in this build.** The monitor no
> longer runs a breach loop, and the isolated safety core only logs + heartbeats
> and writes nothing (ADR-0004 / ALP-857), so nothing currently enqueues an
> `EMERGENCY_INVOCATION_REQUESTED` row on a breach. The auto-trigger evaluator
> still exists in the tree but is not composed into the running monitor — so a
> breach surfaces in `safety_core.log` (troubleshooting.md § 8.9, its breach + price-staleness signals subsection) and via the broker-enforced
> bracket floor, not as an automatic emergency invocation.

**Cost / token regression.** The `metadata.json` files under each agent's
diagnostic subdir record token counts under a `tokens_used` object
(`input_tokens` / `cache_read_tokens` / `cache_write_tokens` /
`output_tokens`). A healthy production run has the
bulk of input volume landing in `cache_read_tokens` (the prompt cache is
hit). If `input_tokens` is large and `cache_read_tokens` is near zero,
something invalidated the cache — investigate before the next invocation
amplifies the cost.

### 5.8 Agent monitoring pattern

The canonical pattern for an agent asked to "watch the next scheduled
invocation":

1. Look up the next fire time from invocations.md § 4's schedule table relative to current
   ET. Confirm via `next_trigger_changed` events on the SSE stream.
2. Open `curl -N http://127.0.0.1:8765/events` via the `Monitor` tool so SSE
   frames become notifications.
3. Wait for `invocation_started`. Record the `invocation_id`.
4. Watch for `phase_transition` → `agent_started` → `agent_succeeded` pairs
   covering every phase through `command_execution`.
5. Wait for `invocation_ended`. On success, summarize cost + wall-clock
   from the archive's per-agent `metadata.json` files. On failure, fetch
   the traceback from `pipeline.log` / `pipeline.err.log` — the
   `invocations` row carries no `exit_reason`; a NULL `command_execution_completed_at`
   is the failure signal.
6. Surface the `invocation_id` to the operator in any report — it's the
   entry point for follow-up archive inspection.

**SSE caveat (why § 5.9 exists).** In practice, consuming the § 5.1 SSE
stream through the `Monitor` tool has proven an unreliable per-agent
notifier — `agent_*` frames do not always surface as notifications — and a
manual `--once` run does not publish to the daemon stream at all (invocations.md § 3). For a
robust, granular per-agent watch, and the *only* live option for manual runs,
prefer the archive-based pattern in § 5.9.

### 5.9 Granular agent-completion watch via the archive (Monitor tool)

Each analysis / decision agent writes its diagnostic subdir
(`analysis/<agent>/` or `decision/<agent>/`) **atomically on completion**, so
the appearance — or mtime change, on a retry — of that agent's
`metadata.json` is a high-fidelity per-agent finish marker carrying
`success`, `stop_reason`, `wall_clock_seconds`, `retry_count`, and the
token / cache counts. Unlike the SSE stream this works identically for
scheduled and manual `--once` runs and reads straight off the WAL DB +
filesystem.

The canonical watcher is **`scripts/ops/watch_invocation.sh`** — pass it to the
`Monitor` tool and it streams one notification per milestone, self-terminating on
a terminal state (including one that already happened *before* it attached).
Select the invocation one of three ways:

```bash
# newest scheduled fire of a run-type (default lookback = now-5min):
bash scripts/ops/watch_invocation.sh --scheduled market_hours_rolling
# a manual --once run, by a substring of its --reason:
bash scripts/ops/watch_invocation.sh --reason "my run reason"
# attach to a specific invocation id:
bash scripts/ops/watch_invocation.sh --inv inv-20260602T170000Z-193173de
```

Optional `--since <ISO8601Z>` widens/narrows the discovery window; `--tz-offset
<h>` (default `6` = MDT) sets the prod-log local offset used to scope abort lines
(pass `7` for MST in winter). Paths are overridable via the `DB` / `LOG` / `ERR` /
`ARCHROOT` env vars (prod defaults are baked in).

It emits, as they happen: `FILL-COLLECTION-COMPLETE` (ingestion + fill-collection summary);
`ACT …` for every `activity_log` row of the run. A `RECONCILIATION_ALERT` row now
means only an **orphan fill** that could not integrate (a poison-pill fill against a
terminal/over-filled position, ALP-761) — the reconcile-adjudication path is deleted
(ALP-854 / ADR-0001), so a positions/cash mismatch is silently rebuilt, never alerted.
The watch also emits all command-execution actions (`ORDER_SUBMITTED` / `PM_DECISION` /
`CAPITAL_RESERVED` / …);
`DISTILLATION` when `composite_state` is written; `AGENT <layer>/<name> success=…
wall=… stop=…` per agent (the strategist's line is prefixed `>>> STRATEGIST`);
`FILLS` on each `unprocessed`→`processed`/`quarantined` transition; `FAULT(log|err)`
on a fresh non-benign traceback; and the terminal `COMMAND-EXECUTION-COMPLETE` (+ a final
positions / cash / fills dump) or `ABORT`. It is **silent while waiting** — no
routine "still waiting" ticks — except a one-shot `WARN scheduler port 8765 DOWN`.

**Why it covers "silence ≠ success."** A healthy run is proven only by
`COMMAND-EXECUTION-COMPLETE` (the sole positive success signal — there is no `status` column,
§ 5.5). A hard crash writes **no** `metadata.json`, so the fault/abort legs catch
what the per-agent leg can't: fresh `Traceback`/`CRITICAL`/`ERROR` in
`pipeline.log` + `pipeline.err.log` (with the benign Windows asyncio-teardown noise
— `ProactorBasePipeTransport`, `WinError 121`, `no close frame`, `ResourceWarning`
— filtered out), plus the driver's `scheduled trigger=<rt> failed` /
`scheduler exited with error`. And the **at-attach terminal check** handles a run
that already finished or aborted before the watch started: it checks
`command_execution_completed_at` and greps the log (scoped to this run's own start + trigger)
once up front, emitting `ALREADY-COMPLETE` / `ALREADY-ABORTED` and exiting instead
of polling a dead invocation. (Before this check, a watcher armed after a fast-fail
abort polled a dead 17:00Z run for 20 min — 2026-06-02.)

Gotchas baked into the script (worth knowing when reading its output):

- Some agents (`synthesizer`, `strategist`) record `tool_calls_used` instead of
  `retry_count`; a missing `retry` field is benign, not a failure.
- The `FILLS unprocessed N→M` leg confirms fill-collection ingestion before analysis even
  starts — how both the 2026-06-01 poison-pill wedge and the 2026-06-02 ALP-824
  cross-writer fix were verified.
- A manual `--once` run does not publish to SSE (§ 5.1), and its
  `scheduler exited with error` may land only in the launching shell's stderr (not
  `pipeline.log`) — so for a manual run the **launching process's exit code** stays
  the authoritative terminal signal; the watcher is the live milestone view.
- Set the `Monitor` `timeout` generously (or `persistent: true`) — a full-roster
  run takes ~25–50 min (invocations.md § 4); the watch exits itself on the terminal state.
- The ACT field separator is a TAB, not a control character: the sqlite3 CLI
  (≥ 3.49; prod runs 3.51) escapes control characters on output, so `char(31)`
  arrives as the literal text `^_` — the field split silently failed and every
  ACT line emitted empty until 2026-06-10. The same CLI emits CRLF; `q()` strips
  the `\r` (command substitution hides this, `while read` pipelines don't).
- The fault legs filter benign noise **per traceback block**, not per line: the
  SDK-subprocess teardown noise (`_ProactorBasePipeTransport.__del__` →
  `ValueError: I/O operation on closed pipe`, after every agent completion) puts
  its benign marker and its bare `Traceback` header on separate lines, so a
  per-line filter leaks a spurious FAULT pair per agent. A real traceback emits
  one line: `FAULT(log|err) Traceback … => <final exception line>`.

