# Migration c5d8e9a1f4b2 ordering bug: UPDATE fires before CHECK constraint is dropped

## Symptom

`alembic upgrade head` against any debug or prod SQLite DB that has rows with `calibration_state = 'bootstrap'` fails with:

```
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) CHECK constraint failed: ck_distillation_ticker_baseline_calibration_state
[SQL: UPDATE distillation_ticker_baseline SET calibration_state = 'accumulating' WHERE calibration_state = 'bootstrap']
```

The four distillation tables (`distillation_ticker_baseline`, `distillation_pair_lag`, `distillation_contract_history`, `distillation_composite_state`) all carry a CHECK constraint of the form `calibration_state IN ('calibrated', 'bootstrap', 'unavailable')`. The migration writes `'accumulating'`, which the *existing* constraint rejects.

Reproduced today (2026-05-18) against `data/alphamind-debug-e2e.db` while running the end-to-end verification runbook's pre-flight `alembic upgrade head` step. A fresh re-snapshot from prod will hit the same failure because prod's snapshot still carries `'bootstrap'` rows and the same old CHECK.

## Evidence

* File: `src/alphamind/persistence/migrations/versions/c5d8e9a1f4b2_calibration_state_accumulating.py`
* `upgrade()` (lines 63-73) iterates all four tables emitting the UPDATE first (lines 65-71), THEN iterates again calling `_replace_check` to drop + recreate the CHECK (lines 72-73).
* The UPDATE runs while the OLD constraint (`'calibrated', 'bootstrap', 'unavailable'`) is still active and rejects the new `'accumulating'` value.

## Root cause

Ordering. SQLite enforces CHECK constraints at row write time, so the old constraint must be dropped *before* the UPDATE can succeed. The migration's docstring at lines 21-23 actually asserts the opposite invariant ("The UPDATE precedes the CHECK recreation so existing rows pass the new constraint at validation time") — that reasoning would only hold if CHECK were a deferred constraint, which it is not in SQLite.

## Scope

Fix `upgrade()` to:

1. Drop the OLD CHECK on all four tables (using `batch_alter_table` + `drop_constraint`).
2. Run the UPDATE on all four tables.
3. Create the NEW CHECK on all four tables.

Fix `downgrade()` symmetrically (currently has the same bug in reverse — it recreates the old CHECK before reverting the values, so the reverse UPDATE `accumulating → bootstrap` would fail against the freshly-installed old CHECK that excludes `'accumulating'`).

Update the docstring at lines 21-23 to reflect the corrected ordering.

## Acceptance criteria

* `DATABASE_PATH=data/alphamind-debug-e2e.db uv run alembic upgrade head` succeeds against a DB containing `'bootstrap'` rows.
* `uv run alembic downgrade c5d8e9a1f4b2~1 && uv run alembic upgrade head` round-trips cleanly against the same DB.
* A new test under `tests/persistence/` exercises the upgrade path against an in-memory DB seeded with at least one `'bootstrap'` row in each of the four affected tables, and asserts the row's `calibration_state` reads back as `'accumulating'` after upgrade.
* A symmetric test exercises `downgrade()` end-to-end.

## Verification

* `uv run pytest tests/persistence/ -n auto` (drop `--testmon` since this adds a new test file).
* `uv run lint-imports`.
* After landing, re-run the end-to-end verification runbook (`scripts/RUNBOOK_end_to_end_verification.md`) pre-flight against `data/alphamind-debug-e2e.db` and confirm `alembic current` matches `alembic heads`.

## Related

* Migration parent issue: [ALP-540](https://linear.app/alphamind-jatassi/issue/ALP-540/calibration-vocabulary-collapses-two-distinct-states-e2e-verification) (calibration vocabulary split — `bootstrap` → `accumulating` / `unavailable`).
* Blocks: any operator running the end-to-end verification runbook against a debug DB snapshotted prior to this migration landing.
