# Production Operations Runbook

The canonical operator workflow for AlphaMind in production — on the Windows
trading machine, against the live `alphamind.db` and paper Alpaca account.

This runbook covers the day-to-day operating loop, the one-time first-run
bootstrap, manual invocations, scheduled invocations, how to monitor those
invocations, command-center access, and service restarts. It assumes the
six NSSM services are the supported runtime form on prod:

| Service                          | Module entry                                    | Log basename       |
|----------------------------------|--------------------------------------------------|--------------------|
| `alphamind-collector`            | `python -m alphamind.collector run`              | `collector.*.log`  |
| `alphamind-scheduler`            | `python -m alphamind.scheduler run`              | `pipeline.*.log`   |
| `alphamind-monitor`              | `python -m alphamind.execution.continuous_monitor run` | `monitor.*.log`    |
| `alphamind-safety-core`          | `python -m alphamind.execution.continuous_monitor.safety_core run`      | `safety_core.*.log`          |
| `alphamind-safety-core-watchdog` | `python -m alphamind.execution.continuous_monitor.safety_core watchdog` | `safety_core_watchdog.*.log` |
| `AlphaMindCommandCenter`         | `python -m alphamind.command_center`             | `command_center.*.log` |

**Safety core + out-of-process watchdog (ADR-0004 / ALP-857).** Breach detection
+ price-staleness — the lone safety item with **no broker floor** — is isolated
into the `alphamind-safety-core` service, which reads the **broker snapshot** +
live price stream, beats a heartbeat file under
`%USERPROFILE%\AlphaMind\logs\safety_core.heartbeat`, and **writes nothing** to
the DB. The `alphamind-safety-core-watchdog` service is a **dedicated
out-of-process** watchdog: it probes that heartbeat file and runs
`nssm restart alphamind-safety-core` when it goes stale (a loop-resident watchdog
cannot catch a freeze of its own loop). The `alphamind-monitor` proper now runs
only **precision/data** tasks (options stops, greeks, entry-window, fill stream +
recovery sweep) and is fail-safe under the broker floor — a frozen monitor
degrades precision while positions stay broker-protected. The watchdog must run
under the **same Windows account** as the safety core so its `nssm restart` has
permission.

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
migration. Stop the safety-core **watchdog before the safety core** so the
watchdog does not `nssm restart` the core while you are taking it down.

```powershell
nssm stop AlphaMindCommandCenter
nssm stop alphamind-safety-core-watchdog
nssm stop alphamind-safety-core
nssm stop alphamind-monitor
nssm stop alphamind-scheduler
nssm stop alphamind-collector
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, alphamind-safety-core, alphamind-safety-core-watchdog, AlphaMindCommandCenter
```

All six should report `Stopped`. The install scripts leave NSSM's default
stop-method timeouts in place; if one hangs in `StopPending` past ~30 s, see
§ 8.3.

### 1.5 Apply migrations

```powershell
uv run alembic upgrade head
uv run alembic current     # confirm now equals heads
```

If the upgrade fails, **do not restart services.** Read the traceback,
restore the DB from the most recent backup (see `RUNBOOK_command_center.md`
§ Recovery → DB corruption restore), then re-apply.

### 1.6 Restart services in dependency order

Start the safety core **before its watchdog** so the watchdog finds a fresh
heartbeat on its first probe (and does not restart a core that is still coming
up).

```powershell
nssm start alphamind-collector
nssm start alphamind-scheduler
nssm start alphamind-monitor
nssm start alphamind-safety-core
nssm start alphamind-safety-core-watchdog
nssm start AlphaMindCommandCenter
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, alphamind-safety-core, alphamind-safety-core-watchdog, AlphaMindCommandCenter
```

All six should report `Running` within ~60 s (NSSM's `SERVICE_DELAYED_AUTO_START`
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

> **If you are performing the broker-boundary genesis cutover (ADR-0001–0005)**
> — swapping to a new Alpaca paper account and a fresh DB — use
> `docs/runbooks/genesis-cutover.md` as your primary procedure. That runbook
> supersedes § 2.2 (the fresh DB replaces the additive migration) and is the
> current first-run procedure for the new design. The account-swap step
> (§ 2.1) and the `--fresh-start` cold-start invocation (§ 2.4) remain
> relevant: § 2.1 for populating the new account's credentials in `.env`,
> and § 2.4 for the `synthetic_id_count == 0` verification and hard-fail
> paths. Return to § 2.5 onward to install the five remaining NSSM services
> after the genesis cutover bootstrap step.

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

NSSM-managed services bypass this — each entrypoint calls `load_dotenv()`,
which parses the repo-root `.env` from the service's working directory (NSSM
`AppDirectory`) and strips line endings, so the `\r` never survives. No shell
`source` step is involved. (The install scripts do **not** inject the vendor
keys via NSSM's `AppEnvironmentExtra`; the only env value set that way is the
command-center session secret in § 2.5.)

### 2.2 Bring the DB to alembic head

> **Cutover to the new broker-boundary design (ADR-0001–0005) requires a
> fresh DB, not an additive migration.** The procedure in
> `docs/runbooks/genesis-cutover.md` — swapping to a new Alpaca paper
> account and a new empty DB file — **supersedes this step** for that
> one-time event. After the genesis cutover § 2 does not apply; the system
> is already bootstrapped and all subsequent operations go through § 1.
>
> The text below applies to the **legacy** setup (old Alpaca account, old
> DB) and to any incremental schema updates after genesis.

```powershell
uv run alembic upgrade head
uv run alembic current      # confirm at heads
```

The collector has been writing into this DB for months; do not delete or
re-create it for incremental updates. Migrations are additive.

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
`cash_ledger` + `drawdown_state` singletons, and runs one `market_open`
invocation. The invocation drives the full pipeline (fill collection → analysis →
decision → command execution broker dispatch) — the PM's accepted commands must land
on Alpaca with real broker order ids. The synthetic `alp-{order_id}`
placeholder is deleted (ALP-847): an order with no broker counterpart — a
monitor-enforced protective leg (armed Intent the continuous monitor
enforces) or a not-yet-routed order — carries a NULL `alpaca_order_id`, never
a placeholder. Refuses to run if either singleton already exists.

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python -m alphamind.scheduler run \
        --fresh-start \
        --once market_open \
        --reason "first-run bootstrap"
```

Expected: one full invocation followed by the process exiting 0. Both
singletons committed to the DB, and — if the PM produced any envelopes —
the corresponding entry + broker-enforced protective legs (the native
equity bracket's take-profit + first stop) should appear on the Alpaca side
with real UUIDs; monitor-enforced legs carry a NULL `alpaca_order_id` (no
broker order — they are not on Alpaca, by design). Wall-clock is
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
print('orphan_pending_no_broker_id:', db.execute(\"SELECT COUNT(*) FROM orders o WHERE o.alpaca_order_id IS NULL AND o.status NOT IN ('PENDING_SUBMIT','CANCELLED','REJECTED') AND NOT EXISTS (SELECT 1 FROM bracket_legs bl WHERE bl.order_id = o.order_id AND bl.enforcement_binding = 'monitor_enforced')\").fetchone())
"
```

Both singleton rows should match Alpaca's reported cash on the freshly-reset
account to the cent. **`synthetic_id_count` must be `0` — by construction**:
ALP-847 deleted the `alp-{order_id}` mint, so no code can persist a synthetic
placeholder; a non-zero count would mean a stale pre-ALP-847 DB (re-baseline
it). A NULL `alpaca_order_id` is now the *expected* steady state for a
monitor-enforced leg or a not-yet-routed (`PENDING_SUBMIT`) order — NOT a
defect. The meaningful check is **`orphan_pending_no_broker_id` must be `0`**:
an active order with no broker id that is *not* a monitor-enforced leg means
broker dispatch was bypassed (the invocation did not reach Alpaca). Regression
checking should also confirm via `TradingClient.get_orders(status=ALL)` that
the broker-enforced orders are visible on the Alpaca side.

**Hard-fail paths.** `--fresh-start` refuses to run when:

- **Alpaca reports any open positions.** The error names the offending
  symbol(s). Reset the Alpaca account first.
- **Alpaca reports any open orders.** `--fresh-start` requires a flat account —
  positions empty **and** the broker order book empty. The error names the
  offending order symbol(s). Cancel them (reset the account) first.
- **`cash_ledger` already has a row.** The error includes the existing
  `current_cash_usd`. Positions/cash are a derived **Projection** rebuilt each
  invocation from the broker-event log applied to the live broker snapshot
  (ALP-854 / ADR-0001) — there is no auto-correct adjudication; a
  re-bootstrap is never the correct path once the singleton is populated.
- **`drawdown_state` already has a row.** Same shape, same recovery.

The CLI exits with code 2 (distinct from the generic scheduler-error code 1)
and prints the message verbatim to stderr — no traceback. Match on the
prefix `--fresh-start:` to filter precondition failures from scheduler
crashes.

**Recovery if the cold-start invocation itself fails after the singletons
committed.** The two rows are committed in their own transaction *before*
the invocation runs, so a fill collection error or transient external dependency
leaves them in place and a retry of `--fresh-start` will hard-fail. Two
options, in preference order:

1. **Re-run without `--fresh-start`.** The singletons are already populated
   correctly; the bootstrap step is no longer needed:

   ```bash
   uv run python -m alphamind.scheduler run \
       --once market_open \
       --reason "retry after bootstrap"
   ```

2. **Wipe the singletons and re-bootstrap** — only if the persisted values
   are wrong (e.g., the Alpaca fetch returned a transient zero-cash
   mid-reset). On the prod machine:

   ```sql
   DELETE FROM drawdown_state;
   DELETE FROM cash_ledger;
   ```

   Then re-run the `--fresh-start --once market_open --reason ...` form.

### 2.5 Install the five remaining NSSM services

Collector is already installed. Install the other five in dependency order
from an elevated PowerShell prompt at the repo root. `install_safety_core_service.ps1`
installs **both** the `alphamind-safety-core` and `alphamind-safety-core-watchdog`
services — set the `ObjectName` on both (the watchdog must run under the same
account so its `nssm restart alphamind-safety-core` has permission):

```powershell
.\scripts\install_pipeline_scheduler_service.ps1
nssm set alphamind-scheduler ObjectName .\<YourUsername>     # prompts for pw

.\scripts\install_monitor_service.ps1
nssm set alphamind-monitor ObjectName .\<YourUsername>

.\scripts\install_safety_core_service.ps1
nssm set alphamind-safety-core ObjectName .\<YourUsername>
nssm set alphamind-safety-core-watchdog ObjectName .\<YourUsername>

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

### 2.6 Start the five new services

Start the safety core **before its watchdog** (the watchdog probes the core's
heartbeat and would restart a core that hasn't beaten yet):

```powershell
nssm start alphamind-scheduler
nssm start alphamind-monitor
nssm start alphamind-safety-core
nssm start alphamind-safety-core-watchdog
nssm start AlphaMindCommandCenter
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, alphamind-safety-core, alphamind-safety-core-watchdog, AlphaMindCommandCenter
```

All six should be `Running`.

### 2.7 Register the first passkey

The command-center daemon mints a one-time setup token on first boot.
Retrieve it from the log:

```powershell
Get-Content "$env:USERPROFILE\AlphaMind\logs\command_center.out.log" -Tail 50 |
    Select-String "command_center setup token"
```

Open `http://localhost:8090/` in a browser on the Windows machine (RDP in if
remote), paste the token, choose a username, and complete WebAuthn
registration with Windows Hello or a hardware key. **Use the `localhost`
hostname, not `127.0.0.1`** — the WebAuthn relying-party id is `localhost`
(`config/security.yaml`), so a passkey ceremony loaded from the `127.0.0.1`
origin is rejected by the browser. See
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
`market_open`, `market_hours_rolling`, `pre_close`, `off_hours_rolling`,
`weekend_saturday`, `weekend_sunday`, `emergency`.

The process exits when the invocation completes (~60–180 s steady-state in
prod; up to a few minutes longer on a cold cache or with the adaptive
researcher fully consumed). All artifacts land in the same archive layout
as scheduled runs — under
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
bootstrap in § 2.4; it hard-fails when the singletons already exist.

**A manual `--once` run is NOT observable on the SSE streams.** The
`--once` code path (`_run_once` in `src/alphamind/scheduler/__main__.py`)
runs the invocation in your shell as its own process; it never constructs
the `SSEEventEmitter` or the `/control` + `/events` HTTP server — those exist
only in the daemon path (`_run_daemon`). So while a manual run executes, the
scheduler daemon's `127.0.0.1:8765/events` stream (§ 5.1) and the command
center's Live Run dashboard (§ 5.3) — which consumes that same stream — show
only heartbeats. They reflect the daemon, and the daemon is idle; your run is
elsewhere. (Don't be fooled into thinking the run stalled: collect → distill
then SSE silence is exactly what a healthy out-of-process run looks like on
those surfaces.)

To watch a manual `--once` invocation live, use:

- **The process's own stdout/stderr** — the terminal you launched it in. This
  is the richest live view: per-phase `distillation.orchestrator` completions,
  per-agent `analysis.*.runner` lines, command execution dispatch, and the final exit
  code all stream here.
- **The structured `pipeline.log`** (§ 5.4) — `_run_once` does call
  `configure_pipeline_logging()`, so records land in the rotating log
  alongside the daemon's (filter by the invocation id).
- **The `invocations` DB row** (§ 5.5) and **per-invocation archive** (§ 5.6),
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
| `emergency`            | (not scheduled)            | On-demand, operator-triggered (command-center control → scheduler `trigger_emergency_invocation`) | Cooldown-gated per `config/breach_behavior.yaml`; auto breach-cascade is **not** wired in this build (§ 5.7) |

~3 scheduled invocations per trading weekday (`market_open` + `market_hours_rolling`
+ `pre_close`), ~16 per full trading week including the Sunday run. There is no
longer any same-minute collision for the dedup window to "collapse" — every slot
is distinct by construction. **Typical steady-state wall-clock per invocation is
~60–180 seconds** (longer when the adaptive researcher consumes its full budget).
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

This is the primary live-monitoring surface for agents and operators —
**for daemon-driven invocations only** (scheduled fires + emergency
cascades). A manual `--once` CLI run is a separate process and does not
publish here; watch it via its process stdout / `pipeline.log` instead
(§ 3).

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

Watch this stream to see fills landing between invocations.

**What is *not* on this stream (and why).** The monitor's SSE emitter still
*defines* `websocket_connected` / `websocket_disconnected` / `breach_detected` /
`emergency_invocation_triggered` / `greeks_refreshed`, but their producers are
not composed into the running monitor, so none of them are emitted:

- **`breach_detected` / `emergency_invocation_triggered`** — breach detection +
  price-staleness moved out of the monitor into the isolated, **log-only safety
  core** (ADR-0004 / ALP-857). The safety core has no SSE surface and writes
  nothing; it logs to `safety_core.log` (§ 8.9). The monitor proper no longer
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
> breach surfaces in `safety_core.log` (§ 8.9) and via the broker-enforced
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

1. Look up the next fire time from § 4's schedule table relative to current
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
manual `--once` run does not publish to the daemon stream at all (§ 3). For a
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

The canonical watcher is **`scripts/watch_invocation.sh`** — pass it to the
`Monitor` tool and it streams one notification per milestone, self-terminating on
a terminal state (including one that already happened *before* it attached).
Select the invocation one of three ways:

```bash
# newest scheduled fire of a run-type (default lookback = now-5min):
bash scripts/watch_invocation.sh --scheduled market_hours_rolling
# a manual --once run, by a substring of its --reason:
bash scripts/watch_invocation.sh --reason "my run reason"
# attach to a specific invocation id:
bash scripts/watch_invocation.sh --inv inv-20260602T170000Z-193173de
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
- Set the `Monitor` `timeout` generously (or `persistent: true`) — cold-cache /
  adaptive-heavy runs reach 25–40 min; the watch exits itself on the terminal state.

---

## 6. Accessing the command center

`http://localhost:8090/` from a browser on the Windows trading machine
(loopback only in v1; the daemon binds `127.0.0.1` but browse via the
`localhost` hostname — the WebAuthn relying-party id is `localhost`). Log in
with your registered passkey.

To reach the UI from a remote workstation, RDP into the Windows machine and
open the browser there. Do **not** SSH-port-forward 8090 — the session cookie is
`SameSite=Strict` and WebAuthn's `expected_origin` / relying-party id are pinned
to `localhost`, so auth fails from any other origin (including a forwarder or
`127.0.0.1`). Do **not** bind the daemon to `0.0.0.0` — the relying-party id is
pinned to `localhost`; binding wider just exposes the API to the LAN without auth
working. Remote-access via VPS Caddy + WireGuard is deferred (see
`RUNBOOK_command_center.md` § Operator handover note).

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
- `alphamind-safety-core`
- `alphamind-safety-core-watchdog`
- `AlphaMindCommandCenter` (note: mixed case, no hyphen)

NSSM's `restart` is `stop` + `start`. The install scripts leave NSSM's default
stop-method timeouts in place (no explicit `AppStopMethod*` is set); in practice
a stop that sits in `StopPending` past ~30 s is wedged — see § 8.3. On stop,
SIGINT/SIGTERM **triggers** a co-operative shutdown: each in-flight task gets a
`CancelledError` and a bounded grace window to wind down — scheduler **10 s**,
monitor **5 s**, command center **30 s** (`supervisor_shutdown_timeout_seconds`
per daemon; the scheduler supervisor uses a manually-bounded wait rather than
`TaskGroup.__aexit__` so the timeout actually bounds exit). Tasks that don't exit
in the window are abandoned and NSSM/the SCM force-terminates the process; there
is no separate SIGTERM "fallback" the supervisor itself fires.

**Two restart gotchas that cost real diagnosis time (2026-06-02 monitor wedge):**

- **`Get-Service … Running` does NOT mean healthy.** NSSM reports `Running`
  while the *wrapper* process is alive. Two liveness layers now apply:
  - **The safety core** (breach + price-staleness, the no-floor item) is
    supervised by the **dedicated out-of-process `alphamind-safety-core-watchdog`**
    (ADR-0004 / ALP-857): it probes the core's heartbeat file
    (`%USERPROFILE%\AlphaMind\logs\safety_core.heartbeat`) and runs
    `nssm restart alphamind-safety-core` when the beat goes stale past
    `breach_evaluation_cadence_seconds × watchdog_cadence_multiplier` (default
    10×). A loop-resident watchdog cannot catch a freeze of its own loop, so the
    watchdog is its own process — a frozen safety core is restarted, not masked.
  - **The monitor proper** keeps its in-process per-cadence stall watchdog for
    its precision/data tasks (`cadence_seconds × watchdog_cadence_multiplier`
    forces `os._exit(1)` → NSSM restart). It no longer runs breach detection, so
    a monitor wedge now **degrades precision while positions stay
    broker-protected** (the broker floor + the isolated safety core hold). The
    **one residual exception is the control-surface HTTP server**, registered
    `watched=False` (it blocks in `await server.serve()`); that task is not
    auto-recycled.

  For everything, confirm a restart by the process **StartTime**
  (fresh PID / StartTime) **and** a behavioral signal — the daemon's SSE
  heartbeat (`8765` / `8766`), the safety-core heartbeat file's mtime advancing,
  fresh structured-log lines, fills flowing — never by `Get-Service Status`
  alone. Read the StartTime with:
  ```powershell
  $p = (Get-CimInstance Win32_Service -Filter "Name='<svc>'").ProcessId
  (Get-Process -Id $p).StartTime
  ```
  See § 8.9 for the fill-stream and underlying-stream auto-recovery details.
- **Restarting `alphamind-scheduler` or `alphamind-monitor` while
  `AlphaMindCommandCenter` is up can be silently refused** (the command center
  depends on both). Stop CC first, restart the target, then start CC — stop
  reverse / start forward: `nssm stop AlphaMindCommandCenter` →
  `nssm restart <target>` → `nssm start AlphaMindCommandCenter`.

**When the operator can restart a single service without coordinating the
others:**

| Restart this        | When                                                          | Side effects                                                                      |
|---------------------|---------------------------------------------------------------|-----------------------------------------------------------------------------------|
| collector           | After a `config/collector_schedule.yaml` or `config/data_sources.yaml` edit | Up to one cadence-cycle of dropped vendor reads                                   |
| scheduler           | After a `config/scheduler.yaml`, `config/run_types/*.yaml`, or `config/main.yaml` edit | Pause flag cleared; an in-flight invocation is cancelled mid-pipeline             |
| monitor             | After a `config/continuous_monitor.yaml` edit affecting precision/data tasks | Halt-mode flag persists; in-flight fill-stream reconnects from the recovery path |
| safety-core         | After a `config/continuous_monitor.yaml` cadence/threshold or a `config/guardrails.yaml` / profile `gross_exposure_pct` / `position_max_size_pct` edit | Brief gap in breach/staleness detection only; positions stay broker-protected. Its watchdog tolerates a restart within the stall bound; for a longer outage stop the watchdog first (else it `nssm restart`s the core mid-restart) |
| safety-core-watchdog | After a `watchdog_cadence_multiplier` edit                   | None to positions; the safety core is unsupervised for the restart window         |
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

The supervisor is blocked on a task that won't cancel gracefully within its
shutdown grace window (§ 7). Force the issue:

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

### 8.9 Monitor wedge — fill-stream or underlying-stream silent (connected-but-not-delivering)

#### Current state (post-ALP-825 hardening)

Both the fill (trade-updates) stream and the underlying-price stream now have
**two** independent auto-recovery layers:

1. **Budget-neutral reconnect on RTH silence.** Each stream runs a
   `StreamActivityMonitor` that beats the stall watchdog on every poll slice and
   raises `StreamStalledError` when the market is open and no frames arrive within
   the configured timeout (`fill_stream_stale_timeout_seconds = 900 s` for the fill
   stream; `underlying_stream_stale_timeout_seconds = 60 s` for the price stream).
   The consumer catches `StreamStalledError` and rebuilds the stream
   **budget-neutrally** — without consuming a reconnect attempt — so a persistently
   silent stream keeps recovering rather than exhausting its budget and exiting. REST
   recovery + the fill-backfill backstop re-capture any gap from a fill-stream
   reconnect. Off-hours, the silence check is disengaged and the clock resets so the
   closed-market gap is not charged against the first RTH window.

2. **Per-cadence stall watchdog.** Every watched task drives its loop through
   `supervised_loop`, which beats the watchdog at the top of each iteration. A task
   that beats on every poll slice (including idle slices between frames) keeps the
   watchdog satisfied. A genuinely-blocked task that stops beating trips
   `os._exit(1)` within `cadence_seconds × watchdog_cadence_multiplier` (the
   `watchdog_cadence_multiplier` default is 10×), and NSSM auto-restarts the
   monitor process. This backstop catches a task wedge that the in-stream reconnect
   could not resolve.

**Operator's primary action** after any suspected monitor wedge:

1. Confirm a fresh PID / StartTime (the watchdog fired and NSSM auto-restarted):
   ```powershell
   $p = (Get-CimInstance Win32_Service -Filter "Name='alphamind-monitor'").ProcessId
   (Get-Process -Id $p).StartTime
   ```
2. Confirm a behavioral signal — an `8766` heartbeat within ~18 s of connecting
   (the SSE keep-alive cadence is 15 s):
   ```bash
   curl -N 127.0.0.1:8766/events
   ```
3. If the process PID/StartTime is fresh and the heartbeat is live, **auto-recovery
   succeeded** — no manual restart is needed. Confirm fills are flowing again
   (check `fill_records` for rows dated today; check `monitor.log` for fresh
   trade-stream connect lines).

**Manual § 7 restart is the fallback** when auto-recovery is itself suspect (e.g.
the StartTime is stale, no `8766` heartbeat is appearing, or `monitor.err.log`
shows a `reconnect budget exhausted` entry). Use the CC-dependency order:
`nssm stop AlphaMindCommandCenter` → `nssm stop` + `nssm start alphamind-monitor`
→ `nssm start AlphaMindCommandCenter`. Confirm by StartTime + heartbeat, not
`Get-Service`.

#### Pre-fix history (2026-06-02 — for reference only)

Before ALP-825: the continuous monitor's Alpaca trade-updates websocket failed on
`OSError: [WinError 121] The semaphore timeout period has expired`, and the
`websockets` library reconnect-looped *internally* without ever raising — so the
fill consumer parked on `await queue.get()` and starved. The process stayed
`Running` (NSSM only restarts on process exit, and the in-process watchdog didn't
trip) and captured **no fills for ~11 h**, with real-time breach/stop monitoring
off the whole time. ALP-819 fixed the fill-stream silent-wedge path; ALP-832
applied the same fix to the underlying-price stream; ALP-826 added the per-cadence
watchdog backstop.

The old detection signals (if you ever see them again, the post-fix recovery path
above should already be firing):

- `monitor.err.log` shows repeated `trading stream websocket error, restarting
  connection: no close frame received or sent` + `OSError: [WinError 121]`.
- `fill_records` has no rows for fills you know landed on Alpaca, and the matching
  local order rows stay `PENDING` with `filled_quantity = 0`.
- `curl -N 127.0.0.1:8766/events` emits no `heartbeat` within ~18 s.

#### Breach + price-staleness signals (safety core)

Breach detection + the price-staleness guard now live in the isolated **safety
core** (ADR-0004 / ALP-857), which surfaces them by being **loud in its log** —
it has no SSE / `/events` channel (§ 5.2) and writes nothing to the DB. Watch
`safety_core.log` for two distinct lines, and interpret them differently:

- **Breach** — `safety_core BREACH: broker snapshot breaches <rules> at <ts>`
  (logged `CRITICAL`). A gross-exposure or single-name-concentration limit is
  breached on the **broker snapshot**. Positions stay protected by the
  broker-enforced bracket floor; the safety core does **not** submit or trigger
  an emergency (§ 5.7) — it detects and stays alive.

- **Price feed globally stale** — `safety_core: underlying price feed globally
  stale — writer appears wedged; stale=… missing=…` (logged `ERROR`). Every
  open-position ticker is simultaneously stale (older than
  `underlying_price_max_age_seconds`, 900 s) or never received — the safety
  core's price feed (subscribed to the **broker snapshot's** equity symbols) has
  gone cold. The safety core keeps beating its heartbeat and re-logs this each
  tick; if the feed task has *crashed*, the process exits and NSSM restarts it,
  but a connected-but-silent feed is logged, **not** auto-recovered (the safety
  core has no in-stream reconnect) — restart `alphamind-safety-core` (§ 7) if it
  persists. A *single* stale ticker (e.g. a just-opened position whose quote
  stream hasn't warmed up) is excluded from that tick silently — broker bracket
  legs still protect it — and is not logged as the global-stale condition.

#### After a monitor restart — expect a brief projection-rebuild lag

- The startup replay + the 15-min `activities_backfill` re-capture missed
  fills into the broker-event log (and `fill_records` as `unprocessed`); the
  **next Phase-1** integrates them (PENDING→OPEN) and the **projection rebuild**
  (ALP-854) folds the event log onto the live broker snapshot, re-deriving
  positions/cash. There is no `_reconcile_cash` writeback — the Projection is
  rebuilt, not adjudicated (ADR-0001). Cash drift and position divergence
  persist only until that Phase-1 runs.
- The monitor's underlying-price stream (used by the greeks + options-stop
  tasks) subscribes to *known* open positions from the DB projection, so a fill
  that hasn't integrated through Phase-1 yet isn't priced for options-stop
  enforcement until the next Phase-1. This is subscription lag, **not** a dead
  price feed. The position's broker-side bracket legs still protect it meanwhile;
  it clears at the next Phase-1.

### 8.10 PM equity CLOSE / reduce — protective legs cancelled before the sell (ALP-937)

A native equity bracket's take-profit + stop legs reserve 100% of the position's
shares at the broker (`held_for_orders`). So a PM-directed equity CLOSE (full **or**
partial) first cancels the broker-enforced protective legs via `submit_cancel`,
**then** submits the SIMPLE close sell. This is expected and visible in the
submission log as leg cancels immediately preceding the close order — not a bug.

Two operational consequences to know:

- **Partial-reduce re-bracket window.** A *partial* close cancels **all** protective
  legs (you cannot partially cancel an OCO leg), so once the reduce fills the
  remaining shares are **broker-unprotected** until the PM re-brackets them on its
  next invocation. The continuous-monitor position-level max-loss guardrail is the
  active backstop during that window (the same cancel-and-review model used for
  corporate actions). Automatic re-protection of the remainder with a fresh OCO is a
  deferred follow-up (ALP-938, ALP-937 deliverable D); until it ships, expect a
  partially-reduced equity position to read with an ACTIVE bracket whose legs are all
  CANCELLED until the next PM pass re-protects or closes it.

- **"NAKED POSITION" CRITICAL alert in `pipeline.log`.** If the close sell is rejected
  *after* the protective legs were already cancelled, the dispatcher logs
  `ALP-937 NAKED POSITION: equity CLOSE of <SYMBOL> ...` at CRITICAL. The position is
  broker-unprotected and the monitor max-loss guardrail is the only backstop until
  the PM re-evaluates next invocation. **Operator action:** confirm the monitor is
  live (§ 8.9), watch the named position's P/L, and — if the next scheduled PM pass
  is far off — consider a manual invocation (§ 3) so the PM re-brackets or closes it.

---

## 9. Feedback loop — analytics CLIs, cadences, and review skills

The feedback loop (ALP-131) is the month-over-month system-improvement substrate:
it pairs the system's reasoning artifacts with the outcomes they produced and
computes deterministic, conditionable metrics over that pairing. It is **read-only
over trading state** — it measures and proposes, never mutating live trading-state
records — and runs **out-of-pipeline** as a set of operator CLIs plus three review
skills. The deliberative pipeline gained three prod-runtime behaviors that feed it
(documented in their own sections, cross-referenced below).

The end-to-end spine is verified out-of-pipeline by
`scripts/verify_feedback_loop.py` (it runs `alembic upgrade head` on a scratch DB,
seeds a controlled closed-position scenario, and drives resolver → metrics → digest
+ snapshot → validation → supersession → retrospective, asserting each stage). Run
it after a change that touches the feedback-loop tables or the resolver:

```bash
uv run python scripts/verify_feedback_loop.py      # exits 0 on PASS, 1 on any FAIL
```

### 9.1 Prod-runtime behavior changes (the pipeline now feeds the loop)

Three behaviors the deliberative pipeline gained — each documented in full where it
lands, listed here so the feedback-loop picture is complete:

- **`agent_calls` telemetry + provenance every invocation** — every LLM agent call
  persists one `agent_calls` row + a four-file provenance tree. Active in production
  (ALP-907). See § 3 ("agent_calls telemetry capture is active in production") and
  the § 10 reference path. The metrics' `WindowDataset` reads `agent_calls` for the
  cost / process / citation-chain tiers.
- **Per-invocation thesis resolution (`ACTIVE → RESOLVED`)** — every deliberative
  invocation runs a `thesis_resolution` step between fill collection and snapshot
  assembly, authoring each closed-position thesis's component outcomes + resolution
  category. See § 5 ("Thesis-resolution skip warnings"). The outcome-tier metrics
  calibrate against these resolved theses; a per-thesis data gap is a skip-WARNING,
  not a fault.
- **Distillation anomaly emission to `activity_log`** — the distillation layer emits
  `DISTILLATION_ANOMALY_FLAG` activity-log entries (ALP-881) when a threshold in its
  anomaly taxonomy trips; the digest's notable-shift surface reads them.

### 9.2 The combined feedback-loop migration (apply before anything reads)

The six feedback-loop tables (`agent_calls`, `validations`, `validation_outcomes`,
`weekly_digest_snapshots`, `retrospective_reports`, `retrospective_decisions`) and
the `activity_log` event-type / source CHECK widenings land in the combined
migration **ALP-879** (`c001fb0000ff_feedback_loop_tables_and_activity_log_check_widen`).
It is applied by the standard `alembic upgrade head` in the update loop (§ 1) /
first-run bootstrap (§ 2). Until it is applied on the prod box the `agent_calls`
inserts silently no-op and every analytics CLI errors on the missing table.

### 9.3 Analytics-read CLI — digest, metric, snapshot

`python -m alphamind.feedback_loop.digest.cli` (all subcommands emit JSON on stdout;
`--db-path` defaults to the live DB, `--config-dir` to `config/`):

- `digest [--week YYYY-MM-DD] [--trajectory-weeks N]` — the full six-section
  `WeeklyDigest` for the week (headline outcomes, process pulse, 8–12-week
  trajectory, validation status, notable shifts, replay queue).
- `metric <metric_id> [--window N] [--week …]` — one `MetricResult` aggregated over
  the trailing `--window` weeks.
- `metrics-list` — the registered metric ids (a metric gated on an absent
  dependency, e.g. PM-accuracy before ALP-129, is simply absent).
- `snapshot [--week …] [--trajectory-weeks N]` — **producer**: writes the week's
  `WeeklyDigest` into `weekly_digest_snapshots`, idempotently (the `week_start`
  UNIQUE makes a re-run a reported no-op).

All four are DB-only — no vendor API, no `.env` needed.

**Cadence.** The `snapshot` producer is intended to run **once per week, Sunday
8am ET**, snapshotting the week just closed (omitting `--week` targets the prior
completed week). It is **not yet wired into the scheduler** — run it from an
operator cron / CI job, or by hand. `digest` / `metric` are on-demand reads the
`/feedback-review` skill drives.

### 9.4 Validation CLI — register, evaluate, list, detect-supersessions

`python -m alphamind.feedback_loop.validation.cli` (JSON on stdout; `register` /
`evaluate` take a JSON payload on `--input`, a path or `-` for stdin). DB-only:

- `register` — snapshot the registering invocation's provenance (regime +
  model id from `agent_calls`), freeze the pre-registered criteria, persist the
  validation.
- `evaluate --validation-id … --outcome-id … --evaluated-at …` — compute the
  pre/post metric comparison, derive the verdict + rollback status, write the
  `validation_outcomes` row (or short-circuit if the validation was superseded).
- `list` — the pending (unevaluated, un-superseded) validations.
- `detect-supersessions` — **producer**: mark each active validation whose
  post-edit window was crossed by a conditioning shift (regime transition, model
  version change, or a commit to the watched artifact) `superseded`.

**Cadence.** Run `detect-supersessions` **once per pipeline invocation and once per
commit** (the two events that can break a validation's conditioning context). It is
**not yet scheduler-wired** — drive it from the pipeline-adjacent cron / a commit
hook / CI, or via `/feedback-validate`.

### 9.5 Retrospective CLI — ingest, save-report, capture-decision

`python -m alphamind.feedback_loop.retrospective.cli` (DB-only):

- `ingest --start … --end …` — pull the Phase-1 retrospective data set over the
  window (the analytics-spine `WindowDataset` + the unresolved pending-rollback
  follow-ups).
- `save-report --start … --end … --markdown-file …` — persist the rendered report
  metadata row + write the markdown under
  `data/retrospective_reports/{report_id}/report.md`.
- `capture-decision --report-id … --item-identifier … --decision-type … --verdict …
  --rationale …` — record one walkthrough decision (promotion candidate or
  follow-up), optionally linking a spawned validation.

### 9.6 The three review skills (headless)

The operator drives the loop through three Claude skills, all **headless** — they
read the analytics through the CLIs above and emit markdown the operator reads:

- **`/feedback-review`** — weekly process-metric / monthly outcome-metric review and
  ad-hoc agent/metric deep-dives, over the digest CLI.
- **`/feedback-validate`** — register a pre-registered validation for a prompt /
  config edit, or evaluate one whose window has elapsed. Enforces pre-registration
  discipline + confounder controls + posterior-band reporting (confirmation bias is
  the failure mode it exists to defeat).
- **`/feedback-retrospective`** — open-ended quarterly LLM-driven deep retrospective:
  read many invocations end-to-end, surface patterns the deterministic metrics did
  not catch, propose promotion candidates.

The interactive dashboard for this surface (View F) is **deferred to ALP-686** — the
loop is headless-only for now.

### 9.7 PM-accuracy verification follows story 06e / ALP-129

The PM-accuracy + modification-effectiveness metrics (and their
`counterfactual_replays` loader sub-bundle) are gated on the counterfactual replay
engine (ALP-129). They are **verified when story 06e (ALP-887) lands** alongside that
engine — the spine verify above does not assert them, and `metrics-list` simply
omits the gated metric ids until the dependency is present. This is a known,
intentional gap, not a missing check.

---

## 10. Reference: ports and paths

| What                         | Where                                                            |
|------------------------------|------------------------------------------------------------------|
| Scheduler `/control` + `/events` | `127.0.0.1:8765` (loopback only)                              |
| Monitor `/control` + `/events`   | `127.0.0.1:8766` (loopback only)                              |
| Command center API + UI          | binds `127.0.0.1:8090` (loopback only); **browse via `http://localhost:8090`** — WebAuthn relying-party id is `localhost` |
| Safety core heartbeat (no port)  | `%USERPROFILE%\AlphaMind\logs\safety_core.heartbeat` (file; the out-of-process watchdog probes it — the safety core has no control port and writes nothing to the DB) |
| Production DB                    | `%USERPROFILE%\AlphaMind\data\alphamind.db`                   |
| Daemon logs                      | `%USERPROFILE%\AlphaMind\logs\<daemon>.{out,err,log}.log` (`safety_core.*`, `safety_core_watchdog.*` for the safety-core services) |
| Invocation archives              | `%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\` |
| Agent-call provenance            | `%USERPROFILE%\AlphaMind\data\provenance\invocations\<invocation_id>\agent_calls\<agent_call_id>\` (one dir per LLM agent call; `system_prompt.md` + `output_schema.json` + `tools_definition.json` + `output.json`; paired with one `agent_calls` table row) |
| Retrospective reports            | `<repo>\data\retrospective_reports\<report_id>\report.md` (markdown body; the `retrospective_reports` row carries the metadata + `report_file_ref`) |
| Feedback-loop CLIs (§ 9)         | `python -m alphamind.feedback_loop.{digest,validation,retrospective}.cli`; spine verify `scripts/verify_feedback_loop.py` |
| Config files                     | `<repo>\config\*.yaml` (incl. `feedback.yaml` sample-size floors, `digest.yaml` shift thresholds), `<repo>\config\run_types\*.yaml`, `<repo>\config\profiles\*.yaml` |
| `.env`                           | `<repo>\.env`                                                  |
