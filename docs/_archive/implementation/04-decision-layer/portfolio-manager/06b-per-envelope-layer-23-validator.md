# 06b — Per-envelope Layer-2/3 validator

## Goal

Author the per-envelope Layer-2/3 cross-field validator. Per parent decision (D), this validator runs **inside** the `submit_envelope` MCP wrapper at submission time (called by story 06c), not in a post-hoc parser pass. The validator takes a single `PMEnvelope` plus per-invocation context (retrieval store, pre-processor bundle, PM view, active sectors, halt-mode flag) and returns a `ValidationResult` mirror analyst's `ValidationResult` with errors and warnings. Mirrors strategist story 05b ([ALP-307](<https://linear.app/alphamind-jatassi/issue/ALP-307>)) extended with the PM's specific cross-record checks and embedded-OMS-command checks.

## Reading

* `src/alphamind/decision/portfolio_manager/models.py` (story 03 / [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>)) — `PMEnvelope`, `PMAnalystEnvelope`, `PMStrategistEnvelope`, all evaluation/modification/concern records.
* `src/alphamind/decision/portfolio_manager/oms_command_models.py` (story 03 / [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>)) — embedded OMS command models the validator inspects.
* `docs/design/04-decision-layer/portfolio-manager.md` § Verdict aggregation, § Anti-patterns, § Existing position management — prose definitions of the invariants the validator enforces.
* `docs/design/04-decision-layer/pm-envelope-schema.md` § Notes on cross-field invariants — the cross-field checks the validator enforces beyond what Pydantic sees.
* `docs/design/oms-command-ids.md` — `envelope_id` integer ↔ `source_recommendation_id` integer bijection rule.
* `docs/design/testing/llm-output-validation.md` — Layer 1–4 validation contract (PM implements Layer 2 and 3).
* `src/alphamind/decision/strategist/validation.py` — sibling pattern (closest analogue); 540+ lines covering parallel checks. Mirror its structure: top-level `validate_strategist_output` function, per-check helper functions, `ValidationResult` / `ValidationError` / `ValidationWarning` types.
* `src/alphamind/decision/analyst/validation.py` — sibling pattern with reference-resolution check via `parse_reference_id` + `RetrievalStore`. Mirror that pattern for `[XX-N]` reference checks.
* `src/alphamind/decision/proposal_pre_processor/__init__.py` — `ProposalPreProcessorBundle` shape; the validator reads `aggregate_observations.combined_set_impact.basis.analyst_proposal_ids` etc. for cross-reference checks.
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decision (O) names every Layer-2/3 invariant.

## Depends on

* [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>) (this work tree, story 03) — provides the typed envelope and OMS command models the validator inspects.

## Scope

Code at `src/alphamind/decision/portfolio_manager/validation.py`. Tests at `tests/decision/portfolio_manager/test_validation.py`.

### 1\. Public types

Mirror analyst's exception/result shape:

* `ValidationError(field_path: str, message: str, criterion: str | None = None)` — one fail-stop fact.
* `ValidationWarning(field_path: str, message: str, criterion: str | None = None)` — non-blocking.
* `ValidationResult(envelope_id: str, errors: tuple[ValidationError, ...], warnings: tuple[ValidationWarning, ...]) -> bool` with `is_valid` property = `len(errors) == 0`.

### 2\. `validate_pm_envelope(...)`

Author the function with signature:

```python
def validate_pm_envelope(
    envelope: PMEnvelope,
    *,
    retrieval_store: RetrievalStore,
    pre_processor_bundle: ProposalPreProcessorBundle,
    pm_view: PortfolioManagerView,
    active_sectors: frozenset[str],
    halt_mode: bool,
) -> ValidationResult: ...
```

Run every check in the order below; accumulate errors and warnings into the `ValidationResult`. The validator returns a single result with all findings — the caller (story 06c's MCP wrapper) reads `is_valid` and surfaces the first-error message in the rejection_payload's `rules_breached[0].rule = "schema_invariant"`.

### 3\. Layer-2 invariants (the validator runs these checks)

**(a)** `envelope_id` ↔ `source_provenance` ↔ `recommendation_type` consistency. Pydantic catches structural mismatches via the discriminated union; this check additionally enforces:

* `pm_analyst` envelope's `envelope_id` integer matches `source_recommendation_id` integer (e.g., `ENV-REC-3` + `REC-3`).
* `pm_strategist` + `position_assessment` envelope's `envelope_id` integer matches `source_recommendation_id` integer (e.g., `ENV-SA-7` + `SA-7`).
* `pm_strategist` + `pending_order_assessment` envelope's `envelope_id` integer matches `source_recommendation_id` integer (e.g., `ENV-SA-ORD-2` + `SA-ORD-2`).

**(b) Verdict-conditional invariants.** Pydantic enforces these intrinsically (story 03's model validators) — the validator double-checks via the same logic for defense-in-depth and surfacing finer error messages.

**(c)** `evaluation` criterion-set ↔ `source_provenance`. Pydantic catches via the typed evaluation field; validator restates as defense-in-depth.

**(d)** `modifications[].adjustment_category` ↔ `phase` ↔ `triggering_rule`. Already enforced by Pydantic on the `ModificationRecord` model; validator restates.

**(e) Embedded** `close` commands' `risk_management_subtype: pm_directed`. Iterate `envelope.commands`; for each `CloseCommand` with `close_rationale_type == "risk_management"`, assert `risk_management_subtype == "pm_directed"`. Pydantic enforces the field's required-when-conditional via the model validator on `CloseCommand`; this check confirms the *value* is `pm_directed` (not `engine_guardrail`, which is engine-originated provenance).

**(f)** `anti_patterns_identified[]` strings ∈ canonical enum. Pydantic enforces via the `Literal` alias; validator restates.

**(g) Halt mode → no embedded OPEN or ADD commands.** When `halt_mode is True`, iterate `envelope.commands`; assert none has `command_type == "open"` or `command_type == "add"`. Mirror parent decision (M)'s halt-mode-as-parameter stance.

**(h) Embedded OPEN/ADD commands'** `instrument` sector ∈ `active_sectors`. For each `OpenCommand` and `AddCommand`, read `command.position_size.sector`; assert `sector in active_sectors`. The `Sector` enum import comes from `alphamind.decision.analyst` (risk-side 4-way per parent decision (L)).

### 4\. Layer-3 referential checks

**(i)** `[XX-N]` references in `rationale_narrative` and `modifications[].rationale` resolve in `retrieval_store`. Use `parse_reference_id` from `alphamind.analysis.synthesizer.models` (re-used by analyst and strategist validators) to extract every reference and check existence in `retrieval_store`. Reference vocabulary is closed: `SA-TECH-N`, `SA-FIN-N`, `SA-ENERGY-N`, `QR-N`, `AR-N`, `CR-N`. Unrecognized prefix is a Layer-2 invariant violation (parser is strict).

**(j)** `source_recommendation_id` ∈ pre-processor bundle.

* For `pm_analyst` envelopes: `source_recommendation_id` (e.g., `REC-5`) matches a `recommendation_id` in `pre_processor_bundle.analyst_section.recommendations[].recommendation` (when mode is normal). In watchlist mode, this should never happen (analyst proposals are absent); flag as Layer-2 invariant violation.
* For `pm_strategist` + `position_assessment`: `source_recommendation_id` (e.g., `SA-3`) matches an `assessment_id` in `pre_processor_bundle.strategist_section.position_assessments[].assessment`.
* For `pm_strategist` + `pending_order_assessment`: `source_recommendation_id` (e.g., `SA-ORD-2`) matches a `pending_order_assessment_id` in `pre_processor_bundle.strategist_section.pending_order_assessments[].pending_order_assessment`.

**(k)** `position_id` ∈ `pm_view.positions`. When the envelope's `position_id` is present (always for `pm_strategist` envelopes), assert it matches a `position.position_id` in `pm_view.positions`. Pending-order assessments may reference positions that don't exist as held — but the strategist's pending-order assessments are for unfilled orders, not positions; for these, `position_id` should still resolve (the `position_id` in this case is the position the order *would create* per `oms-commands.md`, OR the order ID if pending-order assessments use a different cross-reference — confirm in the strategist output schema and align).

### 5\. Helper functions

Mirror analyst's helpers' shape: `_check_envelope_id_source_provenance(envelope) -> Iterable[ValidationError]`, `_check_close_command_subtype(envelope) -> Iterable[ValidationError]`, `_check_halt_mode_no_constructive(envelope, halt_mode) -> Iterable[ValidationError]`, `_check_embedded_command_sector(envelope, active_sectors) -> Iterable[ValidationError]`, `_check_narrative_references(envelope, retrieval_store) -> Iterable[ValidationError]`, `_check_source_recommendation_id_resolves(envelope, pre_processor_bundle) -> Iterable[ValidationError]`, `_check_position_id_resolves(envelope, pm_view) -> Iterable[ValidationError]`.

### 6\. Tests

Tests at `tests/decision/portfolio_manager/test_validation.py`:

* For each Layer-2 invariant (a)-(h): one positive test (envelope passes), one negative test (envelope fails with the expected error in `ValidationResult.errors`).
* For each Layer-3 check (i)-(k): one positive test, one negative test.
* `test_returns_full_inventory_not_first_error` — given an envelope with two distinct invariant violations, `ValidationResult.errors` contains both (not just the first).
* `test_warnings_do_not_invalidate` — given an envelope with only a warning (e.g., a low-conviction-but-PM-favoring concern), `is_valid is True`.
* `test_envelope_id_source_recommendation_id_bijection` — `ENV-REC-3` + `REC-5` fails (integer mismatch); `ENV-SA-7` + `SA-7` passes.

Construct test fixtures inline (small `PortfolioManagerView`, small `ProposalPreProcessorBundle`, small `RetrievalStore`).

### Out of scope

* The harness (story 07) — wires the validator into the SDK retry loop.
* The submit_envelope MCP wrapper (story 06c) — calls the validator on each `submit_envelope` invocation.
* Layer-1 (parsing) — story 06a's parser handles the structured-output sentinel; this validator handles envelopes which arrive as the input to `submit_envelope`.
* Layer-4 (stop-reason classification) — handled by the harness.
* Anti-pattern judgment correctness — the validator only checks that strings ∈ canonical enum; whether the model's anti-pattern self-identification is *right* is a feedback-loop concern.

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/validation.py` exists exporting `validate_pm_envelope`, `ValidationResult`, `ValidationError`, `ValidationWarning`.
- [ ] All eight Layer-2 invariants are enforced and tested.
- [ ] All three Layer-3 referential checks are enforced and tested.
- [ ] The validator returns a complete error/warning inventory (not first-error-only).
- [ ] Every test class covers each named acceptance bullet with a positive + negative case.
- [ ] `uv run pytest tests/decision/portfolio_manager/test_validation.py -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/test_validation.py -n auto` — all positive and negative tests pass. Inspect a representative valid + invalid envelope's `ValidationResult` via REPL to confirm error-message phrasing is informative.
