# Troubleshooting — common operational gotchas

The living gotchas doc: symptom-indexed diagnoses accumulated from prod experience.
Agents append new gotchas here as they hit them (see the `operate-prod` skill's
self-correction duty).

## 8. Common operational gotchas

### 8.1 Service won't start, `.err.log` mentions `NoSuchTableError`

Migrations are behind. Run update-loop.md § 1.3–1.5.

### 8.2 Command center returns 404 at `/`

Frontend bundle missing. Run bootstrap.md § 2.3 (just the `bun install` + `bun run build`
part); no service restart needed — FastAPI's `StaticFiles` mount picks up
the new `dist` bundle on the next request.

### 8.3 NSSM stop hangs in `StopPending` past 30 s

The supervisor is blocked on a task that won't cancel gracefully within its
shutdown grace window (services.md § 7). Force the issue:

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
`source .env` under Git Bash. Re-source via the `tr -d '\r'` form (bootstrap.md § 2.1
CRLF gotcha) before running manual invocations. Services bypass this — they
read the env from NSSM's `AppEnvironmentExtra` so they're unaffected.

### 8.6 Operator browser session invalidated after every restart

`COMMAND_CENTER_SESSION_SECRET` is not pinned on the service environment.
Run the `nssm set AlphaMindCommandCenter AppEnvironmentExtra ...` step from
bootstrap.md § 2.5. Existing sessions stay invalidated this one time; subsequent
restarts preserve them.

### 8.7 Looking for `progress.jsonl` under an invocation archive and not finding one

Expected — monitoring.md § 5 explains. Prod uses the no-op progress emitter; the JSONL
artifact is part of a different harness. Use the SSE stream (monitoring.md § 5.1), the
structured pipeline log (monitoring.md § 5.4), or the `invocations` table (monitoring.md § 5.5)
instead.

### 8.8 Scheduled invocation didn't fire when invocations.md § 4's schedule said it would

Walk monitoring.md § 5.7's "Skipped invocations" checklist: NYSE holiday, daemon paused,
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
   could not resolve — but it runs on the monitor's own event loop, so it cannot
   catch a freeze of the loop itself.

3. **Out-of-process watchdog on the whole event loop (ALP-941).** The supervisor
   beats `monitor.heartbeat` each watchdog-loop pass; the separate
   `alphamind-monitor-watchdog` service probes the file and runs
   `nssm restart alphamind-monitor` when the beat is older than
   `monitor_watchdog_tick_seconds × watchdog_cadence_multiplier` (150 s default).
   This is the only layer that catches a **frozen event loop** (the 2026-06-09
   wedge: loop blocked, process alive, layers 1–2 starved with it). When it
   fires, `monitor_faulthandler.log` holds the frozen main-thread stack — the
   faulthandler deadman dumped it before the restart — so the blocking frame is
   preserved for diagnosis.

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

**Manual services.md § 7 restart is the fallback** when auto-recovery is itself suspect (e.g.
the StartTime is stale, no `8766` heartbeat is appearing, or `monitor.err.log`
shows a `reconnect budget exhausted` entry): `nssm stop alphamind-monitor` +
`nssm start alphamind-monitor` — no other service needs touching. Confirm by
StartTime + heartbeat, not `Get-Service`.

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
it has no SSE / `/events` channel (monitoring.md § 5.2) and writes nothing to the DB. Watch
`safety_core.log` for two distinct lines, and interpret them differently:

- **Breach** — `safety_core BREACH: broker snapshot breaches <rules> at <ts>`
  (logged `CRITICAL`). A gross-exposure or single-name-concentration limit is
  breached on the **broker snapshot**. Positions stay protected by the
  broker-enforced bracket floor; the safety core does **not** submit or trigger
  an emergency (monitoring.md § 5.7) — it detects and stays alive.

- **Price feed globally stale** — `safety_core: underlying price feed globally
  stale — writer appears wedged; stale=… missing=…` (logged `ERROR`). Every
  open-position ticker is simultaneously stale (older than
  `underlying_price_max_age_seconds`, 900 s) or never received — the safety
  core's price feed (subscribed to the **broker snapshot's** equity symbols) has
  gone cold. The safety core keeps beating its heartbeat and re-logs this each
  tick; if the feed task has *crashed*, the process exits and NSSM restarts it,
  but a connected-but-silent feed is logged, **not** auto-recovered (the safety
  core has no in-stream reconnect) — restart `alphamind-safety-core` (services.md § 7) if it
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

### 8.10 PM equity CLOSE / reduce — protective legs cancelled before the sell, remainder auto re-bracketed (ALP-937 / ALP-938)

A native equity bracket's take-profit + stop legs reserve 100% of the position's
shares at the broker (`held_for_orders`). So a PM-directed equity CLOSE (full **or**
partial) first cancels the broker-enforced protective legs via `submit_cancel`,
**then** submits the SIMPLE close sell. This is expected and visible in the
submission log as leg cancels immediately preceding the close order — not a bug.

Two operational consequences to know:

- **Partial-reduce remainder is auto re-bracketed (ALP-938).** A *partial* close
  cancels **all** protective legs (you cannot partially cancel an OCO leg), so the
  reduce briefly leaves the remaining shares broker-unprotected. Fill collection now
  marks the position with a durable `positions.reprotection_needed=1` flag, and a
  post-fill-collection re-bracket step — running every invocation right after fill
  collection commits, **before** the slow thesis/LLM steps — automatically re-protects
  the remainder: it submits a fresh standalone OCO (take-profit + stop) at the
  **original** protective levels for the remaining shares and appends the new legs onto
  the same still-ACTIVE bracket, **without waiting for the PM**. On success the flag
  clears. The continuous-monitor position-level max-loss guardrail backstops the brief
  window between the reduce filling and the re-bracket landing (same cancel-and-review
  model used for corporate actions). A partially-reduced equity position should
  therefore read with an ACTIVE bracket carrying the original CANCELLED legs **plus**
  fresh ACTIVE take-profit + stop legs; a position still showing `reprotection_needed=1`
  with all-CANCELLED legs after an invocation means the re-bracket submit failed (see
  next bullet).

- **"ALP-938 NAKED POSITION: re-bracket OCO submit FAILED" CRITICAL alert in
  `pipeline.log`.** If the automatic re-bracket OCO submit fails (broker gateway failure
  or permanent rejection — e.g. shares-not-available, OCO ineligibility), the step logs
  `ALP-938 NAKED POSITION: re-bracket OCO submit FAILED for <SYMBOL> ...` at CRITICAL,
  **leaves `reprotection_needed=1` set so the next invocation retries**, and never aborts
  the invocation. The remaining shares stay broker-unprotected and the monitor max-loss
  guardrail is the active backstop until a retry succeeds or the PM re-evaluates.
  **Operator action:** confirm the monitor is live (§ 8.9) and watch the named position's
  P/L; if the submit keeps failing across consecutive invocations, investigate the broker
  rejection in the submission log and — if the next scheduled PM pass is far off —
  consider a manual invocation (invocations.md § 3) so the PM re-brackets or closes the position.

- **"NAKED POSITION" CRITICAL alert in `pipeline.log` (close dispatch, ALP-937).** If the
  close sell itself is rejected *after* the protective legs were already cancelled, the
  dispatcher logs `ALP-937 NAKED POSITION: equity CLOSE of <SYMBOL> ...` at CRITICAL. The
  position is broker-unprotected and the monitor max-loss guardrail is the only backstop
  until the PM re-evaluates next invocation. **Operator action:** confirm the monitor is
  live (§ 8.9), watch the named position's P/L, and — if the next scheduled PM pass is far
  off — consider a manual invocation (invocations.md § 3) so the PM re-brackets or closes it.

### 8.11 Equity CLOSE rejected `position_state_drift` (ALP-943)

An equity CLOSE (PM-directed or engine-envelope) was rejected at dispatch by the
live-position drift guard: between the invocation snapshot the decision was made
on and the moment of execution, the broker position changed — it exited entirely
(a monitor re-protection stop or any protective leg filled), flipped side, or
shrank below the requested quantity. The guard re-checks the live broker
position (and, on a rejected protective-leg cancel, the leg's actual broker
state) before any close order reaches the wire; a flat or side-flipped position
rejects the command, and a shrunken one submits a sell clamped to the live
quantity instead. The submission log carries the rejection with
`gateway_reason=position_state_drift` plus a one-line message naming the symbol,
the expected side/qty, and what the broker actually reported, and the PM path
records a `command_abandoned` activity entry.

This is the guard working, not a fault. Without it, the sell executes against a
flat position and Alpaca opens an unmanaged opposite-side position
(`sell_to_open` — the 2026-06-09 −4 MRVL short).

**Operator action: none.** The position is already flat or smaller than the
projection believed — there is nothing left for the rejected CLOSE to do. The
next invocation's fill collection integrates the missed exit fills and the PM
re-evaluates with corrected state; protection for any surviving remainder is the
monitor's auto re-protection job (ALP-938, § 8.10). Only investigate if the same
symbol rejects across consecutive invocations — that means fill integration is
not catching the projection up to broker reality.

### 8.12 Pulled an installer-script change, but the live service still has the old config

`scripts/services/install_*_service.ps1` changes only affect *future* installs —
the update loop never re-runs installers, so an already-registered service keeps
whatever SCM/NSSM config it was installed with. Observed 2026-06-10: PR #350
deleted the command center's `DependOnService` edges from its installer, but
`sc.exe qc AlphaMindCommandCenter` still showed both edges until the one-time
`sc.exe config AlphaMindCommandCenter depend= /` from the commit's deploy note
was applied (with services stopped).

When a pulled commit touches `scripts/services/`, read its commit message for a
deploy note and apply the live-config step it names; verify with `sc.exe qc
<ServiceName>` / `nssm get <ServiceName> <parameter>` afterward (update-loop.md
§ 1.1 carries the scan step).

