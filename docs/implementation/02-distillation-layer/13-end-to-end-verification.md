---
status: not_started
completed_date:
commit_id:
---

# 13 — End-to-end verification

## Goal

Produce the verification scripts and runbook proving external distillation works end-to-end against a real, post-bootstrap database: the orchestrator runs to completion, all six state tables update, all three sector outputs and the correlation/regime brief render, the regime label persists, and the invocation archive is populated. Plus the calibrated-vs-bootstrap behavior verification — running the orchestrator immediately after collector bootstrap should produce a mix of `calibrated` and `bootstrap` outputs per the documented warm-up duration estimate.

## Reading

- `docs/design/02-distillation-layer/external.md` § Output format — what the verification script asserts about output shape
- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy + Warm-up duration estimate — what the verification script asserts about the post-bootstrap calibration mix
- `docs/implementation/01-data-layer/collector/VERIFICATION.md` — the parallel verification doc for the collector; mirror the format
- `docs/architecture/infrastructure.md` § Layer 2 invocation archive — file paths the script verifies
- All prior stories — what's being verified

## Depends on

- 12 (the orchestrator must exist before end-to-end verification can run).

## Scope

In scope:

- `scripts/verify_distillation.py`:
  - Connects to the SQLite database.
  - Runs `run_external_distillation` against the universe scope from `assets.yaml` for an `as_of` of "now."
  - Asserts:
    - `DistillationOutputs.sector_outputs` has exactly three entries; each has non-empty text and a non-empty ticker tuple.
    - `DistillationOutputs.correlation_regime_brief.text` is non-empty and contains a `[CR-1]` reference (the regime line).
    - `DistillationOutputs.universal_regime_label["regime_label"]` is one of the four documented labels.
    - All six state tables have at least one row written within the last 5 minutes (`distillation_ticker_baseline`, `distillation_pair_lag`, `distillation_contract_history`, `distillation_event_history`, `distillation_regime_state`, `distillation_composite_state`).
    - The invocation-archive directory contains `tech_semis_sector.md`, `financials_sector.md`, `energy_sector.md`, `correlation_regime_brief.md`, and `regime.md`.
  - Reports a summary table of: number of blocks per audience, number of anomalies (split by severity), number of bootstrap-tagged outputs, freshness range.
  - Exits 0 on all assertions passing; exits 1 with a per-failure report otherwise.
- `scripts/verify_bootstrap_calibration_mix.py`:
  - Run after `python -m alphamind.collector bootstrap` completes.
  - Runs `run_external_distillation` once, then queries `distillation_ticker_baseline` and reports the calibration-state distribution per `kind`:
    - Volume / ATR / spread baselines: expect mostly `calibrated` (per-ticker observation count ≥ 30 after 252 trading days of bars).
    - Sentiment baseline: expect mostly `bootstrap` initially (vendor sentiment backfill depth varies).
    - Lead-lag pairs: expect mostly `bootstrap` (event-driven, < 10 events on first deploy).
  - Asserts the distribution roughly matches `threshold-calibration.md § Warm-up duration estimate`. Exits 0 when the distribution is within tolerance; exits 1 otherwise with a per-baseline-kind report.
- `scripts/verify_regime_transition.py`:
  - Reads the regime-state table for the last `--lookback-days N` (default 7) and reports the regime label trajectory and any transition events.
  - Asserts:
    - Every consecutive label change is recorded with `prior_label` populated.
    - Every recorded `confirmed` transition was preceded by ≥ 2 invocations at the same label.
    - Every recorded `early-strong` transition has `indicator_agreement_count >= 3`.
    - Every recorded `early-weak` transition has `indicator_agreement_count < 3`.
  - Exits 0 when no invariants are violated; exits 1 with the violating row(s) reported.
- `docs/implementation/02-distillation-layer/VERIFICATION.md` — operator runbook with:
  - Pre-conditions (collector bootstrap completed, distillation orchestrator wired into the pipeline phase entry point or callable directly).
  - Sequence of verification steps using the three scripts above.
  - Expected outputs for each script.
  - "Mental check" example: a worked example showing what the regime label, anomaly summary, and one sector output look like for a sample invocation. Pull from a real archive run if available; otherwise synthesize a plausible example so the operator knows what "good" looks like.
  - Known calibration-warm-up timeline: weeks to expect the bootstrap → calibrated transition for each baseline kind.
- Unit tests for the verification scripts themselves:
  - Each script's assertions trigger correctly against fault-injected fixture inputs (e.g., a missing state-table row should make `verify_distillation.py` exit 1 with the right error class).

Out of scope:
- The orchestrator implementation itself (story 12).
- Running the verification scripts against a live production database — that's an operator concern; the scripts just have to work correctly when invoked.
- Performance benchmarking under load.
- Long-term feedback-loop calibration validation (Phase 4 concern owned by [`feedback-loop.md`](../../../design/feedback-loop.md)).

## Notes

The verification scripts are operator tooling, not pipeline-runtime code. Keep them in `scripts/` (alongside the collector's verify scripts), not in `src/alphamind/distillation/`. Each script takes its database path from `--db-path` (default to the `main.yaml` configured path) and follows the existing collector-verify CLI conventions.

`verify_distillation.py` runs the orchestrator once for the verification — this is intentional, not a redundant production invocation. The script is meant to be called manually by the operator after bootstrap or after deploying a code change that affected distillation. The pipeline runs the orchestrator separately on its scheduled cadence.

`verify_bootstrap_calibration_mix.py` is the calibration-state regression check. The "expected distribution" is qualitative — per `warm-up duration estimate`, volume / ATR / spread should be mostly calibrated immediately after bootstrap; sentiment, gap-fill, extended-hours, lead-lag will be mostly bootstrap. Don't pin tight numeric thresholds; verify "qualitatively correct" with broad bands (e.g., "≥ 80% of volume baselines are calibrated; ≥ 50% of lead-lag pairs are bootstrap").

`verify_regime_transition.py` is the regime state-machine integrity check. Run this after a market event suspected to cause a regime shift to confirm the state machine recorded the transition correctly per the `confirmed` / `early-strong` / `early-weak` rules.

The runbook's "mental check" section is what makes the verification useful for ops: an operator who hasn't read every story still gets a worked example showing what "everything is working" looks like. Pull from a real archive run when one exists; until then, hand-construct one based on the documented output formats.

## Acceptance criteria

- [ ] `scripts/verify_distillation.py` exists and runs the orchestrator end-to-end against the production database.
- [ ] All six state tables verified to have recent rows.
- [ ] All three sector outputs and the correlation/regime brief verified to be non-empty.
- [ ] All five archive files verified to exist at the documented paths.
- [ ] Summary table reports block counts, anomaly counts (by severity), bootstrap-tag counts, and freshness range.
- [ ] `scripts/verify_bootstrap_calibration_mix.py` exists and reports the calibration-state distribution per baseline kind with qualitative assertions matching the warm-up estimate.
- [ ] `scripts/verify_regime_transition.py` exists and asserts the regime state-machine invariants over a configurable lookback window.
- [ ] `VERIFICATION.md` exists with pre-conditions, script invocation procedure, expected outputs, and a worked mental-check example.
- [ ] Unit tests for the verification scripts cover their assertion logic against fault-injected fixtures.
- [ ] After running `python -m alphamind.collector bootstrap` and then `verify_distillation.py`, the script exits 0.
- [ ] After running the orchestrator for one full trading day (multiple invocations) and then `verify_bootstrap_calibration_mix.py`, the script reports a distribution consistent with the warm-up timeline and exits 0.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
