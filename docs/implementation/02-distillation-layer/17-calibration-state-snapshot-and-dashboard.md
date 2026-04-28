---
status: in_progress
completed_date:
commit_id:
---

# 17 — Calibration-state snapshot and command-center panel

## Goal

Land the file-format spec, writer, and dashboard panel for the per-invocation calibration-state snapshot the state-persistence design doc already names but does not yet specify (`data_calibration_state.json`). The snapshot lets the feedback loop condition on whether bootstrap fallback was active during an invocation, and gives the operator a command-center surface that summarizes the calibration mix per invocation and over windows so structural-bootstrap-stuck states are visible at a glance.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — `calibration_state` per-output tagging contract; `calibrated` / `bootstrap` / `unavailable` semantics
- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — Cross-sectional priors and Downstream propagation subsections (the snapshot captures what flowed down)
- `docs/design/02-distillation-layer/threshold-calibration.md` § Warm-up duration estimate — the operator's mental model for what calibration mix to expect at each phase of deployment
- `docs/design/05-execution-layer/state-persistence.md` § Tier 2 — the "Data calibration state reference" field on the invocation record naming the file path the snapshot lives at; this story specs the file's contents
- `docs/design/command-center.md` § Risk and guardrails — the view group the new panel joins (alongside the Throughput panel from cost-and-rate-limit-modeling)
- `docs/design/command-center.md` § Information sources — table the new panel registers in
- `docs/design/feedback-loop.md` — the downstream consumer that reads the snapshot to condition outcome analysis on bootstrap state
- Story `04-calibration-state-and-bootstrap.md` (the framework primitives), story `12-distillation-orchestrator.md` (the invocation-time integration point that calls this story's writer)

## Depends on

- 04 (Calibration state and bootstrap fallback framework) — defines `CalibrationState` enum and the per-output tag the snapshot summarizes.
- 12 (Distillation orchestrator) — the orchestrator's `DistillationOutputs` is the writer's input; the writer is invoked as a phase in the orchestrator (or by the pipeline phase wrapper that consumes its return value).

## Scope

In scope:

- **File format spec**. Define `data_calibration_state.json` at `data/provenance/invocations/{invocation_id}/data_calibration_state.json`. Document the schema in a new "Calibration-state snapshot file" subsection added to `docs/design/02-distillation-layer/threshold-calibration.md` (placed after the "Per-output tagging" subsection).

  Schema:

  ```json
  {
    "schema_version": "1",
    "invocation_id": "<uuid>",
    "as_of": "<ISO8601 UTC>",
    "summary": {
      "total_blocks": 42,
      "by_state": {
        "calibrated": 34,
        "bootstrap": 7,
        "unavailable": 1
      },
      "by_audience": {
        "sector_tech_semis": {"calibrated": 12, "bootstrap": 2, "unavailable": 0},
        "sector_financials": {"calibrated": 10, "bootstrap": 1, "unavailable": 0},
        "sector_energy": {"calibrated": 8, "bootstrap": 2, "unavailable": 1},
        "correlation_regime_brief": {"calibrated": 3, "bootstrap": 1, "unavailable": 0},
        "universal_broadcast": {"calibrated": 1, "bootstrap": 1, "unavailable": 0}
      },
      "by_block_kind": {
        "q1.volume_anomaly": {"calibrated": 60, "bootstrap": 5, "unavailable": 0},
        "q1.price_move_anomaly": {"calibrated": 65, "bootstrap": 0, "unavailable": 0},
        "q3.options_flow": {"calibrated": 0, "bootstrap": 65, "unavailable": 0},
        "regime.label": {"calibrated": 1, "bootstrap": 0, "unavailable": 0}
      }
    },
    "bootstrap_reasons": {
      "<block_id>": "<bootstrap_reason string from CalibratedValue>"
    },
    "unavailable_reasons": {
      "<block_id>": "<unavailable_reason string from CalibratedValue>"
    }
  }
  ```

  Counts sum across all `OutputBlock` instances the orchestrator emitted in this invocation. The `by_block_kind` keys mirror the `block_id` namespacing convention from story 05 (`<category>.<short_name>`); per-ticker blocks are aggregated under their block-kind key (so a `q1.volume_anomaly` block per ticker contributes 65 to the kind's count tuple). The reason maps record only blocks whose state is `bootstrap` or `unavailable`; calibrated blocks are omitted from the maps to keep the file small.

- **Writer module**. Implement `src/alphamind/distillation/calibration_snapshot.py`:

  ```python
  def write_calibration_state_snapshot(
      outputs: "DistillationOutputs",
      invocation_id: str,
      base_path: Path,
  ) -> Path:
      ...
  ```

  The function:
  1. Walks every `OutputBlock` in `outputs` (across all `sector_outputs`, `correlation_regime_brief`, and the universal regime block), inspects each block's `calibration_state` and `bootstrap_reason`.
  2. Aggregates per-state, per-audience, per-block-kind counts.
  3. Populates `bootstrap_reasons` and `unavailable_reasons` maps for the corresponding blocks.
  4. Writes deterministic JSON (sorted keys, fixed indentation) to `<base_path>/invocations/<invocation_id>/data_calibration_state.json`.
  5. Returns the written path so the caller can record it on the invocation row's `data_calibration_state_reference` field.

  Determinism is load-bearing — the same `DistillationOutputs` must produce byte-identical JSON, mirroring the determinism contract from story 05 and story 12.

- **Orchestrator integration**. Extend story 12's phase 6 (invocation-archive write) to call the snapshot writer. The orchestrator's existing fail-closed semantics carry over — a snapshot-write failure aborts the invocation. This story makes the addition (one-line call site plus its imports).

- **Command-center panel spec**. Add a "Calibration mix" panel spec to `docs/design/command-center.md § Risk and guardrails view group` (alongside the Throughput panel). Spec the panel's contents:
  - Per-invocation strip showing the calibration mix as a stacked-segment bar (one segment per state, segment width proportional to count). Hover/tap shows the underlying counts.
  - Trailing-7-day mix-trend line: percent of blocks in each state per invocation, plotted as a stacked area chart.
  - "Stuck-in-bootstrap" alert: a panel-level warning if any `<block_id>` has been continuously `bootstrap` or `unavailable` across the entire trailing 30-day window. Lists the block IDs and their last calibrated date (if any). Cross-references the Warm-up duration estimate from threshold-calibration.md as the operator's interpretation aid.
  - Drill-in: clicking a block kind in the mix opens a side panel showing the per-ticker / per-pair / per-contract breakdown for that kind's most-recent invocation, with reason strings inline.

- **Information-source registration**. Add a row to the `command-center.md § Information sources` table: `data/provenance/invocations/<id>/data_calibration_state.json` produced by the distillation orchestrator (per this story).

- **Tests**:
  - End-to-end test: a fixture `DistillationOutputs` with mixed calibration states across multiple audiences and block kinds. The writer produces a JSON file matching the documented schema. Round-trip parse confirms every count field matches expectation.
  - Determinism test: two invocations of the writer on the same fixture produce byte-identical JSON files.
  - Empty-input test: a `DistillationOutputs` with no blocks produces a snapshot with `total_blocks = 0`, empty `by_*` dicts (or zero-valued dicts), and empty reason maps.
  - Bootstrap-reason capture test: a fixture with two blocks tagged `bootstrap` produces `bootstrap_reasons` containing exactly those two block IDs with the expected reason strings.
  - Unavailable-reason capture test: same shape for `unavailable`.
  - Schema-version field present and equal to `"1"`.
  - File-path test: the writer creates the directory if missing and writes to the documented path.

Out of scope:
- Implementing the dashboard panel itself (frontend work; the command-center frontend has not yet been built per the project tracker). This story specs the panel; the frontend stories will consume the spec.
- Generalized provenance writer for other per-invocation snapshot files (`agent_calls/...`, etc.) — those have their own owners.
- Aggregation queries over the trailing 30-day window for the "stuck-in-bootstrap" alert; the alert's data layer is part of the panel implementation, not this story.
- Migrating the snapshot format if the schema needs to change later — `schema_version: "1"` is the bootstrap; future schema work owns its own migration.
- Backfilling historical snapshots for invocations that ran before this story landed.

## Notes

The snapshot is a reduction of `OutputBlock`s into counts; the `OutputBlock`s themselves persist via the existing invocation-archive mechanism (markdown files). The snapshot is therefore redundant with the markdown in principle, but the JSON shape is what makes downstream queries cheap — the feedback-loop's deterministic-analytics-spine reads the JSON, never the markdown. The redundancy is intentional and the determinism contract guarantees they cannot drift.

The "stuck-in-bootstrap" alert condition matters because the warm-up duration estimate predicts everything except gap-fill / extended-hours per-ticker calibrations should reach `calibrated` within ~1 month. A block continuously `bootstrap` past that horizon is a signal that the underlying data ingest has a gap — the alert is a generic flag-the-anomaly mechanism, not a threshold edit prompt. Per the threshold-calibration.md guidance to refresh sector-pooled fallbacks monthly, the alert thresholds align with the same cadence.

The panel's design intentionally mirrors the Throughput panel's structure (stacked segments per invocation, trend line, alert callout). Operators reading both panels in the Risk & Guardrails view group benefit from a consistent visual grammar.

The integration with story 12 is a single-line addition: phase 6 currently lists three writes (sector outputs, brief, regime label markdown); the snapshot is the fourth. The writer's deterministic JSON output and atomic-write semantics (write to a temp file, rename) are what make this safe under the orchestrator's fail-closed policy.

The "by_block_kind" counts treat per-ticker blocks as one count each, mirroring story 05's convention that per-ticker blocks share a `block_id` prefix. The drill-in panel handles the per-ticker breakdown, so the summary stays scannable.

## Acceptance criteria

- [ ] `docs/design/02-distillation-layer/threshold-calibration.md` includes a "Calibration-state snapshot file" subsection documenting the JSON schema (placed after Per-output tagging).
- [ ] `src/alphamind/distillation/calibration_snapshot.py` defines `write_calibration_state_snapshot(outputs, invocation_id, base_path) -> Path`.
- [ ] The writer aggregates per-state, per-audience, per-block-kind counts from a `DistillationOutputs`.
- [ ] The writer populates `bootstrap_reasons` and `unavailable_reasons` maps with the corresponding `block_id` → reason-string entries; `calibrated` blocks are absent from the reason maps.
- [ ] The writer emits deterministic JSON (sorted keys, fixed indentation); two calls on the same input produce byte-identical files.
- [ ] The writer creates the target directory if missing and writes to `<base_path>/invocations/<invocation_id>/data_calibration_state.json`.
- [ ] Story 12's phase 6 (invocation-archive write) is extended to call the snapshot writer; the writer's returned path is recorded on the invocation row's `data_calibration_state_reference` field.
- [ ] `docs/design/command-center.md § Risk and guardrails view group` includes a "Calibration mix" panel spec covering: per-invocation stacked-segment mix bar, trailing 7-day mix-trend line, stuck-in-bootstrap alert with cross-reference to the warm-up duration estimate, and drill-in to per-ticker breakdown.
- [ ] `docs/design/command-center.md § Information sources` registers the new snapshot file as a source.
- [ ] End-to-end writer test against a fixture `DistillationOutputs` produces a JSON file matching the documented schema with correct counts.
- [ ] Determinism test: two writer invocations on the same input produce byte-identical files.
- [ ] Empty-input test: zero blocks produces a snapshot with `total_blocks = 0` and zero-valued `by_*` dicts.
- [ ] Bootstrap-reason and unavailable-reason capture tests verify reason maps populate correctly.
- [ ] The schema's `schema_version` field is present and equals `"1"`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
