---
status: not_started
completed_date:
commit_id:
---

# 04 — Candidate config loader and content hashing

## Goal

Load a `config/distillation.yaml` snapshot from an arbitrary path (the operator's edited working copy or a prior committed snapshot), validate it through the same Pydantic model the runtime distillation layer uses, and compute the SHA-256 content hash that names the report. The same loader handles both the candidate config and the optional baseline config — the diff-mode pair is two `LoadedCandidateConfig` instances, not a special API.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Inputs — points 2 (candidate config validated against the runtime Pydantic model) and 3 (optional baseline config for diff mode)
- `docs/design/02-distillation-layer/replay-harness.md` § Output — `report_id = {timestamp}_{candidate_config_hash}[_{baseline_config_hash}]`; the hash this story produces is what names the report
- `docs/design/02-distillation-layer/replay-harness.md` § Versioning — the report header's "Candidate config path and SHA-256, baseline config path and SHA-256 (diff mode only)" requirement
- `docs/design/configuration-management.md` § `distillation.yaml` — the YAML structure and seven top-level groups
- `docs/design/configuration-management.md` § Validation — three layers (parse-time, cross-reference, semantic self-test); only parse-time + the distillation-internal semantic self-test apply here (cross-reference checks against other YAML files like `assets.yaml` are not the harness's concern)
- Story 02 of the parent distillation work tree — `DistillationConfig` Pydantic model the harness validates against
- `src/alphamind/config/models.py` — existing config Pydantic models including `DistillationConfig`
- Story 02 — package skeleton this module lands inside

## Depends on

- 02 (package skeleton)

## Scope

In scope: under `src/alphamind/distillation/replay_harness/candidate_config.py` —

- **`LoadedCandidateConfig` frozen dataclass** with fields:
  - `path: pathlib.Path` — absolute path the config was loaded from.
  - `config: DistillationConfig` — the parsed-and-validated Pydantic model imported from `src/alphamind/config/models.py`.
  - `content_hash: str` — SHA-256 hex digest of the raw file bytes (lowercase, 64 hex chars, no `sha256:` prefix). Computed before YAML parsing so the hash names the on-disk file content the operator edited.
  - `content_bytes_size: int` — file size in bytes, for the report header's audit row.

- **`CandidateConfigError`** typed exception (subclass of `ValueError`) raised on parse, validation, or invariant failure with a clear field-path message preserved from the underlying `ValidationError` when applicable.

- **`load_candidate_config(path: pathlib.Path | str) -> LoadedCandidateConfig`** function:
  1. Resolve `path` to an absolute path; raise `FileNotFoundError` if it doesn't exist.
  2. Read raw bytes.
  3. Compute `SHA-256(bytes).hexdigest()` → `content_hash`.
  4. Parse the bytes as UTF-8 YAML.
  5. Validate via `DistillationConfig.model_validate(parsed)`. Wrap any `pydantic.ValidationError` in `CandidateConfigError` preserving the original error chain via `raise ... from`.
  6. Return `LoadedCandidateConfig(path, config, content_hash, content_bytes_size)`.

  The function does NOT plug into the broader cascaded `load_config()` aggregate (which would also load `assets.yaml`, profile files, etc.). The harness needs only the `distillation.yaml` content; cross-file cross-reference validation is out of scope. The Pydantic model's own validators (the thirteen invariants from `threshold-calibration.md § Validation invariants`) run as part of `model_validate`, so the harness fails at the boundary on a malformed candidate exactly as the runtime would.

- **`compute_report_id(timestamp: datetime, candidate_hash: str, baseline_hash: str | None) -> str`** function:
  - Returns `f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{candidate_hash[:16]}"` for single-config mode.
  - Returns `f"{timestamp.strftime('%Y%m%dT%H%M%SZ')}_{candidate_hash[:16]}_{baseline_hash[:16]}"` for diff mode.
  - The truncation-to-16-chars on the hash keeps `report_id` filesystem-friendly while still being unambiguous (16 hex chars = 64 bits = collision-resistant for the harness's run scale). The full hash lives in the report header; the truncated form is for the directory name only.
  - The timestamp must be UTC; if the input has tzinfo other than UTC, the function raises `ValueError`. If it has no tzinfo, the function raises `ValueError` (no implicit tz; the caller decides).

- Unit tests under `tests/distillation/replay_harness/test_candidate_config.py`:
  - Round-trip happy path: load the canonical `config/distillation.yaml` (from the repo); the returned `LoadedCandidateConfig.config` exposes the documented field values; `content_hash` equals `hashlib.sha256(file_bytes).hexdigest()` independently computed in the test; `content_bytes_size` equals the file size.
  - File-not-found: a non-existent path raises `FileNotFoundError`.
  - Malformed YAML: a file containing `:::` raises `CandidateConfigError`.
  - Missing required field: a YAML missing `anomaly_detection.volume_anomaly_sigma` raises `CandidateConfigError` with the field path.
  - Invariant violation: a YAML with `regime_normal_vix_max: 22.0` and `regime_elevated_vix_min: 21.0` (boundary monotonicity violated) raises `CandidateConfigError`.
  - Hash determinism: two loads of the same file produce identical `content_hash`.
  - Hash sensitivity: a file with a single trailing-whitespace difference produces a different `content_hash`.
  - `compute_report_id` single mode: returns the documented timestamp + 16-char hash format.
  - `compute_report_id` diff mode: returns the documented format with both hashes truncated to 16 chars.
  - `compute_report_id` rejects naive datetime: a `datetime.now()` without `tzinfo` raises `ValueError`.
  - `compute_report_id` rejects non-UTC tzinfo: a datetime in `America/New_York` raises `ValueError`.

Out of scope:
- Cross-file YAML validation against `assets.yaml`, profile files, regime files (handled by the runtime config loader, irrelevant to the harness).
- Diffing two configs to surface which fields changed (the harness reports per-regime *behavioral* differences, not field-level config diffs; if the operator wants a field-level diff, `git diff` is the right tool).
- Producing the full report ID at this layer — the timestamp comes from the CLI invocation moment (story 08); this story produces the helper that the CLI calls.
- Loading distillation YAML over network paths or remote stores — local filesystem only.

## Notes

The candidate-config hash is computed over **raw file bytes**, not over a normalized YAML representation. The operator's edited file IS the artifact under audit; trailing whitespace, comment-text, or YAML stylistic choices that round-trip differently would produce a different hash, and that's correct — the operator is reasoning about a specific file content, not a normalized abstract syntax tree. This mirrors how `git` hashes blobs.

The truncation to 16 hex chars (64 bits) for `report_id` is a directory-naming compromise. The full 64-char hash is recorded in the report header so audit traces are unambiguous; the report directory name just needs to be human-readable and unique within the harness's run rate.

The `LoadedCandidateConfig.config` field reuses the runtime `DistillationConfig` Pydantic model rather than introducing a harness-local copy — per the design's "imports its code paths from `src/alphamind/distillation/`" discipline. If the runtime model adds a field, the harness picks it up automatically. The harness's harness_version (story 02) is bumped only when harness aggregation logic changes; runtime model evolution is independent.

Per `feedback_simplify_before_building.md`, do not invent a new YAML loader. The runtime already uses `yaml.safe_load` via the existing `load_config()` aggregate in `src/alphamind/data_sources/_common.py`; reuse `yaml.safe_load` directly here. The separate `load_candidate_config` function is justified by the path-from-anywhere argument (the runtime loader expects `config/distillation.yaml` at the canonical path).

Per `feedback_avoid_numeric_anchors.md`, the 16-char hash truncation is a structural choice (filesystem name length), not a tunable threshold the LLM should treat as semantic. Document it as a constant `REPORT_ID_HASH_PREFIX_LEN = 16` if desired; do not parameterize it via config.

The function signature accepts `pathlib.Path | str` because the CLI's argparse produces strings and tests typically use `Path`. Internal code should normalize to `Path` immediately on entry.

Since the harness's job is to fail at the boundary on a malformed config, this story's tests deliberately cover both pure-Pydantic violations (missing field, wrong type) and the cross-field invariants from `threshold-calibration.md § Validation invariants` (regime monotonicity, `*_min_observations` ≤ baseline days, etc.). The latter are owned by `DistillationConfig`'s `model_validator(mode="after")`; the harness loader inherits them by calling `model_validate`.

## Acceptance criteria

- [ ] `LoadedCandidateConfig` frozen dataclass exists with `path`, `config`, `content_hash`, `content_bytes_size`.
- [ ] `CandidateConfigError` typed exception exists.
- [ ] `load_candidate_config(path)` reads bytes, computes SHA-256, parses YAML, validates via `DistillationConfig.model_validate`, returns the wrapper.
- [ ] Hash determinism test: two loads of the same file produce identical hashes.
- [ ] Hash sensitivity test: a one-byte change in the file produces a different hash.
- [ ] File-not-found, malformed YAML, missing required field, and Class A invariant violation all raise the documented exceptions.
- [ ] `compute_report_id(timestamp, candidate, None)` returns the single-mode form.
- [ ] `compute_report_id(timestamp, candidate, baseline)` returns the diff-mode form.
- [ ] `compute_report_id` rejects naive or non-UTC timestamps.
- [ ] Loading the canonical repo `config/distillation.yaml` produces a config whose field values pass through unchanged.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
