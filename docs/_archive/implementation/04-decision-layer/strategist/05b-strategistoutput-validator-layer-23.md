# 05b — StrategistOutput validator (Layer-2/3)

## Goal

Implement the cross-field-invariant validator that runs against a parsed `StrategistOutput`. Layer 2 enforces structural invariants Pydantic can't see across siblings (e.g., `pending_order_assessment.linked_position_assessment_id` matches an `assessment_id` in the same document); Layer 3 enforces referential integrity (every `[XX-N]` reference in narrative fields resolves in the per-invocation `RetrievalStore`); plus active-sectors filtering. Per parent Issue decision (C), the validator does NOT implement heuristic anti-pattern checks (those are PM-side judgment calls).

## Reading

* `docs/design/04-decision-layer/strategist-output-schema.md` § Notes on cross-field invariants — the cross-document invariants the schema's allOf/anyOf cannot express.
* `src/alphamind/decision/analyst/validation.py` — sibling validator (520 lines). Mirror its overall structure: `ValidationResult` record, `ValidationFailure` enum, per-rule check functions, soft-warning support if applicable.
* `src/alphamind/decision/strategist/models.py` — `StrategistOutput`, `PositionAssessment`, `PendingOrderAssessment`, `PortfolioLevelObservations`, `RegimeTransitionSummary`, `DefensivePostureSummary`, `ActionParameters` discriminated union (story 03).
* `src/alphamind/analysis/synthesizer/retrieval.py` — `RetrievalStore.has_reference(ref_id) -> bool` and the canonical reference-ID format (`[SA-TECH-N]`, `[QR-N]`, etc.).
* `src/alphamind/decision/analyst/__init__.py` — `extract_reference_ids(text) -> tuple[str, ...]` if the analyst exports a regex helper; reuse rather than duplicate.
* `tests/decision/analyst/test_validation.py` — sibling test pattern.
* Parent Issue <issue id="213b1ce9-8210-4b63-8d1b-7957cc9466f2">ALP-116</issue> § Pre-resolved decisions (C) — validator scope (Layer 2 + Layer 3, no heuristic checks).
* Parent Issue <issue id="213b1ce9-8210-4b63-8d1b-7957cc9466f2">ALP-116</issue> § Pre-resolved decisions (E) — `active_sectors: frozenset[str]` parameter.

## Depends on

* <issue id="1fa97cf8-e152-4367-8b87-7cf4e124690a">ALP-303</issue> (Story 03 — StrategistOutput typed model). Validator imports model types.

## Scope

In scope: `src/alphamind/decision/strategist/validation.py`. Tests at `tests/decision/strategist/test_validation.py`.

### 1\. Public entry point

```python
def validate_strategist_output(
    output: StrategistOutput,
    *,
    retrieval_store: RetrievalStore,
    active_sectors: frozenset[str],
) -> ValidationResult:
    """Run Layer-2 (cross-field invariant) and Layer-3 (referential) checks.

    Returns a ValidationResult with overall PASS/FAIL plus a list of failures
    (each with a field-path locator and a human-readable message). PASS means
    no FAIL-class issues; WARN-class issues do not gate. The harness (story
    06) treats FAIL as a corrective-retry trigger; WARN is logged.
    """
```

`ValidationResult` mirrors the analyst's shape: `overall: Literal["PASS", "FAIL"]`, `failures: tuple[ValidationFailure, ...]`, `warnings: tuple[ValidationWarning, ...]`. Both failure and warning records carry `field_path`, `message`, and a `kind` enum tag.

### 2\. Layer-2 checks (cross-field invariants)

Implement each as a private check function returning `tuple[ValidationFailure, ...]`.

* **Action↔parameters action match.** For each `position_assessment`, if `recommended_action != "hold"`, assert `action_parameters.action == recommended_action`. (Pydantic's discriminator catches this on parse, but defensive double-check is cheap.)
* `assessment_id` uniqueness. Within `position_assessments[]`, `assessment_id` values are unique.
* `pending_order_assessment_id` uniqueness. Within `pending_order_assessments[]`, IDs are unique.
* `linked_position_assessment_id` referential. For each `pending_order_assessment` with `linked_position_assessment_id` populated, the value must equal an `assessment_id` in the same document's `position_assessments[]`.
* `remedy_flag` uniqueness across breach IDs. Each unique `breach_id` in `regime_transition_summary.addressed_breaches[].breach_id` should be referenced by at least one `position_assessment.remedy_flag`. Conversely, every `remedy_flag` value should appear in `addressed_breaches[].breach_id`. Mismatches → FAIL.
* `uncured_breaches[].breach_id` not in `addressed_breaches[].breach_id`. A breach can be one or the other, never both.
* `active_sectors` filter. Each `position_assessment.sector` must be in `active_sectors`. If outside, FAIL.
* `defensive_posture_summary` presence. When `mode == "defensive_posture"`, `portfolio_level_observations.defensive_posture_summary` must not be None. (Pydantic enforces this on parse via the StrategistOutput model_validator; defensive double-check.)
* **Conviction/sector taxonomy.** N/A — strategist output has no conviction_level field.

### 3\. Layer-3 checks (referential)

* **Reference-ID resolution.** For each narrative field on every assessment (`status_rationale`, `action_rationale`, `reduce_rationale`, `add_conviction_justification`, `adjustment_rationale`, `remedy_rationale`, `cross_position_observations`) and each pending-order narrative field (`drift_rationale`, `action_rationale`) and each portfolio-level field (`aggregate_thesis_health`, `sector_balance_shifts`, `thesis_dependency_warnings`, `capital_allocation_observations`, `defensive_posture_summary.capital_preservation_notes`), extract every `[SA-TECH-N]` / `[SA-FIN-N]` / `[SA-ENERGY-N]` / `[QR-N]` / `[AR-N]` / `[CR-N]` reference and assert it resolves in `retrieval_store`. Unresolvable → FAIL with the failing reference ID and the field path.

Reuse the analyst-side reference-extraction regex (or a shared helper if one exists at `src/alphamind/decision/_shared.py` or similar). If no shared helper exists, define one privately in this module — the analyst story 05b validator already implements this; choose the path of least redundancy at implementation time.

### 4\. Out-of-scope checks (per parent decision (C))

* No heuristic anti-pattern check (`sunk_cost_persistence`, `rationalized_continuation`, `generic_rationale`, `engine_originated_closure_signal`).
* No `prior_status` referential check against the input view's `ThesisRecord.prior_status`.
* No verification that `guardrail_validation_result` mirrors the actual tool return — trust contract per parent body.
* No band-warning analog of analyst's pct_of_portfolio band-warn — strategist has no conviction-band sizing field.

### Out of scope

* Parser logic — story 05a.
* Harness retry construction — story 06.
* Active_sectors plumbing from the runner — story 07.

## Acceptance criteria

- [ ] `src/alphamind/decision/strategist/validation.py` exports `validate_strategist_output(output, *, retrieval_store, active_sectors) -> ValidationResult` and `ValidationResult`, `ValidationFailure`, `ValidationWarning` types.
- [ ] Each Layer-2 check (action↔parameters, ID uniqueness, linked_position_assessment_id, remedy_flag↔breach_id, active_sectors, defensive_posture_summary presence) is implemented as a private check function and tested independently.
- [ ] Layer-3 reference resolution checks every narrative field on `position_assessments`, `pending_order_assessments`, and `portfolio_level_observations`.
- [ ] `validate_strategist_output(<schema-valid output>, retrieval_store=<store-with-all-refs>, active_sectors=<all-sectors>) -> ValidationResult(overall="PASS", failures=(), warnings=())`.
- [ ] Synthetic FAIL fixtures: a missing `linked_position_assessment_id` target; a `remedy_flag` not in `addressed_breaches`; an unresolvable `[QR-99]` reference; a `sector` outside `active_sectors`. Each produces FAIL with a clear field path.
- [ ] `tests/decision/strategist/test_validation.py` covers each check + happy path + each synthetic FAIL.
- [ ] `uv run pytest tests/decision/strategist/test_validation.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

```bash
uv run pytest tests/decision/strategist/test_validation.py -n auto
```

Inspect the validator's check coverage matrix: each schema-named cross-field invariant has a corresponding test; each narrative field is included in the reference-resolution loop.
