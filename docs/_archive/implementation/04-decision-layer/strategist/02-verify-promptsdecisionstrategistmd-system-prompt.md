# 02 — Verify `prompts/decision/strategist.md` system prompt

## Goal

Round-trip the existing strategist system prompt at `prompts/decision/strategist.md` against the strategist design (`docs/design/04-decision-layer/strategist.md`), the strategist output schema (`docs/design/04-decision-layer/strategist-output-schema.md`), the state-delivery contract (`docs/design/06-risk-guardrails/state-delivery.md`), and the `agent-system-prompts` skill quality bar. The prompt was authored 2026-04-27 during the configuration-management work tree; this story confirms it implements the contract correctly, that all anti-pattern names match the canonical strings the PM aggregates on, that the tool surface (`validate_guardrail`, `retrieve_brief`) matches the harness story (06) MCP wiring, and that the mode handling (`normal` / `defensive_posture` + emergency-invocation flag) matches the schema. Verification-only — do NOT unilaterally rewrite; surface substantive divergence to the operator.

## Reading

* `prompts/decision/strategist.md` — the prompt to verify (full file).
* `docs/design/04-decision-layer/strategist.md` — design contract: role, status classification, action decision logic, anti-pattern discipline, pending-order review, abandoned-action handling, regime-transition remedies, halt-mode + emergency-invocation behavior.
* `docs/design/04-decision-layer/strategist-output-schema.md` — output contract; the `<output_contract>` section must match the schema's required fields, action_parameters discriminated union, and mode-conditional restrictions.
* `docs/design/06-risk-guardrails/state-delivery.md` — strategist guardrail state header (normal + halt-mode), `validate_guardrail` tool I/O contract, emergency-invocation flag.
* `docs/design/04-decision-layer/portfolio-manager.md` — canonical anti-pattern strings the PM aggregates on; verify the strategist's `<constraints>` and anti-pattern names match exactly.
* `prompts/decision/analyst.md` — sibling agent's prompt; verify shared sections (operating_context boilerplate, source-reference vocabulary, retrieve_brief usage policy) match where they should.
* `.claude/skills/agent-system-prompts/SKILL.md` — quality bar (role / operating_context / inputs / task / method / tool_policy / output_contract / example_output / constraints structure; anti-redundancy; canonical naming).
* `src/alphamind/decision/analyst/__init__.py` and `tests/decision/analyst/test_prompt_round_trip.py` — sibling round-trip test pattern.

## Depends on

* None (Wave 1 of work tree).

## Scope

In scope: `prompts/decision/strategist.md` (verify, edit only if substantive divergence) and a new round-trip test under `tests/decision/strategist/test_prompt_round_trip.py`.

### 1\. Round-trip the prompt against the design and schema

Verify each section of `prompts/decision/strategist.md` against the authoritative source:

* `<role>` — matches the design doc's mandate framing (sole agent for thesis-status classification, hold/reduce/close/adjust-bracket/add for existing positions, parallel with analyst, no new entries).
* `<operating_context>` — fresh context, no memory; user-turn order matches the runner's input bundle (header → tool reminder → synthesizer brief → portfolio state); source-reference vocabulary matches what `RetrievalStore` actually surfaces; tool list (`validate_guardrail`, `retrieve_brief`) matches harness MCP wiring.
* `<inputs>` — guardrail header fields (invocation_id, Regime, Mode, EMERGENCY INVOCATION, sector/directional/gross/options headroom, position-level proximity, sector breakdown, drawdown state, regime-transition breaches) match `render_strategist_header` output exactly. Synthesizer brief and portfolio state section descriptions match what the input bundle (story 04) will assemble.
* `<task>` — normal-mode and `defensive_posture`-mode behaviors match the design doc's "Halt mode and defensive-posture behavior" and the schema's mode-conditional restrictions (`add` not permitted in `defensive_posture`; `defensive_posture_summary` required).
* `<method>` — workflow steps (read thesis, classify, select action, burden-of-proof, remedy-flag, validate, scan activity log, pending-order review, portfolio-level observations, presentation order) match the design doc's methodology sections.
* `<tool_policy>` — `validate_guardrail` calls for add + breach-remedy reduce/close + suspected secondary breach; `retrieve_brief` policy matches the design's three-bullet usage criteria.
* `<output_contract>` — top-level shape (invocation_id, timestamp, mode, position_assessments, pending_order_assessments, portfolio_level_observations) matches the schema. Action_parameters mode-dispatch matches schema's allOf conditionals. JSON-only directive matches the SDK JSON-Schema-mode contract.
* `<example_output>` — example JSON validates against the schema; reference IDs and rationale styles are realistic; example length is comparable to analyst's example (representative, not exhaustive).
* `<constraints>` — anti-pattern names (`sunk_cost_persistence`, `rationalized_continuation`, `thesis_contradiction_suppression`, `engine_originated_closure_signal`, `generic_rationale`) match the canonical strings in the PM design doc and the strategist design doc's "LLM failure mode avoidance" section.

### 2\. Verify the example_output is schema-valid

Use the schema (compiled from `StrategistOutput.model_json_schema()` once story 03 lands — for this story, validate against the JSON Schema in `docs/design/04-decision-layer/strategist-output-schema.md` directly via `jsonschema.validate(...)` in the round-trip test).

If story 03 has not landed yet, the round-trip test may use the design-doc schema directly; once story 03 lands, the harness story (06) re-validates against the Pydantic-generated schema.

### 3\. Add a round-trip test

Path: `tests/decision/strategist/test_prompt_round_trip.py`. Test should:

* Load the prompt file and assert it contains each of the eight expected sections (`<role>`, `<operating_context>`, `<inputs>`, `<task>`, `<method>`, `<tool_policy>`, `<output_contract>`, `<example_output>`, `<constraints>`).
* Extract the JSON block from `<example_output>` and validate it against the strategist output schema (loaded from `docs/design/04-decision-layer/strategist-output-schema.md` — extract the `json` code block, parse, and use `jsonschema.validate`).
* Assert the canonical anti-pattern strings appear verbatim in the prompt.
* Assert tool names (`validate_guardrail`, `retrieve_brief`) appear in `<tool_policy>` matching the agents.yaml `tools:` list.

### Out of scope

* Editing `config/agents.yaml` — story 01.
* Implementing the typed model — story 03.
* Implementing parser, validator, input bundle, harness, runner, e2e — stories 04, 05a, 05b, 06, 07, 08.
* Rewriting the prompt without operator approval. If a substantive divergence is found, surface the gap and pause; minor cleanups (typos, formatting) are fine.

## Acceptance criteria

- [ ] `prompts/decision/strategist.md` is verified against the design doc, output schema, state-delivery contract, and agent-system-prompts quality bar; each of the eight sections is present and correct.
- [ ] The `<example_output>` JSON validates against the strategist output schema (Draft 2020-12).
- [ ] Anti-pattern names in `<constraints>` match the canonical PM-aggregation strings (`sunk_cost_persistence`, `rationalized_continuation`, `thesis_contradiction_suppression`, `engine_originated_closure_signal`, `generic_rationale`).
- [ ] `<tool_policy>` references both `validate_guardrail` and `retrieve_brief` matching the agents.yaml `tools:` list.
- [ ] `tests/decision/strategist/test_prompt_round_trip.py` exists with the assertions above and passes under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.
- [ ] If any substantive divergence was found, it has been surfaced to the operator with a recommendation; the divergence is either fixed (operator-approved) or recorded as a follow-up.

## Verification

```bash
uv run pytest tests/decision/strategist/test_prompt_round_trip.py -n auto
```

Manually inspect: `prompts/decision/strategist.md` matches the design contract on all eight sections; the example output is realistic and schema-valid.
