---
status: in_progress
completed_date:
commit_id:
---

# 02 — Package skeleton & CLI stub

## Goal

Stand up the Python package layout for the replay harness under `src/alphamind/distillation/replay_harness/` plus a minimal `python -m alphamind.distillation.replay_harness` entry point that parses the documented argument surface and exits with a not-yet-implemented sentinel. Subsequent stories drop modules into this skeleton and replace the sentinel with real behavior.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Activation — the CLI surface (`--candidate-config`, `--baseline-config`, `--regimes`); the read-only-on-runtime-state invariant
- `docs/design/02-distillation-layer/replay-harness.md` § Versioning — the `harness_version` string the package owns
- `docs/implementation/01-data-layer/portfolio-state/02-package-skeleton-and-config.md` — sibling pattern for `src/alphamind/<feature>/` skeleton
- `docs/implementation/02-distillation-layer/02-config-schema.md` — sibling pattern for landing a Python module under `src/alphamind/distillation/`
- `scripts/validate_universe.py`, `scripts/verify_bootstrap.py` — existing CLI patterns for `--help`, exit codes, argparse layout

## Depends on

None — `src/alphamind/distillation/` is landed (story 02 of the parent distillation work tree creates the package).

## Scope

In scope:

- Top-level package at `src/alphamind/distillation/replay_harness/` with the following module structure (each `__init__.py` exists and is empty unless a member needs to be re-exported):

  ```
  src/alphamind/distillation/replay_harness/
      __init__.py
      __main__.py        # python -m alphamind.distillation.replay_harness entrypoint
      cli.py             # argparse layout + main() function
      version.py         # harness_version constant
  ```

  Subsequent stories drop modules alongside (`fixtures.py`, `candidate_config.py`, `engine.py`, `aggregation.py`, `report.py`). This story does not create those.

- Test mirror at `tests/distillation/replay_harness/` with `__init__.py`. Subsequent stories populate per-module test files.

- `version.py` defines exactly one module-level constant: `HARNESS_VERSION: str`. Initial value `"0.1.0"`. Re-export it from `__init__.py` so `from alphamind.distillation.replay_harness import HARNESS_VERSION` works. The constant follows semantic-versioning convention; later stories advance it when aggregation or fixture-loading semantics change per the design doc's versioning discipline.

- `cli.py` exposes:
  - `def build_parser() -> argparse.ArgumentParser` — constructs the argparse parser with the documented arguments:
    - `--candidate-config PATH` (required) — path to a `config/distillation.yaml` snapshot.
    - `--baseline-config PATH` (optional) — path to a second snapshot for diff mode.
    - `--regimes LABELS` (optional) — comma-separated subset of `low_vol,normal,elevated,crisis`. Default: all four.
  - `def main(argv: list[str] | None = None) -> int` — parses arguments, prints a single-line `"replay harness not yet implemented; story 08 wires the runner"` message to stderr, and returns exit code 2 (analogous to a "not implemented" sentinel — distinct from 0 success and 1 hard error). Subsequent stories replace the sentinel body with the real runner; the signature stays.

- `__main__.py` calls `sys.exit(cli.main())` so `python -m alphamind.distillation.replay_harness` works from the command line.

- The argparse parser's `--help` output reads cleanly: program description naming the harness's purpose (one sentence pointing at `replay-harness.md`), one help line per argument naming acceptable values.

- Unit tests under `tests/distillation/replay_harness/`:
  - `test_imports.py` — imports `alphamind.distillation.replay_harness` and `alphamind.distillation.replay_harness.cli`; both succeed.
  - `test_version.py` — `HARNESS_VERSION` is a non-empty string and equals the value declared in `version.py`.
  - `test_cli_parser.py` — `build_parser().parse_args(["--candidate-config", "x"])` parses; `parse_args([])` raises `SystemExit` (missing required); `parse_args(["--candidate-config", "x", "--regimes", "low_vol,normal"])` parses with `regimes` resolving to a list of two strings.
  - `test_cli_main_sentinel.py` — `main(["--candidate-config", "/nonexistent.yaml"])` returns 2 and writes the not-yet-implemented message to stderr (capture via `capsys`).

Out of scope:
- Loading the candidate or baseline configs (story 04).
- Reading the fixture store (story 03).
- Running any distillation logic (story 05).
- Producing a report file (story 07).
- Wiring `--regimes` into a real filter (story 08).
- Validating that the candidate config path exists (story 04 / 08).

## Notes

`HARNESS_VERSION` lives in its own module (not inline in `__init__.py`) because story 08 records it in the report header alongside the git SHA — keeping it isolated avoids importing the rest of the package just to read the version. Sibling pattern: `verify_bootstrap.py` keeps its constants near the top.

The exit code `2` for the not-yet-implemented sentinel is deliberately distinct from `0` (success after report write) and `1` (real error — invalid config, missing fixture). Subsequent stories will not return `2` once the runner is wired; the test for the sentinel stays in this story's file and is removed in story 08 when the wiring lands.

The package is `replay_harness` (snake_case Python convention), even though the design doc uses "replay harness" as prose. The `python -m alphamind.distillation.replay_harness` invocation matches the design doc's Activation section verbatim.

The argparse parser does not validate that `--regimes` values are in the allowed set yet — that's story 08. This story only ensures the comma-split parses to a list. Empty strings or unknown labels pass through; downstream stories reject them.

Per the user's `feedback_scaffold_per_component_depth.md` memory, prefer per-component subdirectory depth for any future module additions. The skeleton shape above keeps each concern in a sibling module file rather than nesting subdirectories — appropriate at this size, with subdirectories an option later if any concern grows multiple files.

## Acceptance criteria

- [ ] `src/alphamind/distillation/replay_harness/__init__.py` exists.
- [ ] `src/alphamind/distillation/replay_harness/__main__.py` exists and invokes `cli.main()` via `sys.exit`.
- [ ] `src/alphamind/distillation/replay_harness/cli.py` defines `build_parser()` and `main()`.
- [ ] `src/alphamind/distillation/replay_harness/version.py` defines `HARNESS_VERSION` as a non-empty string.
- [ ] `HARNESS_VERSION` is re-exported from `src/alphamind/distillation/replay_harness/__init__.py`.
- [ ] `tests/distillation/replay_harness/__init__.py` exists.
- [ ] `python -m alphamind.distillation.replay_harness --help` prints help text including all three arguments.
- [ ] `python -m alphamind.distillation.replay_harness` (no args) exits non-zero with a clear message about the missing required `--candidate-config` (argparse default).
- [ ] `python -m alphamind.distillation.replay_harness --candidate-config X` exits 2 with the not-yet-implemented sentinel on stderr.
- [ ] `test_imports.py`, `test_version.py`, `test_cli_parser.py`, `test_cli_main_sentinel.py` all exist and pass.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
