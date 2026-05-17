# 02 — Verify `prompts/decision/analyst.md` system prompt

## Goal

Round-trip the existing analyst system prompt at `prompts/decision/analyst.md` against the analyst design (`docs/design/04-decision-layer/analyst.md`), the analyst output schema (`docs/design/04-decision-layer/analyst-output-schema.md`), the state-delivery contract (`docs/design/06-risk-guardrails/state-delivery.md`), and the `agent-system-prompts` skill quality bar. The prompt was authored 2026-04-27; this story confirms it covers every contract the harness (07), parser (05a), and validator (05b) build against, and surfaces any gap before implementation begins.

## Reading

* `prompts/decision/analyst.md` — the file under review.
* `docs/design/04-decision-layer/analyst.md` — role, conviction scale, entry window, ranking behavior, validation workflow.
* `docs/design/04-decision-layer/analyst-output-schema.md` — schema fields, enums, conditional requirements.
* `docs/design/06-risk-guardrails/state-delivery.md` § Analyst guardrail state header, § Halt-mode header modifications, § Guardrail validation tool — the inputs the prompt's `<inputs>` block describes and the tool the prompt's `<tool_policy>` describes.
* `.claude/skills/agent-system-prompts/SKILL.md` — invoke this skill via `/agent-system-prompts` if available, or apply its quality bar manually: role/operating-context/inputs/task/method/tool_policy/output_contract/example_output/constraints sections; explicit RIGHT/WRONG examples for any format the parser is strict about.
* `ALP-209` (synthesizer story 09 — verify system prompt) — sibling-shape for the verification approach.
* `prompts/decision/{strategist,pm}.md` — sibling decision-layer prompts; confirm cross-prompt consistency on the shared retrieve_brief workflow language.

## Depends on

(none — independent)

## Scope

In scope: `prompts/decision/analyst.md` reading + light edits if needed. Tests at `tests/decision/analyst/`.

### 1\. Apply the agent-system-prompts skill review

Walk every section of the prompt against the design contracts above:

* `<role>` — names the agent, the optimization target (thesis falsifiability, calibrated conviction, honest inaction).
* `<operating_context>` — fresh context window per invocation; user-turn order (header + brief); two-tool surface; PM is the consumer; analyst is new-entries-only.
* `<inputs>` — describes the guardrail header fields (invocation_id, Regime, Mode, \*\* EMERGENCY INVOCATION \*\*, Hard blocks, sector/directional/gross/options headroom, Held positions, Abandoned openings) and the synthesizer brief (with the typed reference-prefix taxonomy).
* `<task>` — three modes (normal, watchlist, emergency); inclusion threshold (qualifies-iff-acting-alone); zero-recommendations-is-valid; no count cap.
* `<method>` — eight required thesis elements (signal set, causal chain, catalyst, falsification, conviction by signal characteristics, counterargument, contradiction handling, dedup); validation hook.
* `<tool_policy>` — `validate_guardrail` (action="OPEN" only, cumulative tracking, copy-tool-result-verbatim into output) and `retrieve_brief` (optional, uses-cases, no-retry-same-id rule).
* `<output_contract>` — JSON-only output, top-level shape, mode-conditional fields, source-reference rule, presentation order.
* `<example_output>` — at least one fully-specified normal-mode recommendation matching the schema's required fields.
* `<constraints>` — empty-array-is-valid, FAIL-cannot-be-emitted, no-fabrication, no-invented-references, hard-blocks-are-veto, no-cross-proposal-comparison, no-hedging-language, no-inflated-conviction, watchlist-mode-restrictions, JSON-only-output.

For each section, identify gaps against the design contracts. Surface gaps to the operator BEFORE editing — do not unilaterally rewrite a section unless it's a typo or the omission is unambiguously a bug.

### 2\. Round-trip every output schema field through the prompt's example

Walk every required field in the analyst output schema's `recommendation` `$def` and confirm the prompt's `<example_output>` populates each one. Required fields per schema: `recommendation_id`, `instrument`, `underlying`, `sector`, `conviction_level`, `entry_order`, `position_size`, `target`, `invalidation_legs`, `time_expectation_hours`, `guardrail_validation_result`, `thesis_narrative`, `target_rationale`, `invalidation_rationale`, `position_size_rationale`, `counterarguments_acknowledged`. Conditional: `entry_window` + `entry_window_rationale` (paired). Confirm the example exhibits every required field with a realistic value.

### 3\. Add a prompt round-trip test

`tests/decision/analyst/test_prompt_round_trip.py`: a test that loads the prompt file, asserts it begins with the expected envelope marker (`<role>` per the existing prompt), and that every field in the analyst output schema's `required` list of `recommendation` appears as a substring in the prompt's example output (smoke check that future schema drift forces a prompt update). Mirror the synthesizer's `tests/analysis/synthesizer/test_synthesizer_prompt.py` shape.

### Out of scope

* Editing the prompt for non-bug improvements (e.g., wordsmithing). If the design contract changes, the design doc edits first; prompt follows.
* The actual harness's prompt loading (story 07 owns that — it imports the file path from agents.yaml).

## Acceptance criteria

- [ ] Each section of `prompts/decision/analyst.md` covers every contract from the design docs and analyst-output-schema; gaps surfaced to the operator if any.
- [ ] The prompt's `<example_output>` populates every required field in the schema's `recommendation` $def.
- [ ] `tests/decision/analyst/test_prompt_round_trip.py` exists and pins the prompt-vs-schema invariant; passes under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_prompt_round_trip.py -n auto` passes. Manually skim the prompt one more time after any edits to confirm no introduced regressions in tone or structure.