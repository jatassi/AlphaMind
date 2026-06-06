# 03a — Invocation context assembly + row-metadata population

## Goal

Ship `build_invocation_record(...)` and `open_invocation(...)` — the helpers that gather all 22 `invocations`-row fields, persist the resolved-config snapshot + data-calibration snapshot, and open the per-invocation `InvocationContext` transaction. Every invocation flows through this entry point; the row's NOT-NULL composition-state fields all originate here. This unblocks story 03b's `run_invocation` orchestrator, which wraps the open handle with the actual phase wiring.

**Vocabulary translation (Mode → ActiveMode).** Story 02's `RuntimeDimensions.active_mode` is typed `Mode` (config-layer vocab: `Mode.normal | Mode.halt`). The row's `active_mode` column is typed `ActiveMode` (persistence-layer vocab: `"normal" | "defensive_posture" | "halted"`). Story 03a owns the translation: `Mode.normal → "normal"`, `Mode.halt → "halted"`. `"defensive_posture"` is unreachable from this story's caller until a future story lands the operator-pinning mechanism — see parent surfacing condition (iv).

## Reading

* Parent issue `ALP-431` § Pre-resolved configuration decisions (J) — bootstrap path for `data_calibration_state_snapshot_path` on first-ever invocation
* Parent issue `ALP-431` § Notes for the orchestrator — surfacing condition (iv) on Mode-resolution divergence (drives the translation above)
* `docs/design/05-execution-layer/state-persistence.md` § Invocation records — the 22-column shape, the four field groups (execution scaffolding / trigger / code state / composition state / data-layer state), the per-invocation provenance directory layout
* `docs/design/configuration-management.md` § Composition model + § Validation — invocation-time reload semantics, snapshot persistence
* `src/alphamind/execution/state_persistence/invocation_context/records.py` — `InvocationRecord` Pydantic façade + `invocation_record_to_row` adapter; the `ActiveMode` literal lives here (translation target)
* `src/alphamind/execution/state_persistence/invocation_context/context.py` — `InvocationContext` async context manager (used as-is)
* `src/alphamind/config/load.py` § `load_full_config` — returns `PipelineConfig` with `resolved: ResolvedConfig` + `snapshot: SnapshotResult` carrying `hash`, `path`, `feature_flags_snapshot`
* `src/alphamind/config/snapshot.py` § `persist_snapshot` + `_atomic_write` — writes `<archive_root>/invocations/<id>/resolved_config.json` (and the atomic-write primitive this story reuses for the calibration-state copy)
* `src/alphamind/distillation/calibration_snapshot.py` § `write_calibration_state_snapshot` — the writer that lays down `<base_path>/invocations/<id>/data_calibration_state.json` during Phase 2 of each invocation. **Does NOT currently expose a "latest snapshot path" helper.** Story 03a implements the directory-scan inline (see § 2 below).
* `src/alphamind/persistence/models.py` § `CollectionRuns` — per-collector last-success timestamp source for `data_source_freshness_json`
* `src/alphamind/config/models/modes.py` § `Mode` — the two-member enum story 02 returns
* `ALP-443` (this work tree, story 02) — `resolve_runtime_dimensions` produces the `RuntimeDimensions` this story consumes

## Depends on

* `ALP-443` (this work tree, story 02) — `resolve_runtime_dimensions` produces the runtime dims this story threads into `load_full_config`.

## Scope

In scope under `src/alphamind/scheduler/invocation.py`. Tests at `tests/scheduler/test_invocation_build.py` and `tests/scheduler/test_open_invocation.py`. No new persistence schema.

### 1\. `build_invocation_record`

```python
async def build_invocation_record(
    *,
    session: AsyncSession,
    process_lifetime_id: str,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    runtime: RuntimeDimensions,
    pipeline_config: PipelineConfig,
    archive_root: Path,
    invocation_id: str,
    now: datetime,
) -> InvocationRecord:
    """Compose all 22 InvocationRecord fields from the supplied inputs."""
```

Per-field population:

* `invocation_id` — caller-supplied (story 03b mints it)
* `process_lifetime_id` — from session (set by story 01's startup helper)
* `start_at` — `now` as ISO-8601 with `Z` suffix
* `phase1_completed_at`, `phase2_completed_at` — `None` at insert; downstream stamping done by `stamp_phase_completion` in story 03b
* `trigger_type` — caller-supplied
* `trigger_source` — caller-supplied (scheduler trigger key for scheduled; `"continuous_monitor"` for emergency; `"cli"` for manual)
* `trigger_reason` — caller-supplied (cron expr, breach detail, operator note)
* `git_sha_at_invocation` — `git rev-parse HEAD` subprocess; raise on non-zero
* `active_profile` — `pipeline_config.resolved.profile_label`
* `active_regime` — `runtime.active_regime.value`
* `active_mode` — translated from `runtime.active_mode` via inline mapping:
  * `Mode.normal → "normal"`
  * `Mode.halt → "halted"`
  * Implemented as a small `_mode_to_active_mode_literal(mode: Mode) -> ActiveMode` helper local to this module. Direct `.value` is wrong: `Mode.halt.value == "halt"`, not `"halted"`.
* `active_overlays_json` — `json.dumps([o.value for o in runtime.active_overlays])` (compact, key-sorted)
* `resolved_config_hash` — `pipeline_config.snapshot.hash`
* `resolved_config_snapshot_path` — `str(pipeline_config.snapshot.path)`
* `feature_flags_snapshot_json` — `json.dumps(pipeline_config.snapshot.feature_flags_snapshot, sort_keys=True)`
* `data_calibration_state_snapshot_path` — see § 2 below
* `data_source_freshness_json` — see § 3 below
* `fill_collection_summary_json`, `command_execution_summary_json` — `None` at insert; downstream stamping done by story 03b at end of each phase
* `staleness_flag` — `None` at insert; story 03b sets to `True` only if Phase 1's fill collection failed and the pipeline operated on stale state
* `snapshot_metadata_json` — `None` at insert; reserved for future per-invocation snapshot enrichment

### 2\. Data-calibration snapshot path

Per parent decision (J), this story owns the bootstrap. The path on the row points to the snapshot **the invocation operates on** — i.e., the prior invocation's output, copied into the current invocation's provenance directory. The current invocation's distillation will write its own snapshot during Phase 2 (via `write_calibration_state_snapshot`), overwriting the file. The row's recorded path remains valid (same path).

```python
def _persist_data_calibration_snapshot(
    *,
    archive_root: Path,
    invocation_id: str,
) -> Path:
    """Copy the most recent prior invocation's calibration state to this invocation's provenance dir."""
```

Behavior:

1. **Find the latest prior snapshot via directory scan.** Enumerate `<archive_root>/invocations/` subdirectories whose names match the invocation-id pattern `^inv-\d{8}T\d{6}Z-[0-9a-f]{8}$`. The id format sorts chronologically, so the lexicographically-greatest matching name is the latest. Skip the current `invocation_id` (this invocation's directory may or may not exist yet at scan time). For each candidate (greatest first), check whether its `data_calibration_state.json` exists; use the first match.
2. If a prior snapshot is found, copy its content to `<archive_root>/invocations/<invocation_id>/data_calibration_state.json` atomically using `_atomic_write` from `config/snapshot.py` (write to `.tmp`, fsync, rename).
3. If no prior snapshot is found (first-ever invocation), write `{}` to the target path atomically. Distillation's Phase 2 writer will replace the content at that same path.
4. Return the absolute path. The row's `data_calibration_state_snapshot_path` is `str(path)`.

The `alphamind.distillation.calibration_snapshot` module does not currently expose a "latest snapshot path" helper. Implement the directory-scan in this story's module; do not modify `calibration_snapshot.py`.

### 3\. Data-source freshness JSON

```python
async def _compute_data_source_freshness_json(session: AsyncSession) -> str:
    """JSON map of {provider: latest_successful_pull_isoformat or null}."""
```

Query `collection_runs` for the most recent `status='success'` row per provider; emit `{provider_name: completed_at_isoformat or null}` keyed by provider name (alphabetically sorted). The provider list is derived from `data_sources.yaml`'s top-level `providers` keys. Result is `json.dumps(...)` with `sort_keys=True`.

Empty-DB case: every provider key maps to `null`.

### 4\. `open_invocation` async context manager

```python
@asynccontextmanager
async def open_invocation(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    process_lifetime_id: str,
    trigger_type: TriggerType,
    trigger_source: str,
    trigger_reason: str,
    firing_run_type: RunType,
    runtime: RuntimeDimensions,
    archive_root: Path,
    config_dir: Path,
    env_path: Path,
    now: datetime,
) -> AsyncIterator[InvocationHandle]:
    """Build the record, persist snapshots, and open the InvocationContext."""
```

Behavior:

1. Mint `invocation_id`: `f"inv-{now:%Y%m%dT%H%M%SZ}-{token_hex(4)}"`.
2. Open a short read-only session for the freshness query (§ 3) — separate from the transaction the InvocationContext owns.
3. Call `load_full_config(config_dir=..., env_path=..., archive_root=archive_root, invocation_id=invocation_id, runtime=runtime, today=now.date())` → `PipelineConfig`. This step also persists the resolved-config snapshot via `persist_snapshot`.
4. Persist the data-calibration snapshot via `_persist_data_calibration_snapshot(...)`.
5. Compute `data_source_freshness_json` via `_compute_data_source_freshness_json(short_session)`.
6. Build the `InvocationRecord` via `build_invocation_record(...)`.
7. Construct `InvocationContext(session_factory=session_factory, record=record)`.
8. `yield` the handle from the context manager (`async with ctx as handle: yield handle`).

The `async with` semantics propagate exceptions correctly — Phase 1 / Phase 2 stamping in story 03b runs inside the `yield`'s suite.

### Out of scope

* Phase 1 fill collection / Phase 2 envelope submission — story 03b.
* `run_invocation` orchestrator body — story 03b.
* Updating the row's summary fields (`fill_collection_summary_json`, `command_execution_summary_json`, `staleness_flag`) — story 03b stamps these after each phase.
* Mode-enum extension / defensive_posture pinning — future work.
* Adding a "latest snapshot path" helper to `alphamind.distillation.calibration_snapshot` — out of scope; the directory scan lives inline in this story.

## Acceptance criteria

- [ ] `build_invocation_record(...)` is importable from `alphamind.scheduler.invocation` and returns an `InvocationRecord` whose 22 fields are all populated (Phase 1/2 timestamps + their summary JSONs + staleness_flag + snapshot_metadata_json are `None` at insert; the other 17 are non-None).
- [ ] `build_invocation_record` raises (or propagates `CalledProcessError`) on a non-zero `git rev-parse HEAD` exit.
- [ ] `build_invocation_record` populates `active_mode="normal"` when `runtime.active_mode == Mode.normal`.
- [ ] `build_invocation_record` populates `active_mode="halted"` when `runtime.active_mode == Mode.halt`.
- [ ] `_persist_data_calibration_snapshot` writes `<archive_root>/invocations/<id>/data_calibration_state.json` containing the prior snapshot's content when one exists.
- [ ] On a fresh `<archive_root>/invocations/` directory with no prior snapshot, `_persist_data_calibration_snapshot` writes `{}` to the target path and returns its absolute path.
- [ ] `_persist_data_calibration_snapshot` correctly identifies the lexicographically-greatest matching invocation-id directory as the source of the latest prior snapshot when multiple priors exist.
- [ ] `_compute_data_source_freshness_json` against a DB with `collection_runs` rows for `polygon` (last success today) and `fred` (last success yesterday) returns a JSON string containing both providers' ISO timestamps, alphabetically sorted.
- [ ] `_compute_data_source_freshness_json` against an empty DB returns a JSON map with every provider from `data_sources.yaml` mapping to `null`.
- [ ] `open_invocation(...)` mints an `invocation_id` matching `^inv-\d{8}T\d{6}Z-[0-9a-f]{8}$`.
- [ ] Entering `open_invocation` inserts exactly one row into `invocations` whose `start_at` matches the entry-time clock and whose `phase1_completed_at` / `phase2_completed_at` are NULL.
- [ ] Exiting the `open_invocation` context manager cleanly commits the open transaction; a row queried after exit still has `phase1_completed_at` / `phase2_completed_at` NULL because no phase stamping happened in this story.
- [ ] An exception raised inside the `open_invocation` `async with` body rolls back; no `invocations` row is visible to a fresh session post-exception.
- [ ] `tests/scheduler/test_invocation_build.py` and `tests/scheduler/test_open_invocation.py` cover the criteria above and pass under `uv run pytest tests/scheduler/ -n auto`.
- [ ] `uv run pytest -n auto` (full suite) passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/scheduler/test_invocation_build.py tests/scheduler/test_open_invocation.py -n auto -v` — every new test passes.
* `uv run pytest -n auto` — full suite green.
* Manual: `sqlite3 alphamind.db "SELECT invocation_id, active_regime, active_mode, active_overlays_json, resolved_config_hash, length(data_source_freshness_json) FROM invocations ORDER BY start_at DESC LIMIT 1"` after a test invocation shows all five fields populated and non-empty; `active_mode` is one of `"normal" | "halted"` (the row vocab).
* Lint clean per CLAUDE.md.