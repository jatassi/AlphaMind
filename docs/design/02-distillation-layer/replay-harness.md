# Distillation replay harness

A standalone analytical tool that re-runs the deterministic distillation layer against archived inputs under a candidate `config/distillation.yaml`, emitting a per-regime flag-rate report. The substrate for regime-sensitive sanity checking of threshold tuning, consumed as evidence by the [`/feedback-validate`](../../../.claude/skills/feedback-validate/SKILL.md) REGISTER step.

A tool — reads archived inputs and a candidate config, writes a report file, sits outside the trading hot path. Not a runtime engine and not part of the pipeline. Estimates flag-rate distributions across canonical regimes; same calibrated-not-pessimistic discipline as [paper-evaluation-harness.md](../05-execution-layer/paper-evaluation-harness.md) — the harness uses the same primitives the live distillation phase uses, with no added conservatism.

---

## Why the harness exists

Class A threshold edits ([threshold-calibration.md § Class A](threshold-calibration.md#class-a--static-configuration-constants)) reshape downstream signals across every invocation. Forward observation alone cannot distinguish "the new threshold is better" from "the regime moved" — [feedback-loop.md § Confounder management](../feedback-loop.md#confounder-management) names regime drift as the largest single confounder of feedback inference. The harness lets the operator stratify a candidate config's flag-rate behavior across canonical regimes before registering the edit's expected direction in `/feedback-validate`, so the pre-registered expectation is grounded in regime-conditioned evidence rather than a single live window's noise.

The deterministic layer is the only feedback-loop subject where this is possible: the layer is pure (input vector → output vector, no LLM tokens), so historical replay produces the same outputs the live system would have produced under the candidate config.

---

## Scope

Replay of the deterministic distillation layer:

- Anomaly flags ([external.md § Anomaly detection](external.md))
- Regime classification label ([external.md § Persistent state and composites](external.md))
- Class B rolling baselines ([threshold-calibration.md § Class B](threshold-calibration.md#class-b--computed-rolling-state))

Full-pipeline backtesting questions — fills, P/L, LLM agent outputs — live with the [counterfactual replay engine](../05-execution-layer/counterfactual-replay-engine.md) (forward price data on PM decisions) and the [paper-evaluation harness](../05-execution-layer/paper-evaluation-harness.md) (live execution drag estimation). Each tool's version lineage evolves on its own cadence.

---

## Inputs

1. **Fixture store.** Regime-stratified slices of the data layer's raw input tables — one slice per canonical regime label (`low_vol`, `normal`, `elevated`, `crisis`, matching the regime values produced by [external.md § Volatility regime classification](external.md)). Each slice spans enough invocations to populate the longest Class A persistence window from [threshold-calibration.md § Persistence and percentile windows](threshold-calibration.md#persistence-and-percentile-windows). Curation policy below.
2. **Candidate config.** A `config/distillation.yaml` snapshot path — typically the operator's edited working copy. Validated against the same Pydantic model the runtime loads ([configuration-management.md § `distillation.yaml`](../configuration-management.md)), so a malformed config fails at the harness boundary before any computation.
3. **Baseline config (optional).** A second `config/distillation.yaml` snapshot path for diff mode. When present, the harness runs both configs against the same fixture and emits a side-by-side report.

---

## Process

For each fixture slice:

1. **Load.** Read the slice's raw input tables (`ohlcv_bars`, `macro_observations`, etc. per [storage.md](../01-data-layer/collector/storage.md)) into in-memory frames.
2. **Compute Class B baselines from the slice's own history.** The distillation layer's persistent state for the slice is computed from the slice itself, not loaded from the runtime DB — keeps the replay deterministic and isolated from current state. Reuses the rolling-state computation code paths from `src/alphamind/distillation/` so the harness and the live layer share one implementation.
3. **Run distillation under the candidate config.** Reuse the live distillation orchestrator on the slice's invocation timestamps, with the candidate config substituted for the runtime-loaded one. Capture every anomaly flag, regime label, and Class B baseline emitted across the slice.
4. **(Diff mode)** Repeat step 3 against the baseline config.
5. **Aggregate.** For each canonical regime, compute the rate per anomaly flag type (count of invocations the flag fired / count of invocations in the slice) and the regime-label distribution (how many invocations were classified as each regime under the candidate config — useful when the edit touches regime boundaries). In diff mode, emit deltas alongside the candidate's absolute rates.

---

## Output

A single Markdown report file under `data/replay_reports/{report_id}/report.md`, with `report_id = {timestamp}_{candidate_config_hash}[_{baseline_config_hash}]`. Sections:

| Section | Content |
|---|---|
| Header | Candidate config path and SHA-256, baseline config path and SHA-256 (diff mode only), fixture store version, harness version, generation timestamp |
| Per-regime flag-rate table | One row per anomaly flag type, one column per canonical regime; rate as percentage, with sample-size annotation per cell. Diff mode: candidate rate / baseline rate / delta in three sub-columns per regime |
| Regime-label distribution | Count of invocations classified as each regime under the candidate config, per fixture slice. Diff mode: candidate vs. baseline counts |
| Class B baseline shape summary | For each Class B baseline class (per-ticker volume, per-pair lead-lag, per-contract prediction-market history per [threshold-calibration.md § Where each threshold lives](threshold-calibration.md#where-each-threshold-lives)), summary statistics of the baseline values produced (median, IQR, count); diff mode includes baseline-config equivalents |
| Calibration-state breakdown | Count of outputs tagged `bootstrap` vs. `calibrated` vs. `unavailable` per [threshold-calibration.md § Bootstrap policy](threshold-calibration.md#bootstrap-policy), per regime |

The report is consumed by the operator at the `/feedback-validate` REGISTER step: the regime-stratified flag-rate distribution informs the pre-registered expected direction and magnitude. The operator cites the `report_id` inside the registration's free-text `expected_magnitude` or `success_criterion` ([state-persistence.md § Validations](../05-execution-layer/state-persistence.md)) so the evaluation walk's verbatim re-read surfaces the regime evidence the registration was conditioned on.

---

## Fixture store

Lives at `data/replay_fixtures/{regime_label}/{slice_id}/` with one subdirectory per slice. Each slice is a self-contained snapshot of the raw input tables for the slice's invocation range, plus a `manifest.json` capturing:

- Regime label (the canonical regime the slice exemplifies)
- Source: `live_archive` (curated from a window of live invocation records whose `active_regime` matched) or `historical_curated` (hand-selected historical window predating live operation, e.g., a 2020-Q1 crisis window)
- Invocation timestamp range
- Source-table commit hashes (for `live_archive` slices, the data layer table contents at slice creation; for `historical_curated`, the original ingestion timestamps)
- Curation notes (operator commentary on why this window represents the regime)

Slices are append-only; an existing slice is never edited in place. Updating a slice means creating a new slice with a new `slice_id` and updating the manifest's `replaces` field. The harness's report header records the slice IDs consumed, so any report stays reproducible from the persisted fixture.

Curation cadence: the operator adds slices when paper trading produces a window that exemplifies a regime not yet covered, or when a hand-curated historical window is needed for a regime live operation has not yet observed.

---

## Activation

The harness is operator-invoked from the command line, mirroring the project's existing analytical-script convention ([scripts/validate_universe.py](../../scripts/validate_universe.py), [scripts/verify_bootstrap.py](../../scripts/verify_bootstrap.py)):

```
python -m alphamind.distillation.replay_harness \
  --candidate-config path/to/edited/distillation.yaml \
  [--baseline-config path/to/prior/distillation.yaml] \
  [--regimes low_vol,normal,elevated,crisis]
```

Read-only on runtime state — runs against the fixture store and snapshot configs, never against the runtime distillation tables. Failure to find a fixture for a requested regime aborts with a curation-required error; flag rates from a partial regime set would mislead.

The `/feedback-validate` REGISTER walk surfaces the harness as a recommended evidence step when the edited artifact is `config/distillation.yaml`; the operator runs the harness and cites the resulting `report_id` in the registration's free-text fields. Evaluation re-reads the registration verbatim, so the regime grounding is auditable.

---

## Versioning

The harness emits a `harness_version` string in every report header, advancing when the harness algorithm changes (a new aggregation, a new fixture-loading rule, a Class B baseline computation switching to a new code path). Reports remain valid records tagged with their version. Aggregation queries at retrospective time can filter to a single version when consistency matters.

The harness's distillation code paths are imported from `src/alphamind/distillation/` rather than copied, so a runtime distillation change automatically flows into the next harness run. The version string captures the harness's own aggregation and reporting code, not the imported distillation logic — that's pinned by the git SHA recorded in the report header.

---

## Dependencies

- [Threshold calibration](threshold-calibration.md) — defines the Class A constants the candidate config under test parameterizes, the Class B baselines the harness recomputes, and the bootstrap-tagging convention the report renders
- [External distillation](external.md), [internal distillation](internal.md) — the computations the harness reruns; the harness imports their code paths from `src/alphamind/distillation/` rather than reimplementing them
- [Configuration management](../configuration-management.md) — defines the `distillation.yaml` schema and the Pydantic model the harness validates the candidate config against
- [Storage](../01-data-layer/collector/storage.md) — defines the raw input tables the fixture slices snapshot
- [State persistence § Invocation records](../05-execution-layer/state-persistence.md) — provides the `active_regime` field on each invocation record that drives `live_archive` slice curation
- [Feedback loop § Confounder management](../feedback-loop.md#confounder-management) — names the regime confounder the harness exists to neutralize
- [`/feedback-validate` SKILL](../../../.claude/skills/feedback-validate/SKILL.md) — the consumer of the harness's reports during REGISTER mode for `config/distillation.yaml` edits
