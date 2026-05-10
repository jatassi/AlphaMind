# 01d — Retype `time_expectation_hours` from `str` to `float`

## Goal

Change `ThesisRecord.time_expectation_hours` from `str` to `float` (with `Annotated[float, Field(gt=0)]`) and add a cross-field consistency validator that `expected_resolution_at == generation_timestamp + timedelta(hours=time_expectation_hours)` within a small tolerance. The current `str` typing is a latent bug — every other site that owns this field uses numeric (`analyst.models.Recommendation.time_expectation_hours: float = Field(gt=0, le=72)`, `qualitative_research.loaders` uses `int`), and consumers that compare position-age (a float) against the thesis's expectation field need numeric on both sides. The string field has no parse-validation and silently accepts non-numeric values.

## Reading

* `src/alphamind/portfolio_state/records/theses.py:103` — current `time_expectation_hours: str` field on `ThesisRecord`.
* `src/alphamind/portfolio_state/records/theses.py:128` — current validator `_check_time_expectation_non_empty` (only checks non-empty string).
* `src/alphamind/decision/analyst/models.py:392` — sibling site that already uses `float = Field(gt=0, le=72)` — the numeric vocabulary this story aligns with.
* `src/alphamind/analysis/qualitative_research/loaders.py:157` — sibling site using `int = Field(ge=0)`.
* `docs/design/04-decision-layer/strategist.md:107` — the consumer that compares `position_age_hours` (float) against `time_expectation_hours` for staleness detection.
* `docs/design/04-decision-layer/portfolio-manager.md:143` — PM uses `time_expectation_hours` numerically (e.g., "exceeds the system's 4–72 hour horizon").
* All current callers constructing `ThesisRecord` to identify fixture sites needing string→numeric updates: `src/alphamind/scripts/verify_strategist.py:390`, `src/alphamind/scripts/verify_synthesizer.py:451`, `src/alphamind/scripts/verify_proposal_pre_processor.py:777` (uses 48.0 already as float), `src/alphamind/portfolio_state/consumers/synthesizer.py:39,127`.
* `src/alphamind/analysis/synthesizer/portfolio_tools.py:64` and `src/alphamind/analysis/qualitative_research/input_bundle.py:198` — render the field as `f"{...}h"` or `f"{...}"`; both must produce the same canonical numeric format after the change.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule does NOT apply here; this is a TYPE CHANGE from `str` to `float`. Acceptance criteria below mark this as a breaking schema change requiring fixture updates.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it changes one field's type in `records/theses.py` and updates the small set of fixture builders that construct `ThesisRecord` with string values.

## Scope

Source under `src/alphamind/portfolio_state/records/theses.py` (one field retype + new validator + updated existing validator) and the small set of fixture/test sites that construct `ThesisRecord` with string `time_expectation_hours`. Tests at `tests/portfolio_state/records/test_theses.py` (extend existing TestThesisRecord-style suite).

### 1\. Retype the field

In `src/alphamind/portfolio_state/records/theses.py`:

```python
from typing import Annotated
from pydantic import Field

class ThesisRecord(BaseModel):
    ...
    time_expectation_hours: Annotated[float, Field(gt=0)]
    ...
```

Replace the existing `_check_time_expectation_non_empty` validator (which only checks `not self.time_expectation_hours` for empty strings) with the implicit Pydantic `gt=0` constraint. The non-empty-string check becomes obsolete.

### 2\. Add the cross-field consistency validator

Add a new `model_validator(mode="after")` named `_check_time_expectation_consistency`:

```python
@model_validator(mode="after")
def _check_time_expectation_consistency(self) -> ThesisRecord:
    expected = self.generation_timestamp + timedelta(hours=self.time_expectation_hours)
    delta_seconds = abs((self.expected_resolution_at - expected).total_seconds())
    # Allow 60-second tolerance for fractional-hour rounding in producers
    if delta_seconds > 60.0:
        msg = (
            f"expected_resolution_at ({self.expected_resolution_at!r}) must equal "
            f"generation_timestamp ({self.generation_timestamp!r}) + "
            f"time_expectation_hours ({self.time_expectation_hours}) within 60s; "
            f"got delta of {delta_seconds:.1f}s"
        )
        raise ValueError(msg)
    return self
```

This catches producer drift (e.g., a producer setting `time_expectation_hours=48` but `expected_resolution_at` not 48 hours after `generation_timestamp`).

### 3\. Update fixture/test sites

Update the following producer sites to pass `float` rather than `str`:

* `src/alphamind/scripts/verify_strategist.py` — change `time_expectation_hours="48"` to `time_expectation_hours=48.0`.
* `src/alphamind/scripts/verify_synthesizer.py` — same.
* Any test fixtures in `tests/portfolio_state/records/test_theses.py` and any `tests/portfolio_state/_fixtures.py` shared builders.
* `src/alphamind/portfolio_state/consumers/synthesizer.py:39,127` — update the local DTO and the projection function to type as `float`.
* `src/alphamind/analysis/synthesizer/portfolio_tools.py:64` and `src/alphamind/analysis/qualitative_research/input_bundle.py:198` — render as `f"{int(t.time_expectation_hours)}h"` if you want the legacy integer display, or `f"{t.time_expectation_hours}h"` for the canonical float display. Pick whichever matches existing fixture-asserted output strings; document the choice in a one-line comment.

### Out of scope

* Changing the units (hours stays hours; no conversion to a `timedelta` field).
* Adding a `Field(le=72)` cap mirroring the analyst's hard horizon — the design (`docs/design/04-decision-layer/portfolio-manager.md:143`) treats >72h as a soft criterion failure, not a hard schema reject. ThesisRecord stays unbounded above so existing-position records that drift past 72h remain constructible.
* Refactoring the consumers' string-rendering call sites beyond what's needed to pass type-checking after the change.

## Acceptance criteria

- [ ] `ThesisRecord.time_expectation_hours` is typed as `Annotated[float, Field(gt=0)]` in `src/alphamind/portfolio_state/records/theses.py`.
- [ ] Constructing `ThesisRecord(..., time_expectation_hours=0)` raises `ValidationError` (Pydantic `gt=0` constraint).
- [ ] Constructing `ThesisRecord(..., time_expectation_hours=-5.0)` raises `ValidationError`.
- [ ] Constructing `ThesisRecord(..., time_expectation_hours="48")` raises `ValidationError` (Pydantic strict-int-from-str rejected at the float boundary; if Pydantic auto-coerces, that's acceptable too — document either behavior).
- [ ] `_check_time_expectation_consistency` validator raises `ValueError` whose message names the actual delta when `expected_resolution_at != generation_timestamp + timedelta(hours=time_expectation_hours)` beyond the 60-second tolerance.
- [ ] `_check_time_expectation_consistency` validator passes when the three fields are consistent within tolerance (test with `time_expectation_hours=48.0`, `generation_timestamp=2026-05-01T12:00:00Z`, `expected_resolution_at=2026-05-03T12:00:00Z`).
- [ ] Verify scripts under `src/alphamind/scripts/verify_*.py` that construct `ThesisRecord` are updated to pass `float`; running `uv run python scripts/verify_<name>.py` produces the same verdict as before for any verifier that touches thesis fixtures.
- [ ] All existing `uv run pytest -n auto` tests pass (no pre-existing test fails after the type change is fully applied).
- [ ] `tests/portfolio_state/records/test_theses.py` exercises: (a) valid float construction, (b) zero-or-negative rejection, (c) consistency validator pass case, (d) consistency validator fail case naming the delta in the error message, (e) edge case where `time_expectation_hours` is a non-integer fractional value (e.g., 24.5).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/portfolio_state/records/test_theses.py -n auto -v` — confirm all new test cases pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.theses import ThesisRecord; import inspect; print(ThesisRecord.model_fields['time_expectation_hours'])"` should show `FieldInfo(annotation=float, required=True, metadata=[Gt(gt=0)])` or similar.
* Run any `scripts/verify_*.py` that previously constructed thesis fixtures with string `time_expectation_hours`; confirm they pass after fixture updates.
* Lint clean per CLAUDE.md.