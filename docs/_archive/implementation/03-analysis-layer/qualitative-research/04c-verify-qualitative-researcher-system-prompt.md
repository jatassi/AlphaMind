# 04c — Verify qualitative researcher system prompt

## Goal

Verify the existing qualitative-researcher system prompt at `prompts/analysis/qualitative_researcher.md` (landed by the "Land analysis-layer prompts" quick win) round-trips through the parser (story 03a) and validator (story 03b) cleanly. The prompt was already drafted as production-quality content; this story is review/verification, not draft-from-scratch. The bar is contract conformance, not stylistic preference — edit the prompt only if the round-trip fails. A secondary deliverable: reconcile the prompt's `<tool_policy>` `social_sentiment` clause with this work tree's decision to defer that tool (per ALP-111 § Cross-feature dependencies).

## Reading

* `prompts/analysis/qualitative_researcher.md` — the existing prompt; the contract its `<output_contract>` block names is what stories 03a + 03b enforce.
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Output schema — the canonical contract the prompt must produce.
* `docs/design/testing/llm-output-validation.md` § Round-trip prompt verification — the discipline this story implements.
* `src/alphamind/analysis/qualitative_research/parser.py`, `validation.py`, `models.py` — what the round-trip uses.
* ALP-193, ALP-195, ALP-197 — sibling round-trip-verification stories from the domain-researcher work tree; mirror the test structure.

## Depends on

* ALP-243 — the parser the round-trip uses.
* ALP-244 — the validator the round-trip uses.

## Scope

In scope under `tests/analysis/qualitative_research/test_prompt_round_trip.py` and (only if the round-trip fails) `prompts/analysis/qualitative_researcher.md`.

### 1\. Round-trip test

`tests/analysis/qualitative_research/test_prompt_round_trip.py` extracts the `<example_output>` block from `prompts/analysis/qualitative_researcher.md` and runs:

```python
brief = parse_qualitative_brief(example_output_text, invocation_id="inv-...")
result = validate_qualitative_brief(brief, universe=test_universe)
assert result.is_valid
```

The `test_universe` fixture must include the tickers that appear in the example output's catalyst-watch entries (currently `JPM` per the prompt's example). If the example uses a ticker not in `asset_universe`, the test surfaces this as a prompt-edit signal — the example must reference real universe tickers.

The test exercises one canonical example. If the prompt's `<example_output>` block contains multiple `<example>` entries, exercise each.

### 2\. Edits the prompt only if necessary

If the round-trip fails:

* Parse failure — minimum prompt edit to fix the structural shape. Preserve the prompt's voice, structure, and content scope; do not rewrite stylistically.
* Validation failure — same minimum-edit rule.

If the round-trip succeeds, do not touch the prompt — the existing content is the bar.

### 3\. `social_sentiment` clause reconciliation

The prompt's `<tool_policy>` block currently includes a clause for `social_sentiment`. Per ALP-111 § Cross-feature dependencies, the tool ships disabled in this work tree. Two acceptable resolutions:

* **Strike the clause.** Remove the `social_sentiment` paragraph from `<tool_policy>` and adjust `<inputs>` and `<operating_context>` if they reference the tool. Surface the edit in the story's commit.
* **Retain the clause as forward-looking.** Add a one-line note above the clause: `(Not yet available — `social_sentiment` data layer is deferred per the project tracker. Do not call this tool until storage lands.)` Then add a sentence to `<operating_context>` clarifying that only three tools are currently callable.

Pick one; document the choice in the story's commit. If unsure, surface to the operator.

### Out of scope

* Rewriting the prompt for style — every existing word stays unless the round-trip fails or the `social_sentiment` reconciliation requires it.
* Adding new tools or re-introducing `social_sentiment` — that is a follow-up after data-layer storage lands.
* Editing the `<example_output>` block to demonstrate every possible variation — one canonical example is sufficient for the round-trip.

## Acceptance criteria

- [ ] `tests/analysis/qualitative_research/test_prompt_round_trip.py` exists and exercises the prompt's `<example_output>` block via `parse_qualitative_brief` + `validate_qualitative_brief`.
- [ ] The test passes under `uv run pytest tests/analysis/qualitative_research/test_prompt_round_trip.py -n auto`.
- [ ] `prompts/analysis/qualitative_researcher.md`'s `<tool_policy>` either omits `social_sentiment` or annotates it as not-yet-available; the choice is documented in the commit message.
- [ ] If the prompt was edited beyond the `social_sentiment` reconciliation, the diff is minimal (parse-or-validation-fix only) and the commit message names which contract failure motivated each edit.
- [ ] No new tools are introduced; the prompt does not reference any tool not in `agents.yaml`.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_prompt_round_trip.py -n auto`. Run `uv run ruff check . && uv run mypy` clean. Inspect the diff to `prompts/analysis/qualitative_researcher.md` and confirm it is either (a) empty (round-trip succeeded out of the box, only the `social_sentiment` reconciliation), or (b) limited to lines that the round-trip identified as failing.
