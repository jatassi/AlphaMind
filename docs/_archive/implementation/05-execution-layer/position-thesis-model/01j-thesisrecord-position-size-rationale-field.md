# 01j — `ThesisRecord` `position_size_rationale` field

## Goal

Add an additive `position_size_rationale: str | None = None` field to `ThesisRecord`. The design (`docs/design/05-execution-layer/state-persistence.md:53`) explicitly names `position_size_rationale` as a thesis-level field that the persistence layer captures: *"Other thesis and component fields (summary, component type, linked bracket leg, instrument reference, narrative, key assumptions, generation timestamp, time expectation, position size rationale) are as defined in the thesis model."* Today the field is missing from `ThesisRecord` so the analyst's sizing reasoning is dropped at persistence. The field is the concise prose explanation of *why this size at this conviction* — read by the PM's sizing-proportionality criterion (`docs/design/04-decision-layer/portfolio-manager.md` § Sizing proportionality) when evaluating whether the analyst's size matches the conviction band.

## Reading

* `src/alphamind/portfolio_state/records/theses.py:90-110` — current `ThesisRecord` fields.
* `docs/design/05-execution-layer/state-persistence.md:53` — explicit naming of `position_size_rationale` as a thesis-level field.
* `docs/design/05-execution-layer/thesis-model.md` § Thesis structure — the broader thesis-fields context this slots into.
* `docs/design/04-decision-layer/portfolio-manager.md` § Sizing proportionality — the consumer that will read this field.
* `docs/design/04-decision-layer/analyst-output-schema.md` — analyst-side `position_size_rationale` field on the proposal (likely already defined; the persistence record needs to capture it post-OPEN).
* `tests/portfolio_state/records/test_theses.py` — existing test patterns; extend for the new field.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B) — additive-field rule applies.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it adds one additive field to `ThesisRecord`.

## Scope

Source under `src/alphamind/portfolio_state/records/theses.py` (one field add). Tests at `tests/portfolio_state/records/test_theses.py` (extend existing thesis-record tests).

### 1\. Add the `position_size_rationale` field

In `src/alphamind/portfolio_state/records/theses.py` `ThesisRecord`:

```python
class ThesisRecord(BaseModel):
    ...
    position_size_rationale: str | None = None
    ...
```

Place adjacent to `summary` and `key_catalyst` (the other natural-language thesis-level fields). The default `None` preserves backward compatibility with all existing fixture builders.

### 2\. Validator: non-empty when populated

```python
@field_validator("position_size_rationale")
@classmethod
def _require_non_empty_when_populated(cls, v: str | None) -> str | None:
    if v is not None and not v.strip():
        msg = "position_size_rationale must be non-empty when not None"
        raise ValueError(msg)
    return v
```

A None value is acceptable (legacy records and tests that don't exercise sizing reasoning). An empty or whitespace-only string is rejected — that's a producer bug, not legitimate state.

### 3\. Docstring update

Add a one-line comment or docstring-section describing the field's purpose:

> The analyst's prose rationale for *why this size at this conviction*. Read by the PM's sizing-proportionality evaluation criterion. Persisted from the analyst's proposal at OPEN time; not modified by ADJUST or ADD (those have their own per-action rationale fields).

### Out of scope

* Wiring the analyst's `position_size_rationale` into the OMS Phase 2 OPEN handler — that's the OMS commands work tree (<issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue>).
* Adding `position_size_rationale` to ThesisComponent (the field is thesis-level, not component-level — sizing is a position-level decision spanning all components).
* Validating the rationale's substance — the PM evaluates that, the record only persists.

## Acceptance criteria

- [ ] `ThesisRecord.position_size_rationale: str | None = None` field exists in `alphamind.portfolio_state.records.theses`.
- [ ] All existing `tests/portfolio_state/records/test_theses.py` tests pass after the field add (additive default = None preserves compat).
- [ ] All existing `uv run pytest -n auto` tests pass — no pre-existing test fails.
- [ ] Constructing `ThesisRecord(...)` without specifying `position_size_rationale` succeeds with the field defaulting to None.
- [ ] Constructing `ThesisRecord(..., position_size_rationale="Conviction 4 with tight invalidation supports top-of-band size.")` succeeds.
- [ ] Constructing `ThesisRecord(..., position_size_rationale="")` raises `ValidationError` (whitespace-only rejected).
- [ ] Constructing `ThesisRecord(..., position_size_rationale="   ")` raises `ValidationError` (whitespace-only rejected).
- [ ] `ThesisRecord` class docstring or field-adjacent comment documents the field's purpose per §3.
- [ ] `tests/portfolio_state/records/test_theses.py` exercises: (a) None default, (b) non-empty string accepted, (c) empty string rejected, (d) whitespace-only string rejected.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/portfolio_state/records/test_theses.py -n auto -v` — confirm new tests pass.
* Spot-check by `python -c "from alphamind.portfolio_state.records.theses import ThesisRecord; print('position_size_rationale' in ThesisRecord.model_fields)"` should print `True`.
* Lint clean per CLAUDE.md.