# 01 — Package skeleton + monitor entry point + config + runtime design doc

## Goal

Stand up the `alphamind.execution.continuous_monitor` package with its asyncio supervisor, a `python -m alphamind.execution.continuous_monitor run` entry point modeled on the collector's `__main__.py`, the `MonitorSession` typed record, the logging-to-`monitor.log` helper, and the `config/continuous_monitor.yaml` + Pydantic `ContinuousMonitorConfig` model. Author a new short doc `docs/design/05-execution-layer/continuous-monitor-runtime.md` that rationalizes the monitor's process model alongside the existing collector. No live tasks (fill consumer, breach loop, greeks refresh, etc.) are wired in this story — only the skeleton + the supervisor's task-registration surface that later stories plug into.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 4 Continuous monitor — the five responsibilities this package eventually owns
* `docs/design/01-data-layer/collector/runner.md` — APScheduler + NSSM + logging pattern this story mirrors at the process-supervision level (not the runtime model — the monitor is asyncio, not threaded)
* `src/alphamind/collector/__main__.py` — the `run` / `bootstrap` / `catch-up` subcommand layout this story adapts to a single `run` subcommand
* `src/alphamind/collector/scheduler.py` § `_configure_logging`, `start_blocking`, `mark_orphan_runs` — supervision shape this story adapts (the monitor adds no `mark_orphan_runs` equivalent in this story; future stories may add similar startup hygiene)
* `src/alphamind/config/load.py` — the resolved-config loader story 01 extends to include `continuous_monitor`
* `src/alphamind/config/models/` (e.g., `scheduler.py`, `run_types.py`) — Pydantic config-model patterns to mirror
* Parent issue `ALP-123` § Pre-resolved configuration decisions (A), (E) — defaults this story bakes in

## Depends on

None.

## Scope

In scope under `src/alphamind/execution/continuous_monitor/`. Tests at `tests/execution/continuous_monitor/`. Config files under `config/`. Design doc under `docs/design/05-execution-layer/`.

### 1\. Runtime design doc

Author `docs/design/05-execution-layer/continuous-monitor-runtime.md` (new file). Sections:

* **Process model.** The monitor is a separate long-running process supervised by NSSM as `alphamind-monitor`, parallel to `alphamind-collector`. The runtime is asyncio (websocket + periodic timers + in-memory caches); the collector's `BlockingScheduler` + threads model is a poor fit for the monitor's hot path.
* **Shared utilities reused from collector.** `python-dotenv` for `.env` loading, a logging-helper pattern matching `collector.scheduler._configure_logging` (own `monitor.log`), `default_session_factory()` for the shared SQLite database.
* **Logging.** TimedRotatingFileHandler at `~/AlphaMind/logs/monitor.log` (or `%USERPROFILE%\AlphaMind\logs\monitor.log` on Windows), daily rotation, 30-day retention.
* **Configuration surface.** `config/continuous_monitor.yaml` keyed by `ContinuousMonitorConfig` (Pydantic). Class A tunables; values listed under parent issue § Pre-resolved decision (E).
* **Relationship to the collector.** Parallel NSSM services. Both read the same SQLite database in WAL mode (the collector writes market-data tables; the monitor reads them — story 03a reads the collector-populated `options_chains` for IV). No shared executor.
* **Out of scope for this doc.** Per-responsibility runtime detail lives in `architecture.md § 4`; this doc covers the process shape only.

Add one line to `docs/design/05-execution-layer/architecture.md` § 4 cross-referencing the new runtime doc.

### 2\. Pydantic config model

`src/alphamind/config/models/continuous_monitor.py` — frozen Pydantic `ContinuousMonitorConfig` with the field set named in parent issue § Pre-resolved decision (E). Use `Literal["alpaca-iex"]` for `underlying_stream_provider` (single value today; story-02b leaves room for additions). Add field validators rejecting non-positive cadences / counts.

### 3\. yaml config

`config/continuous_monitor.yaml` with the documented defaults plus inline comments referencing `continuous-monitor-runtime.md`. Match the indentation / comment style of `config/collector_schedule.yaml`.

### 4\. Config loader wiring

Extend `src/alphamind/config/load.py` so the resolved-config object exposes `continuous_monitor: ContinuousMonitorConfig`, loaded from `config/continuous_monitor.yaml`. Add a cross-reference check in `src/alphamind/config/validation/cross_reference.py` only if needed (likely no cross-references in this story; revisit if 03a needs one against `data_sources.yaml`).

### 5\. `MonitorSession` typed record

`src/alphamind/execution/continuous_monitor/session.py` — frozen Pydantic record:

```python
class MonitorSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    session_id: str  # f"mon-{started_at:%Y%m%dT%H%M%SZ}-{token_hex(4)}"
    started_at: datetime  # tz-aware UTC
    mode: Literal["paper", "live"]
```

Plus a `def new_session(*, mode: Literal["paper", "live"]) -> MonitorSession` factory that produces a fresh session id at call time. Session ID derivation must be deterministic given inputs; use `secrets.token_hex(4)` for the random suffix.

### 6\. Logging helper

`src/alphamind/execution/continuous_monitor/logging_setup.py` — `def configure_monitor_logging() -> None` mirroring `collector.scheduler._configure_logging` shape, writing to `monitor.log` instead of `collector.log`. Handle Windows + POSIX path resolution consistently with the collector helper (which uses `%USERPROFILE%` / `~`).

### 7\. Asyncio supervisor

`src/alphamind/execution/continuous_monitor/supervisor.py`:

```python
class MonitorSupervisor:
    def __init__(self, *, session: MonitorSession, config: ContinuousMonitorConfig) -> None: ...
    def register_task(
        self, *, name: str, coro_fn: Callable[[MonitorSession, ContinuousMonitorConfig], Awaitable[None]],
    ) -> None: ...
    async def run(self) -> None: ...  # starts all registered tasks, waits for stop, cancels + awaits on shutdown
    def request_stop(self) -> None: ...  # SIGINT / SIGTERM handler entry point
```

In this story the registry is empty by default — later stories call `supervisor.register_task(...)` from their own modules. `run()` installs SIGINT / SIGTERM handlers that call `request_stop()`. On stop, each task is cancelled in registration order; the supervisor awaits each with a per-task timeout (configurable; default 5 s) and logs per-task exit reason (cancelled cleanly, timed out, exception). Any task exception propagates out of `run()` so the NSSM-restart policy can kick in.

### 8\. `__main__.py` entry point

`src/alphamind/execution/continuous_monitor/__main__.py`:

```
usage: python -m alphamind.execution.continuous_monitor run [--mode {paper,live}]
```

Loads `.env`, loads resolved config, configures logging, generates a `MonitorSession`, instantiates `MonitorSupervisor`, calls `asyncio.run(supervisor.run())`. Mode defaults to `paper`. No other subcommands in this story (no `bootstrap` / `catch-up` analogues — the monitor is forward-only).

### 9\. Package `__init__.py`

Re-export `MonitorSession`, `new_session`, `MonitorSupervisor`, `configure_monitor_logging`.

### Out of scope

* Any concrete task (fill consumer, underlying stream, breach loop, greeks refresh, options bracket-stop, emergency trigger) — those land in later stories.
* NSSM install / uninstall scripts — story 05.
* Reading `options_chains` for IV — story 03a.
* Wiring `compose_phase_1_enforcement` into the pipeline — story 02a.

## Acceptance criteria

- [ ] `docs/design/05-execution-layer/continuous-monitor-runtime.md` exists with the six documented sections and is cross-referenced from `architecture.md § 4`.
- [ ] `ContinuousMonitorConfig` Pydantic model is importable from `alphamind.config.models.continuous_monitor` with the six fields at the design's named defaults; constructing with `breach_evaluation_cadence_seconds=0` raises `ValidationError`.
- [ ] `config/continuous_monitor.yaml` parses cleanly via the resolved-config loader and exposes the values on `resolved_config.continuous_monitor`.
- [ ] `MonitorSession` is a frozen Pydantic record; `new_session(mode="paper")` returns a session whose `session_id` matches `^mon-\d{8}T\d{6}Z-[0-9a-f]{8}$`.
- [ ] `configure_monitor_logging()` installs a `TimedRotatingFileHandler` writing to `~/AlphaMind/logs/monitor.log` (or `%USERPROFILE%\AlphaMind\logs\monitor.log` on Windows) with daily rotation and 30-day retention.
- [ ] `MonitorSupervisor.register_task(...)` accepts a coroutine factory and stores it under the given name.
- [ ] `MonitorSupervisor.run()` with no registered tasks completes cleanly when `request_stop()` is called.
- [ ] `MonitorSupervisor.run()` with one registered task starts it, lets it run, and cancels + awaits it on stop; the task's cancellation is observable via a test fixture.
- [ ] `MonitorSupervisor.run()` installs SIGINT and SIGTERM handlers that trigger `request_stop()`.
- [ ] A task that raises propagates out of `MonitorSupervisor.run()` after sibling tasks have been cancelled and awaited.
- [ ] `python -m alphamind.execution.continuous_monitor run --mode paper` starts the process; sending SIGINT shuts it down cleanly within the configured shutdown timeout; the session-start line appears in `monitor.log`.
- [ ] `tests/execution/continuous_monitor/test_session.py`, `test_supervisor.py`, `test_logging_setup.py`, and `test_config.py` cover the criteria above and pass under `uv run pytest tests/execution/continuous_monitor/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes — no regressions in existing config-loader / cross-reference tests.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/execution/continuous_monitor/ -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: `python -m alphamind.execution.continuous_monitor run --mode paper` in a foreground shell; Ctrl-C terminates within \~5 s; `tail ~/AlphaMind/logs/monitor.log` shows the session-start and clean-shutdown lines.
* Lint clean per CLAUDE.md.