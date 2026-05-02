---
status: done
completed_date: 2026-04-27
commit_id: 1392731
---

# 03h — `digest.yaml` + Pydantic model

## Goal

Land `config/digest.yaml` (notable-shift thresholds for the weekly feedback-loop digest) and a Pydantic model. The file is the operator's tuning surface for "fire vs. don't fire" on the digest's notable-shifts section: each threshold defines when a deterministic shift detector flags a shift worth surfacing in the weekly review.

## Reading

- `docs/design/configuration-management.md` § `digest.yaml` — schema and worked example
- `docs/design/feedback-loop.md` § Section 5 — Notable shifts — narrative description of each shift detector
- `docs/design/configuration-management.md` § Validation — semantic-self-test invariants applicable to `digest.yaml` (every `baseline_window_weeks` ≥ 1, `multiplier_vs_baseline` > 1.0, etc.)
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/digest.yaml` populated with the design-doc worked example values:
  - `anti_pattern_spike: { baseline_window_weeks: 4, multiplier_vs_baseline: 2.0, min_occurrences_this_week: 5 }`
  - `regime_change: { enabled: true }`
  - `sector_underperform: { baseline_window_weeks: 4, median_offset_sigma: 1.5 }`
  - `citation_chain_shift: { baseline_window_weeks: 4, delta_pp_threshold: 20.0 }`
  - `source_signal_survival_drop: { baseline_window_weeks: 4, delta_pp_threshold: 20.0 }`
  - `validation_window_end: { days_before_due: 7 }`
  - `validation_superseded: { enabled: true }`
- `src/alphamind/config/models/digest.py` defining one nested model per shift block:
  - `AntiPatternSpike` (`baseline_window_weeks: int = Field(ge=1)`, `multiplier_vs_baseline: float = Field(gt=1.0)`, `min_occurrences_this_week: int = Field(ge=0)`)
  - `RegimeChange` (`enabled: bool`)
  - `SectorUnderperform` (`baseline_window_weeks: int = Field(ge=1)`, `median_offset_sigma: float = Field(gt=0)`)
  - `CitationChainShift` (`baseline_window_weeks: int = Field(ge=1)`, `delta_pp_threshold: float = Field(gt=0, le=100)`)
  - `SourceSignalSurvivalDrop` (`baseline_window_weeks: int = Field(ge=1)`, `delta_pp_threshold: float = Field(gt=0, le=100)`)
  - `ValidationWindowEnd` (`days_before_due: int = Field(ge=0)`)
  - `ValidationSuperseded` (`enabled: bool`)
  - `DigestConfig` (BaseModel composing all seven nested models as required fields)
- The Field constraints above implement the semantic-self-test invariants from `configuration-management.md § Validation` at parse time. No additional model validators are needed for this file — every invariant is a single-field range check.
- Re-export `DigestConfig` plus the seven nested models from `models/__init__.py`.
- Unit tests covering: shipped `config/digest.yaml` parses cleanly; missing nested block raises; `multiplier_vs_baseline: 1.0` raises; `delta_pp_threshold: 0` raises; `delta_pp_threshold: 150` raises; `baseline_window_weeks: 0` raises; `median_offset_sigma: -1.0` raises.

Out of scope:
- The shift-detector implementations themselves — owned by the feedback-loop work, not this feature.
- Coupling to `feedback-loop.md`'s validation-evaluation surface — the threshold values land here, the consumer reads them.
- Wiring `DigestConfig` into the loader aggregate (story 08).

## Notes

Each shift block has a different substructure — exactly what that shift needs. The schema does not impose a uniform shape across blocks (e.g., `regime_change` and `validation_superseded` carry only `enabled`, while the others carry windows and thresholds). Resist the temptation to lift common fields into a base class; the substructures are intentionally distinct, and the design doc expresses them as such.

`delta_pp_threshold` is a **percentage-point** value, not a percentage. The design doc enforces it within `(0, 100]` because a percentage-point delta cannot exceed 100. This story's range constraint matches.

`min_occurrences_this_week ≥ 0` allows the operator to set the floor to zero (every spike fires regardless of raw count). That's a reasonable operator choice; do not constrain to `> 0`.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/digest.yaml` exists, declares all seven shift blocks with the keys and values shown in Scope.
- [ ] `config/digest.yaml` parses cleanly via `yaml.safe_load` and validates against `DigestConfig`.
- [ ] `src/alphamind/config/models/digest.py` defines `DigestConfig` and the seven nested models.
- [ ] `models/__init__.py` re-exports the eight names.
- [ ] A unit test asserts the shipped `config/digest.yaml` parses and exposes all seven blocks.
- [ ] A unit test asserts a YAML missing `regime_change` raises `ValidationError`.
- [ ] A unit test asserts `anti_pattern_spike.multiplier_vs_baseline: 1.0` raises `ValidationError`.
- [ ] A unit test asserts `anti_pattern_spike.multiplier_vs_baseline: 0.5` raises `ValidationError`.
- [ ] A unit test asserts `citation_chain_shift.delta_pp_threshold: 0` raises `ValidationError`.
- [ ] A unit test asserts `citation_chain_shift.delta_pp_threshold: 100.0` is accepted (boundary inclusive).
- [ ] A unit test asserts `citation_chain_shift.delta_pp_threshold: 100.1` raises `ValidationError`.
- [ ] A unit test asserts `sector_underperform.median_offset_sigma: 0` raises `ValidationError`.
- [ ] A unit test asserts `validation_window_end.days_before_due: 0` is accepted; `-1` raises.
- [ ] A unit test asserts `anti_pattern_spike.min_occurrences_this_week: 0` is accepted.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
