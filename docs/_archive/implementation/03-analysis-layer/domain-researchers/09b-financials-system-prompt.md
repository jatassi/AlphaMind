## Goal

Verify the existing financials domain researcher system prompt at `prompts/analysis/financials_researcher.md` (commit `9ac6aea`) round-trips through the parser (story 04) and validator (story 05) cleanly. Edit the prompt only if the round-trip fails.

The prompt was already drafted as part of an earlier work tree; this story is review/verification, not draft-from-scratch. The bar is contract conformance, not stylistic preference.

## Reading

* `prompts/analysis/financials_researcher.md` — the existing prompt; verify against the design doc.
* `docs/design/03-analysis-layer/domain-researchers/financials.md` — the authoritative spec (sector mandate, rate-sensitivity focus, key signals — yield curve, credit spreads, loan growth, M&A).
* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the shared output contract every sector researcher uses (reused with `SA-FIN` prefix).
* `docs/design/01-data-layer/external/qualitative.md` § 6c — sector-specific qualitative input (credit conditions, rate environment, M&A pipeline, regulatory posture, consumer/payment, crypto).
* `docs/design/02-distillation-layer/external.md` § Output format — the distillation slice the agent reads.
* Story 04 (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief` and `ParseError`.
* Story 05 (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief` and `ValidationResult`.

## Depends on

* **04** (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief`, `ParseError`.
* **05** (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief`, `ValidationResult`.

## Scope — verification round-trip

Extract the `<example_output>` block from `prompts/analysis/financials_researcher.md` and feed it through `parse_brief(example_text, sector=Sector.FINANCIALS)` followed by `validate_brief(parsed_brief)`. Both must succeed: the parser returns a `SectorBrief`, the validator returns `ValidationResult(is_valid=True, errors=())`.

This adds the `test_financials_prompt_round_trip` case to `tests/analysis/domain_researchers/test_prompt_round_trip.py` (story 09a creates the file).

## Scope — failure-mode response

If the round-trip succeeds: the story is `Done`. No prompt edits.

If the round-trip fails: surface the exact failure and edit the prompt's example output to fix only the documented contract failure. Do not touch other sections unless they directly cause the round-trip failure.

If the prompt's output contract section deviates from `tech-semis.md § Domain researcher output contract` (the shared contract), surface to the operator before silently picking.

## Scope — invariants to confirm by inspection

* The reference-prefix throughout is `SA-FIN` (not `SA-TECH` or `SA-ENERGY`).

## Out of scope

* Drafting the prompt from scratch.

## Acceptance criteria

- [ ] `tests/analysis/domain_researchers/test_prompt_round_trip.py` includes `test_financials_prompt_round_trip`.
- [ ] The test extracts the `<example_output>` block from `prompts/analysis/financials_researcher.md` and asserts `parse_brief(...)` succeeds.
- [ ] The same test asserts `validate_brief(parsed_brief).is_valid is True`.
- [ ] Inspection-pass invariants confirmed (reference-prefix `SA-FIN`, output-contract markers, role mentions rate-sensitivity, method covers crypto independence, constraints section, no numeric anchors).
- [ ] Any prompt edits made are limited to fixing documented round-trip failures.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
