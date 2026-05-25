## Problem

`collection_runs` rows in `status='running'` with `completed_at IS NULL` accumulate silently when a collection wrapper fails to update its row before the service dies. Nothing scans for or cleans up these zombies — they persist across normal service restarts indefinitely.

Discovered during the PR #16 restart on 2026-05-03: snapshot found 4 stuck rows from 2026-05-01 (3 days old) that survived all intervening restarts. Backfilled manually before restart so freshness checks had a clean baseline.

## Evidence

### Stuck rows from 2026-05-01

* `polygon.options` started 19:00:00.061894 UTC
* `polymarket.contracts` started 19:30:00.042026 UTC
* `kalshi.contracts` started 19:30:00.047692 UTC
* `polygon.equity` started 19:30:00.166084 UTC

### Anomaly classes

Two distinct patterns among the four rows, neither fully diagnosable from logs.

**(A)** `polymarket` and `kalshi` at 15:30 ET. `collector.log.2026-05-01` shows clean `start → done` pairs (106s and 156s, normal latencies). The `done` log only fires when `track_run.__exit__` runs the success branch *and* `repo.update_success(...).commit()` returns without raising (else the wrapper at `src/alphamind/collector/scheduler.py:191` logs `error`). Yet the row state never persisted to disk. WAL-durability theories don't fit — the next service restart was 5 hours later.

**(B)** `polygon.options` at 15:00 ET and `polygon.equity` at 15:30 ET. No scheduler `start`/`done`/`error` log entries at all for those firings. But DB rows exist with cron-second-aligned `started_at` timestamps. Implies either a code path that bypasses `_make_job._job` and writes to `collection_runs` directly, or a logging-emission ordering issue.

No `Traceback`, no `database is locked`, no `missed by` warnings in the May-1 14:00–17:00 ET log window.

## Proposed fix

In `start_blocking()` (or a new `_cleanup_stale_runs_on_startup` helper invoked before `register_jobs(sched)`), run one UPDATE at service startup:

```sql
UPDATE collection_runs
SET status = 'failed',
    completed_at = started_at,
    error_summary = 'stale running row cleaned up on service restart'
WHERE status = 'running' AND completed_at IS NULL;
```

This is safe because any genuinely-in-flight run from a previous process invocation cannot continue after that process dies — the row was already orphaned by definition. The new process will create fresh rows for any newly-fired jobs. No data loss. Idempotent.

A small unit test that inserts a fake `running` row, calls the cleanup helper, and asserts the row was fail-marked covers regression.

## Operational impact

Zero today. Zombies don't affect freshness checks, scheduling, or row counts in any current consumer. The fix is preventative — keeps `collection_runs` self-healing and keeps future debugging simpler. Low priority.

## Out of scope

Root-cause investigation of *why* commits in class (A) didn't persist, or *what* code path created class (B) rows without logs. Those are independently worth understanding but not blocked by this fix.