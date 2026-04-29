---
status: in_progress
completed_date:
commit_id:
---

# 07 — Markdown report renderer

## Goal

Render the `AggregatedReport` (story 06) plus the run's provenance metadata into a deterministic single-file Markdown report at `data/replay_reports/{report_id}/report.md`. The renderer handles single-config and diff-mode layouts using the same function — the diff layout adds candidate / baseline / delta sub-columns and a baseline-config row in the header. Output is byte-deterministic so that a re-run of the harness on the same inputs produces an identical file.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Output — authoritative table of report sections (Header, Per-regime flag-rate table, Regime-label distribution, Class B baseline shape summary, Calibration-state breakdown), including the diff-mode column shape and the `data/replay_reports/{report_id}/report.md` filesystem location
- `docs/design/02-distillation-layer/replay-harness.md` § Versioning — the `harness_version` and per-report version-string emission
- `docs/design/02-distillation-layer/replay-harness.md` § Activation — the `report_id` cited in `/feedback-validate` registrations; renderer emits the `report_id` prominently in the header
- Story 04 of this work tree — `LoadedCandidateConfig`, `compute_report_id`, the SHA-256 hash that names the report
- Story 02 of this work tree — `HARNESS_VERSION` constant
- Story 06 of this work tree — `AggregatedReport` and component dataclasses consumed here
- Story 03 of this work tree — slice manifest fields for the "fixture slice IDs consumed" header row

## Depends on

- 04 (config loader for `LoadedCandidateConfig` shape)
- 06 (aggregation results shape)

## Scope

In scope: under `src/alphamind/distillation/replay_harness/report.py` —

- **`ReportProvenance` frozen dataclass** carrying the metadata story 08 assembles for the header (kept separate from the aggregation result so the renderer can be tested in isolation):
  - `report_id: str` — from `compute_report_id`.
  - `generated_at: datetime` — UTC timestamp of report generation.
  - `harness_version: str` — `HARNESS_VERSION`.
  - `git_sha: str` — git HEAD SHA at the moment the harness ran.
  - `candidate: LoadedCandidateConfig`.
  - `baseline: LoadedCandidateConfig | None` — null in single mode.
  - `fixture_slice_ids: dict[str, list[str]]` — regime → ordered list of slice IDs that were replayed (recorded for reproducibility per the design's "report header records the slice IDs consumed").
  - `regimes_replayed: tuple[str, ...]` — the four canonical regime labels actually replayed (subset of the four canonical labels).

- **`render_report(provenance: ReportProvenance, aggregated: AggregatedReport) -> str`** function that returns the Markdown content as a single string. The function is pure — no I/O.

  Section structure:

  1. **Header section** (top-level `# Replay harness report`):
     - First line: `**report_id:** {report_id}`.
     - `**generated_at:**`, `**harness_version:**`, `**git_sha:**`.
     - `**candidate_config_path:**` and `**candidate_config_sha256:**` (full 64-char hash).
     - In diff mode: `**baseline_config_path:**` and `**baseline_config_sha256:**`.
     - `**candidate_config_size_bytes:**` and (diff-only) `**baseline_config_size_bytes:**`.
     - `**regimes_replayed:**` — comma-separated list.
     - `**fixture_slices_consumed:**` — for each regime, a sub-bullet listing `slice_id` values consumed.
     - A blank line follows.

  2. **Per-regime flag-rate table** (`## Per-regime flag-rate table`):
     - Single mode: a Markdown table with rows = flag types, columns = regimes. Each cell shows `{rate:.2%} (n={total})`. Empty cells (flag did not fire in any invocation in that regime) show `—`.
     - Diff mode: the column header row has `regime / candidate / baseline / Δ` for each regime. Each cell shows three sub-columns: candidate `{rate:.2%}`, baseline `{rate:.2%}`, delta `{Δ:+.2pp}` (signed, percentage-point format). Render either as a wider plain table or as nested cells separated by `<br>` inside Markdown table cells — pick the one that renders cleanly in GitHub-flavored Markdown without HTML; the wider plain table is preferred for greppability.
     - Trailing blank line.

  3. **Regime-label distribution** (`## Regime-label distribution`):
     - One sub-heading per regime (level `###`), then a per-slice list:
       - `- {slice_id}: candidate={count}, baseline={count}` (diff) or `candidate={count}` (single) per regime label observed.
     - Trailing blank line.

  4. **Class B baseline shape summary** (`## Class B baseline shape summary`):
     - One sub-heading per baseline kind (level `###`), then a per-regime table:
       - Single mode: rows = regimes, columns = `count`, `median`, `IQR`. Cells with `value_count == 0` show `—` for median and IQR.
       - Diff mode: each metric column splits into candidate / baseline sub-columns.
     - Trailing blank line.

  5. **Calibration-state breakdown** (`## Calibration-state breakdown`):
     - Single mode: a Markdown table with rows = regimes, columns = `calibrated`, `bootstrap`, `unavailable` showing counts.
     - Diff mode: each calibration-state column splits into candidate / baseline sub-columns.
     - Trailing blank line.

  6. **Footer** (`---` then a one-line citation prompt): `*Cite this report's `report_id` in `/feedback-validate` REGISTER step's `expected_magnitude` or `success_criterion` field for `config/distillation.yaml` edits.*` (no trailing whitespace).

- **`write_report(provenance: ReportProvenance, aggregated: AggregatedReport, root: pathlib.Path) -> pathlib.Path`** function:
  - Computes the destination directory: `root / provenance.report_id`.
  - Creates it with `parents=True, exist_ok=False`. If it already exists, raises `ReportAlreadyExistsError` (subclass of `FileExistsError`) with the existing path. Re-running the harness with the same inputs reuses the same `report_id` (same timestamp + same hash), so an existing directory means the operator is overwriting; refuse to clobber. The CLI (story 08) translates this into a clean exit message.
  - Writes the rendered string to `root / provenance.report_id / "report.md"` with UTF-8 encoding and `\n` line terminators (no `\r\n` even on Windows; the canonical encoding is LF for diffability across platforms).
  - Returns the absolute path of the written file.

- **Determinism guarantees:**
  - Section ordering and sub-section ordering are fixed.
  - Iteration order over dataclass tuples is preserved (the aggregator pre-sorts; the renderer must NOT re-sort).
  - Numeric formatting uses fixed precision (`{:.2%}` for rates, `{:+.2pp}` for deltas, `{:.4g}` for medians and IQRs to keep small and large values both readable).
  - Date formatting in the header uses ISO 8601 UTC with `Z` suffix.
  - All strings are emitted in UTF-8; no locale-dependent number formatting.

- **`ReportRenderError`** typed exception (subclass of `ValueError`) raised on input shape violations (e.g., diff-mode aggregation paired with single-mode provenance); the renderer expects matching modes and validates the pair.

- **Unit tests** under `tests/distillation/replay_harness/test_report.py`:
  - Single-mode happy path: a synthesized `(provenance, aggregated)` pair with mode `single` renders to a string containing all five section headings + footer.
  - Diff-mode happy path: a synthesized pair with mode `diff` renders, with `**baseline_config_path:**` present in the header and delta columns in every table.
  - Mode mismatch: single-mode aggregation paired with diff-mode provenance (baseline non-null) raises `ReportRenderError`. The reverse (diff aggregation, no baseline provenance) also raises.
  - Determinism: rendering twice produces byte-identical output.
  - Empty-flag table: an aggregation with zero flag types produces an "empty" sub-message under the header (`*No anomaly flags fired across replayed invocations.*`) — the section is still rendered so the consumer can see the intentional absence.
  - Empty-regime case: `regimes_replayed` covering only one regime produces a one-column flag-rate table.
  - Number formatting: a rate of `0.0125` renders as `1.25%`; a delta of `-0.0034` renders as `-0.34pp`; a median of `12345.6789` renders as `1.235e+04` (scientific to fit `:.4g`) — adjust the spec to whatever fixed-precision format renders cleanly across the value range, but the format MUST be deterministic and tested.
  - File write happy path: `write_report` against a temp directory creates `report_id/report.md` containing the rendered string.
  - File write conflict: `write_report` raises `ReportAlreadyExistsError` when the destination exists.
  - LF line terminators: the written file contains no `\r` bytes.

Out of scope:
- Generating the report ID (story 04 owns `compute_report_id`; this story consumes it via provenance).
- HTML or PDF rendering — Markdown is the contract; downstream tooling can convert.
- Embedded charts or images — the report is text-only per the design.
- Diffing two report files — the operator uses standard `diff` tooling; the renderer produces the comparable artifact.
- Per-section configuration (e.g., "render only the flag-rate table") — the report is monolithic; partial rendering would dilute the audit invariant that one report covers everything.

## Notes

The header's `**report_id:**` line is the citation anchor. The `/feedback-validate` REGISTER walk's free-text fields (`expected_magnitude`, `success_criterion`) reference this string verbatim, and the EVALUATE walk's verbatim re-read surfaces it back to the operator. Make it unmistakable in the rendered output — bold, top of file, no surrounding chrome.

Diff-mode column-shape decision (separate columns vs. nested HTML in cells): GitHub renders multi-row Markdown tables consistently when each cell is a single string; nested `<br>` works but is visually noisy. Prefer the wider plain table — three sub-columns per regime — and add a top-row spanner via `:------:` alignment. If column count exceeds GitHub's rendering width gracefully, the rendered output is still grep-able, which is the harness's primary consumer.

Per `feedback_no_decision_trails.md`, the rendered report does not narrate "this section was added in v0.1.1" or "deprecated since X." The header includes `harness_version` and `git_sha`; that's the audit anchor. The report's content is the contract for the version recorded.

Per `feedback_simplify_before_building.md`, the renderer is one function. Helper functions per section (`_render_header`, `_render_flag_rates`, etc.) are module-private; do not extract a `ReportRenderer` class.

Per `feedback_avoid_numeric_anchors.md`, the precision-format constants (`{:.2%}` for rates, etc.) are formatting choices — not LLM-targeting numerics. Keep them consistent across the report.

The `report.md` filename is mandatory per the design — `data/replay_reports/{report_id}/report.md`. Future versions of the harness might add ancillary files in the same directory (e.g., the raw aggregated JSON for programmatic reads); this story ships only `report.md`. Story 08's CLI does not pre-create the directory; `write_report` does.

The `git_sha` field is populated by story 08's CLI via `git rev-parse HEAD` from the working tree at invocation time, recorded in provenance, and embedded here. The renderer treats it as an opaque string; if it's empty (e.g., the operator runs the harness outside a git checkout), the renderer emits `git_sha: (not in a git checkout)` rather than failing — the CLI passes the literal string, the renderer just formats it.

The empty-flag-table message (`*No anomaly flags fired across replayed invocations.*`) is a deliberate signal, not an error. A correctly-tuned threshold can produce zero anomalies in a calm regime, and the report should make that visible rather than rendering an empty table.

## Acceptance criteria

- [ ] `ReportProvenance` frozen dataclass exists with all documented fields.
- [ ] `render_report(provenance, aggregated)` returns a single string.
- [ ] The header section emits `report_id`, `generated_at`, `harness_version`, `git_sha`, candidate config path/SHA/size, and (diff-only) baseline config path/SHA/size, regimes_replayed, fixture_slices_consumed.
- [ ] The flag-rate table renders single mode as one cell per (flag_type, regime) and diff mode as candidate / baseline / delta sub-columns.
- [ ] The regime-label distribution renders one sub-heading per regime with per-slice counts.
- [ ] The Class B baseline shape summary renders one sub-heading per baseline kind with rows=regimes and `count` / `median` / `IQR` columns.
- [ ] The calibration-state breakdown renders rows=regimes and columns=`calibrated` / `bootstrap` / `unavailable`.
- [ ] The footer cites the `/feedback-validate` REGISTER usage.
- [ ] Mode-mismatch (single aggregation + diff provenance, or vice versa) raises `ReportRenderError`.
- [ ] Determinism: two renders of identical inputs produce byte-identical output.
- [ ] Empty-flag-table case emits the documented "no anomaly flags fired" message instead of an empty table.
- [ ] Number formatting uses fixed precision and is deterministic across runs.
- [ ] `write_report(provenance, aggregated, root)` creates `root / report_id / report.md` with UTF-8 + LF line terminators.
- [ ] `write_report` against an existing destination raises `ReportAlreadyExistsError`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
