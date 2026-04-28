---
status: done
completed_date: 2026-04-27
commit_id: 592dcb3
---

# 08 — End-to-end loader entry point

## Goal

Land the single function the pipeline calls at invocation start to load, validate, resolve, and persist the configuration snapshot. The function ties together every prior story's output: read every YAML file, validate parse-time + cross-reference + semantic-self-test, run the composition resolver, persist the snapshot to the filesystem, and return the resolved config plus snapshot metadata. On any validation failure, abort the load (per `configuration-management.md § Validation`) — the caller propagates the abort upstream.

## Reading

- `docs/design/configuration-management.md` § Reload model — fresh-context reload at every invocation start
- `docs/design/configuration-management.md` § Validation — three layers, all run before any agent is invoked
- `docs/design/05-execution-layer/state-persistence.md` § Composition state fields — what the snapshot result feeds into the invocation record
- `src/alphamind/config/loaders.py`, `resolver.py`, `snapshot.py`, `validation/cross_reference.py`, `validation/semantic.py` — every prior story's surface

## Depends on

- 03a–03i (all flat-tail schemas)
- 04a–04e (all bundle schemas)
- 05 (resolver)
- 06a (cross-reference validators)
- 06b (semantic-self-test validators)
- 07 (snapshot persistence)

## Scope

In scope:
- `src/alphamind/config/__init__.py` (or `src/alphamind/config/load.py`, exposed through `__init__`) defining:
  - `LoadedConfig` (frozen dataclass): `resolved: ResolvedConfig`, `snapshot: SnapshotResult`. The single object the pipeline runs on for the invocation.
  - `load_full_config(*, config_dir: Path, env_path: Path, archive_root: Path, invocation_id: str, active_regime: Regime, active_mode: Mode, active_overlays: tuple[Overlay, ...], firing_trigger: RunType, today: date) -> LoadedConfig`
- The `load_full_config` execution sequence:
  1. **Parse-time validation.** Read every YAML file via the per-file loader (`AssetsConfig.model_validate(yaml.safe_load(...))` etc.) and the bundle loaders (`load_profiles`, `load_regimes`, `load_modes`, `load_overlays`, `load_run_types`). Any `ValidationError` propagates to the caller as a parse-time failure.
  2. **Cross-reference validation.** Read `.env` keys via `read_env_keys(env_path)`. Call `validate_cross_references(...)` with every loaded model + the env keys + `REGISTERED_TOOLS`. Any `CrossReferenceError` propagates.
  3. **Composition.** Call `compose_config(...)` with the runtime-resolved identity dimensions. The output is the `ResolvedConfig` for this invocation.
  4. **Semantic-self-test validation.** Call `enumerate_compositions(...)` to materialize the (profile × regime × mode × overlays × run_type) matrix; call `validate_semantic_invariants(...)` against the matrix and the YAML inputs. Any `SemanticInvariantError` propagates.
  5. **Snapshot persistence.** Call `persist_snapshot(resolved, archive_root=archive_root, invocation_id=invocation_id)`. Returns `SnapshotResult`.
  6. **Wrap.** Return `LoadedConfig(resolved=resolved, snapshot=snapshot_result)`.
- An end-to-end integration test (in `tests/test_config_loader.py` or similar) that:
  - Sets up a temporary `config/` tree using fixtures (or symlinks to the real `config/` tree if the shipped tree is internally consistent — preferred).
  - Sets up a temporary `archive_root` and `.env` (with placeholder values for every required `*_env` reference).
  - Calls `load_full_config(...)` end-to-end with `active_regime=Regime.normal`, `active_mode=Mode.normal`, `active_overlays=()`, `firing_trigger=RunType.pre_open`.
  - Asserts the call returns a `LoadedConfig` whose `resolved` carries the expected medium-profile rule values, the expected agent roster (10 agents), and whose `snapshot.path` points at a written file.
  - Asserts the snapshot file exists and parses as JSON.
  - Asserts the snapshot hash is a valid 64-hex string and matches `compute_snapshot_hash(serialize_resolved_config(loaded.resolved))`.
- Failure-injection tests: introduce one synthetic break per validation layer (parse, cross-reference, semantic) and assert the right exception type propagates.
- The function does **not** catch any exception. Failures abort the invocation upstream, per `configuration-management.md § Validation` ("A failure at any layer aborts the invocation and alerts the operator").

Out of scope:
- The invocation record write — owned by the not-yet-built state-persistence implementation. The caller takes `LoadedConfig.snapshot` and writes the hash + path + feature flags into the invocation record.
- Operator alerting on failure — the alert fires from the upstream layer that catches the exception (the pipeline scheduler, ultimately routing through the command center's alert registry).
- Logging — minimal tagged structured logging at major step boundaries is acceptable; the tests assert behavior, not log content.
- Determining `active_regime`, `active_mode`, `active_overlays`, `firing_trigger` — those come from the distillation layer, pipeline state, event calendar, and APScheduler trigger respectively. The loader receives them as parameters.

## Notes

**Why a single entry point.** The pipeline's invocation start is the only place this sequence runs. A single function makes the contract explicit and the test surface small. Subsequent layers (state-persistence record write, agent invocation, engine execution) all consume `LoadedConfig`.

**Path resolution responsibility.** `config_dir`, `env_path`, `archive_root` are absolute `Path` objects. The caller resolves any environment-variable expansion (e.g., Windows `%USERPROFILE%` from `MainConfig.paths.archive`) before passing them in. The loader is platform-agnostic.

**Re-reading `MainConfig.paths` after parse.** The first parse-time read produces `MainConfig`, which carries `paths.archive` as a literal string. The caller of `load_full_config` is responsible for expanding that string and providing the resolved `archive_root`. This pre-resolution step lives at the pipeline-runtime layer, not in the configuration-management feature.

**Composition matrix size in `enumerate_compositions`.** Per story 06b's notes, the worst-case matrix is 768 compositions, each computed in O(rule count) ≈ O(20) — fast. Profile-only or profile×regime narrowing is acceptable as long as the closure invariant is still verified. Test the tightest matrix the validator supports; loosen later if needed.

**`active_overlays` ordering.** The pipeline-runtime layer determines which overlays are active and in what order; the loader trusts the order it receives. Multiplicative composition is commutative for non-conflicting overlays, so order is review-surface only — but the snapshot hash is order-sensitive, which is the intended behavior (a different `active_overlays` order produces a different hash, even when arithmetic-equivalent).

**Test fixture strategy.** The simplest test is "the shipped `config/` tree loads cleanly under (medium, normal, (), pre_open)". This is the canonical happy-path regression test. Failure-injection tests use temporary fixture directories with a single perturbation. Avoid mutating the real `config/` tree in-place during tests.

**Frozen `LoadedConfig`.** Both `ResolvedConfig` (story 05) and `SnapshotResult` (story 07) are frozen dataclasses. `LoadedConfig` is also frozen. The loader's output is immutable; downstream consumers can compare two `LoadedConfig` instances by hash.

## Acceptance criteria

- [ ] `src/alphamind/config/load.py` (re-exported through `src/alphamind/config/__init__.py`) defines `LoadedConfig` and `load_full_config(...)`.
- [ ] `LoadedConfig` is a `dataclass(frozen=True, slots=True)` carrying `resolved: ResolvedConfig` and `snapshot: SnapshotResult`.
- [ ] `load_full_config` runs the documented six-step sequence in order.
- [ ] An integration test calls `load_full_config(...)` against the shipped `config/` tree (with a placeholder `.env` and a temp `archive_root`) and the call succeeds, returning a `LoadedConfig`.
- [ ] The integration test asserts `loaded.resolved.profile == Profile.medium`, `loaded.resolved.regime == Regime.normal`, `loaded.resolved.run_type == RunType.pre_open`, `loaded.resolved.enabled_agents` has 10 entries.
- [ ] The integration test asserts `loaded.snapshot.path.exists()` and the file content parses as JSON.
- [ ] The integration test asserts `loaded.snapshot.hash == compute_snapshot_hash(serialize_resolved_config(loaded.resolved))`.
- [ ] A failure-injection test asserts a parse-time failure (e.g., a malformed YAML in a fixture tree) raises `pydantic.ValidationError`, never `CrossReferenceError` or `SemanticInvariantError`.
- [ ] A failure-injection test asserts a cross-reference failure raises `CrossReferenceError`.
- [ ] A failure-injection test asserts a semantic-invariant failure raises `SemanticInvariantError`.
- [ ] A test asserts the loader does **not** catch any of the three exception types — they propagate to the caller.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
