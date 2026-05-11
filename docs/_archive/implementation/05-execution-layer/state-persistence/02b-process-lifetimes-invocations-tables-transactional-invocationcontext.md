# 02b — Process lifetimes + invocations tables + transactional InvocationContext

## Goal

Ship two SQLAlchemy tables (`process_lifetimes`, `invocations`) plus the transactional `InvocationContext` manager and a typed Pydantic façade for each. After this story, every pipeline invocation can register its provenance in a single transaction that other write paths join into; `process_lifetimes` carries one row per long-running process start (pipeline / monitor); `invocations` carries one row per pipeline invocation with `process_lifetime_id` FK + trigger metadata + composition/data-state fields. The `InvocationContext` is the substrate the configuration loader and the three blocked distillation stories ([ALP-100](https://linear.app/alphamind-jatassi/issue/ALP-100), [ALP-99](https://linear.app/alphamind-jatassi/issue/ALP-99)) opt into.

## Reading

* `docs/design/05-execution-layer/state-persistence.md` § Process lifetimes — full record shape (process role, git SHA, dirty flag, Python version, pip freeze hash + filesystem snapshot reference, SDK versions, OS release)
* `docs/design/05-execution-layer/state-persistence.md` § Invocation records — full record shape (execution scaffolding, trigger fields, code state, composition state, data layer state) plus filesystem snapshot layout for the per-invocation reference fields
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path — defines the atomic-transaction discipline `InvocationContext` enforces
* `docs/design/02-distillation-layer/threshold-calibration.md` § Update process — context for how the calibration-log commit-message convention threads through `git_sha` on the invocation row
* `src/alphamind/persistence/models.py` — existing SQLAlchemy `Base` + table patterns to mirror (look at `RegimeAdaptationStateRow` for the cleanest single-table example)
* `src/alphamind/persistence/migrations/versions/3fbbfd3049f5_add_regime_adaptation_state_table.py` — sibling-feature migration shape to mirror (single-table migration, idempotent up/downgrade)
* `src/alphamind/persistence/session.py` — `make_engine`, `make_session_factory`, the four SQLite pragmas already applied. Tests use `make_engine(":memory:")`
* `src/alphamind/execution/state_persistence/config.py` (from story 01) — `pip_freeze_snapshot_root` and `invocation_provenance_root` config knobs this story consumes for filesystem snapshot writes

## Depends on

* <issue id="71c92bc8-3b67-473b-9c34-82652af670df">ALP-354</issue> (this work tree, story 01) — `StatePersistenceConfig` is needed for the filesystem-snapshot root paths.

## Scope

In scope, all under `src/alphamind/execution/state_persistence/`. Tests at `tests/execution/state_persistence/`.

### 1\. `ProcessLifetimeRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/process_lifetimes.py`, define a `ProcessLifetimeRow(Base)` SQLAlchemy table mirroring the design doc's process-lifetime fields. Columns:

* `process_lifetime_id: TEXT PRIMARY KEY` — UUID-shaped string generated at process start
* `process_role: TEXT NOT NULL` — `"pipeline"` or `"monitor"` (CHECK constraint)
* `process_start_at: TEXT NOT NULL` — ISO 8601 UTC timestamp
* `process_pid: INTEGER NOT NULL`
* `hostname: TEXT NOT NULL`
* `git_sha: TEXT NOT NULL`
* `git_branch: TEXT NOT NULL`
* `git_dirty: INTEGER NOT NULL` — 0/1 boolean
* `python_version: TEXT NOT NULL`
* `pip_freeze_hash: TEXT NOT NULL` — SHA-256 hex
* `pip_freeze_snapshot_path: TEXT NOT NULL` — filesystem path under `pip_freeze_snapshot_root`
* `anthropic_sdk_version: TEXT NOT NULL`
* `claude_agent_sdk_version: TEXT NOT NULL`
* `os_release: TEXT NOT NULL`

### 2\. `InvocationRow` SQLAlchemy model

In `src/alphamind/execution/state_persistence/tables/invocations.py`, define `InvocationRow(Base)`:

* `invocation_id: TEXT PRIMARY KEY` — monotonically increasing string (e.g., `inv-2026-05-07T14:30:00Z-3a8f`)
* `process_lifetime_id: TEXT NOT NULL FK process_lifetimes.process_lifetime_id ON DELETE RESTRICT`
* `start_at: TEXT NOT NULL` — ISO 8601 UTC
* `phase1_completed_at: TEXT` (nullable until Phase 1 commits)
* `phase2_completed_at: TEXT` (nullable until Phase 2 commits)
* `trigger_type: TEXT NOT NULL` — CHECK in (`"scheduled"`, `"emergency"`, `"manual"`)
* `trigger_source: TEXT NOT NULL`
* `trigger_reason: TEXT NOT NULL`
* `git_sha_at_invocation: TEXT NOT NULL`
* `active_profile: TEXT NOT NULL`
* `active_regime: TEXT NOT NULL`
* `active_mode: TEXT NOT NULL` — CHECK in (`"normal"`, `"defensive_posture"`, `"halted"`)
* `active_overlays_json: TEXT NOT NULL` — JSON array of overlay names
* `resolved_config_hash: TEXT NOT NULL` — SHA-256
* `resolved_config_snapshot_path: TEXT NOT NULL`
* `feature_flags_snapshot_json: TEXT NOT NULL` — inline JSON
* `data_calibration_state_snapshot_path: TEXT NOT NULL`
* `data_source_freshness_json: TEXT NOT NULL`
* `fill_collection_summary_json: TEXT` (nullable until Phase 1 commits)
* `command_execution_summary_json: TEXT` (nullable until Phase 2 commits)
* `staleness_flag: INTEGER` (nullable until Phase 1; 0/1)
* `snapshot_metadata_json: TEXT` (nullable)

### 3\. Typed Pydantic façade

In `src/alphamind/execution/state_persistence/invocation_context/records.py`, define frozen Pydantic models `ProcessLifetimeRecord` and `InvocationRecord` that mirror the SQLAlchemy column shape. These are the typed handles callers operate on; the SQLAlchemy rows are the storage shape.

### 4\. `InvocationContext` transaction manager

In `src/alphamind/execution/state_persistence/invocation_context/context.py`, define an async context manager:

```python
class InvocationContext:
    async def __aenter__(self) -> InvocationHandle: ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
```

Behavior:

* On enter: open a SQLAlchemy `AsyncSession` transaction; INSERT the supplied `InvocationRecord` into `invocations`; return an `InvocationHandle` carrying the session reference and the invocation_id.
* On exit (no exception): commit the transaction.
* On exit (exception): rollback the transaction; re-raise.

The handle exposes the open session so subsequent stories' write paths (story 03's activity-log emission, stories 07–08's Phase 1/Phase 2 writes) can join the same transaction without re-discovering the session.

### 5\. Filesystem snapshot writers

In `src/alphamind/execution/state_persistence/invocation_context/snapshots.py`, ship two helpers:

* `write_pip_freeze_snapshot(process_lifetime_id, pip_freeze_text, root) -> str` — writes to `<root>/process_lifetimes/<id>/pip_freeze.txt`, returns the absolute path.
* `write_invocation_provenance_snapshots(invocation_id, resolved_config_dict, data_calibration_state_dict, root) -> tuple[str, str]` — writes both `resolved_config.json` and `data_calibration_state.json` under `<root>/invocations/<id>/`, returns the two paths.

### 6\. Alembic migration

A single migration file at `src/alphamind/persistence/migrations/versions/<rev>_add_invocations_and_process_lifetimes.py` creating both tables in one upgrade (the `invocations.process_lifetime_id` FK requires `process_lifetimes` to exist within the same migration). `down_revision` chains to the most recent existing migration (likely `3fbbfd3049f5_add_regime_adaptation_state_table.py`).

### Out of scope

* No `activity_log` table — story 03.
* No fill records — story 05.
* No `agent_calls` (deferred to feedback loop per parent issue Pre-resolved decision (B)).
* No write hooks into the configuration loader — that's [ALP-100](https://linear.app/alphamind-jatassi/issue/ALP-100)'s job once this substrate ships.

## Acceptance criteria

- [ ] `process_lifetimes` table exists with all 14 columns named above and the `process_role` CHECK constraint.
- [ ] `invocations` table exists with all 22 columns named above, the `trigger_type` and `active_mode` CHECK constraints, and a FK on `process_lifetime_id` ON DELETE RESTRICT.
- [ ] `ProcessLifetimeRecord` and `InvocationRecord` Pydantic models round-trip with their SQLAlchemy rows (write → read → equality).
- [ ] `InvocationContext` async context manager commits on clean exit and rolls back on exception, with a test parametrising both paths.
- [ ] `write_pip_freeze_snapshot` writes to `<root>/process_lifetimes/<id>/pip_freeze.txt` and creates the parent directories.
- [ ] `write_invocation_provenance_snapshots` writes `resolved_config.json` and `data_calibration_state.json` under `<root>/invocations/<id>/`.
- [ ] The Alembic migration upgrade creates both tables; downgrade drops them. `alembic upgrade head` then `alembic downgrade -1` is idempotent.
- [ ] `tests/execution/state_persistence/test_invocation_context.py` exists with at least: clean-commit test, exception-rollback test, FK-enforcement test (insert invocation referencing nonexistent process_lifetime fails), CHECK-constraint test on each enum.
- [ ] `tests/execution/state_persistence/test_snapshot_writers.py` exists with happy-path tests for both filesystem writers and a path-already-exists test.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` reports clean.
- [ ] `uv run pytest tests/execution/state_persistence/ -n auto` passes.

## Verification

Run the test suite (`uv run pytest tests/execution/state_persistence/ -n auto`). Run `uv run alembic upgrade head` against an in-memory DB and confirm both tables exist; run `alembic downgrade -1` and confirm clean drop. Spot-check `tests/execution/state_persistence/test_invocation_context.py`'s exception-rollback test for the assertion that the invocation row is NOT present after rollback.