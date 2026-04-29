---
status: in_progress
completed_date:
commit_id:
---

# 09 — End-to-end verification

## Goal

A single integration test that runs the full harness against a committed canonical fixture and verifies the produced report's invariants — the report exists at the right path, contains all five sections, lists the expected slice IDs, names the expected harness version, and is byte-deterministic across re-runs. Distinct from earlier stories' unit tests: this story exercises the CLI end-to-end with no mocks beyond the `datetime.now` fixture and a tmp-path-rooted output directory.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` — the full design; this story's verification covers the invariants the design promises
- `docs/design/02-distillation-layer/replay-harness.md` § Output — five sections, deterministic header
- `docs/design/02-distillation-layer/replay-harness.md` § Versioning — every report carries `harness_version` and the imported-distillation git SHA
- `docs/implementation/02-distillation-layer/13-end-to-end-verification.md` — sibling end-to-end story shape for the parent distillation work tree
- `docs/implementation/01-data-layer/collector/08-end-to-end-verification.md` — sibling pattern for collector E2E
- All preceding stories in this work tree

## Depends on

- 08 (the CLI being verified)

## Scope

In scope: under `tests/distillation/replay_harness/test_e2e.py` —

- **Canonical fixture.** A small, committed-to-the-repo fixture under `tests/fixtures/replay_harness/`:
  ```
  tests/fixtures/replay_harness/
    fixture_store/
      low_vol/
        slice_e2e_lowvol_0001/
          manifest.json
          raw_inputs.sqlite
      normal/
        slice_e2e_normal_0001/
          manifest.json
          raw_inputs.sqlite
    candidate_distillation.yaml
    baseline_distillation.yaml      # differs from candidate in volume_anomaly_sigma only
  ```
  The two `raw_inputs.sqlite` files are committed binary blobs — small (`< 1 MB` each — limit by using ~5 tickers × ~25 invocations). They are produced by a one-time generator script `tests/fixtures/replay_harness/generate_fixtures.py` that:
    - Spins up an in-memory SQLite with the runtime schema.
    - Inserts deterministic `asset_universe`, `sector_classification`, `ohlcv_bars`, `macro_observations`, `event_calendar`, and `prediction_market_snapshots` rows synthesized from a fixed RNG seed.
    - Writes the in-memory DB out to the slice file via SQLite `VACUUM INTO`.
    - Emits the corresponding `manifest.json` with explicit invocation timestamps spanning the slice.
  The generator script is checked in but not run in CI — it is operator-invoked when the fixture format changes (committed binary fixtures are stable). A single test (`test_generator_output_matches_committed_fixture`) compares a re-generated fixture against the committed one to catch drift.

- **Tests:**

  1. **`test_e2e_single_mode_writes_report`** — Invoke `cli.main` with `--candidate-config` only (canonical `distillation.yaml`), `--fixture-store-root` pointing at the committed fixture store, `--report-root` pointing at `tmp_path`, `--regimes low_vol,normal`. Assert:
     - Exit code 0.
     - `tmp_path / report_id / report.md` exists.
     - The file contains `# Replay harness report`, `## Per-regime flag-rate table`, `## Regime-label distribution`, `## Class B baseline shape summary`, `## Calibration-state breakdown`.
     - The file contains `**harness_version:** {HARNESS_VERSION}`.
     - The file contains the two slice IDs (`slice_e2e_lowvol_0001`, `slice_e2e_normal_0001`).
     - The header contains `**candidate_config_path:**` and `**candidate_config_sha256:**` followed by a 64-char hex hash.
     - The file does NOT contain `**baseline_config_path:**` (single mode).

  2. **`test_e2e_diff_mode_writes_diff_report`** — Same invocation plus `--baseline-config`. Assert:
     - Exit code 0.
     - The file contains `**baseline_config_path:**` and `**baseline_config_sha256:**`.
     - The flag-rate table contains a `Δ` column header for each regime.
     - At least one delta cell is non-zero (the configs differ in `volume_anomaly_sigma`, so flag rates must differ for `volume_anomaly`).

  3. **`test_e2e_curation_required`** — Invoke with `--regimes crisis` against the fixture (which has no `crisis` slices). Assert exit 1 and stderr contains `no fixture slices for regime 'crisis'`.

  4. **`test_e2e_determinism`** — Run the harness twice in single mode (with `datetime.now` mocked to a fixed UTC instant so the `report_id` and `generated_at` are stable). Read both `report.md` contents. Assert byte-equal.
     - The second invocation must use a different `report_root` (or the first's report directory is cleaned up between runs) to avoid `ReportAlreadyExistsError`.

  5. **`test_e2e_no_runtime_db_access`** — Invoke the CLI with the runtime DB path environment variables (e.g., `ALPHAMIND_DATABASE_PATH`) pointing at a path that does not exist. Assert exit 0 (the harness ignores the runtime DB entirely).

  6. **`test_generator_output_matches_committed_fixture`** — Run `generate_fixtures.py` to produce a fresh slice into a tmp_path; compare its `manifest.json` byte-for-byte against the committed `manifest.json`, and the `raw_inputs.sqlite` via `sqlite3` row-count + per-table CRC against the committed file. Drift means the operator changed the schema or generator and forgot to re-run `generate_fixtures.py`; the test surfaces the mismatch.

  7. **`test_e2e_report_id_format`** — From `test_e2e_single_mode_writes_report`'s output, parse the report directory name and assert it matches the documented format: `^\d{8}T\d{6}Z_[0-9a-f]{16}$` (single mode) or `..._[0-9a-f]{16}$` extended (diff mode covered by test 2).

- **Non-test artifacts:**
  - `tests/fixtures/replay_harness/README.md` (one-liner): "Canonical fixtures for the replay-harness E2E test. Re-generate with `python tests/fixtures/replay_harness/generate_fixtures.py` when the runtime schema or fixture format changes."
  - `tests/fixtures/replay_harness/generate_fixtures.py` — operator-invoked generator, idempotent, deterministic.

- **What this story does NOT verify:**
  - Per-cell numeric correctness of aggregation (story 06's unit tests cover that).
  - Per-section format correctness (story 07's unit tests cover that).
  - Per-argument parsing correctness (story 02's and 08's unit tests cover that).
  - Schema correctness of the slice's `raw_inputs.sqlite` (the generator's deterministic synthesis covers it; if the schema drifts, `test_generator_output_matches_committed_fixture` fails).
  - Performance (the harness is operator-invoked; this is informational, not a CI gate).

Out of scope:
- A CI workflow that re-runs `generate_fixtures.py` automatically (the binary fixtures stay committed; regeneration is operator-driven).
- A "nightly E2E" against larger fixtures.
- Comparing the harness's report against a `verify_*.py`-style golden Markdown file — golden files are brittle for byte-determinism tests of timestamps and hashes; the assertions above name the structural invariants instead.

## Notes

The committed binary fixture files are the smallest viable substrate that exercises every code path. The runtime schema landed in the parent distillation work tree (story 03), so the generator's output is the substrate the engine operates on; if the schema evolves, the generator and committed fixture need to evolve together. The drift-detection test (`test_generator_output_matches_committed_fixture`) is the canary.

Per the user's `feedback_avoid_numeric_anchors.md`, this story does NOT assert specific anomaly counts or flag rates against the committed fixture. The fixture is a structural smoke test; the assertions are on report shape, slice ID presence, and version-string emission.

Per `feedback_simplify_before_building.md`, do not pre-create a "test harness" fixture-builder class. The generator script is a few hundred lines of imperative Python writing rows; tests use `pathlib` and `subprocess` against the committed files. No abstraction layer.

Per `feedback_no_decision_trails.md`, the README under `tests/fixtures/replay_harness/` is one positive sentence — no "this fixture used to be X" history. Future readers see the current contract.

The `datetime.now(UTC)` mock for determinism uses `unittest.mock.patch` against `alphamind.distillation.replay_harness.cli.datetime` (or wherever the CLI's import lives). The patch must target the call site, not `datetime.datetime` globally. Sibling pattern: any existing tests in the project that mock `datetime.now` (search for examples to align style).

Story 06 of the parent distillation work tree adds normalization primitives; the synthesized `ohlcv_bars` rows in this story's generator must include `adj_*` and `unadj_*` paired columns per `storage.md § Cross-cutting rules`, otherwise the orchestrator's normalization step fails. The generator's row count is small enough to populate by hand.

Per `feedback_harness_calibrated_not_pessimistic.md`, the E2E test exercises the harness against the same primitives the runtime would use — no separate "stricter" or "looser" mode for the test. If the test reveals divergence between the harness and the runtime, that's a regression in the parent distillation work tree, not a harness fix.

## Acceptance criteria

- [ ] `tests/distillation/replay_harness/test_e2e.py` exists with the seven documented tests.
- [ ] `tests/fixtures/replay_harness/fixture_store/` contains valid slices for `low_vol` and `normal`.
- [ ] `tests/fixtures/replay_harness/candidate_distillation.yaml` and `baseline_distillation.yaml` exist; they parse via `load_candidate_config`; they differ in at least `volume_anomaly_sigma`.
- [ ] `tests/fixtures/replay_harness/generate_fixtures.py` exists, is deterministic, and produces output matching the committed fixture.
- [ ] `test_e2e_single_mode_writes_report` passes: exit 0, report file present, all five section headings emitted, `harness_version` line present.
- [ ] `test_e2e_diff_mode_writes_diff_report` passes: baseline config emitted in header, delta column present, at least one non-zero delta cell.
- [ ] `test_e2e_curation_required` passes: missing-regime exit 1 with documented message.
- [ ] `test_e2e_determinism` passes: two runs (with mocked timestamp) produce byte-identical `report.md`.
- [ ] `test_e2e_no_runtime_db_access` passes: invocation with bogus runtime DB path still exits 0.
- [ ] `test_generator_output_matches_committed_fixture` passes against the committed fixture.
- [ ] `test_e2e_report_id_format` passes against the directory naming pattern.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
