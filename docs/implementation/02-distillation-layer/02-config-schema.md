---
status: not_started
completed_date:
commit_id:
---

# 02 — Distillation config schema

## Goal

Land `config/distillation.yaml` populated with every Class A threshold, plus a Pydantic model in `src/alphamind/config/models.py` that parses, type-checks, range-checks, and runs the documented validation invariants. Wire the file into the existing `load_config()` aggregate so distillation code reads it the same way collector code reads `data_sources.yaml`.

## Reading

- `docs/design/02-distillation-layer/threshold-calibration.md` § Static configuration thresholds — authoritative table of every Class A value with rationale
- `docs/design/02-distillation-layer/threshold-calibration.md` § Validation invariants — the cross-field checks that must run at load
- `docs/design/configuration-management.md` § `distillation.yaml` — worked example showing the file's seven top-level groups
- `docs/design/configuration-management.md` § Reload model, § Validation — semantics: every YAML reloads at the start of every invocation; failures abort
- `src/alphamind/config/models.py` — existing Pydantic models for `data_sources.yaml`, `collector_schedule.yaml`, `news_outlets.yaml` (the pattern to mirror)
- `src/alphamind/data_sources/_common.py` — `load_config()` aggregate that the new model plugs into

## Depends on

None — `models.py`, `_common.py`, and the `config/` directory all exist.

## Scope

In scope:
- `config/distillation.yaml` populated with every value from `threshold-calibration.md § Static configuration thresholds`. Match key names exactly; do not invent groupings beyond the seven the design doc names (`anomaly_detection`, `regime_classification`, `regime_transition`, `lead_lag`, `narrative_lag`, `persistence_windows`, `prediction_market`).
- A Pydantic model (e.g., `DistillationConfig`) in `src/alphamind/config/models.py` with one nested model per top-level group, all fields required, types and ranges enforced.
- Validators implementing every cross-field invariant from `threshold-calibration.md § Validation invariants`. Each invariant raises a clear error naming the offending key(s) when violated.
- Extend `load_config()` in `_common.py` to load `config/distillation.yaml`, validate via the new model, and return it as part of the immutable aggregate config object.
- Unit tests:
  - Canonical happy-path parse of the shipped `config/distillation.yaml`.
  - One mutation per invariant proving the validator rejects it (e.g., a regime boundary that breaks monotonicity, a `*_min_observations` exceeding its `*_baseline_days`, a negative sigma).
  - Type-coercion failures (string where float expected) raise.

Out of scope:
- Operator-tuning workflow / calibration log (covered in [`threshold-calibration.md § Update process`](../../../design/02-distillation-layer/threshold-calibration.md#update-process); operational, not implementation).
- Class B state schema or refresh logic (story 03 / 07).
- `bootstrap` cross-sectional fallbacks (story 04).
- Threshold consumers — anomaly detection, regime classification, etc. (stories 08*, 09).

## Notes

Pydantic v2: prefer `Field(..., ge=0)` for non-negative scalars and `model_validator(mode="after")` for cross-field invariants. Keep the model immutable (`model_config = ConfigDict(frozen=True)`) to match the existing pattern.

YAML key names in `threshold-calibration.md` are authoritative. Do not transliterate them to Python `snake_case` rewrites — the snake_case is already there in the source. The Pydantic field name should equal the YAML key.

`load_config()` already returns a frozen aggregate. Add `distillation: DistillationConfig` to the aggregate dataclass and load `config/distillation.yaml` alongside the others. Keep the load order alphabetical for greppability.

The "all `*_min_observations` ≤ corresponding `*_baseline_days`" invariant maps `sentiment_min_observations` → `sentiment_baseline_days`, `gap_fill_min_events` → `gap_fill_baseline_days`, `extended_hours_min_events` → `extended_hours_confirmation_days`. Implement the mapping explicitly rather than by name-pattern matching — the suffix convention varies (`_events` vs. `_observations`).

The `regime_skip_emergency_trigger` field is a boolean, not a numeric threshold. Cover it in a separate type-validator test.

## Acceptance criteria

- [ ] `config/distillation.yaml` exists and contains every key from `threshold-calibration.md § Static configuration thresholds` with the documented default value.
- [ ] `src/alphamind/config/models.py` defines a `DistillationConfig` Pydantic model with one nested model per top-level group; every field is required and type-checked.
- [ ] All thirteen validation invariants from `threshold-calibration.md § Validation invariants` are implemented and tested.
- [ ] `load_config()` in `_common.py` loads, validates, and returns `distillation` as part of the frozen aggregate.
- [ ] A unit test parses the shipped `config/distillation.yaml` cleanly and accesses fields through the aggregate config object.
- [ ] One unit test per invariant demonstrates the validator raises a clear error on a mutation that violates only that invariant.
- [ ] A unit test covers a non-numeric value where a numeric is expected (Pydantic raises ValidationError).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
