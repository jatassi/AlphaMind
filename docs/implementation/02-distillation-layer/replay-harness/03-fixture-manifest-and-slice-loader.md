---
status: in_progress
completed_date:
commit_id:
---

# 03 — Fixture manifest schema and slice loader

## Goal

Define the on-disk contract for a regime-stratified replay fixture (`data/replay_fixtures/{regime_label}/{slice_id}/`) — the `manifest.json` schema, the raw-input-table layout, and a Pydantic-validated loader that the engine (story 05) consumes. Slices are append-only, operator-curated; this story provides the read path the harness uses.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Fixture store — authoritative spec for slice layout, manifest fields, append-only invariant, curation cadence
- `docs/design/02-distillation-layer/replay-harness.md` § Inputs — fixture store as input #1 to the harness
- `docs/design/02-distillation-layer/replay-harness.md` § Process — what the engine does with a loaded slice (compute Class B baselines from the slice's history, run the orchestrator on the slice's invocation timestamps)
- `docs/design/01-data-layer/collector/storage.md` § Tables — every raw-input table the harness reads; slices snapshot a subset
- `docs/design/01-data-layer/collector/storage.md` § Cross-cutting rules — UTC ISO 8601 TEXT timestamp convention; the manifest's timestamp fields follow it
- `src/alphamind/persistence/models.py` — existing SQLAlchemy 2.0 declarative models for the raw-input tables
- `src/alphamind/config/models.py` — existing Pydantic patterns (`model_config = ConfigDict(frozen=True)`, `Field(..., ge=0)`, `model_validator(mode="after")`)
- Story 02 — package skeleton this module lands inside

## Depends on

- 02 (package skeleton)

## Scope

In scope: under `src/alphamind/distillation/replay_harness/fixtures.py` —

- **`SliceSource` enum** with members `LIVE_ARCHIVE` and `HISTORICAL_CURATED` (string values matching the design doc's `live_archive` / `historical_curated`).

- **`SliceManifest` Pydantic model** (frozen) with fields:
  - `slice_id: str` — non-empty; matches `^[a-z0-9_-]+$`. Equal to the directory basename containing the manifest.
  - `regime_label: str` — one of `low_vol`, `normal`, `elevated`, `crisis`. Equal to the parent directory's name.
  - `source: SliceSource` — discriminator naming the curation provenance.
  - `invocation_timestamps: list[str]` — non-empty, strictly monotonically increasing, each value an ISO 8601 UTC string ending `Z` (e.g., `2024-01-15T13:30:00Z`). The engine iterates these in order; each represents one orchestrator run.
  - `source_table_commit_hashes: dict[str, str] | None` — populated for `LIVE_ARCHIVE` slices (table name → SHA-256 of the table contents at slice creation time); null for `HISTORICAL_CURATED`.
  - `original_ingestion_timestamps: dict[str, str] | None` — populated for `HISTORICAL_CURATED` slices (table name → ISO 8601 UTC string of the original ingestion event); null for `LIVE_ARCHIVE`.
  - `replaces: list[str]` — supersession chain; an empty list means the slice is original. Each entry is a `slice_id` of a superseded slice in the same regime directory. Default `[]`.
  - `curation_notes: str` — non-empty; operator's commentary on why this slice exemplifies its regime.
  - `created_at: str` — ISO 8601 UTC string identifying when the slice was sealed.

  Cross-field validators:
  - `source == LIVE_ARCHIVE` ⇒ `source_table_commit_hashes` is non-null and `original_ingestion_timestamps` is null.
  - `source == HISTORICAL_CURATED` ⇒ `source_table_commit_hashes` is null and `original_ingestion_timestamps` is non-null.
  - `regime_label` matches one of the four canonical values.
  - `invocation_timestamps` is strictly ascending (no duplicates, no reversals).

- **`FixtureSlice` dataclass** (frozen) with fields:
  - `manifest: SliceManifest`
  - `slice_dir: pathlib.Path` — the absolute directory the slice was loaded from.

  The dataclass intentionally does not pre-load raw-input table contents. The engine (story 05) reads tables lazily into an isolated SQLAlchemy session via the `slice_dir`-relative SQLite snapshot file (`raw_inputs.sqlite`); doing the load in this story would couple manifest validation to engine concerns.

- **Raw-input snapshot layout** within each slice directory:

  ```
  data/replay_fixtures/{regime_label}/{slice_id}/
      manifest.json
      raw_inputs.sqlite        # SQLite file holding the snapshotted raw-input tables
  ```

  This story declares the path convention in source as a named constant `RAW_INPUTS_FILENAME = "raw_inputs.sqlite"`; story 05 connects to it. Distillation state tables (`distillation_*`) are NOT included in `raw_inputs.sqlite` — the engine spins them up empty per the design's "compute Class B baselines from the slice's own history" invariant.

- **`load_slice_manifest(slice_dir: pathlib.Path) -> SliceManifest`** — reads `slice_dir / "manifest.json"`, parses + validates against `SliceManifest`, returns it. Raises a typed `FixtureManifestError` (subclass of `ValueError`) with a clear message naming the offending field on validation failure. Raises `FixtureNotFoundError` (subclass of `FileNotFoundError`) when the manifest file is missing.

- **`load_fixture_slice(slice_dir: pathlib.Path) -> FixtureSlice`** — calls `load_slice_manifest`, asserts `slice_dir / RAW_INPUTS_FILENAME` exists (raises `FixtureNotFoundError` otherwise), validates `slice_dir.name == manifest.slice_id` and `slice_dir.parent.name == manifest.regime_label`, returns a `FixtureSlice`. Does not open the SQLite file.

- **`discover_fixture_store(root: pathlib.Path) -> dict[str, list[FixtureSlice]]`** — walks `root` (typically `data/replay_fixtures/`), finds every slice directory (any subdirectory two levels deep that contains a `manifest.json`), loads each via `load_fixture_slice`, returns a mapping `regime_label → [FixtureSlice, ...]`. The list is ordered by `created_at` ascending; superseded slices (whose `slice_id` appears in another slice's `replaces`) are filtered OUT — only the currently-authoritative slice per `slice_id` chain is returned. The function logs each discovered slice via the `alphamind` logger and skips (with a warning) directories that fail manifest validation rather than raising — operator-driven curation should not be blocked by one malformed slice. A counter of skipped slices is included in a returned `DiscoveryReport` shape (e.g., `dict[str, list[FixtureSlice]]` directly is fine; if a counter is needed, return a small named record). Implementation choice acceptable: either `dict[str, list[FixtureSlice]]` plus a separate `discovery_warnings: list[str]` returned by a paired helper, or a single dataclass `FixtureStore(slices: dict, warnings: list)`. Whichever, the warnings surface so story 08's CLI can print them.

- Unit tests under `tests/distillation/replay_harness/test_fixtures.py`:
  - Round-trip: a sample valid `manifest.json` (live_archive) loads via `load_slice_manifest` and re-serializes byte-equivalently after validation.
  - Round-trip: a sample valid `manifest.json` (historical_curated) loads.
  - Validation: `source: LIVE_ARCHIVE` with null `source_table_commit_hashes` raises `FixtureManifestError`.
  - Validation: `source: HISTORICAL_CURATED` with non-null `source_table_commit_hashes` raises.
  - Validation: non-monotonic `invocation_timestamps` raises.
  - Validation: an unknown `regime_label` raises.
  - Validation: empty `invocation_timestamps` raises.
  - Validation: a `slice_id` not matching its directory basename raises (test via `load_fixture_slice`).
  - Validation: a `regime_label` not matching its parent directory name raises (test via `load_fixture_slice`).
  - File-system: `load_fixture_slice` against a directory missing `manifest.json` raises `FixtureNotFoundError`.
  - File-system: `load_fixture_slice` against a directory missing `raw_inputs.sqlite` raises `FixtureNotFoundError`.
  - Discovery: a fixture-store root with three slices across two regimes loads all three; the regime → slices mapping is correct and ordered by `created_at`.
  - Discovery: a slice whose `slice_id` is listed in another slice's `replaces` does not appear in the returned mapping (only the superseder).
  - Discovery: a slice with a malformed manifest is skipped with a warning, not raised.

Out of scope:
- Reading or validating the contents of `raw_inputs.sqlite` (story 05 — the engine connects via SQLAlchemy).
- A fixture-creation CLI (operator-driven; out of scope per the design's Curation cadence section).
- Validating that the snapshotted raw-input tables match `storage.md` schema versions exactly (story 05 will surface this when it tries to query and fails).
- Computing or verifying `source_table_commit_hashes` (operator records them at curation; the harness only reads).
- Migrating fixtures across schema changes — when the data-layer schema evolves, the operator re-curates affected slices with new `slice_id`s and updates `replaces`.

## Notes

A slice's directory layout uses SQLite (rather than per-table parquet files) so the engine can connect via the existing SQLAlchemy session machinery without a separate parquet-loading step. The trade-off: SQLite snapshots are larger than parquet for the same data, but the harness runs offline against curated slices and the volume is bounded by 252-day persistence windows on a 65-ticker universe — sub-GB per slice. The choice keeps the engine code path identical to the live distillation reads.

The `replaces` chain is operator-managed. The harness validates that referenced slice IDs are well-formed strings but does not enforce that they exist on disk — a slice can supersede a slice that has since been deleted from the fixture store. The discovery pass filters superseded slices out by name; a missing referenced slice is fine.

`source_table_commit_hashes` for `LIVE_ARCHIVE` slices is operator-recorded at curation. The hash is over the SQLite table contents (e.g., `SHA-256(serialized rows ordered by primary key)`); the exact hashing scheme is operator-specified at curation time and not validated by the harness. The hashes serve as audit anchors in the report header (story 07).

Per `feedback_avoid_numeric_anchors.md`, do not put any numeric thresholds (slice age limits, minimum invocation counts) into validators here. The design doc's "spans enough invocations to populate the longest Class A persistence window" is operator-side curation discipline, not a harness-enforced floor.

Per `feedback_no_decision_trails.md`, the manifest's mutually exclusive `source_table_commit_hashes` / `original_ingestion_timestamps` fields are stated positively (one or the other based on `source`), not as a "use this if X, otherwise that" prohibition narrative.

The discovery function's "skip with warning" behavior on malformed slices is deliberate: paper trading produces curated slices over months, and one malformed manifest should not prevent the harness from running against the others. The warnings surface to the CLI so the operator can fix them off-line.

The `FixtureSlice` does not eagerly open `raw_inputs.sqlite` because the engine (story 05) needs full control of session lifetime — it spins up an isolated, in-memory database derived from the snapshot file rather than connecting directly. Keeping the loader cheap (manifest only) lets story 08 enumerate slices for the CLI without paying for SQLite reads on every regime.

## Acceptance criteria

- [ ] `SliceSource` enum exists with `LIVE_ARCHIVE` and `HISTORICAL_CURATED` members.
- [ ] `SliceManifest` Pydantic model exists with all documented fields, validators, and `frozen=True` config.
- [ ] `FixtureSlice` frozen dataclass exists with `manifest` and `slice_dir` fields.
- [ ] `RAW_INPUTS_FILENAME = "raw_inputs.sqlite"` named constant exists.
- [ ] `FixtureManifestError` and `FixtureNotFoundError` typed exceptions exist.
- [ ] `load_slice_manifest(path)` parses and validates, raising on schema/cross-field violations.
- [ ] `load_fixture_slice(path)` validates manifest + SQLite presence + directory-name agreement.
- [ ] `discover_fixture_store(root)` returns regime → slices mapping ordered by `created_at`, with superseded slices filtered out.
- [ ] All validation tests pass (live/historical mutual exclusion, monotonic timestamps, regime-label whitelist, empty timestamps, slice-id/regime mismatches).
- [ ] All file-system tests pass (missing manifest, missing SQLite, malformed-skipped-with-warning).
- [ ] All discovery tests pass (multi-regime listing, supersession filter, ordering).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
