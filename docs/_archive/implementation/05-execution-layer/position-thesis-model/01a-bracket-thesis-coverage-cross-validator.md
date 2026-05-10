# 01a — Bracket-thesis coverage cross-validator

## Goal

Add `linked_bracket_leg_id: str | None` to `ThesisComponent` and ship `validate_bracket_thesis_coverage(bracket: BracketRecord, thesis: ThesisRecord) → None` as a free function in a new module `src/alphamind/execution/thesis_model/coverage.py`. The validator enforces the design's bidirectional-traceability rule: every `protective_leg` in the bracket has at least one thesis component whose `linked_bracket_leg_id` references its `leg_id`, with component_type compatible with the leg type. Callers (eventually OMS commands' OPEN/ADJUST handlers per <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue>) invoke the validator explicitly when accepting a new bracket+thesis pair from the PM. The new field is additive and defaults to `None` so the 279 existing import sites continue to construct valid `ThesisComponent` records without edits.

## Reading

* `docs/design/05-execution-layer/thesis-model.md` § Mandatory coverage — the rule the validator enforces ("every bracket leg has a corresponding thesis component"; the thesis model's component-level linkage `linked_bracket_leg` references the bracket's leg identifiers).
* `docs/design/05-execution-layer/orders-and-brackets.md` § Mandatory thesis-linked brackets — bracket structure, leg types (TAKE_PROFIT, PRICE_STOP, TIME_EXPIRATION, EVENT_INVALIDATION), hard-backstop requirement.
* `src/alphamind/portfolio_state/records/theses.py` — existing `ThesisComponent`, `ThesisRecord`, `ThesisComponentType` enum; existing `_check_mandatory_coverage` (line 144) which checks the three component types exist within a thesis (within-record invariant — keep as is, not redundant with the new cross-validator).
* `src/alphamind/portfolio_state/records/orders.py` — existing `BracketRecord`, `BracketLeg`, `BracketLegType` enum, `_check_hard_backstop` (line 351) for the within-bracket invariant the new cross-validator complements.
* `tests/portfolio_state/records/test_theses.py` — test patterns for ThesisRecord/ThesisComponent fixture construction the new test module should mirror.
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (B), (C) — additive-field rule and free-function-not-validator rule that govern this story's shape.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it edits the existing typed-records surface and ships a new pure-function module.

## Scope

Source under `src/alphamind/portfolio_state/records/theses.py` (one field add) and `src/alphamind/execution/thesis_model/{__init__.py, coverage.py}` (new module). Tests at `tests/execution/thesis_model/test_coverage.py`.

### 1\. Add `linked_bracket_leg_id` to `ThesisComponent`

In `src/alphamind/portfolio_state/records/theses.py`, add to the `ThesisComponent` model:

```python
linked_bracket_leg_id: str | None = None
```

Place adjacent to the existing `linked_bracket_leg_type: BracketLegType | None` field. The default `None` preserves backward compatibility with all existing fixture builders. Frozen-model semantics (`ConfigDict(frozen=True)`) and field ordering are unchanged. No `model_validator` rule added on this field — its absence is legitimate (legacy records and tests that don't exercise the cross-validator).

### 2\. Implement the cross-validator

Create `src/alphamind/execution/thesis_model/coverage.py` exporting:

```python
from alphamind.portfolio_state.records.orders import BracketRecord
from alphamind.portfolio_state.records.theses import ThesisRecord


def validate_bracket_thesis_coverage(
    bracket: BracketRecord,
    thesis: ThesisRecord,
) -> None:
    """Raise ValueError if any bracket protective leg lacks a coverage-compatible thesis component.

    Coverage rule (per docs/design/05-execution-layer/thesis-model.md § Mandatory coverage):
    every protective leg's leg_id must be referenced by ≥1 thesis component whose
    linked_bracket_leg_id matches it AND whose component_type is compatible with the
    leg's leg_type:

    * TAKE_PROFIT leg requires a component with component_type=TARGET_RATIONALE
    * PRICE_STOP / TIME_EXPIRATION / EVENT_INVALIDATION leg requires a component
      with component_type=INVALIDATION_RATIONALE

    Additionally, the thesis must contain ≥1 ENTRY_RATIONALE component (this mirrors
    the design's "one entry rationale per order leg" rule; the new cross-validator
    enforces presence here, while ThesisRecord._check_mandatory_coverage continues
    enforcing the three-types-exist invariant within the thesis itself).

    Components whose linked_bracket_leg_id is None are ignored by this validator —
    they may be legacy records or components linked to non-bracket entities. The
    validator does not require every component to point at a leg; only that every
    leg is pointed at by ≥1 compatible component.
    """
```

The error message must name the offending leg_id, the leg_type, and the type-of-component the validator was looking for. When multiple legs are uncovered, report all in one error (collect missing-coverage entries and raise once at the end with an aggregated message).

### 3\. Module exports

`src/alphamind/execution/thesis_model/__init__.py` exports `validate_bracket_thesis_coverage`.

### Out of scope

* Wiring the validator into a Pydantic `model_validator` on `BracketRecord` or `ThesisRecord` — per pre-resolved decision (C), the validator is opt-in.
* Migrating any of the 279 existing import sites to populate the new field — they remain free to leave it `None`.
* Calling the validator from any production code path — the natural callers (OMS commands <issue id="01ef5ee7-054d-41dc-bfca-be85dfce225c">ALP-120</issue> OPEN/ADJUST handlers) don't exist yet; this story ships the validator standalone.
* Modifying `ThesisRecord._check_mandatory_coverage` — that within-record invariant continues to enforce the three-component-types presence rule independently.
* Validating that `bracket.position_id == thesis.position_id` — a separate concern owned by the OPEN handler; this validator focuses on leg-coverage only.

## Acceptance criteria

- [ ] `ThesisComponent.linked_bracket_leg_id: str | None = None` field exists and is importable from `alphamind.portfolio_state.records.theses`.
- [ ] All existing `tests/portfolio_state/records/test_theses.py` tests pass unchanged after the field add (additive verification).
- [ ] All existing `uv run pytest -n auto` tests (full suite) pass unchanged after the field add — no pre-existing test fails.
- [ ] `validate_bracket_thesis_coverage` is importable from `alphamind.execution.thesis_model`.
- [ ] Calling `validate_bracket_thesis_coverage(bracket, thesis)` returns `None` (no raise) when every `bracket.protective_legs` leg has ≥1 thesis component whose `linked_bracket_leg_id` matches its `leg_id` AND whose `component_type` is compatible per the leg-type↔component-type table in the docstring.
- [ ] Calling `validate_bracket_thesis_coverage` raises `ValueError` whose message names *every* uncovered leg_id, the leg_type, and the missing component_type when one or more legs lack coverage.
- [ ] Calling `validate_bracket_thesis_coverage` raises `ValueError` when no thesis component has `component_type=ENTRY_RATIONALE`.
- [ ] Components with `linked_bracket_leg_id=None` are silently ignored (the validator does not require every component to reference a leg; only that every leg is referenced).
- [ ] When the bracket has 4 legs and one of them is uncovered, the raised ValueError message contains the offending leg_id and leg_type strings exactly once.
- [ ] When the bracket has 4 legs and three of them are uncovered, the raised ValueError message contains all three offending leg_ids, in stable (insertion) order.
- [ ] `tests/execution/thesis_model/test_coverage.py` exists and exercises: (a) coverage pass, (b) coverage fail with one uncovered leg, (c) coverage fail with multiple uncovered legs, (d) component-type mismatch (TAKE_PROFIT leg covered only by an INVALIDATION_RATIONALE component → fail), (e) missing ENTRY_RATIONALE → fail, (f) components with `linked_bracket_leg_id=None` ignored, (g) bracket with TIME_EXPIRATION + EVENT_INVALIDATION legs coverage paths.
- [ ] All new tests pass under `uv run pytest tests/execution/thesis_model/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/execution/thesis_model/test_coverage.py -n auto -v` — confirm the seven test cases above all pass.
* Spot-check the new module by `python -c "from alphamind.execution.thesis_model import validate_bracket_thesis_coverage; print(validate_bracket_thesis_coverage.__doc__)"`.
* Spot-check the additive field by `python -c "from alphamind.portfolio_state.records.theses import ThesisComponent; print('linked_bracket_leg_id' in ThesisComponent.model_fields)"` should print `True`.
* Lint clean per CLAUDE.md.