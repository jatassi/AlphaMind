# Command Center Operator Runbook

Operator workflow for the AlphaMind command center — the operator-facing web
application running alongside the pipeline scheduler + continuous monitor on
the trading machine. Covers initial bring-up, day-to-day operations, passkey
management, troubleshooting, recovery, and the operator handover for the
deferred remote-access work tree.

The command center is the third long-running AlphaMind daemon (after the
pipeline scheduler and the continuous monitor); the three are loosely coupled
via loopback HTTP — the command center proxies operator actions to the
pipeline's `/control/*` and the monitor's `/control/*` surfaces, and consumes
both daemons' `/events` SSE streams. See
[`docs/design/command-center.md`](../docs/design/command-center.md) for the
full design.

## Purpose

A green `scripts/verify_command_center.py` run proves the command center is
ready to serve traffic on a production machine:

- The FastAPI app binds to the configured loopback port within 30 s.
- A passkey registration + login roundtrip completes end-to-end.
- All 8 control verbs (`pause`, `resume`, `trigger_emergency_invocation`,
  `switch_profile`, `run_universe_validation`, `cancel_order`,
  `force_close_position`, `set_halt_mode`) return success envelopes AND
  write an `OPERATOR_CONSOLE` row to `activity_log`.
- The alert engine fires on a synthetic failed-invocation event; the in-app
  channel + the Discord webhook + the `alerts` table all observe the fire.
- SSE roundtrip — one pipeline frame + one monitor frame land at a subscribed
  client.
- The frontend bundle (`bun run build` output) is present at the configured
  `dist_path`.
- SIGTERM → clean shutdown within 10 s.

The verify script exits 0 only on full pass; non-zero on any failure with a
per-step PASS / FAIL report.

## Prerequisites

1. **Working directory** — the AlphaMind repo root on the production machine.
2. **`CLAUDE_CODE_OAUTH_TOKEN` exported.** Same form as pipeline / monitor
   bring-up — `set -a && source <(tr -d '\r' < .env) && set +a` strips the
   CRLF that bites Git Bash on Windows.
3. **`uv sync` completed.** The verify script + the service entry point both
   pull from the uv-managed venv.
4. **`bun` installed and on PATH.** The frontend bundle is built with bun
   (lockfile is `bun.lock`); npm / yarn are NOT supported and will produce
   different `node_modules` resolutions than the locked toolchain. The
   canonical install command is documented at
   [bun.sh/docs/installation](https://bun.sh/docs/installation). Pin bun to
   1.1.x or newer — earlier versions had `--frozen-lockfile` bugs that
   silently fell back to mutating the lockfile.
5. **`nssm` installed and on PATH.** Same prerequisite as
   `install_pipeline_scheduler_service.ps1` /
   `install_monitor_service.ps1`. NSSM ships as a single .exe; drop into a
   directory on PATH.
6. **Pipeline + monitor services already installed.** The command center
   declares both as service dependencies (see
   `install_command_center_service.ps1`); a missing dependency causes the
   Windows SCM to refuse the start.

## Initial bring-up

The first time a command center comes up on a machine. Each step is
idempotent — re-running is safe if you need to recover from a partial
install.

### 1. Bring DB schema up to date

The command center owns three tables (`alerts`, `webauthn_credentials`,
`operator_sessions`) that ship in the same `alphamind.db` SQLite file the
pipeline + monitor use. The schema is created via Alembic migrations.

```bash
uv run alembic upgrade head
```

The migration log is in `alembic.ini`'s configured location; verify the
target revision matches `head` after upgrade.

### 2. Build the frontend bundle

The command center serves the SPA from
`src/alphamind/command_center/frontend/dist/`. Bun produces this bundle
from the TypeScript / React source under `frontend/src/`.

```bash
cd src/alphamind/command_center/frontend
bun install --frozen-lockfile
bun run build
```

`bun install --frozen-lockfile` is strict on the lockfile — if dependencies
drifted, it fails instead of silently rewriting. `bun run build` runs `tsc -b
&& vite build` → `dist/`. Confirm `dist/index.html` exists before continuing.

### 3. Install the NSSM service

```powershell
.\scripts\install_command_center_service.ps1
nssm set AlphaMindCommandCenter ObjectName .\<YourUsername>
nssm start AlphaMindCommandCenter
Get-Service AlphaMindCommandCenter
```

The install script wires:

- `python -m alphamind.command_center` as the entry point.
- AppExit Default Restart with a 60 s throttle.
- Stdout / stderr → `%USERPROFILE%\AlphaMind\logs\command_center.{out,err}.log`.
- Dependency on `alphamind-scheduler` + `alphamind-monitor`.

The `ObjectName` step is manual — NSSM prompts for the account password
interactively, which keeps it out of shell history.

### 4. Retrieve the setup token

On first start, the daemon mints a one-time setup token + prints it to
stdout. NSSM captures stdout to the log file:

```powershell
Get-Content -Path "$env:USERPROFILE\AlphaMind\logs\command_center.out.log" -Tail 50
```

Look for a line of the form:

```
command_center setup token: <URL-safe random string>
```

This token authorizes the first passkey registration; subsequent
registrations go through the existing-session bearer path. The token is
single-use — it's burned on the first successful `/auth/register/complete`.

A daemon restart re-mints a fresh token (the gate is in-process state). If
you need to re-enroll after losing all credentials, see § Recovery →
Credential reset.

### 5. Register the first passkey

The verify script uses an in-memory verifier; production registration uses a
real WebAuthn authenticator (YubiKey, platform passkey via Windows Hello,
TouchID on macOS, etc.).

1. Open `http://127.0.0.1:8080/` in a WebAuthn-capable browser.
2. The first-launch UI prompts for the setup token (paste the value from
   step 4).
3. Choose a username (operator handle — e.g. "operator").
4. The browser prompts your authenticator. Touch / tap / scan to complete
   the registration ceremony.
5. The UI redirects to the dashboard; the session cookie now governs all
   subsequent traffic.

The setup token is now consumed. If you need to register a second
authenticator (e.g. a backup YubiKey), use the "Add a passkey" flow below
while logged in — no setup token required.

### 6. Run the verify script

The end-to-end gate is `scripts/verify_command_center.py`. Run it from the
repo root after the service is up:

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify_command_center.py
```

Expected output (one line per check, all PASS):

```
PASS: daemons_bind — command center bound to 127.0.0.1:<port> after 0.XXs
PASS: passkey_roundtrip — WebAuthn register + login ceremony pair completed
PASS: control_verbs — all 8 control verbs returned ok=true; 8 OPERATOR_CONSOLE rows + 1 PROFILE_SWITCHED row(s) landed
PASS: alert_fires — pipeline_aborted alert fired; 1 alerts row(s), 1 Discord call(s) recorded
PASS: sse_roundtrip — one pipeline + one monitor event observed at multiplexer subscriber
PASS: frontend_build — <path>/dist/index.html present; StaticFiles mount has a bundle to serve
PASS: clean_teardown — supervisor stopped cleanly within 10s budget
=== COMMAND-CENTER VERIFICATION === 7/7 checks passed
```

Exit code 0 confirms readiness. Any FAIL line short-circuits to exit code 1.

## Day-to-day operations

### Accessing the UI

`http://127.0.0.1:8080/` from a browser on the same machine. Loopback only
in v1 — the VPS Caddy + WireGuard remote-access path is deferred (§ Operator
handover note). For day-to-day operations on the trading machine, the
operator logs in via RDP / console and uses the browser there.

### Restarting the service

A service restart re-mints the in-process session-cookie signing secret
unless `COMMAND_CENTER_SESSION_SECRET` is set in the service environment —
without the env-var pin, every restart invalidates active sessions
(operators re-authenticate after restart). To pin the secret:

```powershell
# Generate a fresh secret (cryptographically random, 32 bytes base64):
$secret = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 -Minimum 0 } | ForEach-Object { [byte]$_ }))
# Set it on the service environment (NSSM AppEnvironmentExtra):
nssm set AlphaMindCommandCenter AppEnvironmentExtra "COMMAND_CENTER_SESSION_SECRET=$secret"
nssm restart AlphaMindCommandCenter
```

The secret is sensitive — treat it like a service account password. Rotating
it invalidates every active session (operators re-login).

### Log locations

| Daemon | stdout | stderr |
|---|---|---|
| Command center | `%USERPROFILE%\AlphaMind\logs\command_center.out.log` | `%USERPROFILE%\AlphaMind\logs\command_center.err.log` |
| Pipeline | `%USERPROFILE%\AlphaMind\logs\pipeline.out.log` | `%USERPROFILE%\AlphaMind\logs\pipeline.err.log` |
| Monitor | `%USERPROFILE%\AlphaMind\logs\monitor.out.log` | `%USERPROFILE%\AlphaMind\logs\monitor.err.log` |
| Collector | `%USERPROFILE%\AlphaMind\logs\collector.out.log` | `%USERPROFILE%\AlphaMind\logs\collector.err.log` |

The command center's `configure_command_center_logging()` also writes a
structured `command_center.log` next to the NSSM-captured `.out` and `.err`
files. Cross-reference both when triaging — NSSM captures raw print
statements, the structured log captures the Python logger emit.

### Stopping / starting

```powershell
nssm stop AlphaMindCommandCenter
nssm start AlphaMindCommandCenter
Get-Service AlphaMindCommandCenter
```

The supervisor's shutdown timeout is 30 s (matches NSSM's
`AppStopMethodConsole` budget). A graceful stop drains Uvicorn + every
TaskGroup task; a hard kill follows automatically if the budget expires.

## Adding a passkey

A second (or third) authenticator can be enrolled while logged in with an
existing one — the WebAuthn API supports multiple registered credentials per
account. Reasons to add a passkey:

- Backup hardware key in case the primary is lost.
- Cross-device access via platform authenticators (Windows Hello +
  TouchID on a Mac, both registered for the same operator account).
- Operator handover — register the new operator's authenticator before
  revoking the old one.

Workflow:

1. Log in via your current passkey.
2. Open the dashboard's "Settings → Authenticators" view (the precise URL is
   in the UI's navigation; the route lives under `/auth/credentials/`).
3. Click "Add a passkey". The browser prompts the new authenticator; touch
   / tap / scan to complete.
4. The new credential is committed to `webauthn_credentials` alongside the
   existing one. Both can subsequently sign in.

No setup token required for subsequent registrations — the existing-session
bearer path covers it.

## Troubleshooting

### 1. `daemons_bind` FAIL — daemon never reaches Uvicorn.serve()

The verify wrapper polled `127.0.0.1:<port>` for 30 s without a successful
TCP connect. Common causes:

- **Another process is listening on the configured port.** Check with
  `netstat -ano | findstr :<port>`. Adjust `config/command-center.yaml`'s
  `bind.port` if a conflict exists.
- **The Python module failed to import.** Check `command_center.err.log` for
  a `ModuleNotFoundError` / `ImportError` / Pydantic validation failure on
  one of the three configs.
- **The Alembic schema is behind.** The lifespan opens engines against the
  configured DB path; a missing `alerts` / `webauthn_credentials` /
  `operator_sessions` table surfaces as a `NoSuchTableError` on first
  request. Run `uv run alembic upgrade head`.

### 2. `passkey_roundtrip` FAIL on register/begin — setup_token rejected

The setup token gate has three terminal states:

- **Never minted** — the daemon never reached `record_process_lifetime` /
  `mint()`. Look upstream in the boot log for a crash before
  `command center session start`.
- **Already consumed** — a prior registration succeeded and the gate is
  locked. To re-enroll, see § Recovery → Credential reset.
- **Wrong token presented** — typo or you copied an older token. Restart the
  daemon; the gate re-mints; re-read the new token from the log.

### 3. `control_verbs` FAIL — upstream pipeline / monitor unreachable

The proxy forwards each verb to the upstream daemon over loopback HTTP. A
failed connection surfaces as `upstream_unreachable` in the response
envelope. Cause: pipeline or monitor is down.

```powershell
Get-Service alphamind-scheduler
Get-Service alphamind-monitor
```

Restart whichever is stopped. The command center will resume serving verbs
once both upstreams are reachable.

### 4. `alert_fires` FAIL — Discord webhook 401 / 429

The default `config/alerts.yaml` channels list includes `discord` for the
critical + important tiers. The webhook URL lives in the
`ALPHAMIND_DISCORD_WEBHOOK` env var.

- **401 / 403** — webhook URL is wrong or expired. Generate a fresh one in
  Discord (Server Settings → Integrations → Webhooks → New Webhook).
- **429** — rate-limited; back off + retry. Discord caps webhook posts at
  ~5/sec/channel. The alert engine's per-rule debounce defaults (15–720
  minutes depending on severity) keep this from happening in normal
  operation.

A missing env var disables Discord fanout — the in-app channel still works.
The startup log emits `Discord alerts disabled` if so.

### 5. SSE in browser disconnects every 60 s

EventSource over HTTP/1.1 keeps the connection open with periodic
heartbeats. A 60-s disconnect cycle is usually a misconfigured reverse proxy
(remote-access work tree only — the loopback config doesn't proxy). Adjust
proxy timeouts or set `proxy_buffering off` for the `/api/events` path.

### 6. **bun version mismatch — install fails with "lockfile out of sync"**

The frontend lockfile is generated by bun 1.1.x. Earlier versions (1.0.x and
older) used a different lockfile format and silently rewrite on `bun
install`. Confirm with:

```bash
bun --version
```

Upgrade with the curl|sh installer from bun.sh. If `--frozen-lockfile` keeps
rejecting the lockfile, delete `bun.lock` and re-run `bun install` once to
regenerate against the current bun version — but commit the regenerated
lockfile (it is checked into the repo).

### 7. Frontend build OK but UI shows "API unreachable"

The dev workflow runs Vite at :5173 with a proxy to FastAPI :8080. In dev,
the daemon needs `COMMAND_CENTER_DEV_MODE=1` set so the FastAPI StaticFiles
mount is skipped (Vite serves the SPA). Production never sets this env var
— FastAPI serves both API + SPA from :8080.

If you see "API unreachable" in dev, confirm:

- FastAPI is running on :8080 with `COMMAND_CENTER_DEV_MODE=1`.
- Vite is running on :5173 (its console prints the URL on start).
- `vite.config.ts`'s proxy block points at :8080 (it does by default).

## Recovery

### DB corruption restore

The command center's three owned tables (`alerts`, `webauthn_credentials`,
`operator_sessions`) live in the same `alphamind.db` SQLite file as the
pipeline + monitor's state. The canonical recovery is the same as for the
pipeline / monitor:

1. Stop all four services:
   ```powershell
   nssm stop AlphaMindCommandCenter
   nssm stop alphamind-monitor
   nssm stop alphamind-scheduler
   nssm stop alphamind-collector
   ```
2. Restore the most recent `alphamind.db` backup (the pipeline scheduler's
   `archive/` directory carries periodic snapshots; pick the most recent
   verified-good copy).
3. `uv run alembic upgrade head` to bring the restored DB's schema current.
4. Restart the four services in dependency order:
   ```powershell
   nssm start alphamind-collector
   nssm start alphamind-scheduler
   nssm start alphamind-monitor
   nssm start AlphaMindCommandCenter
   ```
5. Re-register passkeys — the restored DB carries the prior credentials, so
   no new passkey enrollment is required UNLESS the credentials predate the
   sign-count drift detection (see § Credential reset below).

### Credential reset (locked out of the UI)

If you've lost every registered authenticator (primary lost AND backup
lost), the only recovery is a credential wipe + re-enrollment via setup
token:

1. Stop the service: `nssm stop AlphaMindCommandCenter`.
2. Delete every row in `webauthn_credentials` AND `operator_sessions` —
   open the DB with the sqlite3 CLI (closed-service-only; never edit a live
   DB):
   ```bash
   sqlite3 "$env:USERPROFILE\AlphaMind\data\alphamind.db" \
       "DELETE FROM operator_sessions; DELETE FROM webauthn_credentials;"
   ```
3. Restart the service: `nssm start AlphaMindCommandCenter`.
4. Read the fresh setup token from `command_center.out.log` (the gate
   re-mints because `count_credentials == 0`).
5. Re-enroll via the first-launch flow.

This is intentionally hands-on — the command center has no remote-recovery
path because the threat model assumes a compromised remote attacker should
NOT be able to reset auth from outside.

## Operator handover note — VPS Caddy + WireGuard deferred

The v1 design ships loopback-only — the FastAPI app binds to `127.0.0.1`,
and the only way to reach it is to be logged into the trading machine via
RDP / console. The design doc § Remote access enumerates the future shape:

- **VPS Caddy reverse proxy.** A Caddy instance on a VPS terminates TLS,
  authenticates the WireGuard tunnel's origin, and forwards `/api/*`,
  `/auth/*`, `/api/events` to the command center over the tunnel.
- **WireGuard tunnel.** Trading machine → VPS, single peer, key-pinned.
- **`security.yaml.webauthn.relying_party_id` adjusted** to the operator
  domain so passkey origin checks accept the VPS-hosted URL.
- **`AuthOverrides.cookies_secure=True`** flipped on (HTTPS-only flag now
  truthy).

Implementation is tracked as a future story (no ticket yet — file one when
the operator needs remote access). Until then:

- Do NOT bind to `0.0.0.0` thinking that suffices. WebAuthn origin checks
  pin the relying party to the configured host; binding wider just exposes
  the API to the LAN without auth working.
- Do NOT proxy through a generic forwarder (e.g. ssh -L 8080). The CSRF
  double-submit cookie check fails because the cookie was issued for the
  loopback origin.
- The deferred work is small (Caddy config + WireGuard install + the two
  config flips) but cross-cuts security review; treat it as its own work
  tree when scheduled.
