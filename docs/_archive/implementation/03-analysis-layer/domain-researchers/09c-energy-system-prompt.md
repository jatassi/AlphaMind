## Goal

Verify the existing energy domain researcher system prompt at `prompts/analysis/energy_researcher.md` (commit `9ac6aea`) round-trips through the parser (story 04) and validator (story 05) cleanly. Edit the prompt only if the round-trip fails.

The prompt was already drafted as part of an earlier work tree; this story is review/verification, not draft-from-scratch. The bar is contract conformance, not stylistic preference.

## Reading

* `prompts/analysis/energy_researcher.md` — the existing prompt; verify against the design doc.
* `docs/design/03-analysis-layer/domain-researchers/energy.md` — the authoritative spec (sector mandate, commodity-price-linkage focus, key signals — EIA inventory, OPEC, geopolitics, refining margins, LNG).
* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the shared output contract every sector researcher uses (reused with `SA-ENERGY` prefix).
* `docs/design/01-data-layer/external/qualitative.md` § 6b — sector-specific qualitative input (OPEC rhetoric, inventory narrative, weather, pipeline).
* `docs/design/02-distillation-layer/external.md` § Output format — the distillation slice the agent reads (commodity-relevant outputs from §8).
* Story 04 (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief` and `ParseError`.
* Story 05 (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief` and `ValidationResult`.

## Depends on

* **04** (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief`, `ParseError`.
* **05** (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief`, `ValidationResult`.

## Scope — verification round-trip

Extract the `<example_output>` block from `prompts/analysis/energy_researcher.md` and feed it through `parse_brief(example_text, sector=Sector.ENERGY)` followed by `validate_brief(parsed_brief)`. Both must succeed.

This adds the `test_energy_prompt_round_trip` case to `tests/analysis/domain_researchers/test_prompt_round_trip.py` (story 09a creates the file).

## Scope — failure-mode response

If the round-trip succeeds: `Done`. No prompt edits.

If the round-trip fails: surface the exact failure and edit the prompt's example output to fix only the documented contract failure. Do not touch other sections unless they directly cause the round-trip failure.

## Scope — invariants to confirm by inspection

* The reference-prefix throughout is `SA-ENERGY` (not `SA-TECH` or `SA-FIN`).

## Out of scope

* Drafting the prompt from scratch.

## Acceptance criteria

- [ ] `tests/analysis/domain_researchers/test_prompt_round_trip.py` includes `test_energy_prompt_round_trip`.
- [ ] The test extracts the `<example_output>` block from `prompts/analysis/energy_researcher.md` and asserts `parse_brief(...)` succeeds.
- [ ] The same test asserts `validate_brief(parsed_brief).is_valid is True`.
- [ ] Inspection-pass invariants confirmed (reference-prefix `SA-ENERGY`, output-contract markers, role mentions commodity-price-linkage, method names four business-mix categories, constraints section, no numeric anchors).
- [ ] Any prompt edits made are limited to fixing documented round-trip failures.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
