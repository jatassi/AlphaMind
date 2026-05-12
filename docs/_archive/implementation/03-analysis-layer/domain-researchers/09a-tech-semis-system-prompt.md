## Goal

Verify the existing tech & semis domain researcher system prompt at `prompts/analysis/tech_semis_researcher.md` (commit `9ac6aea`) round-trips through the parser (story 04) and validator (story 05) cleanly. Edit the prompt only if the round-trip fails.

The prompt was already drafted as part of an earlier work tree; this story is review/verification, not draft-from-scratch. The bar is contract conformance, not stylistic preference — do not let a subagent rewrite a shipped prompt because it "could be better."

## Reading

* `prompts/analysis/tech_semis_researcher.md` — the existing prompt; verify against the design doc.
* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` — the authoritative spec (sector mandate, key signals, output contract).
* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the prose schema the prompt's output-contract section codifies.
* `docs/design/01-data-layer/external/qualitative.md` § 6a — sector-specific qualitative input the agent reads.
* `docs/design/02-distillation-layer/external.md` § Output format — the distillation slice the agent reads.
* Story 04 (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief` and `ParseError`, the round-trip target.
* Story 05 (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief` and `ValidationResult`, the round-trip target.
* User memories `feedback_avoid_numeric_anchors`, `feedback_llm_agents_uniformly_critical` — discipline applied here.

## Depends on

* **04** (<issue id="23a74e6a-5b88-4557-9657-588c2773ca10">ALP-192</issue>) — `parse_brief`, `ParseError` (the round-trip test parses the prompt's example output).
* **05** (<issue id="265372f4-2d92-45fa-a158-106fe48edf21">ALP-196</issue>) — `validate_brief`, `ValidationResult` (the round-trip test validates the parsed brief).

## Scope — verification round-trip

Extract the `<example_output>` block from `prompts/analysis/tech_semis_researcher.md` and feed it through `parse_brief(example_text, sector=Sector.TECH_SEMIS)` followed by `validate_brief(parsed_brief)`. Both must succeed: the parser returns a `SectorBrief`, the validator returns `ValidationResult(is_valid=True, errors=())`.

Encode this as a unit test in `tests/analysis/domain_researchers/test_prompt_round_trip.py` parametrized across the three sector prompts (this story owns the tech/semis case; stories 09b and 09c add the financials and energy cases).

## Scope — failure-mode response

If the round-trip succeeds: the story is `Done`. No prompt edits.

If the round-trip fails: surface the exact failure (parse error or validation error) and the offending line in the prompt. Edit the prompt's example output to fix only the documented contract failure — do not touch the role, operating context, inputs, task, method, output contract, or constraints sections unless they directly cause the round-trip failure. Preserve all existing wording that is not the cause of the failure.

If the prompt's *output contract section* (the section that documents the format the LLM must emit) deviates from `tech-semis.md § Domain researcher output contract`, the deviation must be reconciled. Either the design doc or the prompt is wrong; surface to the operator before silently picking.

## Scope — invariants to confirm by inspection (not test-driven)

Confirm by reading the prompt:

* The reference-prefix throughout is `SA-TECH` (not `SA-FIN` or `SA-ENERGY`).

Surface any violations to the operator with a one-line each. Do not unilaterally rewrite — the prompt was authored deliberately and its phrasing carries calibration the test suite cannot capture.

## Out of scope

* Drafting the prompt from scratch — the prompt already exists.

## Acceptance criteria

- [ ] `tests/analysis/domain_researchers/test_prompt_round_trip.py` exists with at minimum a `test_tech_semis_prompt_round_trip` case.
- [ ] The test extracts the `<example_output>` block from `prompts/analysis/tech_semis_researcher.md` and asserts `parse_brief(...)` succeeds.
- [ ] The same test asserts `validate_brief(parsed_brief).is_valid is True`.
- [ ] Inspection-pass invariants confirmed (reference-prefix, output-contract markers, constraints section, no numeric anchors).
- [ ] Any prompt edits made are limited to fixing documented round-trip failures; the diff is attached to the verification report.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
