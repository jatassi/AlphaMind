# Standard update loop

Deploying latest `main` to the prod Windows machine: pull, sync, migrate, restart,
verify. Service table, ports, and paths live in [services.md](services.md);
troubleshooting in [troubleshooting.md](troubleshooting.md).

> **Service-stop precondition.** If the market is open AND open positions exist,
> obtain explicit Operator confirmation before stopping services — § 1.4 takes the
> monitor and safety core down, leaving Monitor-enforced legs unwatched and breach
> detection dark until § 1.6 completes. Outside that window, proceed.

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

Scan what came in: `git log <old-HEAD>..HEAD --oneline`, then read the full
message of any commit touching `scripts/services/` — installer-script changes
do **not** reconfigure already-installed services, so such commits carry a
deploy note naming a one-time live-config step (`sc.exe config ...` /
`nssm set ...`) to run while services are stopped (troubleshooting.md § 8.12).

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

### 1.4 Stop services (reverse of the start order)

The stop order is operational convention — no service declares an SCM
dependency on another, so the SCM enforces no ordering (the topology
invariant; services.md § 7). Stop the
command center first so its loopback consumers sit quiet through the
migration window rather than reconnect-looping against stopped upstreams.
Collector is independent of the trading DB schema but stop it too to keep
the snapshot quiet during the migration. Stop each **watchdog before the
service it supervises** so the watchdog does not `nssm restart` its target
while you are taking it down.

```powershell
nssm stop AlphaMindCommandCenter
nssm stop alphamind-safety-core-watchdog
nssm stop alphamind-safety-core
nssm stop alphamind-monitor-watchdog
nssm stop alphamind-monitor
nssm stop alphamind-scheduler
nssm stop alphamind-collector
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, alphamind-monitor-watchdog, alphamind-safety-core, alphamind-safety-core-watchdog, AlphaMindCommandCenter
```

All seven should report `Stopped`. The install scripts leave NSSM's default
stop-method timeouts in place; if one hangs in `StopPending` past ~30 s, see
troubleshooting.md § 8.3.

### 1.5 Apply migrations

```powershell
uv run alembic upgrade head
uv run alembic current     # confirm now equals heads
```

If the upgrade fails, **do not restart services.** Read the traceback,
restore the DB from the most recent backup (see [command-center.md](command-center.md)
§ Recovery → DB corruption restore), then re-apply.

### 1.6 Restart services (conventional start order)

The start order is operational convention, not SCM-enforced. Start each
supervised service **before its watchdog** so the watchdog finds a fresh
heartbeat on its first probe (and does not restart a service that is still
coming up). The command center comes last only by symmetry with § 1.4 — it
starts fine before its upstreams (its consumers reconnect with capped
backoff until they come up).

```powershell
nssm start alphamind-collector
nssm start alphamind-scheduler
nssm start alphamind-monitor
nssm start alphamind-monitor-watchdog
nssm start alphamind-safety-core
nssm start alphamind-safety-core-watchdog
nssm start AlphaMindCommandCenter
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, alphamind-monitor-watchdog, alphamind-safety-core, alphamind-safety-core-watchdog, AlphaMindCommandCenter
```

All seven should report `Running` within ~60 s (NSSM's `SERVICE_DELAYED_AUTO_START`
+ the supervisors' own startup time). If any stays in `StartPending` past
that, tail its `.err.log` immediately.

### 1.7 Verify green

The `.out.log` files are empty by design — every daemon logs to stderr — so the
startup signal lives in the `.err.log` files and the monitor's dedicated
`monitor.log`. (One exception: the collector also mirrors apscheduler INFO
chatter to `collector.out.log`, unrotated and hundreds of MB — ignore it; the
collector's startup signal is the `scheduler startup` / `collector scheduler
starting` lines in its dedicated `collector.log`.) Shutdown residue from § 1.4 is expected at the tails: a
`KeyboardInterrupt` traceback in the collector/safety-core/watchdog `.err.log`s,
and a cancelled websocket-connect `TimeoutError` in `monitor.err.log`. A
traceback only matters if it is timestamped **after** the § 1.6 starts.

```powershell
# Scheduler: expect "Application startup complete" + a next-fire line per trigger.
Get-Content "$env:USERPROFILE\AlphaMind\logs\pipeline.err.log" -Tail 15
# Monitor: expect a fresh "monitor session start" + control surface on 8766,
# and a heartbeat younger than the watchdog's 150 s stall bound.
Get-Content "$env:USERPROFILE\AlphaMind\logs\monitor.log" -Tail 10
$hb = [double](Get-Content "$env:USERPROFILE\AlphaMind\logs\monitor.heartbeat")
"heartbeat age: $([math]::Round([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000 - $hb, 1)) s"
# Command center: expect uvicorn on 8090 + 200 OK GETs to both upstream /events
# (8765 pipeline, 8766 monitor). The dormant alert-rule WARNINGs are known noise.
Get-Content "$env:USERPROFILE\AlphaMind\logs\command_center.err.log" -Tail 15
```

Optional gate (runs in ~1 min, exercises 7 command-center checks):

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify/verify_command_center.py
```

