# Services: table, restarts, ports and paths

The seven NSSM services, restarting a single one, and the ports/paths reference.
The full-cycle deploy order is [update-loop.md](update-loop.md), and its
service-stop precondition applies to any restart that stops the monitor or
safety core — evaluate it there before stopping either.

The seven NSSM services are the supported runtime form on prod:

| Service                          | Module entry                                    | Log basename       |
|----------------------------------|--------------------------------------------------|--------------------|
| `alphamind-collector`            | `python -m alphamind.collector run`              | `collector.*.log`  |
| `alphamind-scheduler`            | `python -m alphamind.scheduler run`              | `pipeline.*.log`   |
| `alphamind-monitor`              | `python -m alphamind.execution.continuous_monitor run` | `monitor.*.log`    |
| `alphamind-monitor-watchdog`     | `python -m alphamind.execution.continuous_monitor watchdog` | `monitor_watchdog.*.log` |
| `alphamind-safety-core`          | `python -m alphamind.execution.continuous_monitor.safety_core run`      | `safety_core.*.log`          |
| `alphamind-safety-core-watchdog` | `python -m alphamind.execution.continuous_monitor.safety_core watchdog` | `safety_core_watchdog.*.log` |
| `AlphaMindCommandCenter`         | `python -m alphamind.command_center`             | `command_center.*.log` |

**Safety core + out-of-process watchdog (ADR-0004 / ALP-857).** Breach detection
+ price-staleness — the lone safety item with **no broker floor** — is isolated
into the `alphamind-safety-core` service, which reads the **broker snapshot** +
**REST latest-quote snapshots** (polled, no second market-data websocket —
ALP-940), beats a heartbeat file under
`%USERPROFILE%\AlphaMind\logs\safety_core.heartbeat`, and **writes nothing** to
the DB. The safety core polls REST quotes rather than opening its own
`StockDataStream`: Alpaca's free IEX plan allows exactly **one** authenticated
market-data websocket per account, so a second stream collided with the
monitor's (`connection limit exceeded`) and starved both feeds. REST latest-quote
is not connection-limited, so dropping it frees the single market-data connection
for the `alphamind-monitor` proper. The `alphamind-safety-core-watchdog` service is a **dedicated
out-of-process** watchdog: it probes that heartbeat file and runs
`nssm restart alphamind-safety-core` when it goes stale (a loop-resident watchdog
cannot catch a freeze of its own loop). The `alphamind-monitor` proper now runs
only **precision/data** tasks (options stops, greeks, entry-window, fill stream +
recovery sweep) and is fail-safe under the broker floor — a frozen monitor
degrades precision while positions stay broker-protected. The watchdog must run
under the **same Windows account** as the safety core so its `nssm restart` has
permission.

**Monitor watchdog (ALP-941).** The monitor gets the same treatment: its
supervisor beats `%USERPROFILE%\AlphaMind\logs\monitor.heartbeat` while its
event loop turns, and the `alphamind-monitor-watchdog` service probes that file
and runs `nssm restart alphamind-monitor` when the beat is older than
`monitor_watchdog_tick_seconds × watchdog_cadence_multiplier` (15 s × 10 =
150 s by default). The monitor's **in-process** stall watchdog runs on the very
event loop it guards, so a frozen loop (the 2026-06-09 wedge: 8 h of no fills
with NSSM "Running") starves it — only an out-of-process watchdog bounds that
failure. A frozen loop also self-captures its blocking frame: the faulthandler
deadman dumps the frozen stack to `monitor_faulthandler.log` before the restart
lands. The same-account rule applies to this watchdog too.

Logs land under `%USERPROFILE%\AlphaMind\logs\`. DB is
`%USERPROFILE%\AlphaMind\data\alphamind.db`. Invocation archives land under
`%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\`.


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
- `alphamind-monitor-watchdog`
- `alphamind-safety-core`
- `alphamind-safety-core-watchdog`
- `AlphaMindCommandCenter` (note: mixed case, no hyphen)

NSSM's `restart` is `stop` + `start`. The install scripts leave NSSM's default
stop-method timeouts in place (no explicit `AppStopMethod*` is set); in practice
a stop that sits in `StopPending` past ~30 s is wedged — see troubleshooting.md § 8.3. On stop,
SIGINT/SIGTERM **triggers** a co-operative shutdown: each in-flight task gets a
`CancelledError` and a bounded grace window to wind down — scheduler **10 s**,
monitor **5 s**, command center **30 s** (`supervisor_shutdown_timeout_seconds`
per daemon; the scheduler supervisor uses a manually-bounded wait rather than
`TaskGroup.__aexit__` so the timeout actually bounds exit). Tasks that don't exit
in the window are abandoned and NSSM/the SCM force-terminates the process; there
is no separate SIGTERM "fallback" the supervisor itself fires.

A plain `nssm restart <service>` works for any single service with all the
others running: no service declares an SCM dependency on another, so the SCM
never refuses the stop half of the restart. That is a deliberate topology
invariant (ALP-945, the 2026-06-10 incident — a `DependOnService` edge from
the command center made the SCM refuse every monitor-watchdog restart,
wedging the monitor for 42 minutes): **never declare an SCM dependency on a
watchdog-supervised service.**

**One restart gotcha that cost real diagnosis time (2026-06-02 monitor wedge):**

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
    forces `os._exit(1)` → NSSM restart) — but that watchdog runs on the very
    event loop it guards, so a freeze of the **loop itself** (the 2026-06-09
    ALP-941 wedge) starves it. The **dedicated out-of-process
    `alphamind-monitor-watchdog`** closes that gap: it probes the monitor's
    heartbeat file (`%USERPROFILE%\AlphaMind\logs\monitor.heartbeat`) and runs
    `nssm restart alphamind-monitor` when the beat goes stale past
    `monitor_watchdog_tick_seconds × watchdog_cadence_multiplier` (default
    15 s × 10 = 150 s). On a loop freeze the faulthandler deadman also dumps
    the frozen stack to `monitor_faulthandler.log` — read it to identify the
    blocking frame **before** diagnosing further. The monitor no longer runs
    breach detection, so a monitor wedge **degrades precision while positions
    stay broker-protected** (the broker floor + the isolated safety core
    hold). The **one residual exception is the control-surface HTTP server**,
    registered `watched=False` (it blocks in `await server.serve()`); that
    task is not auto-recycled in-process, though a full loop freeze that takes
    it down now is (via the external watchdog).

  For everything, confirm a restart by the process **StartTime**
  (fresh PID / StartTime) **and** a behavioral signal — the daemon's SSE
  heartbeat (`8765` / `8766`), the safety-core / monitor heartbeat files'
  mtime advancing, fresh structured-log lines, fills flowing — never by
  `Get-Service Status` alone. Read the StartTime with:
  ```powershell
  $p = (Get-CimInstance Win32_Service -Filter "Name='<svc>'").ProcessId
  (Get-Process -Id $p).StartTime
  ```
  See troubleshooting.md § 8.9 for the fill-stream and underlying-stream auto-recovery details.

**When the operator can restart a single service without coordinating the
others:**

| Restart this        | When                                                          | Side effects                                                                      |
|---------------------|---------------------------------------------------------------|-----------------------------------------------------------------------------------|
| collector           | After a `config/collector_schedule.yaml` or `config/data_sources.yaml` edit | Up to one cadence-cycle of dropped vendor reads                                   |
| scheduler           | After a `config/scheduler.yaml`, `config/run_types/*.yaml`, or `config/main.yaml` edit | Pause flag cleared; an in-flight invocation is cancelled mid-pipeline             |
| monitor             | After a `config/continuous_monitor.yaml` edit affecting precision/data tasks | Halt-mode flag persists; in-flight fill-stream reconnects from the recovery path. Its watchdog tolerates a restart within the stall bound (150 s default); for a longer outage stop `alphamind-monitor-watchdog` first |
| monitor-watchdog    | After a `monitor_watchdog_tick_seconds` or `watchdog_cadence_multiplier` edit | None to positions; the monitor is unsupervised against loop freezes for the restart window |
| safety-core         | After a `config/continuous_monitor.yaml` cadence/threshold or a `config/guardrails.yaml` / profile `gross_exposure_pct` / `position_max_size_pct` edit | Brief gap in breach/staleness detection only; positions stay broker-protected. Its watchdog tolerates a restart within the stall bound; for a longer outage stop the watchdog first (else it `nssm restart`s the core mid-restart) |
| safety-core-watchdog | After a `watchdog_cadence_multiplier` edit                   | None to positions; the safety core is unsupervised for the restart window         |
| AlphaMindCommandCenter | After a frontend rebuild or alert-rule edit                | Active SSE clients reconnect; operator sessions persist if `COMMAND_CENTER_SESSION_SECRET` is pinned (bootstrap.md § 2.5), else are invalidated |

**When to use the full update loop (update-loop.md § 1) instead of a single restart:**

- Any pull from main that touches `src/`, `pyproject.toml`, `uv.lock`, or a
  migration file. (Always run update-loop.md § 1 in this case — start by checking
  `git log --oneline ORIG_HEAD..` for migrations before assuming a single
  service restart suffices.)
- Anything you're uncertain about. The full loop is ~3 min on a clean run
  and idempotent — over-restarting costs nothing.

---


## 10. Reference: ports and paths

| What                         | Where                                                            |
|------------------------------|------------------------------------------------------------------|
| Scheduler `/control` + `/events` | `127.0.0.1:8765` (loopback only)                              |
| Monitor `/control` + `/events`   | `127.0.0.1:8766` (loopback only)                              |
| Command center API + UI          | binds `127.0.0.1:8090` (loopback only); **browse via `http://localhost:8090`** — WebAuthn relying-party id is `localhost` |
| Safety core heartbeat (no port)  | `%USERPROFILE%\AlphaMind\logs\safety_core.heartbeat` (file; the out-of-process watchdog probes it — the safety core has no control port and writes nothing to the DB) |
| Monitor heartbeat (file)         | `%USERPROFILE%\AlphaMind\logs\monitor.heartbeat` (the `alphamind-monitor-watchdog` service probes it; written by the monitor supervisor's watchdog loop — ALP-941) |
| Monitor freeze stack dump        | `%USERPROFILE%\AlphaMind\logs\monitor_faulthandler.log` (the faulthandler deadman dumps the frozen main-thread stack here when the monitor's event loop wedges) |
| Production DB                    | `%USERPROFILE%\AlphaMind\data\alphamind.db`                   |
| Daemon logs                      | `%USERPROFILE%\AlphaMind\logs\<daemon>.{out,err,log}.log` (`safety_core.*`, `safety_core_watchdog.*` for the safety-core services; `monitor_watchdog.*` for the monitor watchdog) |
| Invocation archives              | `%USERPROFILE%\AlphaMind\archive\<YYYY-MM-DD>\<invocation_id>\` |
| Agent-call provenance            | `%USERPROFILE%\AlphaMind\data\provenance\invocations\<invocation_id>\agent_calls\<agent_call_id>\` (one dir per LLM agent call; `system_prompt.md` + `output_schema.json` + `tools_definition.json` + `output.json`; paired with one `agent_calls` table row) |
| Retrospective reports            | `<repo>\data\retrospective_reports\<report_id>\report.md` (markdown body; the `retrospective_reports` row carries the metadata + `report_file_ref`) |
| Feedback-loop CLIs (feedback-loop.md § 9)         | `python -m alphamind.feedback_loop.{digest,validation,retrospective}.cli`; spine verify `scripts/verify/verify_feedback_loop.py` |
| Config files                     | `<repo>\config\*.yaml` (incl. `feedback.yaml` sample-size floors, `digest.yaml` shift thresholds), `<repo>\config\run_types\*.yaml`, `<repo>\config\profiles\*.yaml` |
| `.env`                           | `<repo>\.env`                                                  |
