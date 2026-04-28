---
status: in_progress
completed_date:
commit_id:
---

# 06 — Aggregation primitives

## Goal

Take the per-slice `SliceReplayResult` collections produced by the engine (story 05) and compute the four content aggregations that populate the report (story 07): per-regime flag-rate table, regime-label distribution per slice, Class B baseline shape summary, calibration-state breakdown. The aggregator handles single-config and dual-config (diff-mode) inputs by accepting a list of `SliceReplayResult` per regime — single-config means one entry, diff-mode means two; the diff aggregations compute candidate/baseline/delta tuples.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Output — the four content sections this story computes (Per-regime flag-rate table, Regime-label distribution, Class B baseline shape summary, Calibration-state breakdown), including the diff-mode column shape (candidate / baseline / delta)
- `docs/design/02-distillation-layer/replay-harness.md` § Process — step 5 "Aggregate" defining the per-regime rate as `count of invocations the flag fired / count of invocations in the slice`
- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — the `calibrated` / `bootstrap` / `unavailable` tag space the calibration breakdown counts
- `docs/design/02-distillation-layer/threshold-calibration.md` § Where each threshold lives — the four Class B baseline classes (per-ticker volume, per-pair lead-lag, per-contract prediction-market history, per-event-history) the shape summary groups by
- Story 04 of the parent distillation work tree — the `CalibrationState` enum the aggregator counts against
- Story 05 of this work tree — `SliceReplayResult`, `InvocationOutputs`, `AnomalyFlagRecord`, `BaselineValueRecord` shapes consumed here
- Story 03 of this work tree — `SliceManifest.regime_label` (the *exemplary* regime per slice, distinct from per-invocation classification)

## Depends on

- 05 (engine — produces the `SliceReplayResult` instances aggregated here)

## Scope

In scope: under `src/alphamind/distillation/replay_harness/aggregation.py` —

- **`FlagRateCell` frozen dataclass** for one cell of the per-regime flag-rate table:
  - `flag_type: str`
  - `regime_label: str`
  - `flag_invocation_count: int` — invocations the flag fired in.
  - `total_invocation_count: int` — invocations in the slice(s) for that regime.
  - `rate: float` — `flag_invocation_count / total_invocation_count` if denominator > 0 else `0.0`.

- **`FlagRateDelta` frozen dataclass** for diff-mode cells:
  - `candidate: FlagRateCell`
  - `baseline: FlagRateCell`
  - `delta_rate: float` — `candidate.rate - baseline.rate`.

- **`PerRegimeFlagRates` frozen dataclass**:
  - `mode: str` — `single` or `diff`.
  - `single_cells: tuple[FlagRateCell, ...] | None` — populated in single mode.
  - `diff_cells: tuple[FlagRateDelta, ...] | None` — populated in diff mode.
  - `flag_types: tuple[str, ...]` — sorted union of all flag types observed across both configs.
  - `regimes: tuple[str, ...]` — sorted set of regimes covered (a subset of `low_vol`, `normal`, `elevated`, `crisis`).

- **`RegimeLabelDistributionEntry` frozen dataclass**:
  - `slice_id: str`
  - `slice_exemplary_regime: str` — from the manifest.
  - `candidate_label_counts: dict[str, int]` — per-orchestrator-classified-label counts under the candidate config.
  - `baseline_label_counts: dict[str, int] | None` — null in single mode; populated in diff mode.

- **`RegimeLabelDistribution` frozen dataclass** wrapping a `tuple[RegimeLabelDistributionEntry, ...]` ordered by `(slice_exemplary_regime, slice_id)`.

- **`BaselineShapeSummary` frozen dataclass** for one Class B baseline class × regime cell:
  - `baseline_kind: str` — one of `volume`, `atr`, `spread`, `sentiment`, `pair_lag`, `contract_history`, `event_history` (the union of categories the engine emits via `BaselineValueRecord.baseline_kind`).
  - `regime_label: str`
  - `value_count: int`
  - `value_median: float | None` — null when `value_count == 0`.
  - `value_iqr: float | None` — null when `value_count == 0`.

- **`BaselineShapeDelta` frozen dataclass**:
  - `candidate: BaselineShapeSummary`
  - `baseline: BaselineShapeSummary`

- **`PerBaselineShapeSummary` frozen dataclass**:
  - `mode: str`
  - `single_cells: tuple[BaselineShapeSummary, ...] | None`
  - `diff_cells: tuple[BaselineShapeDelta, ...] | None`

- **`CalibrationStateBreakdownEntry` frozen dataclass**:
  - `regime_label: str`
  - `calibration_state: str` — one of `calibrated`, `bootstrap`, `unavailable`.
  - `candidate_count: int`
  - `baseline_count: int | None` — null in single mode.

- **`CalibrationStateBreakdown` frozen dataclass** wrapping `tuple[CalibrationStateBreakdownEntry, ...]` ordered by `(regime_label, calibration_state)`.

- **`AggregatedReport` frozen dataclass** combining the four aggregations:
  - `mode: str`
  - `flag_rates: PerRegimeFlagRates`
  - `regime_distribution: RegimeLabelDistribution`
  - `baseline_shape: PerBaselineShapeSummary`
  - `calibration_breakdown: CalibrationStateBreakdown`

- **`aggregate_replay_results(candidate_results: dict[str, list[SliceReplayResult]], baseline_results: dict[str, list[SliceReplayResult]] | None = None) -> AggregatedReport`** function:
  - The `dict` key is the `regime_label` (matching the slice's exemplary regime); the value is the list of slice results for that regime under the named config.
  - When `baseline_results is None`, runs single-config aggregation. Otherwise diff mode.
  - In diff mode, both dicts must cover the same set of `regime_label` keys and each regime must contain results for the same ordered list of slice IDs — the aggregator validates this and raises `AggregationInputError` with a clear message naming the mismatch.

  Procedure:

  1. **Per-regime flag-rate table.** For each `(flag_type, regime_label)`:
     - `flag_invocation_count` = number of invocations across all slices in that regime where the flag appeared in `InvocationOutputs.anomaly_flags` (counted at the invocation level — multiple flags of the same type within one invocation count as 1).
     - `total_invocation_count` = total invocations across all slices in that regime.
     - `rate` = the ratio.
     - In diff mode, compute the same for both configs and emit `FlagRateDelta`.
     - The `flag_types` list is the sorted union over both configs (a flag firing only under candidate appears in the table with baseline rate 0; vice versa).

  2. **Regime-label distribution.** For each slice:
     - `candidate_label_counts` = `Counter(invocation.regime_label for invocation in candidate_results[regime][slice_idx].invocations)`.
     - `baseline_label_counts` = same against baseline (null in single mode).

  3. **Class B baseline shape summary.** For each `(baseline_kind, regime_label)`:
     - Collect `BaselineValueRecord.value` across every invocation across every slice in that regime. Drop `None` values.
     - `value_count` = count of non-null values.
     - `value_median`, `value_iqr` = `numpy.median` and `numpy.percentile(values, 75) - numpy.percentile(values, 25)`. Use `numpy` if it's already a project dependency; otherwise `statistics.median` and a manual percentile via `statistics` or sorted-index — the existing `validate_universe.py` script imports `statistics` and `pandas`, so `pandas` is acceptable.

  4. **Calibration-state breakdown.** For each `(regime_label, calibration_state)`:
     - Count every output (anomaly flag and baseline value) tagged with that state across every invocation across every slice in the regime.
     - In diff mode, compute the same for both configs.

  5. **Determinism.** All sorting (flag types, regimes, slice IDs, baseline kinds) is alphabetical to make the report byte-deterministic across runs.

- **Edge cases:**
  - A regime with zero slices is dropped from all aggregations (rather than zero-padding) — story 08's CLI handles the curation-required error on missing fixtures before reaching the aggregator.
  - A flag that fires in zero invocations across all regimes does not appear in the flag-rate table.
  - A baseline kind that produces zero values does not appear in the shape summary.
  - The calibration breakdown lists every (regime, state) combination observed, even at zero counts within a regime — so the operator sees a complete picture of which regimes had `unavailable` outputs.

- **`AggregationInputError`** typed exception (subclass of `ValueError`) raised on input-validation failure (mismatched regime keys, mismatched slice IDs, empty candidate dict). Aggregation is purely functional; if inputs are well-formed, the function does not raise.

- **Unit tests** under `tests/distillation/replay_harness/test_aggregation.py`:
  - Construct synthetic `SliceReplayResult` instances directly (no engine dependency in this story's tests):
    - 1 regime, 2 slices, 5 invocations each, 3 flag types — verify `FlagRateCell.rate` equals expected fraction.
    - 4 regimes, 1 slice each — verify the table has 4 columns.
    - Diff mode: same fixture run with two configs, candidate has higher rate than baseline — `FlagRateDelta.delta_rate` is positive.
    - Single mode: `aggregate_replay_results(...)` with no baseline returns `mode='single'`, `single_cells` populated, `diff_cells` is None.
    - Diff mode: `mode='diff'`, `diff_cells` populated, `single_cells` is None.
    - Mismatched regime keys between candidate and baseline raises `AggregationInputError`.
    - Mismatched slice IDs within the same regime raises `AggregationInputError`.
  - Regime-label distribution:
    - A slice with 5 invocations all classified as `vol_expansion` produces `candidate_label_counts == {"vol_expansion": 5}`.
    - A slice straddling a regime transition produces a multi-key counter.
  - Baseline shape summary:
    - A baseline kind with 10 values [1, 2, ..., 10] produces `value_median == 5.5` and `value_iqr == 5.0` (for the standard pandas/numpy default of linear interpolation; specify the interpolation choice in the docstring and stick with it).
    - A baseline kind with zero values does not appear in the output.
  - Calibration breakdown:
    - A fixture with 80% `calibrated` / 15% `bootstrap` / 5% `unavailable` outputs in one regime produces matching counts.
  - Determinism:
    - Two runs of `aggregate_replay_results` on the same inputs produce equal `AggregatedReport` (compare via `dataclasses.asdict`).

Out of scope:
- Rendering Markdown (story 07).
- Sampling or weighting — the aggregator treats every invocation equally; if the operator wants weighted statistics, a future story can add a weights argument.
- Computing significance bands on the rates (the operator interprets the rate; the harness is informational, per the design's calibrated-not-pessimistic discipline).
- Cross-regime aggregation rolls (the per-regime view IS the deliverable; no "all regimes" column).
- Filtering low-sample-size cells (annotation in the rendered report, not in the aggregation).

## Notes

The `(flag_type, regime_label) → cell` lookup the renderer needs is a sparse table — for any combination not produced by the aggregator, the renderer (story 07) emits a "—" cell (zero observations). Build the cells list including only observed combinations; the renderer expands the grid.

The per-invocation deduplication in the flag-rate count is deliberate: the design says "rate per anomaly flag type (count of invocations the flag fired / count of invocations in the slice)". An invocation that fires three volume anomalies for three different tickers counts as 1, not 3. The flag is "fired this invocation" or "did not fire this invocation."

The calibration breakdown counts BOTH anomaly flags and baseline values, not just one. The design says "Count of outputs tagged `bootstrap` vs. `calibrated` vs. `unavailable`" — "outputs" includes both. A test should cover this explicitly.

Pandas vs. NumPy vs. stdlib `statistics` for IQR — pick one and document it in the function's docstring. The choice doesn't affect correctness for this story (the data volumes are small and the values are floats); pick the one already imported elsewhere in the project to minimize new dependencies.

Per `feedback_simplify_before_building.md`, do not introduce a `Aggregator` class with stateful methods. The aggregation is one function call producing an immutable result. Helper functions (`_compute_flag_rates`, `_compute_baseline_shapes`, etc.) can be module-private.

Per `feedback_no_decision_trails.md`, do not add Notes-section commentary like "we used to compute X but it's now Y" or "this aggregation is NOT cross-regime." State the contracts positively in Scope; let the reader read the test cases for boundary clarity.

Per `feedback_avoid_numeric_anchors.md`, the test fixtures' "80%/15%/5%" calibration split is illustrative; the assertion is on counts (`candidate_count == 80`), not on percentages or thresholds.

The diff-mode contract — "both dicts must cover the same regimes and same slice IDs" — is strict because side-by-side comparison only makes sense over a shared substrate. Story 08's CLI guarantees this by feeding the same fixture set through both configs.

## Acceptance criteria

- [ ] All listed dataclasses (`FlagRateCell`, `FlagRateDelta`, `PerRegimeFlagRates`, `RegimeLabelDistributionEntry`, `RegimeLabelDistribution`, `BaselineShapeSummary`, `BaselineShapeDelta`, `PerBaselineShapeSummary`, `CalibrationStateBreakdownEntry`, `CalibrationStateBreakdown`, `AggregatedReport`) exist with the documented fields and `frozen=True` where applicable.
- [ ] `aggregate_replay_results(candidate, baseline=None)` produces single-mode output when `baseline is None` and diff-mode output otherwise.
- [ ] In diff mode, mismatched regime keys raise `AggregationInputError`.
- [ ] In diff mode, mismatched slice IDs within a regime raise `AggregationInputError`.
- [ ] Per-regime flag-rate cells use per-invocation deduplication: an invocation with three flags of the same type counts as 1.
- [ ] Flag types observed only under one config appear in diff-mode cells with rate 0 for the missing side.
- [ ] Regime-label distribution counts come from the orchestrator's per-invocation classification, not the slice's exemplary regime.
- [ ] Baseline shape median + IQR computation uses the documented interpolation method.
- [ ] Calibration-state breakdown counts both anomaly flags and Class B baseline values.
- [ ] Determinism test: identical inputs produce equal `AggregatedReport`.
- [ ] All sorting (flag types, regimes, slice IDs, baseline kinds, calibration states) is alphabetical.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
