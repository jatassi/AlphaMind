# 01 — Verify `config/agents.yaml` entry + raise output budget

## Goal

Round-trip the existing `analyst` entry in `config/agents.yaml` against the analyst design contract and the harness story (07) shape. The entry was authored 2026-04-27 during the configuration-management work tree's "Land analysis-layer prompts" quick win; this story confirms the values are intentional, surfaces any discrepancy to the operator before harness work begins, AND applies the parent issue's pre-resolved decision (B) to bump `output_token_budget` from 2000 to 4000.

## Reading

* `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate, § Latency budgets — the design-doc canonical values for the analyst slot.
* `config/agents.yaml` — the file to verify and edit (analyst entry near the top under the Decision layer block).
* `src/alphamind/config/models/agents.py` — `AgentsConfig`, `BaseAgentConfig`, `AgentName.analyst` — the validator that consumes agents.yaml.
* `prompts/decision/analyst.md` — confirm the path the yaml entry references resolves and the prompt exists.
* `docs/design/04-decision-layer/analyst.md` § Output, § Pre-submission guardrail validation, § Source brief retrieval — confirm the yaml's `tools:` list (`retrieve_brief`, `validate_guardrail`) matches the design's two-tool contract.
* `ALP-200` (synthesizer story 02 — verify agents.yaml) — sibling-shape for the verification approach.

## Depends on

(none — independent)

## Scope

In scope, all under `config/agents.yaml`. Tests at `tests/config/`.

### 1\. Round-trip the analyst entry

Read `config/agents.yaml` and validate via `AgentsConfig.model_validate(...)`. Confirm:

* `model: claude-opus-4-7`
* `prompt: prompts/decision/analyst.md` (and the path resolves from the repo root)
* `latency_budget_seconds: 180`
* `context_token_budget: 8000`
* `tools: [retrieve_brief, validate_guardrail]`

Each value should match the design contract. Surface any divergence as a finding and stop until the operator confirms.

### 2\. Raise `output_token_budget` from 2000 to 4000

Edit the analyst entry in `config/agents.yaml` to change `output_token_budget: 2000` to `output_token_budget: 4000`. Add a YAML comment above the field referencing this story's reasoning: each recommendation serializes to \~500–800 tokens with the rich nested shape (instrument + entry_order + position_size + target + invalidation_legs + 4 narrative fields + guardrail_validation_result mirror); 1–3 recommendations sit at 1500–2500 tokens with no headroom for retry or empty-day clarifications. The 4000 figure leaves \~50% headroom.

### 3\. Add a unit test that pins the analyst entry's contract

`tests/config/test_agents_yaml_analyst.py`: a single test that validates `AgentsConfig.model_validate(yaml.safe_load(open("config/agents.yaml")))` and asserts the analyst entry's full shape (model, prompt path resolves, latency_budget_seconds, context_token_budget, output_token_budget=4000, tools list). Future drift produces a clear test failure.

### Out of scope

* The system-prompt verification (story 02 owns that).
* The harness implementation itself (story 07 owns that).
* Production-side reload — `agents.yaml` is read at process start; the next invocation picks up the new value.

## Acceptance criteria

- [ ] `config/agents.yaml` analyst entry has `output_token_budget: 4000` with a YAML comment explaining the headroom math.
- [ ] All other analyst-entry fields match the design-doc contract (model, prompt path, latency, context, tools list).
- [ ] `tests/config/test_agents_yaml_analyst.py` exists and pins the full analyst entry shape; passes under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/config/test_agents_yaml_analyst.py -n auto` passes; the test name is descriptive enough that a future drift produces a clear failure. Manually re-read the analyst entry to confirm the comment is in place and accurately describes the math.