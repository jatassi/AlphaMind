---
status: in_progress
completed_date:
commit_id:
---

# 08 — CLI entry point and provenance wiring

## Goal

Replace the story-02 sentinel `cli.main` with the production CLI: discover the fixture store, validate the candidate (and optional baseline) config, replay each regime's slices through the engine, aggregate, render, and write the report file. Wire the provenance metadata (timestamp, harness version, git SHA, fixture slice IDs consumed) and surface the curation-required error when a requested regime has no fixture. Single command-line invocation produces one report file.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Activation — authoritative CLI surface, the `--candidate-config` / `--baseline-config` / `--regimes` arguments, the curation-required-error invariant on missing fixtures, the read-only-on-runtime-state invariant
- `docs/design/02-distillation-layer/replay-harness.md` § Output — the `data/replay_reports/{report_id}/report.md` destination
- `docs/design/02-distillation-layer/replay-harness.md` § Versioning — the harness version and git SHA appearing in every report header
- `scripts/validate_universe.py` — sibling CLI pattern with argparse, exit codes, environment-driven config
- `scripts/verify_bootstrap.py` — sibling CLI pattern with database-path resolution and clear exit codes
- All preceding stories in this work tree — every primitive the CLI orchestrates

## Depends on

- 03 (fixture loader)
- 04 (config loader)
- 05 (engine)
- 06 (aggregation)
- 07 (renderer)

## Scope

In scope: under `src/alphamind/distillation/replay_harness/cli.py` —

Replace the story-02 sentinel `main()` body with the production runner. The `build_parser()` from story 02 stays; if any argument needs a tweak, document it in this story's Notes and update story 02's tests accordingly (frontmatter remains story 02's; this story does not edit story 02's file).

- **`main(argv: list[str] | None = None) -> int`** procedure:

  1. **Parse args** via `build_parser()`.

  2. **Resolve paths.** Convert `--candidate-config` and `--baseline-config` (if present) to absolute paths. Resolve the fixture store root from a new optional argument `--fixture-store-root PATH` (default: `data/replay_fixtures/` relative to the project root). Resolve the report output root from a new optional argument `--report-root PATH` (default: `data/replay_reports/`). Both new arguments are added in this story; update story 02's parser tests to account for them when this story dispatches.

  3. **Resolve regimes.** Parse `--regimes` if provided, else default to the four canonical labels. Validate each label is in `{low_vol, normal, elevated, crisis}`. Invalid label → exit 1 with a clear message.

  4. **Load configs.**
     - `candidate = load_candidate_config(args.candidate_config)`. On `CandidateConfigError` or `FileNotFoundError`, exit 1 with the wrapped message.
     - If `--baseline-config` is set, `baseline = load_candidate_config(args.baseline_config)` likewise.

  5. **Discover fixtures.** Call `discover_fixture_store(args.fixture_store_root)`. Capture `discovery_warnings` and print them to stderr (one per line, prefixed `warning:`).

  6. **Validate fixture coverage.** For every regime in `args.regimes`, the fixture store must have at least one slice. If any requested regime is missing slices, exit 1 with: `error: no fixture slices for regime '{label}'; curate one before replay`. This is the curation-required error from the design doc.

  7. **Run the engine, single mode.**
     - For each regime in `args.regimes` (sorted ascending), iterate the regime's slices in `created_at` order.
     - For each slice, call `replay_slice(slice, candidate)`. Collect results in `candidate_results: dict[str, list[SliceReplayResult]]`.
     - On any `replay_slice` exception, print the exception detail to stderr, log the slice ID, and exit 1. Do not continue on partial failure — a partial replay misleads the operator about regime coverage.

  8. **Run the engine, diff mode.** If `baseline` is set, repeat step 7 against `baseline` to produce `baseline_results`.

  9. **Aggregate.** Call `aggregate_replay_results(candidate_results, baseline_results)`. On `AggregationInputError`, exit 1.

  10. **Build provenance.**
      - `generated_at = datetime.now(UTC)`.
      - `report_id = compute_report_id(generated_at, candidate.content_hash, baseline.content_hash if baseline else None)`.
      - `git_sha = _resolve_git_sha()` — see helper below.
      - `fixture_slice_ids = {regime: [slice.manifest.slice_id for slice in candidate_results[regime]] for regime in args.regimes}`.
      - Construct `ReportProvenance` with these and the loaded configs.

  11. **Render and write.** Call `render_report(provenance, aggregated)` then `write_report(provenance, aggregated, args.report_root)`. On `ReportAlreadyExistsError`, exit 1 with: `error: report directory {path} already exists; refusing to overwrite (re-run with a different timestamp or remove the existing report)`.

  12. **Print success.** A single line on stdout: `wrote report: {absolute_path}`. Return 0.

- **`_resolve_git_sha() -> str`** module-private helper:
  - Runs `git rev-parse HEAD` in the project root via `subprocess.run` with `check=False`, `capture_output=True`, `text=True`, `timeout=5`.
  - Returns `stdout.strip()` on success, or the literal string `"(not in a git checkout)"` on any failure (non-zero return code, missing git binary, timeout, or stdout empty).
  - Never raises — the CLI prefers a recorded sentinel over an unhandled exception in the provenance pipeline.

- **Exit codes:**
  - `0` — report written successfully.
  - `1` — any operator-facing error (invalid argument, missing fixture, malformed config, replay failure, report-already-exists). One single error class; the message names the cause.
  - `2` — reserved by argparse for malformed CLI arguments. The story-02 sentinel exit (also `2`) is replaced here; a fresh argparse rejection still yields `2`.

  The `2` reservation conflicts with the story-02 sentinel. This story removes the sentinel test from `test_cli_main_sentinel.py`. New tests cover the production exits.

- **Logging.** Use the `alphamind` module logger. INFO-level lines for major milestones (configs loaded, N regimes resolved, slice X replay started, slice X replay finished in Y ms, aggregation complete, report written). Operator-facing messages go to stderr; success status goes to stdout (one line).

- **Environment.** No environment variables are required. The optional `ALPHAMIND_DATABASE_PATH` is *not* read — the harness is read-only on the runtime DB and uses the fixture store, not the runtime tables. If the env var is set, the CLI ignores it. The CLI does not emit a warning for the unused env var.

- **Unit tests** under `tests/distillation/replay_harness/test_cli.py` (replacing or augmenting story 02's `test_cli_main_sentinel.py`):
  - End-to-end happy path (single mode): build a tmp_path-rooted fixture store with one slice in `low_vol` and one canonical `distillation.yaml`. Invoke `main(["--candidate-config", str(yaml_path), "--fixture-store-root", str(fixture_root), "--report-root", str(report_root), "--regimes", "low_vol"])`. Assert exit 0, the printed path exists, and `report.md` contains the `report_id` from the header.
  - End-to-end happy path (diff mode): same fixture, two configs (canonical and one with looser `volume_anomaly_sigma`). Invoke with both `--candidate-config` and `--baseline-config`. Assert exit 0 and the rendered file contains `**baseline_config_path:**`.
  - Curation-required: invoke against a fixture root that has `low_vol` slices but no `crisis`, with `--regimes crisis`. Assert exit 1 and stderr matches the documented `no fixture slices for regime 'crisis'` message.
  - Invalid regime label: `--regimes unknown` → exit 1 with a clear message.
  - Malformed candidate config: a `distillation.yaml` with a missing required field → exit 1.
  - Existing report: invoke twice with deterministic timestamps (mocked `datetime.now`); the second invocation exits 1 with `ReportAlreadyExistsError`'s message.
  - Replay failure: a fixture whose `raw_inputs.sqlite` is corrupted → exit 1 with the engine's exception message; the destination report directory is not created.
  - Git SHA fallback: a tmp_path that's not a git repo (test runs in `subprocess.run` with `cwd=tmp_path` or by mocking `subprocess.run` to fail) — the rendered file contains `(not in a git checkout)` rather than failing the run.
  - Discovery warnings: a fixture root with one valid and one malformed slice — the malformed one is skipped, a `warning:` line appears on stderr, and the run still succeeds.

Out of scope:
- Multi-report invocation (one CLI call → one report; if the operator wants to compare three configs, they run the harness three times pairwise).
- Cron / scheduled invocation — operator-driven only.
- Any modification to the runtime database. The CLI MUST refuse to open the runtime DB; this is enforced structurally by the engine using an isolated in-memory session, not by the CLI inspecting the path.
- Streaming / progress output beyond the documented INFO lines (no progress bars).
- Markdown linting of the rendered output — the renderer's tests cover format determinism; linting is not the CLI's concern.

## Notes

The two new arguments (`--fixture-store-root` and `--report-root`) are added in this story rather than story 02 because they only matter once the runner is wired. Story 02's tests covered just the three core arguments from the design doc; this story extends the argparse parser as a side effect of replacing the sentinel. Update story 02's `test_cli_parser.py` here in this story (the test file is shared); document that the parser surface evolves between the two stories.

The "single error class" exit-code policy (1 for everything operator-facing) is deliberately less granular than `verify_bootstrap.py`'s typed exits. The harness has many more error sources (config malformed, fixture missing, replay failure, report exists) and a fine-grained code map would burden the operator with mapping. The error message is the contract; the exit code is just `success / failure / argparse`.

`generated_at = datetime.now(UTC)` is captured once at step 10, after replay completes, so the timestamp records when the *report was generated*, not when the *CLI started*. For long replays, the difference is observable; the design doc's "generation timestamp" reads naturally as "report-generation moment."

Per `feedback_simplify_before_building.md`, the CLI is one `main()` function with private helpers (`_resolve_git_sha`, `_load_configs`, `_run_replay`, `_handle_curation_error`). No `CLIRunner` class. Helpers are testable individually but the integration is one function.

Per `feedback_no_decision_trails.md`, the error messages state the cause positively (`no fixture slices for regime 'crisis'; curate one before replay`) rather than narrating "this validation was added because…". The message names the user-actionable next step.

Per `feedback_avoid_numeric_anchors.md`, the `subprocess.run` 5-second timeout for `git rev-parse` is a structural choice (rev-parse takes milliseconds in any healthy repo); not a tunable threshold.

Per `feedback_harness_calibrated_not_pessimistic.md`, the CLI does not introduce conservative defaults beyond what each primitive already provides. If the engine reports a regime label, the CLI passes it through; the CLI does not re-classify or apply safety floors.

The `subprocess.run(["git", "rev-parse", "HEAD"], ...)` invocation is the only shell-out in this work tree. Per project hygiene, do not introduce additional shell-outs without the user confirming — paths and timestamps come from Python's pathlib and datetime modules.

The `ReportAlreadyExistsError` path is reachable when the operator runs the harness twice within the same UTC second against identical configs. In practice this never happens (replay takes minutes) but the test fixture mocks `datetime.now` to make it reproducible. The error message names the path so the operator can `rm -rf` it deliberately if that's their intent.

## Acceptance criteria

- [ ] `cli.main()` runs the documented twelve-step procedure end-to-end.
- [ ] `--fixture-store-root` and `--report-root` arguments exist with documented defaults.
- [ ] `--regimes` defaults to all four canonical labels when not provided.
- [ ] An unrecognized regime label exits 1 with a clear message.
- [ ] A missing candidate-config file exits 1.
- [ ] A malformed candidate config exits 1 with the validation error.
- [ ] A requested regime with no fixture slices exits 1 with the curation-required message.
- [ ] A successful run writes `data/replay_reports/{report_id}/report.md`, prints the path, and exits 0.
- [ ] Diff-mode invocation produces a report with the baseline-config header rows present.
- [ ] An existing report directory exits 1 without overwriting.
- [ ] A replay failure exits 1 without writing a partial report.
- [ ] Discovery warnings (skipped malformed slices) appear on stderr but do not abort the run.
- [ ] `_resolve_git_sha` returns the literal `"(not in a git checkout)"` when git fails or is unavailable.
- [ ] Story 02's sentinel test is removed; replacement tests cover the production exits.
- [ ] The CLI does not read or write the runtime distillation database.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
