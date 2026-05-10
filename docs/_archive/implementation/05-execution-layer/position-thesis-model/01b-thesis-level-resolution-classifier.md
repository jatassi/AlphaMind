# 01b — Thesis-level resolution classifier

## Goal

Ship `classify_thesis_resolution(component_outcomes, realized_pnl_usd, exit_method) → ThesisResolutionCategory` as a free function in a new module `src/alphamind/execution/thesis_model/resolution.py`. The classifier maps the design's four RESOLVED-thesis categories — `VALIDATED`, `PROFITABLE_BUT_WRONG`, `INVALIDATED_STOPPED_CORRECTLY`, `INVALIDATED_WRONG_ON_EXIT` — from a tuple of per-component outcomes (each `VALIDATED` / `WRONG` / `INCONCLUSIVE`), the position's realized P/L (signed dollars), and the position's exit method. Natural caller is State persistence (<issue id="beaf98a0-a9fc44a8-ac46-32e50f604345">ALP-119</issue>) Phase 1 thesis update step when transitioning a thesis from active to resolved; this story ships the function standalone with unit-level fixtures.

## Reading

* `docs/design/05-execution-layer/thesis-model.md` § Resolution: component-level then thesis-level — defines the four resolution categories with one-paragraph descriptions each. The function's mapping rules derive from these descriptions.
* `docs/design/05-execution-layer/state-persistence.md` § Phase 1 write path — confirms thesis resolution is a Phase 1 OMS responsibility ("Update the thesis. Position closure: set thesis resolution timestamp and status to resolved. (Component-level resolution outcomes are set separately by the analysis pipeline.)"). Establishes that the classifier consumes outcomes already set on components, not raw P/L alone.
* `src/alphamind/portfolio_state/records/theses.py` — existing `ThesisComponentOutcome` enum (`VALIDATED`, `WRONG`, `INCONCLUSIVE`), existing `ThesisResolutionCategory` enum (5 values incl. `CANCELLED_NEVER_ENTERED` which this classifier does NOT produce).
* `src/alphamind/portfolio_state/records/activity_log.py` — existing `PositionExitMethod` enum (the `exit_method` parameter type). Look at the enum's exact values to ground the "stopped correctly" vs "wrong on exit" distinction in pre-resolved decision (D).
* <issue id="31a1a496-91d6-48c5-811f-4fa19ea1f787">ALP-122</issue> parent body § Pre-resolved decision (D) — function signature and the `exit_method`-based distinction between `INVALIDATED_STOPPED_CORRECTLY` and `INVALIDATED_WRONG_ON_EXIT`.

## Depends on

None. This story has no in-tree predecessors and no cross-feature gates — it ships a new pure-function module that consumes existing typed records.

## Scope

Source under `src/alphamind/execution/thesis_model/{__init__.py, resolution.py}` (new module; `__init__.py` already touched by story 01a — re-exports must merge cleanly via additive editing). Tests at `tests/execution/thesis_model/test_resolution.py`.

### 1\. Implement the classifier

Create `src/alphamind/execution/thesis_model/resolution.py` exporting:

```python
from alphamind.portfolio_state.records.activity_log import PositionExitMethod
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisResolutionCategory,
)


def classify_thesis_resolution(
    component_outcomes: tuple[ThesisComponentOutcome, ...],
    realized_pnl_usd: float,
    exit_method: PositionExitMethod,
) -> ThesisResolutionCategory:
    """Classify a RESOLVED thesis into one of four resolution categories.

    Mapping rules per docs/design/05-execution-layer/thesis-model.md:

    * VALIDATED — most components VALIDATED AND realized_pnl_usd > 0.
      Thesis played out, catalyst fired, position hit the target. The ideal
      outcome — reasoning was sound.

    * PROFITABLE_BUT_WRONG — realized_pnl_usd > 0 but most components are
      WRONG or INCONCLUSIVE. Lucky outcome that shouldn't reinforce the
      reasoning patterns that produced it.

    * INVALIDATED_STOPPED_CORRECTLY — realized_pnl_usd <= 0 AND exit_method
      indicates a mechanically-triggered exit (stop-triggered, time-expired,
      target-reached, margin-liquidation, forced-buy-in, corporate_action_*).
      Negative P/L outcome but positive process outcome — the system correctly
      identified when it was wrong.

    * INVALIDATED_WRONG_ON_EXIT — realized_pnl_usd <= 0 AND exit_method
      indicates a PM-judgment exit (pm-decision). Process failure — system
      held too long against an already-wrong thesis until the PM manually
      closed.

    The "most components" predicate counts VALIDATED vs WRONG; INCONCLUSIVE
    components do not weigh either side. A tie (equal validated and wrong
    counts) classifies as PROFITABLE_BUT_WRONG when P/L > 0 (the validated
    case requires affirmative weight) and INVALIDATED_STOPPED_CORRECTLY or
    INVALIDATED_WRONG_ON_EXIT (per exit_method) when P/L <= 0.

    Raises ValueError if component_outcomes is empty (a resolved thesis must
    have ≥1 resolved component per ThesisRecord._check_resolved_fields).

    Does NOT produce CANCELLED_NEVER_ENTERED — that category is set by the
    persistence layer on CANCELLED-status theses (entry order cancelled before
    fill), not by this classifier which handles RESOLVED-status theses only.
    """
```

### 2\. Tie-breaking and edge-case behavior (testable specifics)

Document and implement these rules in the function body, mirroring the docstring's prose precisely. Each becomes an acceptance criterion below.

* **Validated vs wrong tally:** `validated_count = sum(o == VALIDATED for o in component_outcomes)`; `wrong_count = sum(o == WRONG for o in component_outcomes)`. INCONCLUSIVE outcomes contribute to neither.
* **"Most components VALIDATED":** `validated_count > wrong_count`.
* **"Most components WRONG or INCONCLUSIVE":** `not (validated_count > wrong_count)` — i.e., the negation, which covers the tied case and the wrong-dominant case.
* **Sign-of-P/L threshold:** `realized_pnl_usd > 0` is positive; `realized_pnl_usd <= 0` is non-positive (zero counts as a loss for classification purposes).
* **Exit-method partition:** mechanical exits = {`stop-triggered`, `time-expired`, `target-reached`, `margin-liquidation`, `forced-buy-in`, `corporate_action_cash_merger`}; PM-judgment exits = {`pm-decision`}. (Confirm exact `PositionExitMethod` enum values during implementation against `src/alphamind/portfolio_state/records/activity_log.py:108`; the surfacing condition in the parent body covers any unanticipated value.)

### 3\. Module exports

`src/alphamind/execution/thesis_model/__init__.py` exports `classify_thesis_resolution` alongside whatever 01a exports. The export list grows additively across stories; do not delete 01a's export.

### Out of scope

* Computing `realized_pnl_usd` from position fill records — the caller (State persistence Phase 1 per coordination note in parent body) supplies the signed P/L.
* Setting per-component `resolution_outcome` values — that is the analysis pipeline's responsibility per the design's "Component-level resolution is performed by the analysis pipeline (LLM-evaluated for qualitative components, programmatic for falsifiable quantitative assumptions)."
* Producing `CANCELLED_NEVER_ENTERED` — that category is set by the persistence layer on CANCELLED-status theses (entry cancelled before fill).
* Wiring the classifier into any production code path — natural callers don't exist yet; this story ships the function standalone.

## Acceptance criteria

- [ ] `classify_thesis_resolution` is importable from `alphamind.execution.thesis_model`.
- [ ] Function signature matches `(tuple[ThesisComponentOutcome, ...], float, PositionExitMethod) → ThesisResolutionCategory`.
- [ ] All-VALIDATED components + positive P/L + any exit_method → returns `VALIDATED`.
- [ ] All-WRONG components + positive P/L + any exit_method → returns `PROFITABLE_BUT_WRONG`.
- [ ] All-WRONG components + zero P/L + `stop-triggered` exit_method → returns `INVALIDATED_STOPPED_CORRECTLY`.
- [ ] All-WRONG components + negative P/L + `time-expired` exit_method → returns `INVALIDATED_STOPPED_CORRECTLY`.
- [ ] All-WRONG components + negative P/L + `pm-decision` exit_method → returns `INVALIDATED_WRONG_ON_EXIT`.
- [ ] Mixed `(VALIDATED, WRONG, INCONCLUSIVE)` → 1 each + positive P/L → returns `PROFITABLE_BUT_WRONG` (tie between validated_count and wrong_count counts as not-most-validated).
- [ ] All-INCONCLUSIVE components + positive P/L → returns `PROFITABLE_BUT_WRONG`.
- [ ] All-INCONCLUSIVE components + negative P/L + `stop-triggered` → returns `INVALIDATED_STOPPED_CORRECTLY`.
- [ ] Empty `component_outcomes` tuple → raises `ValueError` with a message naming the empty-input condition.
- [ ] The function never returns `CANCELLED_NEVER_ENTERED`.
- [ ] `tests/execution/thesis_model/test_resolution.py` exists and exercises every row of the table above plus a parametrized sweep covering each `PositionExitMethod` enum value crossed with each `ThesisComponentOutcome` distribution shape.
- [ ] All new tests pass under `uv run pytest tests/execution/thesis_model/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` passes clean.

## Verification

* Run `uv run pytest -n auto` (full suite) — every existing test passes plus all new tests.
* Run `uv run pytest tests/execution/thesis_model/test_resolution.py -n auto -v` — confirm all rows of the truth table above pass.
* Spot-check by `python -c "from alphamind.execution.thesis_model import classify_thesis_resolution; print(classify_thesis_resolution.__doc__)"`.
* Lint clean per CLAUDE.md.