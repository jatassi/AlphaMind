# 01 — Canonical validator-result types in commands/validation_results.py

## Goal

Extend `src/alphamind/commands/validation_results.py` to serve as the single canonical home for `ValidationError`, `ValidationResult`, and `ValidationWarning` used by all 7 LLM-output validators. Migrate the 5 validators that still define their own dataclasses (analyst, strategist, domain researchers, qualitative researcher, adaptive researcher) to import from `commands.validation_results`. Strategist's `ValidationFailure` → `ValidationError` and `ValidationResult.overall`/`.failures` → `is_valid` property / `.errors` are the largest call-site changes; analysis-layer trio gains `warnings` as an empty tuple by default. PM is already canonical and is the migration target's shape.

## Reading

* `src/alphamind/commands/validation_results.py` — the current PM-canonical home ([ALP-458](https://linear.app/alphamind-jatassi/issue/ALP-458/02b-extract-commands-kernel-invert-submit-envelope-mcp-break)); your migration target. Note `ValidationError(field_path, message, criterion: str | None = None)` and `ValidationResult(envelope_id, errors, warnings, is_valid: property)`.
* `src/alphamind/decision/analyst/validation.py` — uses `ValidationError(field_path, rule, message)` + `ValidationResult(is_valid, errors, warnings)`. Rename `rule` survives; `is_valid` becomes a property; `warnings` already present.
* `src/alphamind/decision/strategist/validation.py` — uses `ValidationFailure(field_path, rule, message)` + `ValidationResult(overall: Literal["PASS","FAIL"], failures, warnings)`. Largest rename: `ValidationFailure` → `ValidationError`; `overall` → `is_valid` property; `failures` → `errors`.
* `src/alphamind/analysis/domain_researchers/validation.py` — uses `ValidationError(field_path, rule, message)` + `ValidationResult(is_valid, errors)`. Gains an empty `warnings` field.
* `src/alphamind/analysis/qualitative_research/validation.py` — same shape as domain researchers.
* `src/alphamind/analysis/adaptive_research/validation.py` — same shape as domain researchers.
* `src/alphamind/decision/portfolio_manager/validation.py` — already imports from `commands.validation_results`; do not touch.
* `src/alphamind/decision/strategist/harness.py` — call site for `ValidationResult.overall` / `.failures`. Update to `not result.is_valid` / `.errors`.
* `tests/decision/strategist/test_validation.py` and `tests/decision/strategist/test_harness.py` — call sites for the renamed shape.
* [ALP-458](https://linear.app/alphamind-jatassi/issue/ALP-458/02b-extract-commands-kernel-invert-submit-envelope-mcp-break) (PM canonical-types hoist) — for the precedent.

## Depends on

* None within this work tree (this is sequence position 01).
* Sibling features (`Done`): [ALP-115](https://linear.app/alphamind-jatassi/issue/ALP-115/analyst) / 116 / 117 / 113 / 111 / 112 / 458. No active gates.

## Scope

In scope, all under `src/alphamind/commands/validation_results.py` and the 5 migrating `validation.py` modules + their import-site call sites + tests. Tests at `tests/commands/test_validation_results.py` (new file for the canonical-shape unit tests) and the existing per-agent test modules (updated for the rename).

### 1\. Canonical types in `commands/validation_results.py`

Land a single canonical shape that satisfies all 6 existing validators (PM + the 5 migrating):

* `ValidationError` — frozen dataclass with `field_path: str`, `rule: str`, `message: str`, `criterion: str | None = None`. `rule` is the analyst/strategist/researcher convention (e.g. `"invalidation_leg_id_pairing"`); `criterion` is the PM convention for evaluation-criterion-set checks. Keep both so neither vocabulary breaks.
* `ValidationWarning` — same shape as `ValidationError` (frozen dataclass with `field_path`, `rule`, `message`, `criterion: str | None = None`).
* `ValidationResult` — frozen dataclass with `errors: tuple[ValidationError, ...]`, `warnings: tuple[ValidationWarning, ...] = ()`, `envelope_id: EnvelopeId | None = None`. Exposes `is_valid: bool` as a `@property` returning `not self.errors`. The default-empty `warnings` lets analysis-layer validators keep returning results with no warnings without per-call construction. The default-`None` `envelope_id` accommodates PM today without forcing other validators to track an irrelevant field.

Module docstring updated to reflect the new cross-cutting role (no longer just PM).

### 2\. Migrate `decision/analyst/validation.py`

Replace the three local dataclass definitions with `from alphamind.commands.validation_results import ValidationError, ValidationResult, ValidationWarning`. The public function `validate_analyst_output` returns the canonical `ValidationResult` unchanged in semantics. The `ValidationError` fields (`field_path`, `rule`, `message`) are unchanged because `criterion` defaults to `None`.

### 3\. Migrate `decision/strategist/validation.py`

The largest set of changes:

* Replace `ValidationFailure` with `ValidationError` from the canonical module (rename in every yield/return site).
* Replace the `ValidationResult` local definition with the canonical import.
* `validate_strategist_output` constructs the canonical `ValidationResult(errors=..., warnings=...)` and returns it. Drop the `overall: Literal["PASS","FAIL"]` field — replaced by the `is_valid` property at call sites.
* Update `decision/strategist/harness.py` call sites: `result.overall == "FAIL"` → `not result.is_valid`; iteration over `result.failures` → `result.errors`. Validate every reference with `git grep result.overall src/alphamind/decision/strategist/ tests/decision/strategist/` and `git grep result.failures src/alphamind/decision/strategist/ tests/decision/strategist/`.

### 4\. Migrate `analysis/{domain_researchers,qualitative_research,adaptive_research}/validation.py`

Each gains the canonical import and drops the local dataclass definitions. Each `validate_*` function returns `ValidationResult(errors=..., warnings=())` (warnings stays empty by default since none of the three validators produce warnings today).

### 5\. Unit tests for the canonical types

New `tests/commands/test_validation_results.py` exercising:

* Constructor with defaults (warnings empty, envelope_id None, criterion None).
* `is_valid` property: True when errors empty, False when one or more errors.
* Frozen-dataclass equality (two errors with identical field values compare equal).
* Hashing where applicable.

### 6\. Tests migration

Update tests in `tests/decision/analyst/test_validation.py`, `tests/decision/strategist/test_validation.py` + `tests/decision/strategist/test_harness.py`, `tests/analysis/domain_researchers/test_validation.py`, `tests/analysis/qualitative_research/test_validation.py`, `tests/analysis/adaptive_research/test_validation.py` to import the canonical types and to check `.is_valid` / `.errors` (strategist tests only).

### Out of scope

* Adding new validator behavior (bare-prefix detection is story 02).
* Touching PM's validator or call sites — already canonical.
* Centralizing `parse_reference_id` or producer-side ID formats (decision G in parent issue).
* Building a `LLMValidationResult` abstraction layer separate from the canonical types — the canonical type IS the abstraction.

## Acceptance criteria

- [ ] `src/alphamind/commands/validation_results.py` defines `ValidationError`, `ValidationWarning`, `ValidationResult` with the canonical shape described in scope §1; module docstring updated.
- [ ] `decision/analyst/validation.py`, `decision/strategist/validation.py`, `analysis/domain_researchers/validation.py`, `analysis/qualitative_research/validation.py`, `analysis/adaptive_research/validation.py` import from `commands.validation_results` and define no local `ValidationError`/`ValidationResult`/`ValidationWarning`/`ValidationFailure`.
- [ ] `decision/strategist/harness.py` and any test file using `.overall` or `.failures` are updated to `is_valid` and `.errors`; `git grep -n -E 'ValidationFailure|\.failures|result\.overall' src/alphamind/ tests/` returns no matches inside the migrated modules.
- [ ] `tests/commands/test_validation_results.py` exists and exercises the canonical types' constructor defaults, `is_valid` property, and frozen-equality.
- [ ] All existing validator and harness tests for the 5 migrating modules pass without semantic change to validator behavior (the test bodies may need import + attribute-name updates only).
- [ ] `uv run pytest -n auto` is clean (full suite — testmon's collection-graph tracking does not see the import-site changes reliably).
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports` are clean.

## Verification

The orchestrator confirms by:

* Running `uv run pytest -n auto` from the work-tree root and observing 0 failures.
* Running the lint chain in CLAUDE.md and observing 0 issues.
* `git grep -n 'class ValidationError\|class ValidationFailure\|class ValidationResult\|class ValidationWarning' src/alphamind/` returns exactly one match per type, all in `src/alphamind/commands/validation_results.py`.
* Visual spot-check of the 5 migrating modules' top-of-file imports and the strategist harness's result-handling block.