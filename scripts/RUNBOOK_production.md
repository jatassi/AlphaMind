# Production Operations Runbook

The canonical operator workflow for AlphaMind in production — on the Windows
trading machine, against the live `alphamind.db` and paper Alpaca account.

This runbook covers the day-to-day operating loop, the one-time first-run
bootstrap, manual invocations, scheduled invocations, how to monitor those
invocations, command-center access, and service restarts. It assumes the
four NSSM services are the supported runtime form on prod:

| Service                  | Module entry                                    | Log basename       |
|--------------------------|--------------------------------------------------|--------------------|
| `alphamind-collector`    | `python -m alphamind.collector run`              | `collector.*.log`  |
| `alphamind-scheduler`    | `python -m alphamind.scheduler run`              | `pipeline.*.log`   |
| `alphamind-monitor`      | `python -m alphamind.execution.continuous_monitor run` | `monitor.*.log`    |
| `AlphaMindCommandCenter` | `python -m alphamind.command_center`             | `command_center.*.log` |

Logs land under `%USERPROFILE%\AlphaMind\logs\`. DB is
`%USERPROFILE%\AlphaMind\data\alphamind.db`. Invocation archives land under
`%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\`.

`RUNBOOK_command_center.md` is the sibling runbook for passkey registration,
credential recovery, and alert-engine configuration. This runbook is
otherwise self-contained.

> **Living document — agents, keep it current.** If you follow a procedure
> here and it's wrong, outdated, or doesn't match actual prod behavior,
> update this runbook in the same change that produces the fix. Don't paper
> over a broken step with an ad-hoc workaround — make the runbook reflect
> reality. New monitoring patterns, new gotchas, new failure modes, schedule
> changes, port changes — they all belong here so the next operator (human
> or agent) inherits the lesson.

---

## 1. The standard update loop

**Run this every time you touch prod, even for "I just want to restart the
monitor" tasks.** The order is non-negotiable: a migration applied while a
service is reading the schema corrupts the SQLAlchemy metadata cache, and a
service started before its migrations are in place crashes at first DB read.

From the repo root in PowerShell (elevated — NSSM control needs admin):

### 1.1 Pull main

```powershell
cd $env:USERPROFILE\AlphaMind        # the prod checkout (NSSM AppDirectory points here)
git fetch origin
git status                          # MUST be on `main` and clean
git pull --ff-only origin main
```

If `git status` shows you on a feature branch or with uncommitted changes,
stop — investigate before pulling. Prod runs `main`, full stop.

### 1.2 Sync dependencies

```powershell
uv sync
```

Idempotent; only re-resolves when `uv.lock` changed. If the frontend changed
(check `git log --oneline -5 -- src/alphamind/command_center/frontend/`),
also:

```powershell
cd src\alphamind\command_center\frontend
bun install --frozen-lockfile
bun run build
cd ..\..\..\..\..
```

### 1.3 Check for pending migrations

```powershell
uv run alembic current     # what's applied to the DB
uv run alembic heads       # what the codebase expects
```

If the two revisions match, **skip to § 1.6**. Nothing to migrate; just
restart any services whose code changed.

### 1.4 Stop services in reverse dependency order

The command center depends on scheduler + monitor; stop it first so its
loopback consumers don't reconnect mid-migration. Collector is independent of
the trading DB schema but stop it too to keep the snapshot quiet during the
migration.

```powershell
nssm stop AlphaMindCommandCenter
nssm stop alphamind-monitor
nssm stop alphamind-scheduler
nssm stop alphamind-collector
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, AlphaMindCommandCenter
```

All four should report `Stopped`. NSSM's stop budget is 30 s per service; if
one hangs in `StopPending`, see § 8.3.

### 1.5 Apply migrations

```powershell
uv run alembic upgrade head
uv run alembic current     # confirm now equals heads
```

If the upgrade fails, **do not restart services.** Read the traceback,
restore the DB from the most recent backup (see `RUNBOOK_command_center.md`
§ Recovery → DB corruption restore), then re-apply.

### 1.6 Restart services in dependency order

```powershell
nssm start alphamind-collector
nssm start alphamind-scheduler
nssm start alphamind-monitor
nssm start AlphaMindCommandCenter
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, AlphaMindCommandCenter
```

All four should report `Running` within ~60 s (NSSM's `SERVICE_DELAYED_AUTO_START`
+ the supervisors' own startup time). If any stays in `StartPending` past
that, tail its `.err.log` immediately.

### 1.7 Verify green

```powershell
# Tail the recent end of each daemon's log; look for "startup complete" /
# "supervisor started" / equivalent. No traceback should appear in the last
# 60 s of any .err.log.
Get-Content "$env:USERPROFILE\AlphaMind\logs\pipeline.out.log" -Tail 30
Get-Content "$env:USERPROFILE\AlphaMind\logs\monitor.out.log" -Tail 30
Get-Content "$env:USERPROFILE\AlphaMind\logs\command_center.out.log" -Tail 30
```

Optional gate (runs in ~1 min, exercises 7 command-center checks):

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify_command_center.py
```

---

## 2. First-time bootstrap

**Run § 2 exactly once, the very first time AlphaMind comes up on the
production machine.** Subsequent operations all go through § 1.

The collector is already running on this box (it was installed first to
accumulate the months of distillation-calibration data the analysis layer
needs). Steps 2.5–2.6 below assume that and skip re-installing it.

### 2.1 Populate `.env`

Copy `.env.example` to `.env` at the repo root, then fill in real values for
the vendor keys it lists. The template is incomplete — three additional keys
are required by prod but missing from `.env.example`. Append them after the
existing block:

```bash
# === Append to .env after the vendor-key block ===

# Required: Claude SDK OAuth token. Every pipeline + monitor invocation's
# SDK subprocess inherits this from the process environment. Generate via
# `claude setup-token` on a machine signed into the operator's Anthropic
# account, then copy the value here.
CLAUDE_CODE_OAUTH_TOKEN=

# Optional: Discord alert fanout. When unset, the command center logs
# "Discord alerts disabled" at startup and the in-app channel still works.
# Generate via Discord → Server Settings → Integrations → Webhooks → New
# Webhook. Treat the URL as a secret.
ALPHAMIND_DISCORD_WEBHOOK=

# Optional but strongly recommended: pinned session-cookie signing key.
# When unset, the command center mints a fresh key on every restart and
# every active operator session is invalidated. Generate a 32-byte
# base64 random value:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
# Placeholder is fine for first boot — the command center auto-generates
# an in-process secret if this is empty. Pin it before the first scheduled
# restart you care about.
COMMAND_CENTER_SESSION_SECRET=
```

`ALPACA_PAPER_KEY` and `ALPACA_PAPER_SECRET` (already present in the
template) must be populated with paper-account credentials from
`app.alpaca.markets/paper/dashboard/overview`. Live keys are deliberately
left blank — the `--fresh-start` argparse blocks `--mode live` and the
scheduler defaults `--mode paper` so no service can accidentally route to
the live endpoint.

**CRLF gotcha.** On Windows the editor will save `.env` with `\r\n` line
endings. Bare `source .env` under Git Bash leaves a trailing `\r` on every
value — `CLAUDE_CODE_OAUTH_TOKEN` then fails bearer auth several minutes into
a pipeline run. For any manual CLI invocation, source via:

```bash
set -a && source <(tr -d '\r' < .env) && set +a
```

NSSM-managed services bypass this — they read the env from
`AppEnvironmentExtra` directly, no shell sourcing involved.

### 2.2 Bring the DB to alembic head

```powershell
uv run alembic upgrade head
uv run alembic current      # confirm at heads
```

The collector has been writing into this DB for months; do not delete or
re-create it. Migrations are additive.

### 2.3 Build the command-center frontend

```powershell
cd src\alphamind\command_center\frontend
bun install --frozen-lockfile
bun run build
Test-Path .\dist\index.html       # must return True
cd ..\..\..\..\..
```

If `bun --version` reports anything older than 1.1.x, upgrade first — earlier
versions silently mutate `bun.lock` on `--frozen-lockfile`.

### 2.4 Cold-start bootstrap invocation

The paper-Alpaca account must be at zero positions with starting cash before
this runs. The bootstrap fetches Alpaca's reported cash, writes the
`cash_ledger` + `drawdown_state` singletons, and runs one `pre_open`
invocation. The invocation drives the full pipeline (Phase 1 → analysis →
decision → Phase 2 broker dispatch) — the PM's accepted commands must land
on Alpaca with real broker order ids, not synthetic `alp-{order_id}`
placeholders. Refuses to run if either singleton already exists.

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python -m alphamind.scheduler run \
        --fresh-start \
        --once pre_open \
        --reason "first-run bootstrap"
```

Expected: one full invocation followed by the process exiting 0. Both
singletons committed to the DB, and — if the PM produced any envelopes —
the corresponding orders + protective legs should appear on the Alpaca
side with real UUIDs (not synthetic `alp-…` placeholders). Wall-clock is
**~25–35 min** on a cold-cache cold-start — every SDK call pays first-fill
`cache_write` cost (no warm prompt cache), the deterministic distillation
step takes ~3–5 min against the full prod data layer, and at least one of
the Sonnet phases (`adaptive` is the usual culprit) typically dominates at
~6–8 min. This is meaningfully slower than the steady-state ~60–180 s
scheduled invocations document in § 4; budget accordingly and don't restart
the process if it looks "stuck" inside that window — run the e2e progress
monitor (`scripts/RUNBOOK_end_to_end_verification.md` § Monitoring
progress mid-run) against the invocation's archive to see live phase
transitions. Confirm:

```bash
uv run python -c "
import sqlite3, os
db = sqlite3.connect(os.path.expandvars(r'%USERPROFILE%\AlphaMind\data\alphamind.db'))
print('cash_ledger:', db.execute('SELECT current_cash_usd FROM cash_ledger').fetchone())
print('drawdown_state:', db.execute('SELECT equity_high_water_mark_usd FROM drawdown_state').fetchone())
print('order_count:', db.execute('SELECT COUNT(*) FROM orders').fetchone())
print('synthetic_id_count:', db.execute(\"SELECT COUNT(*) FROM orders WHERE alpaca_order_id LIKE 'alp-%'\").fetchone())
"
```

Both singleton rows should match Alpaca's reported cash on the freshly-reset
account to the cent. **`synthetic_id_count` must be `0`** — every persisted
order should carry a real Alpaca UUID. A non-zero count indicates broker
dispatch was bypassed (the invocation did not reach Alpaca); regression
checking should also confirm via `TradingClient.get_orders(status=ALL)`
that the same orders are visible on the Alpaca side. Pre-ALP-711 the
bootstrap silently persisted synthetic placeholders without ever calling
Alpaca; that class of regression is detectable here before completing the
bootstrap.

**Hard-fail paths.** `--fresh-start` refuses to run when:

- **Alpaca reports any open positions.** The error names the offending
  symbol(s). Reset the Alpaca account first.
- **`cash_ledger` already has a row.** The error includes the existing
  `current_cash_usd`. The auto-correct reconciliation path keeps drift on
  the existing row aligned with Alpaca on every subsequent invocation; a
  re-bootstrap is never the correct path once the singleton is populated.
- **`drawdown_state` already has a row.** Same shape, same recovery.

The CLI exits with code 2 (distinct from the generic scheduler-error code 1)
and prints the message verbatim to stderr — no traceback. Match on the
prefix `--fresh-start:` to filter precondition failures from scheduler
crashes.

**Recovery if the cold-start invocation itself fails after the singletons
committed.** The two rows are committed in their own transaction *before*
the invocation runs, so a Phase 1 error or transient external dependency
leaves them in place and a retry of `--fresh-start` will hard-fail. Two
options, in preference order:

1. **Re-run without `--fresh-start`.** The singletons are already populated
   correctly; the bootstrap step is no longer needed:

   ```bash
   uv run python -m alphamind.scheduler run \
       --once pre_open \
       --reason "retry after bootstrap"
   ```

2. **Wipe the singletons and re-bootstrap** — only if the persisted values
   are wrong (e.g., the Alpaca fetch returned a transient zero-cash
   mid-reset). On the prod machine:

   ```sql
   DELETE FROM drawdown_state;
   DELETE FROM cash_ledger;
   ```

   Then re-run the `--fresh-start --once pre_open --reason ...` form.

### 2.5 Install the three remaining NSSM services

Collector is already installed. Install the other three in dependency order
from an elevated PowerShell prompt at the repo root:

```powershell
.\scripts\install_pipeline_scheduler_service.ps1
nssm set alphamind-scheduler ObjectName .\<YourUsername>     # prompts for pw

.\scripts\install_monitor_service.ps1
nssm set alphamind-monitor ObjectName .\<YourUsername>

.\scripts\install_command_center_service.ps1
nssm set AlphaMindCommandCenter ObjectName .\<YourUsername>
```

Pin the command-center session secret on the service environment so restarts
don't kick operators out:

```powershell
$secret = python -c "import secrets; print(secrets.token_urlsafe(32))"
nssm set AlphaMindCommandCenter AppEnvironmentExtra "COMMAND_CENTER_SESSION_SECRET=$secret"
```

(Also copy the value into `.env`'s `COMMAND_CENTER_SESSION_SECRET=` line so
manual CLI invocations get the same key.)

### 2.6 Start the three new services

```powershell
nssm start alphamind-scheduler
nssm start alphamind-monitor
nssm start AlphaMindCommandCenter
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, AlphaMindCommandCenter
```

All four should be `Running`.

### 2.7 Register the first passkey

The command-center daemon mints a one-time setup token on first boot.
Retrieve it from the log:

```powershell
Get-Content "$env:USERPROFILE\AlphaMind\logs\command_center.out.log" -Tail 50 |
    Select-String "command_center setup token"
```

Open `http://127.0.0.1:8080/` in a browser on the Windows machine (RDP in if
remote), paste the token, choose a username, and complete WebAuthn
registration with Windows Hello or a hardware key. See
`RUNBOOK_command_center.md` § Register the first passkey for details and §
Adding a passkey for enrolling a backup authenticator.

### 2.8 Verify green

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify_command_center.py
```

Expect `7/7 checks passed`. AlphaMind is live in paper-trading mode.

---

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
`pre_open`, `market_hours_rolling`, `pre_close`, `off_hours_rolling`,
`weekend_saturday`, `weekend_sunday`, `emergency`.

The process exits when the invocation completes (~60–180 s steady-state in
prod; up to a few minutes longer on a cold cache or with the adaptive
researcher fully consumed). All artifacts land in the same archive layout
as scheduled runs — under
`%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\`.

**Do not pass `--fresh-start`.** That flag is reserved for the one-time
bootstrap in § 2.4; it hard-fails when the singletons already exist.

To watch the invocation live, use the same surfaces documented for scheduled
runs in § 5 — the structured `pipeline.log`, the scheduler's `/events` SSE
stream, and the command center's Live Run dashboard all work identically
whether the invocation came from the daemon or from `--once`.

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
| `pre_open`             | `0 9 * * mon-fri`          | 09:00 weekdays                            | NYSE-calendar-gated                          |
| `market_hours_rolling` | `0 13 * * mon-fri`         | 13:00 weekdays (single mid-day read)      | NYSE-gated; dedup-gated within 30 min        |
| `pre_close`            | `30 15 * * mon-fri`        | 15:30 weekdays                            | NYSE-gated; sole owner of the close slot     |
| `weekend_sunday`       | `0 18 * * sun`             | 18:00 Sunday                              | Unconditional                                |
| `off_hours_rolling`    | (unscheduled)              | Manual / emergency only                   | Valid run type; overlay retained, not on cron |
| `weekend_saturday`     | (unscheduled)              | Manual / emergency only                   | Valid run type; overlay retained, not on cron |
| `emergency`            | (not scheduled)            | On-demand, breach-cascade-triggered       | Cooldown-gated per `config/breach_behavior.yaml` |

~3 scheduled invocations per trading weekday (`pre_open` + `market_hours_rolling`
+ `pre_close`), ~16 per full trading week including the Sunday run. There is no
longer any same-minute collision for the dedup window to "collapse" — every slot
is distinct by construction. **Typical steady-state wall-clock per invocation is
~60–180 seconds** (longer when the adaptive researcher consumes its full budget;
shorter — ~25–35 s — for `emergency` invocations that bypass the analysis
layer).

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

---

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

This is the primary live-monitoring surface for agents and operators.

```bash
curl -N http://127.0.0.1:8765/events
```

Emits SSE frames for each of these event types:

| Event                   | When                                                     |
|-------------------------|----------------------------------------------------------|
| `invocation_started`    | A scheduled or manual invocation begins                  |
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

The continuous monitor publishes its own SSE stream covering
between-invocation activity — websocket health, fills, breaches, emergency
triggers, greeks refreshes.

```bash
curl -N http://127.0.0.1:8766/events
```

| Event                            | When                                                       |
|----------------------------------|------------------------------------------------------------|
| `websocket_connected`            | Alpaca trade-updates / underlying-stream websocket opens   |
| `websocket_disconnected`         | The websocket drops (with reason)                          |
| `fill_received`                  | A fill arrived from the trade-updates stream               |
| `breach_detected`                | A guardrail rule fired; classified `immediate` or `deferred` |
| `emergency_invocation_triggered` | A `breach_detected` classified `immediate` cascaded to an emergency invocation |
| `greeks_refreshed`               | A scheduled or move-triggered options-Greeks refresh ran   |

Watch this stream when you want to see what's happening *between*
invocations — fills landing, breaches firing, the bracket-stops watcher
closing options positions.

### 5.3 Real-time: the command center Live Run dashboard

`http://127.0.0.1:8080/` → log in → Live Run. The dashboard merges both
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
for row in db.execute('SELECT invocation_id, trigger_type, trigger_source, trigger_reason, start_at, phase1_completed_at, phase2_completed_at, git_sha_at_invocation, staleness_flag FROM invocations ORDER BY start_at DESC LIMIT 20').fetchall():
    print(dict(row))
"
```

The table does **not** carry a single `status` column. Completion is read
from the phase timestamps: a row with a non-null `phase2_completed_at`
succeeded through Phase 2; a row with `start_at` set but
`phase2_completed_at` NULL either is still in-flight or aborted mid-pipeline
(cross-check the SSE stream / `pipeline.log`). Useful columns:
`trigger_type` (the run-type — `pre_open`, `market_hours_rolling`, etc.),
`trigger_source` (`scheduled` / manual / `emergency_trigger`),
`trigger_reason` (free-form), `phase1_completed_at` / `phase2_completed_at`
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
`latency_budget_s` (in `config/run_types/<trigger>.yaml`). The orchestrator
will eventually time out and emit `agent_failed`, but if you're watching
live and the budget is generous, the stall is visible first as
unexplained silence in the SSE stream.

**Failed invocation.** An `invocation_ended` with `status: failed` (or
similar non-success). Tail `pipeline.log` for the traceback; the
`exit_reason` column on the `invocations` row names the failing layer in
one line.

**Skipped invocations.** When you expected a scheduled fire (per § 4) and
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

**Emergency invocation cascade.** The expected sequence:

1. Monitor: `breach_detected` with `response_classification: immediate`.
2. Monitor: `emergency_invocation_triggered` with the breach's `reason`.
3. Scheduler: `invocation_started` with `run_type: emergency` and
   `trigger_source: emergency_trigger`.

If you see (1) and (2) but not (3) within a few seconds, the cooldown gate
suppressed the cascade — check `config/breach_behavior.yaml`'s cooldown
window and the recent `activity_log` rows for the prior emergency.

**Cost / token regression.** The `metadata.json` files under each agent's
diagnostic subdir record `input_tokens` / `cache_read_tokens` /
`cache_write_tokens` / `output_tokens`. A healthy production run has the
bulk of input volume landing in `cache_read_tokens` (the prompt cache is
hit). If `input_tokens` is large and `cache_read_tokens` is near zero,
something invalidated the cache — investigate before the next invocation
amplifies the cost.

### 5.8 Agent monitoring pattern

The canonical pattern for an agent asked to "watch the next scheduled
invocation":

1. Look up the next fire time from § 4's schedule table relative to current
   ET. Confirm via `next_trigger_changed` events on the SSE stream.
2. Open `curl -N http://127.0.0.1:8765/events` via the `Monitor` tool so SSE
   frames become notifications.
3. Wait for `invocation_started`. Record the `invocation_id`.
4. Watch for `phase_transition` → `agent_started` → `agent_succeeded` pairs
   covering every phase through `phase2`.
5. Wait for `invocation_ended`. If `status: succeeded`, summarize cost +
   wall-clock from the archive's per-agent `metadata.json` files. If
   `status: failed`, fetch the traceback from `pipeline.log` and the
   `exit_reason` from the `invocations` row.
6. Surface the `invocation_id` to the operator in any report — it's the
   entry point for follow-up archive inspection.

---

## 6. Accessing the command center

`http://127.0.0.1:8080/` from a browser on the Windows trading machine
(loopback only in v1). Log in with your registered passkey.

To reach the UI from a remote workstation, RDP into the Windows machine and
open the browser there. Do **not** SSH-port-forward 8080 — the CSRF
double-submit check pins the cookie to the loopback origin and fails through
a forwarder. Do **not** bind the daemon to `0.0.0.0` — WebAuthn's relying
party ID is pinned to `127.0.0.1`; binding wider just exposes the API to the
LAN without auth working. Remote-access via VPS Caddy + WireGuard is
deferred (see `RUNBOOK_command_center.md` § Operator handover note).

The dashboard surfaces, in order of operator priority:

- **Controls** — pause/resume, trigger emergency invocation, switch profile,
  cancel order, force-close position, set halt mode.
- **Live run watcher** — current invocation's phase + agent SSE stream.
- **Run history** — last N invocations with status + duration + cost.
- **Activity log** — every `activity_log` row, filterable.
- **Portfolio / theses / risk** — current state snapshot.
- **Alerts** — active + acked + history.
- **Configs** — per-file editor + git diff view.

See `RUNBOOK_command_center.md` for passkey management, recovery, and the
alert engine config.

---

## 7. Restarting a single service

For any one service, in PowerShell as admin:

```powershell
nssm restart <ServiceName>
Get-Service <ServiceName>
```

The valid service names are:

- `alphamind-collector`
- `alphamind-scheduler`
- `alphamind-monitor`
- `AlphaMindCommandCenter` (note: mixed case, no hyphen)

NSSM's `restart` is `stop` + `start` with a 30 s budget per phase. If a stop
hangs in `StopPending` past that budget, the SCM force-terminates the
process. The supervisor's `TaskGroup` shutdown is co-operative — every
in-flight task gets a `CancelledError` and 10 s to wind down before the
SIGTERM fallback fires.

**When the operator can restart a single service without coordinating the
others:**

| Restart this        | When                                                          | Side effects                                                                      |
|---------------------|---------------------------------------------------------------|-----------------------------------------------------------------------------------|
| collector           | After a `config/collectors.yaml` edit                         | Up to one cadence-cycle of dropped vendor reads                                   |
| scheduler           | After a `config/scheduler.yaml`, `config/run_types/*.yaml`, or `config/main.yaml` edit | Pause flag cleared; an in-flight invocation is cancelled mid-pipeline             |
| monitor             | After a `config/continuous_monitor.yaml` or breach-rule edit  | Halt-mode flag persists; in-flight fill-stream reconnects from the recovery path |
| AlphaMindCommandCenter | After a frontend rebuild or alert-rule edit                | Active SSE clients reconnect; operator sessions persist if `COMMAND_CENTER_SESSION_SECRET` is pinned (§ 2.5), else are invalidated |

**When to use the full update loop (§ 1) instead of a single restart:**

- Any pull from main that touches `src/`, `pyproject.toml`, `uv.lock`, or a
  migration file. (Always run § 1 in this case — start by checking
  `git log --oneline ORIG_HEAD..` for migrations before assuming a single
  service restart suffices.)
- Anything you're uncertain about. The full loop is ~3 min on a clean run
  and idempotent — over-restarting costs nothing.

---

## 8. Common operational gotchas

### 8.1 Service won't start, `.err.log` mentions `NoSuchTableError`

Migrations are behind. Run § 1.3–1.5.

### 8.2 Command center returns 404 at `/`

Frontend bundle missing. Run § 2.3 (just the `bun install` + `bun run build`
part); no service restart needed — FastAPI's `StaticFiles` mount picks up
the new `dist/` on the next request.

### 8.3 NSSM stop hangs in `StopPending` past 30 s

The supervisor's `TaskGroup` is blocked on a task that won't cancel
gracefully. Force the issue:

```powershell
nssm kill <ServiceName>            # SIGKILL equivalent
Get-Service <ServiceName>          # should now be Stopped
```

Then investigate the hang in the `.out.log` / `.err.log` before restarting.
A regular hang here is a bug worth filing.

### 8.4 `gh pr checks` says CI is green but a service won't start

CI doesn't exercise the Windows service supervisors. A Linux-only behavior
in the merged code (POSIX `os.fsync` on a directory handle is the classic
example) can pass CI and crash on prod. Tail the failing service's
`.err.log` and file the issue against `main`.

### 8.5 Pipeline invocation aborts with a Claude SDK auth error 5 minutes in

`CLAUDE_CODE_OAUTH_TOKEN` is set but carries a trailing `\r` from a bare
`source .env` under Git Bash. Re-source via the `tr -d '\r'` form (§ 2.1
CRLF gotcha) before running manual invocations. Services bypass this — they
read the env from NSSM's `AppEnvironmentExtra` so they're unaffected.

### 8.6 Operator browser session invalidated after every restart

`COMMAND_CENTER_SESSION_SECRET` is not pinned on the service environment.
Run the `nssm set AlphaMindCommandCenter AppEnvironmentExtra ...` step from
§ 2.5. Existing sessions stay invalidated this one time; subsequent
restarts preserve them.

### 8.7 Looking for `progress.jsonl` under an invocation archive and not finding one

Expected — § 5 explains. Prod uses the no-op progress emitter; the JSONL
artifact is part of a different harness. Use the SSE stream (§ 5.1), the
structured pipeline log (§ 5.4), or the `invocations` table (§ 5.5)
instead.

### 8.8 Scheduled invocation didn't fire when § 4's schedule said it would

Walk § 5.7's "Skipped invocations" checklist: NYSE holiday, daemon paused,
or dedup suppressed it (only around an off-schedule run — under Tier B no two
scheduled triggers share a minute). The `next_trigger_changed` events on the
SSE stream are the source of truth for what the scheduler thinks comes next.

---

## 9. Reference: ports and paths

| What                         | Where                                                            |
|------------------------------|------------------------------------------------------------------|
| Scheduler `/control` + `/events` | `127.0.0.1:8765` (loopback only)                              |
| Monitor `/control` + `/events`   | `127.0.0.1:8766` (loopback only)                              |
| Command center API + UI          | `127.0.0.1:8080` (loopback only)                              |
| Production DB                    | `%USERPROFILE%\AlphaMind\data\alphamind.db`                   |
| Daemon logs                      | `%USERPROFILE%\AlphaMind\logs\<daemon>.{out,err,log}.log`     |
| Invocation archives              | `%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\` |
| Config files                     | `<repo>\config\*.yaml`, `<repo>\config\run_types\*.yaml`, `<repo>\config\profiles\*.yaml` |
| `.env`                           | `<repo>\.env`                                                  |
