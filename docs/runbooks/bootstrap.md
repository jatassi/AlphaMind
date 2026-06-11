# First-time bootstrap

One-time first-run procedure standing up the seven NSSM services from a bare Windows
machine — Operator-supervised, not an agent route. The recurring deploy procedure is
[update-loop.md](update-loop.md).

## 2. First-time bootstrap

**Run § 2 exactly once, the very first time AlphaMind comes up on the
production machine.** Subsequent operations all go through update-loop.md § 1.

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
> is already bootstrapped and all subsequent operations go through update-loop.md § 1.
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
~6–8 min. This is the same band as the steady-state ~25–50 min scheduled
invocations document in invocations.md § 4; budget accordingly and don't restart
the process if it looks "stuck" inside that window — run the e2e progress
monitor (`scripts/verify/RUNBOOK_end_to_end_verification.md` § Monitoring
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

### 2.5 Install the six remaining NSSM services

Collector is already installed. Install the other six in dependency order
from an elevated PowerShell prompt at the repo root. `install_monitor_service.ps1`
and `install_safety_core_service.ps1` each install **both** their supervised
service and its dedicated watchdog — set the `ObjectName` on all four (each
watchdog must run under the same account so its `nssm restart <target>` has
permission):

```powershell
.\scripts\services\install_pipeline_scheduler_service.ps1
nssm set alphamind-scheduler ObjectName .\<YourUsername>     # prompts for pw

.\scripts\services\install_monitor_service.ps1
nssm set alphamind-monitor ObjectName .\<YourUsername>
nssm set alphamind-monitor-watchdog ObjectName .\<YourUsername>

.\scripts\services\install_safety_core_service.ps1
nssm set alphamind-safety-core ObjectName .\<YourUsername>
nssm set alphamind-safety-core-watchdog ObjectName .\<YourUsername>

.\scripts\services\install_command_center_service.ps1
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

### 2.6 Start the six new services

Start each supervised service **before its watchdog** (a watchdog probes its
target's heartbeat and would restart a service that hasn't beaten yet):

```powershell
nssm start alphamind-scheduler
nssm start alphamind-monitor
nssm start alphamind-monitor-watchdog
nssm start alphamind-safety-core
nssm start alphamind-safety-core-watchdog
nssm start AlphaMindCommandCenter
Get-Service alphamind-collector, alphamind-scheduler, alphamind-monitor, alphamind-monitor-watchdog, alphamind-safety-core, alphamind-safety-core-watchdog, AlphaMindCommandCenter
```

All seven should be `Running`.

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
[command-center.md](command-center.md) § Register the first passkey for details and §
Adding a passkey for enrolling a backup authenticator.

### 2.8 Verify green

```bash
set -a && source <(tr -d '\r' < .env) && set +a && \
    uv run python scripts/verify/verify_command_center.py
```

Expect `7/7 checks passed`. AlphaMind is live in paper-trading mode.

---

