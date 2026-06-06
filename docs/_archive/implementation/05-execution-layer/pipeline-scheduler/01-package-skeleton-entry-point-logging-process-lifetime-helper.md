# 01 — Package skeleton + entry point + logging + process-lifetime helper

## Goal

Stand up the `alphamind.scheduler` package with its asyncio supervisor, a `python -m alphamind.scheduler run` entry point (and `--once` flag for one-shot manual invocations), a `PipelineSession` typed record, the logging-to-`pipeline.log` helper, and the shared `process_lifetimes` row writer that this work tree (and later the continuous monitor) uses at startup. No live triggers / no orchestrator wiring in this story — only the skeleton + supervisor's task-registration surface that later stories plug into, plus the `process_lifetimes` row + helper that's load-bearing for every invocation's foreign key.

## Reading

* Parent issue `ALP-431` § Pre-resolved configuration decisions (A), (D), (G), (H) — defaults this story bakes in
* `docs/architecture/infrastructure.md` § Deployment + Process supervision — NSSM service shape + log file paths
* `docs/design/05-execution-layer/state-persistence.md` § Process lifetimes — the 13 row fields, FK relationship to `invocations`, append-only / immutable invariant
* `src/alphamind/execution/state_persistence/tables/process_lifetimes.py` — existing SQLAlchemy mapping (does not need modification)
* `src/alphamind/execution/state_persistence/invocation_context/records.py` — existing `ProcessLifetimeRecord` Pydantic façade + adapters (use as-is; this story builds the writer that uses them)
* `src/alphamind/execution/state_persistence/invocation_context/snapshots.py` — existing `persist_pip_freeze` helper this story calls
* `src/alphamind/collector/__main__.py` + `src/alphamind/collector/scheduler.py` § `_configure_logging` — supervision + logging precedent (collector is thread-based; this story adapts the *logging* shape, not the scheduler model)
* `ALP-432` (continuous monitor 01) § 7 Asyncio supervisor — the `MonitorSupervisor` pattern this story mirrors at `PipelineSupervisor`
* `scripts/install_collector_service.ps1` — NOT modified here; referenced only so the entry-point name + log paths story 05 will mirror are visible

## Depends on

None.

## Scope

In scope under `src/alphamind/scheduler/`. Shared helper under `src/alphamind/execution/state_persistence/`. Tests at `tests/scheduler/` and `tests/execution/state_persistence/`. No config edits, no design-doc additions.

### 1\. Shared process-lifetime helper

`src/alphamind/execution/state_persistence/process_lifetime.py` (new module):

```python
async def record_process_lifetime(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    process_role: Literal["pipeline", "monitor"],
    archive_root: Path,
) -> str:
    """Insert one `process_lifetimes` row at process start, return its id."""
```

Gathers:

* `process_lifetime_id`: `f"plt-{process_role}-{started_at:%Y%m%dT%H%M%SZ}-{token_hex(4)}"`
* `process_start_at`: tz-aware UTC, ISO-8601 with `Z` suffix
* `process_pid`: `os.getpid()`
* `hostname`: `socket.gethostname()`
* `git_sha`: `git rev-parse HEAD` via `subprocess.run`; raise on non-zero exit
* `git_branch`: `git rev-parse --abbrev-ref HEAD`
* `git_dirty`: `git status --porcelain` non-empty → `True`
* `python_version`: `sys.version`
* `pip_freeze_hash`: SHA-256 of `pip freeze` output
* `pip_freeze_snapshot_path`: filesystem path; calls `persist_pip_freeze(...)` from `snapshots.py` to write the full output under `<archive_root>/process_lifetimes/<id>/pip_freeze.txt`
* `anthropic_sdk_version`: `anthropic.__version__`
* `claude_agent_sdk_version`: `importlib.metadata.version("claude-agent-sdk")`
* `os_release`: `platform.platform()`

Builds `ProcessLifetimeRecord`, converts via `process_lifetime_record_to_row`, opens a fresh session from the factory, inserts and commits, returns the id. The transaction is its own — `process_lifetimes` is process-scoped, not invocation-scoped.

### 2\. `PipelineSession` typed record

`src/alphamind/scheduler/session.py`:

```python
class PipelineSession(BaseModel):
    model_config = ConfigDict(frozen=True)
    process_lifetime_id: str
    started_at: datetime  # tz-aware UTC
    mode: Literal["paper", "live"]
```

Plus `def new_session(*, process_lifetime_id: str, mode: Literal["paper", "live"]) -> PipelineSession` factory stamping `started_at = datetime.now(UTC)`.

### 3\. Logging helper

`src/alphamind/scheduler/logging_setup.py` — `def configure_pipeline_logging() -> None` mirroring `collector.scheduler._configure_logging` shape, writing to `pipeline.log` (under `%USERPROFILE%\AlphaMind\logs\` on Windows; `~/AlphaMind/logs/` on POSIX). TimedRotatingFileHandler, daily rotation, 30-day retention. Logger name `alphamind` so the collector + monitor share the root.

### 4\. Asyncio supervisor

`src/alphamind/scheduler/supervisor.py`:

```python
class PipelineSupervisor:
    def __init__(self, *, session: PipelineSession, shutdown_timeout_seconds: int) -> None: ...
    def register_task(
        self,
        *,
        name: str,
        coro_fn: Callable[[PipelineSession], Awaitable[None]],
    ) -> None: ...
    async def run(self) -> None: ...
    def request_stop(self) -> None: ...
```

In this story the registry is empty by default — later stories (04a APScheduler driver, 04b emergency receiver) call `register_task(...)`. `run()` installs SIGINT / SIGTERM handlers that call `request_stop()`. On stop, each task is cancelled in registration order; the supervisor awaits each with `shutdown_timeout_seconds` and logs per-task exit reason (cancelled cleanly, timed out, exception). Per-task exceptions propagate out of `run()` so NSSM restart kicks in for fatal supervisor errors — but per-invocation exceptions (later stories) are caught at the task level, not here.

### 5\. `__main__.py` entry point

`src/alphamind/scheduler/__main__.py`:

```
usage:
  python -m alphamind.scheduler run                          # daemon mode
  python -m alphamind.scheduler run --once <run_type>        # one-shot manual
      --reason "operator dry run"
      [--mode {paper,live}]
```

`run` (daemon):

1. `load_dotenv()`
2. Read `config/scheduler.yaml` for `supervisor_shutdown_timeout_seconds` (new field added in this story — see § 6)
3. `configure_pipeline_logging()`
4. `await record_process_lifetime(session_factory=..., process_role="pipeline", archive_root=...)` → `process_lifetime_id`
5. `session = new_session(process_lifetime_id=..., mode=...)`
6. `supervisor = PipelineSupervisor(session=session, shutdown_timeout_seconds=...)`
7. *Empty registry in this story — later stories add* `register_task` calls before this point.
8. `asyncio.run(supervisor.run())`

`run --once <run_type> --reason ...`:

1. Steps 1-5 above
2. Validate `<run_type>` parses as a `RunType` member; raise on bad input
3. Call `await run_invocation(trigger_type="manual", trigger_source="cli", trigger_reason=<reason>, firing_run_type=<run_type>, ...)` — **NOTE**: `run_invocation` lands in story 03b; in story 01, the `--once` branch raises `NotImplementedError("run_invocation not yet wired; ships in story 03b")` after argument parsing. Story 03b removes the `NotImplementedError` and adds the call. The CLI surface is committed here so story 05's verify script can target it.
4. Exit 0 on success, non-zero on exception

`--mode` defaults to `paper`. No other subcommands in this story.

### 6\. `SchedulerConfig` widening for runtime knobs

Per parent decision (D), extend `SchedulerConfig` in `src/alphamind/config/models/scheduler.py` with three new fields:

```python
emergency_poll_interval_seconds: int = Field(ge=1)
market_calendar_exchange: str = Field(min_length=1)
supervisor_shutdown_timeout_seconds: int = Field(ge=1)
```

Add the three fields to `config/scheduler.yaml` at the documented defaults: `emergency_poll_interval_seconds: 5`, `market_calendar_exchange: "XNYS"`, `supervisor_shutdown_timeout_seconds: 10`. Update the file's leading comment to note the new knobs are scheduler-runtime knobs (loaded directly at process start), not resolver-cascade values.

The resolver (`compose_config`) already exposes `scheduler: SchedulerConfig` on `ResolvedConfig`; no resolver change needed. Tests in `tests/config/test_scheduler.py` cover the new fields' validation (rejection of zero / negative / empty values).

### 7\. Package `__init__.py`

Re-export `PipelineSession`, `new_session`, `PipelineSupervisor`, `configure_pipeline_logging`.

### Out of scope

* Runtime-dimensions resolver — story 02.
* Invocation context assembly / row population — story 03a.
* `run_invocation` orchestrator body — story 03b (CLI shim raises `NotImplementedError` here).
* APScheduler driver — story 04a.
* Emergency receiver — story 04b.
* NSSM install scripts + e2e verify + runbook — story 05.

## Acceptance criteria

- [ ] `record_process_lifetime(...)` is importable from `alphamind.execution.state_persistence.process_lifetime` and, when called against an in-memory DB fixture, inserts exactly one row whose 13 fields match the gathered runtime values.
- [ ] `record_process_lifetime` raises `RuntimeError` (or propagates `CalledProcessError`) on a non-zero `git rev-parse HEAD` exit; the row is NOT inserted.
- [ ] `record_process_lifetime` writes `<archive_root>/process_lifetimes/<id>/pip_freeze.txt` and stores the absolute path in the row's `pip_freeze_snapshot_path` field.
- [ ] `PipelineSession` is a frozen Pydantic record; `new_session(process_lifetime_id="plt-pipeline-...", mode="paper")` returns an instance whose `started_at` is tz-aware UTC.
- [ ] `configure_pipeline_logging()` installs a `TimedRotatingFileHandler` writing to `~/AlphaMind/logs/pipeline.log` (or `%USERPROFILE%\AlphaMind\logs\pipeline.log` on Windows) with daily rotation and 30-day retention.
- [ ] `PipelineSupervisor.register_task(...)` accepts a coroutine factory and stores it under the given name.
- [ ] `PipelineSupervisor.run()` with no registered tasks completes cleanly when `request_stop()` is called.
- [ ] `PipelineSupervisor.run()` with one registered task starts it, lets it run, and cancels + awaits it on stop; the task's cancellation is observable via a test fixture.
- [ ] `PipelineSupervisor.run()` installs SIGINT and SIGTERM handlers that trigger `request_stop()`.
- [ ] A task that raises propagates out of `PipelineSupervisor.run()` after sibling tasks have been cancelled and awaited.
- [ ] `python -m alphamind.scheduler run --mode paper` starts the process; sending SIGINT shuts it down cleanly within `supervisor_shutdown_timeout_seconds`; the session-start line appears in `pipeline.log`; the `process_lifetimes` row is present in the DB at shutdown.
- [ ] `python -m alphamind.scheduler run --once market_hours_rolling --reason "test" --mode paper` raises `NotImplementedError("run_invocation not yet wired; ships in story 03b")` after argument parsing (the CLI surface itself parses cleanly).
- [ ] `SchedulerConfig` parses `config/scheduler.yaml` with the three new fields; constructing with `emergency_poll_interval_seconds=0` raises `ValidationError`.
- [ ] `tests/scheduler/test_session.py`, `test_supervisor.py`, `test_logging_setup.py`, `test_main.py`, and `tests/execution/state_persistence/test_process_lifetime.py` cover the criteria above and pass under `uv run pytest tests/scheduler/ tests/execution/state_persistence/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes — no regressions in existing config-loader / cross-reference tests.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/scheduler/ -n auto -v` and `uv run pytest tests/execution/state_persistence/test_process_lifetime.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: `python -m alphamind.scheduler run --mode paper` in a foreground shell; Ctrl-C terminates within \~10 s; `tail ~/AlphaMind/logs/pipeline.log` shows the session-start and clean-shutdown lines; `sqlite3 alphamind.db "SELECT process_lifetime_id, process_role FROM process_lifetimes ORDER BY process_start_at DESC LIMIT 1"` returns the newly-inserted row.
* Lint clean per CLAUDE.md.