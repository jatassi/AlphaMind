## Goal

Replace the hand-constructed upstream-brief fixtures in the phase 4 and phase 5 verification scripts with live phase 2/3 outputs, so a green end-to-end verification proves that today's actual upstream artifacts flow correctly through the analysis-layer pipeline. Today the runbook (`scripts/RUNBOOK_end_to_end_verification.md`) carries four explicit caveats noting this gap; the composition runner (`run_analysis_pipeline`, [ALP-276](https://linear.app/alphamind-jatassi/issue/ALP-276/analysis-layer-pipeline-composition-wiring)) makes wiring the verifications onto live data structurally trivial — the wire-up just hadn't been done.

## Why Option C (stage-artifact cache)

Three approaches were considered during the 2026-05-03 e2e run analysis:

**Option A — single new pipeline-only verification script** that calls `run_analysis_pipeline` end-to-end and asserts the synthesizer's output. Simpler implementation, but the per-stage verifications stay fixture-based — phase 4/5 e2e still relies on running the new pipeline script alongside (or in place of) them, and the cross-layer-flow proof lives in only the new script.

**Option B —** `--upstream live` flag on existing scripts. Each per-stage script gains a flag that runs upstream stages before invoking its target. Backwards-compatible, but every adaptive run re-spends phase 2+3 SDK tokens; running adaptive *and* synthesizer in `live` mode quadruples upstream costs.

**Option C — stage-artifact cache (chosen).** Each verify script writes its parsed brief(s) to disk on success; downstream scripts read those artifacts when present, fall back to fixtures when absent. End-to-end runs spend each stage's SDK cost exactly once. Per-stage prompt-iteration runs still work without upstream stages. Order-dependence becomes explicit and intentional. Cleanest for the existing per-stage runbook discipline.

## Scope

**(A) Stage-artifact directory contract.** Pick a canonical layout under each invocation's archive root, e.g.:

```
<archive_root>/invocations/<invocation_id>/stage_artifacts/
    distillation_outputs.json
    correlation_regime_brief.json
    universal_regime_label.json
    sector_briefs.json           # tuple[SectorBrief, ...] — 3 sectors
    qualitative_brief.json
    adaptive_brief.json
    retrieval_store.json         # synthesizer's per-invocation reference store
```

Each file is the JSON serialization of one typed result. The path is derived from `archive_root` + `invocation_id` so two parallel runs don't collide.

**(B) Typed serialization helpers.** Add module-level `dump_<type>(obj, path)` / `load_<type>(path)` pairs for each typed result the analysis layer threads. Pydantic models (`SectorBrief`, `QualitativeBrief`, `AdaptiveBrief`) get `model_dump_json()` / `model_validate_json()`. Dataclasses (`DistillationOutputs`, `CorrelationRegimeBrief`) need explicit dict-conversion + a custom JSON-encoder that handles datetimes, enums, and nested Pydantic models. The `RetrievalStore` may need a snapshot method that captures the resolved reference table without internal state.

Where these helpers live: probably `src/alphamind/scripts/_artifact_io.py` (a new module sibling to `_common.py`). Test round-trip equality for every type.

**(C) Writer hooks in the producer scripts.**

* `verify_distillation.py`: on success, dump `distillation_outputs.json`, `correlation_regime_brief.json`, `universal_regime_label.json`.
* `verify_domain_researchers.py`: on success, dump `sector_briefs.json` (the 3-tuple).
* `verify_qualitative_researcher.py`: on success, dump `qualitative_brief.json`.
* `verify_adaptive_researcher.py`: on success, dump `adaptive_brief.json`.
* `verify_synthesizer.py`: on success, dump `retrieval_store.json` snapshot.

**(D) Loader hooks +** `--upstream-from <dir>` flag. Each downstream script gains a flag pointing at a stage-artifact directory. When the flag is supplied:

* `verify_domain_researchers.py`: load `distillation_outputs.json` instead of running its own internal distillation invocation. The script currently re-runs distillation as a setup step — this avoids the duplicate SDK cost.
* `verify_qualitative_researcher.py`: load `universal_regime_label.json` instead of reading from `distillation_regime_state` (more reliable than the existing DB-state approach, which can produce stub labels on a cold DB).
* `verify_adaptive_researcher.py`: load `sector_briefs.json` + `qualitative_brief.json` + `correlation_regime_brief.json` + `distillation_outputs.json` + `universal_regime_label.json` instead of `_load_e2e_fixtures_lazy()`.
* `verify_synthesizer.py`: load all 6 upstream briefs (3 sectors + correlation/regime + qualitative + adaptive) instead of `build_fixture_*` helpers.

When the flag is absent, scripts behave as today (fixture-based or stub-based) so unit-iteration runs don't change.

When the flag is supplied but a required artifact is missing, fail with a clear "run phase N first" message naming the producer script.

**(E) Runbook update.** `scripts/RUNBOOK_end_to_end_verification.md`:

* Add a `--archive-root` + `--upstream-from <archive_root>/invocations/<invocation_id>/stage_artifacts/` flag chain to every phase 2-5 invocation in the canonical command sequence. Use a fresh `invocation_id` once at the top so all phases write to the same artifact directory.
* Remove the four caveats currently in the runbook: the "Important: this is NOT a single live composed pipeline run" callout at the top, the two "⚠️ Heads up: hand-constructed input" boxes in phases 4 and 5, and known-gap entry (1) in § Known gaps.
* Document the order-dependence: "phase N requires phase M's artifacts; if the directory is empty, the script fails fast."

**(F) Tests.**

* `tests/scripts/test_artifact_io.py` — round-trip equality per typed result.
* Update `tests/scripts/test_verify_*` to cover the new loader-from-disk paths (the existing fixture paths stay covered).

## Out of scope

* Removing the test-tree fixtures (`tests/analysis/adaptive_research/fixtures/*.json` + `verify_synthesizer.build_fixture_*`). Those fixtures still serve unit/CI tests and per-stage prompt iteration; this story makes the e2e path live, not the unit path.
* Wiring the decision-layer agents to consume the stage-artifact cache. That's a separate story under the decision-layer epic.

## Acceptance criteria

* The 5 phase 2-5 verification scripts each accept `--archive-root <dir>` and (where applicable) `--upstream-from <stage-artifacts dir>`.
* Running phases 2-5 in sequence with a shared `invocation_id` produces a single `stage_artifacts/` directory, each phase reading its predecessors' outputs.
* The runbook's TL;DR and per-phase commands reference the new flags.
* The four caveats listed in scope item (E) are removed from the runbook.
* A green end-to-end run proves: today's distillation outputs were consumed by the domain researchers AND the qualitative researcher; their actual outputs were consumed by the adaptive researcher; all five upstream briefs were consumed by the synthesizer.
* All existing tests pass; new round-trip tests cover every typed result.

## Estimated effort

4–6 hours of focused work across \~10 files (1 new module, 5 verify scripts updated in `src/alphamind/scripts/`, 1 runbook updated, 1-2 new test files, possibly typed serialization helpers if any model needs custom encoding).

## Discovered

End-to-end verification run, 2026-05-03 (operator follow-up after observing that [ALP-276](https://linear.app/alphamind-jatassi/issue/ALP-276/analysis-layer-pipeline-composition-wiring) had landed but the per-script verifications still consumed fixtures). See conversation logs.