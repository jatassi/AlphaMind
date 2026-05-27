# Production Operations Runbook

The canonical operator workflow for AlphaMind in production — on the Windows
trading machine, against the live `alphamind.db` and paper Alpaca account.

This runbook covers the day-to-day operating loop, the one-time first-run
bootstrap, manual invocations, scheduled invocations, command-center access,
and service restarts. It assumes the four NSSM services are the supported
runtime form on prod:

| Service                  | Module entry                                    | Log basename       |
|--------------------------|--------------------------------------------------|--------------------|
| `alphamind-collector`    | `python -m alphamind.collector run`              | `collector.*.log`  |
| `alphamind-scheduler`    | `python -m alphamind.scheduler run`              | `pipeline.*.log`   |
| `alphamind-monitor`      | `python -m alphamind.execution.continuous_monitor run` | `monitor.*.log`    |
| `AlphaMindCommandCenter` | `python -m alphamind.command_center`             | `command_center.*.log` |

Logs land under `%USERPROFILE%\AlphaMind\logs\`. DB is
`%USERPROFILE%\AlphaMind\data\alphamind.db`.

Related runbooks:
- `RUNBOOK_command_center.md` — passkey registration, recovery, alert config.
- `RUNBOOK_end_to_end_verification.md` — debug-e2e harness, `--fresh-start`
  semantics, failure triage.

---

## 1. The standard update loop

**Run this every time you touch prod, even for "I just want to restart the
monitor" tasks.** The order is non-negotiable: a migration applied while a
service is reading the schema corrupts the SQLAlchemy metadata cache, and a
service started before its migrations are in place crashes at first DB read.

From the repo root in PowerShell (elevated — NSSM control needs admin):

### 1.1 Pull main

```powershell
cd $env:USERPROFILE\Git\AlphaMind   # or wherever the prod checkout lives
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
one hangs in `StopPending`, see § 7.3.

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
invocation to validate the pipeline composes end-to-end against the freshly
seeded state. Refuses to run if either singleton already exists.

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python -m alphamind.scheduler run \
        --fresh-start \
        --once pre_open \
        --reason "first-run bootstrap"
```

Expected: one full invocation (5–15 min wall-clock) followed by the process
exiting 0. Both singletons committed to the DB. Confirm:

```bash
uv run python -c "
import sqlite3, os
db = sqlite3.connect(os.path.expandvars(r'%USERPROFILE%\AlphaMind\data\alphamind.db'))
print('cash_ledger:', db.execute('SELECT current_cash_usd FROM cash_ledger').fetchone())
print('drawdown_state:', db.execute('SELECT equity_high_water_mark_usd FROM drawdown_state').fetchone())
"
```

Both rows should match Alpaca's reported cash. See
`RUNBOOK_end_to_end_verification.md` § `--fresh-start` — production
cold-start for hard-fail paths and recovery.

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
debugging a specific run-type's behavior.

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

The process exits when the invocation completes (5–15 min typical). All
artifacts land in the same archive layout as scheduled runs — under
`%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\`.

**Do not pass `--fresh-start`.** That flag is reserved for the one-time
bootstrap in § 2.4; it hard-fails when the singletons already exist.

To watch the invocation live, in a second shell tail the progress JSONL —
see `RUNBOOK_end_to_end_verification.md` § Monitoring progress mid-run for
the authoritative monitor loop. Same shape, different archive path.

---

## 4. Scheduled invocations (the daemon's job)

Once `alphamind-scheduler` is running as an NSSM service, APScheduler drives
all scheduled invocations automatically against the NYSE calendar:

- `pre_open` — daily before market open
- `market_hours_rolling` — every N minutes during the regular session
- `pre_close` — daily before close
- `off_hours_rolling` — overnight cadence
- `weekend_saturday`, `weekend_sunday` — weekend slots
- `emergency` — fires when the monitor writes an
  `EMERGENCY_INVOCATION_REQUESTED` activity-log row (e.g., breach cascade
  fires; cooldown-gated per `config/breach_behavior.yaml`)

The cadence and time windows live in `config/run_types/*.yaml`. Edits to
those configs take effect on the next scheduler restart — APScheduler reads
them at boot. To apply a schedule change:

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

## 5. Accessing the command center

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

## 6. Restarting a single service

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
| scheduler           | After a `config/run_types/*.yaml` or `config/main.yaml` edit  | Pause flag cleared; an in-flight invocation is cancelled mid-pipeline             |
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

## 7. Common operational gotchas

### 7.1 Service won't start, `.err.log` mentions `NoSuchTableError`

Migrations are behind. Run § 1.3–1.5.

### 7.2 Command center returns 404 at `/`

Frontend bundle missing. Run § 2.3 (just the `bun install` + `bun run build`
part); no service restart needed — FastAPI's `StaticFiles` mount picks up
the new `dist/` on the next request.

### 7.3 NSSM stop hangs in `StopPending` past 30 s

The supervisor's `TaskGroup` is blocked on a task that won't cancel
gracefully. Force the issue:

```powershell
nssm kill <ServiceName>            # SIGKILL equivalent
Get-Service <ServiceName>          # should now be Stopped
```

Then investigate the hang in the `.out.log` / `.err.log` before restarting.
A regular hang here is a bug worth filing.

### 7.4 `gh pr checks` says CI is green but a service won't start

CI doesn't exercise the Windows service supervisors. A Linux-only behavior
in the merged code (POSIX `os.fsync` on a directory handle is the classic
example) can pass CI and crash on prod. Tail the failing service's
`.err.log` and file the issue against `main`.

### 7.5 Pipeline invocation aborts with a Claude SDK auth error 5 minutes in

`CLAUDE_CODE_OAUTH_TOKEN` is set but carries a trailing `\r` from a bare
`source .env` under Git Bash. Re-source via the `tr -d '\r'` form (§ 2.1
CRLF gotcha) before running manual invocations. Services bypass this — they
read the env from NSSM's `AppEnvironmentExtra` so they're unaffected.

### 7.6 Operator browser session invalidated after every restart

`COMMAND_CENTER_SESSION_SECRET` is not pinned on the service environment.
Run the `nssm set AlphaMindCommandCenter AppEnvironmentExtra ...` step from
§ 2.5. Existing sessions stay invalidated this one time; subsequent
restarts preserve them.

---

## 8. Reference: ports and paths

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
