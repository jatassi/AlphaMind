# 04a — APScheduler driver (cron + market-calendar + dedup window)

## Goal

Ship the `AsyncIOScheduler`-based driver that registers one cron job per `scheduler.yaml` trigger key, gates each fire on (i) the NYSE trading calendar for weekday triggers and (ii) the overlap-dedup window across rolling triggers, and dispatches `run_invocation(trigger_type="scheduled", ...)` from story 03b on every fire that survives the guards. The driver registers as a `PipelineSupervisor` task from story 01 so SIGINT / SIGTERM cleanly shut it down. On per-invocation exceptions, the driver catches and logs (the daemon keeps running per parent decision (H)).

## Reading

* Parent issue `ALP-431` § Pre-resolved configuration decisions (E) — market-calendar gating rule, weekday vs weekend trigger names
* `docs/architecture/infrastructure.md` § Scheduling — trigger schedule, overlap deduplication semantics, market calendar usage, APScheduler v3 rationale
* `docs/design/configuration-management.md` § `scheduler.yaml` — `overlap_dedup_lookback_minutes`, `max_instances`, trigger keys
* `config/scheduler.yaml` — current trigger map (6 keys: pre_open, market_hours_rolling, pre_close, off_hours_rolling, weekend_saturday, weekend_sunday)
* `src/alphamind/collector/scheduler.py` § `register_jobs`, `_make_job` — collector precedent (thread-based; this story adapts the shape to `AsyncIOScheduler` + async job wrappers)
* `src/alphamind/config/models/scheduler.py` — `SchedulerConfig` with new fields from story 01 (`market_calendar_exchange`, `supervisor_shutdown_timeout_seconds`)
* `src/alphamind/config/models/run_types.py` — `RunType` enum members; the driver maps each trigger key to `RunType[key]`
* `src/alphamind/execution/state_persistence/tables/invocations.py` — query target for the dedup-window guard
* `src/alphamind/scheduler/orchestrator.py` (story 03b) — `run_invocation` is the dispatch target
* `src/alphamind/scheduler/supervisor.py` (story 01) — `PipelineSupervisor.register_task` is the registration surface
* `exchange-calendars` Python package docs — already a project dep; `get_calendar(name).is_session(date)` is the per-day predicate this story uses

## Depends on

* `ALP-445` (this work tree, story 03b) — `run_invocation` is the dispatch target.

## Scope

In scope under `src/alphamind/scheduler/driver.py`. Wires `PipelineSupervisor.register_task` from `__main__.py`. Tests at `tests/scheduler/test_driver.py`. No new config keys (uses the runtime knobs from story 01).

### 1\. Trigger key → `RunType` mapping

Every `RunType` member's string value matches the corresponding `scheduler.yaml` trigger key. The mapping is `RunType[trigger_key]` — no separate mapping table needed.

Validation: at driver startup, every key in `scheduler_config.triggers` must resolve to a `RunType` member. On mismatch, raise `ValueError(f"scheduler.yaml trigger {key!r} has no matching RunType member")` before installing jobs.

### 2\. Weekday-vs-weekend trigger split

Hard-coded set of weekday triggers that consult the market calendar:

```python
_MARKET_CALENDAR_GATED_TRIGGERS: frozenset[RunType] = frozenset({
    RunType.pre_open,
    RunType.market_hours_rolling,
    RunType.pre_close,
    RunType.off_hours_rolling,
})
```

`RunType.weekend_saturday` and `RunType.weekend_sunday` are NOT gated — they fire unconditionally on their named days. `RunType.emergency` (added in story 04b) is not a scheduled trigger; it never appears in `scheduler_config.triggers`.

### 3\. Market-calendar guard

```python
def _is_trading_day(calendar_name: str, on_date: date) -> bool:
    """Return True if `on_date` is a trading session on the named exchange."""
```

Uses `exchange_calendars.get_calendar(calendar_name).is_session(pd.Timestamp(on_date))`. Cache the calendar instance at driver construction (calendars are immutable).

### 4\. Overlap-dedup guard

```python
async def _is_within_dedup_window(
    session: AsyncSession,
    *,
    now: datetime,
    lookback_minutes: int,
) -> bool:
    """Return True if any invocation completed within the lookback window."""
```

Queries `invocations` for rows with `phase2_completed_at >= now - lookback_minutes` (treat NULL as "not completed"). If at least one exists, dedup-suppress the current rolling fire.

Dedup applies ONLY to rolling triggers, NOT to anchored triggers:

```python
_DEDUP_GATED_TRIGGERS: frozenset[RunType] = frozenset({
    RunType.market_hours_rolling,
    RunType.off_hours_rolling,
})
```

Pre-open, pre-close, weekend triggers are anchored — they bypass the dedup guard so the documented schedule is preserved even when a slow invocation finished moments before.

### 5\. The job wrapper

```python
def _make_scheduled_job(
    *,
    trigger_key: str,
    run_type: RunType,
    cron_expression: str,
    session_factory: async_sessionmaker[AsyncSession],
    scheduler_config: SchedulerConfig,
    process_lifetime_id: str,
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
) -> Callable[[], Awaitable[None]]:
    """Build the async coroutine APScheduler fires per trigger."""
```

The returned coroutine:

1. `now = datetime.now(UTC)`
2. **Market-calendar guard.** If `run_type ∈ _MARKET_CALENDAR_GATED_TRIGGERS` and `_is_trading_day(scheduler_config.market_calendar_exchange, now.date()) is False`: log `INFO scheduled trigger=<key> skipped — non-trading day`; return.
3. **Dedup guard.** Open a short read-only session. If `run_type ∈ _DEDUP_GATED_TRIGGERS` and `_is_within_dedup_window(session, now=now, lookback_minutes=scheduler_config.overlap_dedup_lookback_minutes) is True`: log `INFO scheduled trigger=<key> skipped — dedup window`; return.
4. **Dispatch.** Try:
   * `summary = await run_invocation(trigger_type="scheduled", trigger_source=trigger_key, trigger_reason=cron_expression, firing_run_type=run_type, ...)`
   * Log `INFO scheduled trigger=<key> completed invocation=<id> duration=<sec>s commands=<n>`
     Except Exception:
   * `log.exception("scheduled trigger=%s failed", trigger_key)`
   * **Do NOT re-raise.** Per parent decision (H), the daemon keeps running.

### 6\. `register_pipeline_jobs`

```python
def register_pipeline_jobs(
    *,
    scheduler: AsyncIOScheduler,
    scheduler_config: SchedulerConfig,
    session_factory: async_sessionmaker[AsyncSession],
    process_lifetime_id: str,
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
) -> None:
    """Register one CronTrigger job per scheduler_config.triggers entry."""
```

For each `(trigger_key, cron_expression)` pair:

* Resolve `run_type = RunType[trigger_key]`. Raise `ValueError` on mismatch.
* `scheduler.add_job(_make_scheduled_job(...), trigger=CronTrigger.from_crontab(cron_expression, timezone=scheduler_config.timezone), id=trigger_key, max_instances=scheduler_config.max_instances, coalesce=True, misfire_grace_time=60)`.

`coalesce=True` and `misfire_grace_time=60` mirror the collector's behavior. `max_instances=1` (from yaml) ensures one in-flight invocation at a time per APScheduler's safety net.

### 7\. `run_pipeline_scheduler_task` — the `PipelineSupervisor` task

```python
async def run_pipeline_scheduler_task(
    session: PipelineSession,
    *,
    scheduler_config: SchedulerConfig,
    session_factory: async_sessionmaker[AsyncSession],
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    venue_config: VenueConfig,
    execution_mode: ExecutionMode,
) -> None:
    """Build the AsyncIOScheduler, register jobs, start, and await cancellation."""
```

Behavior:

1. `scheduler = AsyncIOScheduler(timezone=scheduler_config.timezone)`
2. `register_pipeline_jobs(scheduler=scheduler, ..., process_lifetime_id=session.process_lifetime_id, ...)`
3. `scheduler.start()`
4. Log next-fire schedule for each registered trigger.
5. `await asyncio.Event().wait()` (or equivalent) — the task lives until cancelled.
6. On cancellation: `scheduler.shutdown(wait=True)` in a `finally`; log `INFO pipeline scheduler stopped`.

### 8\. Wire into `__main__.py`

Update the daemon-mode branch in `src/alphamind/scheduler/__main__.py`:

```python
supervisor.register_task(
    name="apscheduler",
    coro_fn=partial(
        run_pipeline_scheduler_task,
        scheduler_config=scheduler_config,
        session_factory=session_factory,
        archive_root=archive_root,
        config_dir=config_dir,
        env_path=env_path,
        venue_config=venue_config,
        execution_mode=execution_mode,
    ),
)
```

The supervisor calls the partial with `session: PipelineSession`; the function ignores everything but the session shape (the partial closes over the rest).

### Out of scope

* Emergency-invocation receiver — story 04b registers a separate `PipelineSupervisor` task.
* HTTP `/control/*` surface — deferred per parent decision (B).
* NSSM install scripts — story 05.
* End-to-end verify script — story 05.

## Acceptance criteria

- [ ] `register_pipeline_jobs(...)` registers exactly one APScheduler job per key in `scheduler_config.triggers`; each job's ID matches its trigger key.
- [ ] `register_pipeline_jobs` raises `ValueError` when a yaml trigger key does not resolve to a `RunType` member (e.g., a typo).
- [ ] `_make_scheduled_job` returns an awaitable that, when called with no fills / no commands fixture, dispatches `run_invocation(trigger_type="scheduled", ...)` and logs the completion line.
- [ ] On a non-trading day (e.g., 2026-12-25), the `pre_open` job logs `skipped — non-trading day` and does NOT dispatch.
- [ ] On a non-trading day, the `weekend_saturday` job DOES dispatch (per parent decision (E)).
- [ ] When a successful invocation completed 10 minutes before the next `market_hours_rolling` fire and `overlap_dedup_lookback_minutes=30`, the rolling job logs `skipped — dedup window` and does NOT dispatch.
- [ ] When the same setup applies to `pre_close` (anchored), the pre-close job DOES dispatch (anchored triggers bypass dedup).
- [ ] An exception raised by `run_invocation` inside `_make_scheduled_job` is logged via `log.exception` and does NOT propagate (the daemon continues).
- [ ] `run_pipeline_scheduler_task` starts the scheduler, logs the next-fire schedule for each trigger, and shuts down cleanly on cancellation.
- [ ] `python -m alphamind.scheduler run --mode paper` installs all six trigger jobs and the daemon stays running; SIGINT shuts down within `supervisor_shutdown_timeout_seconds`; the next-fire schedule appears in `pipeline.log`.
- [ ] `tests/scheduler/test_driver.py` covers the criteria above (including market-calendar and dedup branches via fixture clocks / fixture DBs) and passes under `uv run pytest tests/scheduler/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/scheduler/test_driver.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: `python -m alphamind.scheduler run --mode paper` against the paper DB starts the daemon; `tail ~/AlphaMind/logs/pipeline.log` shows six "next fire at ..." lines; Ctrl-C shuts down cleanly; the log shows the dedup / market-calendar suppression lines when a sub-window fire fires.
* Lint clean per CLAUDE.md.