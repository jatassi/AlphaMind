# 01a — Per-cadence default-on stall watchdog

## Goal

Convert `MonitorSupervisor`'s stall watchdog ([ALP-768](https://linear.app/alphamind-jatassi/issue/ALP-768/monitor-hangs-not-crashes-on-a-trading-stream-oserror-detached)/819) from opt-in + single-global-timeout to default-on + per-task-cadence. Today only tasks that happen to call `MonitorSupervisor.beat()` are watched (3 of \~10), and the one global `watchdog_stall_timeout_seconds` (3600s) bounds tasks whose cadences span 1s (`bracket_stops`) to 60s (`breach_loop`), so the safety-critical 1s loop inherits a \~1h detection latency. This story delivers a `supervised_loop(name, cadence_seconds)` heartbeat seam every run-forever loop drives so beating is automatic, derives each task's stall bound from its declared cadence, makes a registered-but-never-watched task loud at startup rather than silently uncovered, and converts the currently-unwatched *leaf* tasks as proof. It is the Defect A foundation every other story in this tree builds on.

## Reading

* `src/alphamind/execution/continuous_monitor/supervisor.py` — `MonitorSupervisor`: `register_task`, `beat`, `_heartbeats`, `_watchdog_loop`, `run`/`_stop_event` (the current opt-in + global-timeout mechanism to replace).
* `src/alphamind/config/models/continuous_monitor.py` + `config/continuous_monitor.yaml` — `watchdog_stall_timeout_seconds` (the global knob this story removes), `supervisor_shutdown_timeout_seconds` (keep).
* `src/alphamind/execution/continuous_monitor/__main__.py` — the `register_task` call sites for `realized_vol_refresh` and the control surface; [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768)'s `beat=lambda: supervisor.beat(...)` wiring for the fill consumer.
* `src/alphamind/execution/continuous_monitor/entry_window/task.py`, `borrow_accrual/task.py`, `activities_backfill/task.py`, and `__main__.py`'s `realized_vol_refresh` loop — the four leaf-task `while True` loops this story converts.
* `ALP-819` — the `beat()` + `os._exit(1)` → NSSM pattern this story generalizes (its fill/breach/bracket beats keep working unchanged through this story).
* [continuous-monitor-runtime.md](<docs/design/05-execution-layer/continuous-monitor-runtime.md>) — supervisor + NSSM restart contract; [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) § shutdown-timeout origin.

## Depends on

* None — foundation. ([ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768) is the merged substrate; related, not a blocker.)

## Scope

In scope: `src/alphamind/execution/continuous_monitor/supervisor.py`; `src/alphamind/config/models/continuous_monitor.py`; `config/continuous_monitor.yaml`; the **control-surface** `register_task` **call** in `__main__.py`; and the **four leaf-task loop bodies** (`entry_window`, `borrow_accrual`, `fill_backfill`, `realized_vol_refresh`) plus their wiring. Tests at `tests/execution/continuous_monitor/test_supervisor.py` (+ touched leaf-task test modules).

### 1\. The `supervised_loop` heartbeat seam

Add `MonitorSupervisor.supervised_loop(name: str, cadence_seconds: float) -> AsyncIterator[None]`: an async iterator a run-forever task drives as `async for _ in supervisor.supervised_loop(name, cadence): <body>`. It records a beat for `name` at the top of every iteration, then sleeps `cadence_seconds` before the next yield (so the loop body no longer owns its own trailing `asyncio.sleep`). `cadence_seconds` is the **heartbeat interval** (how often the loop iterates and beats), *not* the task's functional work interval — a task that acts on a daily wall-clock schedule (e.g. `borrow_accrual`) still iterates on a short heartbeat cadence and checks the wall clock inside the body. Stream poll-loops that beat per poll slice instead receive the existing per-call `beat(name)` (the path the 02a kernel drives) and declare their poll cadence the same way; both `supervised_loop` and a per-slice `beat` register the cadence used for the bound.

### 2\. Per-task stall bound; remove the global timeout

The watchdog bound for a watched task is `cadence_seconds * watchdog_cadence_multiplier`, unless an explicit `stall_timeout_seconds` override is supplied for an irregular task. Add `watchdog_cadence_multiplier` to `ContinuousMonitorConfig` (value in `config/continuous_monitor.yaml`, not a literal in code) and **remove** `watchdog_stall_timeout_seconds` — it is fully superseded. `_watchdog_loop` checks each watched task against its own bound; the check interval keys off the smallest active bound, not a global.

### 3\. Default-on: loud on an uncovered task

`register_task` gains `watched: bool = True`. A task registered `watched=True` (the default) that has not beaten within a startup grace window emits a `log.warning` naming it as registered-but-uncovered — so a future task that forgets the seam is loud, not silently outside the net. The control-surface HTTP server (`await server.serve()`, no natural per-iteration heartbeat) is the one task registered `watched=False`, with a one-line reason; port-level liveness for it is a noted follow-up, not built here.

### 4\. Convert the leaf tasks

Convert `entry_window`, `borrow_accrual`, `fill_backfill`, and `realized_vol_refresh` to drive their loop through `supervised_loop` (each declaring its own heartbeat cadence; `borrow_accrual` uses a short heartbeat cadence and checks for its daily wall-clock tick inside the body). These four are owned end-to-end here (no other story touches them).

### Out of scope

* The loop bodies and wiring of `fill_stream_consumer` (02a), `bracket_stops` (02b), `greeks_refresh` (02c), `breach_loop` (02d), `underlying_stream` (03) — those stories adopt the seam for their own tasks and declare their own cadences. This story does not edit their `task.py` or `wiring.py`. [ALP-819](https://linear.app/alphamind-jatassi/issue/ALP-819/monitor-fill-stream-still-hangs-on-trading-stream-winerror-121-alp-768)'s existing fill/breach/bracket `beat()` wiring keeps working unchanged until those stories migrate it.
* Stream frame-staleness / proactive reconnect — the distinct timeout in 02a (decision E).

## Acceptance criteria

- [ ] `supervised_loop(name, cadence)` beats `name` at the top of each iteration and paces the loop at `cadence`; a loop driven by it is watched without any hand-wired `beat()`.
- [ ] A watched task's bound is `cadence_seconds * watchdog_cadence_multiplier`; an explicit `stall_timeout_seconds` override takes precedence — verified for a 1s-cadence task and a 60s-cadence task tripping `os._exit(1)` at different bounds (no shared global).
- [ ] `watchdog_stall_timeout_seconds` is removed from the model and yaml; nothing reads it.
- [ ] A `watched=True` task that never beats logs a startup-grace warning naming it; a `watched=False` task (the control surface) is never tripped and emits no such warning.
- [ ] The four leaf tasks drive their loops through `supervised_loop` and are watched (a stalled leaf task trips the watchdog in a regression test); `borrow_accrual` beats on its short heartbeat cadence while waiting for its daily tick (a stub clock advanced minutes, not a day, keeps it un-tripped).
- [ ] `watchdog_cadence_multiplier` loads from `config/continuous_monitor.yaml`; `tests/config/test_snapshot.py` is re-pinned for the config change.
- [ ] Graceful shutdown still cancels the watchdog and all tasks within `supervisor_shutdown_timeout_seconds` (no regression of the [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) path).
- [ ] `uv run pytest tests/execution/continuous_monitor/test_supervisor.py -n auto` passes; lint + mypy + lint-imports clean.

## Verification

Unit tests over `MonitorSupervisor` with a fake clock: `supervised_loop` beats + paces; per-task bounds trip independently; the `watched=False` opt-out never trips and never warns; a never-beating `watched=True` task warns at startup grace; `borrow_accrual`'s short-heartbeat loop is not tripped across a multi-minute (sub-daily) wait; graceful shutdown is unaffected. Re-pin the config snapshot. Confirm `git grep watchdog_stall_timeout_seconds` returns nothing.