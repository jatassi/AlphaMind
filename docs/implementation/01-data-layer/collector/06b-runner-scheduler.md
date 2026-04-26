---
status: not_started
completed_date:
commit_id:
---

# 06b — Runner & scheduler

## Goal

Implement the long-running `python -m alphamind.collector run` process that schedules and dispatches calls into the data sources library, plus the `catch-up` one-shot command and the `__main__.py` argparse dispatcher.

## Reading

- `docs/design/01-data-layer/collector/runner.md` — process model, executors, cadence, supervision
- `docs/design/01-data-layer/collector/data-sources.md` — collection function shape
- `docs/design/01-data-layer/collector/lifecycle.md` § Catch-up — `since=None` behavior

## Depends on

- 05a–05j (vendor adapters must expose `collect_*` functions)
- 04 (`_common.py` for config loading)

## Scope

In scope: under `src/alphamind/collector/` —
- `__main__.py` — argparse with three subcommands:
  - `run` → calls `scheduler.start_blocking()` (blocks; intended for NSSM)
  - `bootstrap` → calls `bootstrap.run_all(only_vendor=...)` from story 06a
  - `catch-up` → calls every vendor's `collect_*` function once with `since=None`, exits
- `scheduler.py`:
  - `build_scheduler(config)` — constructs `BlockingScheduler` with one `ThreadPoolExecutor(max_workers=1)` per vendor named in the config.
  - `register_jobs(scheduler, schedule_config)` — for each entry in `collector_schedule.yaml`'s `collectors:` map, registers an APScheduler cron job. Maps the dotted collector ID (e.g., `polygon.equity`) to the corresponding `data_sources.<vendor>.<domain>.collect_*` function via a small registry dict.
  - `start_blocking()` — convenience that builds + registers + starts.
  - Logging: each job logs start, end, success/failure, rows-written.
- `catchup.py`:
  - `run_all()` — iterates the same registry dict and calls each `collect_*` function with `since=None`. Sequential (no APScheduler).
- Logging setup: `TimedRotatingFileHandler` writing to `%USERPROFILE%\AlphaMind\logs\collector.log`, daily rotation, 30-day retention. `LOG_LEVEL` env override for development.
- Unit tests with mocked collection functions verifying:
  - APScheduler registers one job per `collectors:` entry, with the expected executor and `max_instances=1`.
  - Vendor-serialization: two jobs registered to the same executor do not run concurrently.
  - The `catch-up` command invokes every registered function with `since=None`.
  - The `__main__` dispatcher routes to the correct subcommand.

Out of scope:
- NSSM service registration (story 07).
- The bootstrap orchestrator implementation itself (story 06a).
- End-to-end verification (story 08).

## Notes

The collector → function registry is the central piece of glue between `collector_schedule.yaml`'s string IDs and the actual Python functions. Keep it explicit in `scheduler.py`:

```python
COLLECTORS = {
    "polygon.equity":             polygon.equity.collect_universe_bars,
    "polygon.equity_offhrs":      polygon.equity.collect_universe_bars,  # same function, different schedule
    "polygon.options":            polygon.options.collect_options_chains,
    "polygon.corporate_actions":  polygon.corporate_actions.collect_corporate_actions,
    "polygon.reference":          polygon.reference.collect_reference,
    "fred.macro":                 fred.macro.collect_series,
    # ...
}
```

`max_instances=1` on every job — prevents overlap when a long-running job's next fire arrives.

Scheduler timezone is `US/Eastern` per `collector_schedule.yaml.timezone`.

`catch-up` runs every collection function in a single sequential pass — vendor-serialization not needed since there's only one in-flight call at a time.

Job registration translates a single cron string to APScheduler `CronTrigger.from_crontab()`.

Logging tags every record with the `collector` name (e.g., `polygon.equity`) so log lines are greppable.

## Acceptance criteria

- [ ] `__main__.py` defines `run`, `bootstrap`, and `catch-up` subcommands via argparse.
- [ ] `scheduler.build_scheduler()` constructs one `ThreadPoolExecutor(max_workers=1)` per vendor from `data_sources.yaml`.
- [ ] `scheduler.register_jobs()` registers one APScheduler cron job per `collectors:` entry in `collector_schedule.yaml`.
- [ ] Every registered job has `executor=<vendor>` and `max_instances=1`.
- [ ] The `COLLECTORS` registry dict maps every cron entry's string ID to the corresponding `data_sources.<vendor>.<domain>.collect_*` function.
- [ ] `catchup.run_all()` invokes every `COLLECTORS` function with `since=None` sequentially.
- [ ] Logging uses `TimedRotatingFileHandler`, daily rotation, 30-day retention, file at `%USERPROFILE%\AlphaMind\logs\collector.log`.
- [ ] Unit tests verify two jobs on the same vendor executor do not run concurrently (use a fixture that asserts non-overlap).
- [ ] Unit tests verify `__main__` dispatcher routes correctly for each subcommand.
- [ ] `python -m alphamind.collector --help` lists all three subcommands.
- [ ] `uv run pytest` passes.
