---
status: not_started
completed_date:
commit_id:
---

# 03 — Distillation→guardrail regime mapping

## Goal

Land the pure function that maps the distillation layer's four-tier regime classification (`LOW_VOL_COMPRESSION`, `VOL_EXPANSION`, `CRISIS_SPIKE`, `VOL_NORMALIZATION`) plus the supporting VIX level to the guardrail-side four-tier `Regime` enum (`low_vol`, `normal`, `elevated`, `crisis`) the configuration resolver consumes. The two label sets are deliberately not 1:1 — `VOL_EXPANSION` covers both `normal` and `elevated` VIX bands, and `VOL_NORMALIZATION` is a transitioning-down state that resolves to whichever band the current VIX implies — so the mapping requires the VIX boundary thresholds in addition to the classification label.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Regime classification mapping — the four guardrail regimes and their typical VIX ranges
- `docs/design/02-distillation-layer/external.md` § 4 Persistent state and composites — the four-tier classification ladder semantics; `VOL_EXPANSION` covers both normal and elevated VIX bands; `VOL_NORMALIZATION` is a transitioning state
- `docs/design/02-distillation-layer/threshold-calibration.md` § Regime classification boundaries — the VIX boundary thresholds (`regime_low_vol_vix_max=14.0`, `regime_normal_vix_max=22.0`, `regime_elevated_vix_max=35.0`)
- `src/alphamind/distillation/regime.py` — `RegimeLabel` (the four distillation labels), `classify_vix_band` (the underlying VIX-band classification function the distillation layer uses for skip detection), `VixBand` (the underlying VIX-band enum: `LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS`)
- `src/alphamind/config/models/regimes.py` — `Regime` StrEnum (the four guardrail regimes — `low_vol`, `normal`, `elevated`, `crisis`)
- `src/alphamind/config/models/distillation.py` — `RegimeClassification` Pydantic model carrying the VIX boundary thresholds the mapper reads
- `02-package-skeleton-and-types.md` — `regime_mapping.py` is the target module
- `tests/distillation/` — existing distillation regime tests; conventions for synthetic-snapshot fixtures

## Depends on

- 02 (package skeleton)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/regime_mapping.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_regime_mapping.py`.

### 1. Public function

```python
def map_distillation_to_guardrail_regime(
    *,
    distillation_label: DistillationRegimeLabel,
    vix_level: float,
    vix_thresholds: VixBoundaryThresholds,
) -> Regime
```

- `distillation_label` — the distillation layer's `RegimeLabel` (re-imported as `DistillationRegimeLabel` to avoid name collision with the portfolio-state `RegimeLabel` enum and to make the contract explicit at the boundary).
- `vix_level` — the current VIX spot level passed through from the distillation block's payload.
- `vix_thresholds` — the four VIX boundaries pulled from `RegimeClassification`. Wrapped in a small dataclass `VixBoundaryThresholds(low_vol_vix_max: float, normal_vix_max: float, elevated_vix_max: float)` so the function signature stays narrow.

Returns the matching `Regime`. The function is total over its input domain — every `(distillation_label, vix_level)` combination produces exactly one guardrail `Regime`.

### 2. `VixBoundaryThresholds` dataclass

```python
@dataclass(frozen=True, slots=True)
class VixBoundaryThresholds:
    low_vol_vix_max: float
    normal_vix_max: float
    elevated_vix_max: float
```

- Validates on construction that `0 < low_vol_vix_max < normal_vix_max < elevated_vix_max`. Raises `ValueError` with all three field names in the message.
- An adapter helper `from_regime_classification(rc: RegimeClassification) -> VixBoundaryThresholds` lives in the same file. The adapter copies the three values verbatim; downstream callers (08, 09) use it to bridge the upstream Pydantic model to the dataclass.

### 3. Mapping table

The mapping rules:

| Distillation label | Guardrail regime |
|---|---|
| `LOW_VOL_COMPRESSION` | `low_vol` |
| `CRISIS_SPIKE` | `crisis` |
| `VOL_EXPANSION` | resolved by VIX band (see below) |
| `VOL_NORMALIZATION` | resolved by VIX band (see below) |

For the two band-resolved labels, classify `vix_level` into the underlying VIX band and emit:

| VIX band (computed from `vix_level` and thresholds) | Guardrail regime |
|---|---|
| VIX ≤ `low_vol_vix_max` | `low_vol` |
| `low_vol_vix_max` < VIX ≤ `normal_vix_max` | `normal` |
| `normal_vix_max` < VIX ≤ `elevated_vix_max` | `elevated` |
| VIX > `elevated_vix_max` | `crisis` |

Boundary semantics: `≤` at the upper end of each band, matching the YAML invariant `regime_low_vol_vix_max == regime_normal_vix_min` already locked by the distillation layer's `classify_vix_band` function.

### 4. Implementation requirements

- The function reuses `alphamind.distillation.regime.classify_vix_band` for the VIX-band computation rather than reimplementing the boundaries — the boundary semantics live in one place. Pass the three threshold values as keyword arguments.
- The function does not look up thresholds from a global config object; it accepts `VixBoundaryThresholds` as input. Keeps the function pure and testable without filesystem fixtures.
- The function does not raise on the two definitionally-resolved labels (`LOW_VOL_COMPRESSION`, `CRISIS_SPIKE`); it accepts any non-negative `vix_level` without sanity-checking against the band — the distillation layer is the source of truth for the label and the mapper trusts it.
- The function does raise (`ValueError`) on a negative `vix_level`. Surfaces upstream data corruption rather than silently classifying a negative VIX as `low_vol`.
- Edge case at the boundary: `vix_level == low_vol_vix_max` resolves to `low_vol` (the `≤` semantics). Same for `vix_level == normal_vix_max` → `normal`, and `vix_level == elevated_vix_max` → `elevated`.

### 5. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_regime_mapping.py`:

- **`LOW_VOL_COMPRESSION` always maps to `low_vol`.** Test with `vix_level` values spanning `[5.0, 14.0, 50.0]` (including pathological cases above the low-vol band — the distillation layer is trusted).
- **`CRISIS_SPIKE` always maps to `crisis`.** Test with `vix_level` values `[10.0, 35.0, 80.0]` (including pathological cases below the crisis band).
- **`VOL_EXPANSION` at `vix_level=18.0` (mid-normal-band) maps to `normal`.**
- **`VOL_EXPANSION` at `vix_level=28.0` (mid-elevated-band) maps to `elevated`.**
- **`VOL_EXPANSION` at `vix_level=8.0` (below low-vol-max) maps to `low_vol`.** Verifies the band fallback covers the full VIX range, even though distillation rarely emits `VOL_EXPANSION` below the normal band.
- **`VOL_EXPANSION` at `vix_level=50.0` (above elevated-max) maps to `crisis`.** Same as above for the upper end.
- **`VOL_NORMALIZATION` at `vix_level=18.0` maps to `normal`.**
- **`VOL_NORMALIZATION` at `vix_level=28.0` maps to `elevated`.**
- **Boundary at `vix_level=14.0` (== `low_vol_vix_max`) for `VOL_EXPANSION` maps to `low_vol`.**
- **Boundary at `vix_level=22.0` (== `normal_vix_max`) for `VOL_EXPANSION` maps to `normal`.**
- **Boundary at `vix_level=35.0` (== `elevated_vix_max`) for `VOL_EXPANSION` maps to `elevated`.**
- **`vix_level=22.001` for `VOL_EXPANSION` maps to `elevated`.** Locks the strict-inequality semantics one tick above the boundary.
- **Negative `vix_level` raises `ValueError` regardless of the distillation label.** Mention the field name in the message.
- **`VixBoundaryThresholds` rejects construction when the bounds are not strictly ordered** — `low_vol_vix_max=14, normal_vix_max=10, elevated_vix_max=35` raises `ValueError` naming all three.
- **`VixBoundaryThresholds` rejects `low_vol_vix_max <= 0`.**
- **`VixBoundaryThresholds.from_regime_classification` mirrors all three values verbatim from a synthetic `RegimeClassification` instance.**
- **Determinism / purity:** identical inputs produce identical outputs; calling the mapper does not mutate `vix_thresholds`.

Out of scope:

- Reading the VIX boundaries from `config/distillation.yaml` at module load time. The mapper accepts the thresholds; the orchestrator (09) does the YAML→dataclass adaptation.
- Using `RegimeRefreshResult` directly as input. The mapper accepts the carved minimum (`distillation_label`, `vix_level`); the orchestrator extracts those from the upstream block.
- Persisting the mapping decision. The orchestrator (09) records `distillation_regime_label` and `active_regime` separately on `RegimeAdaptationState`; this story produces only the function.

## Notes

The `VOL_NORMALIZATION` label means "the system was elevated/crisis and is transitioning down." For the purpose of guardrail-multiplier resolution, we treat this as a normal/elevated regime once VIX has actually fallen into that band. The asymmetric loosening interpolation (story 04c) handles the *transition* itself; this mapper is concerned only with what regime the *current* state implies.

Per `feedback_no_inventing_component_names.md`, both `Regime` (guardrail) and `RegimeLabel` (distillation) are the existing names. The mapper does not introduce a third name; the file imports them from their canonical homes and uses `DistillationRegimeLabel` as a local type alias to disambiguate at the function signature.

Per `feedback_avoid_numeric_anchors.md`, no thresholds are hard-coded in the mapper. The `VixBoundaryThresholds` dataclass is the seam; the orchestrator wires in the values from `config/distillation.yaml`.

The mapper's strict-inequality boundary semantics (`vix_level == 22.0` → `normal`, `vix_level == 22.001` → `elevated`) match `alphamind.distillation.regime.classify_vix_band`'s existing `<=` semantics. Tests pin both behaviors so a future refactor that changes either function reveals the coupling.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/regime_mapping.py` exists and defines `map_distillation_to_guardrail_regime`, `VixBoundaryThresholds`, and `VixBoundaryThresholds.from_regime_classification`.
- [ ] `VixBoundaryThresholds` is a `dataclass(frozen=True, slots=True)`.
- [ ] `map_distillation_to_guardrail_regime` and `VixBoundaryThresholds` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] `LOW_VOL_COMPRESSION` maps to `Regime.low_vol` regardless of VIX level.
- [ ] `CRISIS_SPIKE` maps to `Regime.crisis` regardless of VIX level.
- [ ] `VOL_EXPANSION` and `VOL_NORMALIZATION` map by VIX band — `low_vol` below `low_vol_vix_max`, `normal` in the normal band, `elevated` in the elevated band, `crisis` above `elevated_vix_max`.
- [ ] Boundary semantics are inclusive at the upper end of each band (`<=`), matching `classify_vix_band`.
- [ ] Negative `vix_level` raises `ValueError`.
- [ ] `VixBoundaryThresholds` rejects construction with non-strictly-ordered bounds; raises `ValueError` naming all three fields.
- [ ] `VixBoundaryThresholds.from_regime_classification` produces a `VixBoundaryThresholds` whose three fields equal the input `RegimeClassification`'s `regime_low_vol_vix_max`, `regime_normal_vix_max`, and `regime_elevated_vix_max`.
- [ ] The function is pure: equal inputs produce equal outputs; the input dataclass is not mutated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
