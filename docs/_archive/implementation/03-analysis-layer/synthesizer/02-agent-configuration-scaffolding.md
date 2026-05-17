# 02 — Verify agent configuration

## Goal

Round-trip the existing `synthesizer` entry in `config/agents.yaml` against the design contract and the harness story (08). The entry was authored 2026-04-27 as part of the configuration-management work tree's "Land analysis-layer prompts" quick win; this story confirms the values are intentional and the prompt path resolves, and surfaces any discrepancy to the operator before the harness story builds against it.

## Reading

* `config/agents.yaml` § synthesizer — the existing entry (lines \~110–116).
* `src/alphamind/config/models/agents.py` — `BaseAgentConfig`, `AgentName`. The Pydantic model the entry must validate against.
* `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate — the design target (`Synthesizer | ~10,000 input | ~1,500 output`); the actual `output_token_budget: 2000` carries \~33% headroom.
* `docs/design/03-analysis-layer/synthesizer.md` § Output — the prose-not-schema stance that justifies the absence of a parser/validator and informs the lower output budget vs. the 3000 originally proposed.
* `docs/architecture/llm-integration.md` § Agent inventory — Sonnet model assignment.
* `prompts/analysis/synthesizer.md` — the prompt file the entry's `prompt:` field references.
* [ALP-209](https://linear.app/alphamind-jatassi/issue/ALP-209) (story 09) — the verification story for the prompt itself.
* [ALP-208](https://linear.app/alphamind-jatassi/issue/ALP-208) (story 08) — the harness that loads the entry's fields at invocation time.
* Parent issue § Notes for the orchestrator § Per-invocation MCP wiring — explains why `tools: []` is intentional.

## Depends on

Nothing — verification.

## Scope

#### Expected field values

The synthesizer entry under `agents.synthesizer` in `config/agents.yaml` should hold the values listed below. Every value matches a design-doc target.

* `model == "claude-sonnet-4-6"` — matches the agent inventory.
* `prompt == "prompts/analysis/synthesizer.md"` — file exists and is readable from repo root.
* `latency_budget_seconds == 180` — matches `cost-and-rate-limit-modeling.md` § Latency budgets.
* `output_token_budget == 2000` — within \~33% of the 1500 design target; correct headroom for the prose-not-schema output stance.
* `context_token_budget == 12000` — fits six brief inputs + regime label + tool reminders.
* `tools == []` — **intentionally empty**. The synthesizer's three portfolio-state tools are wired at the harness level via per-invocation closures over `SynthesizerPortfolioStateReader` (see parent issue § Notes for the orchestrator: per-invocation MCP wiring). The global `alphamind.analysis.tools.TOOLS` registry pattern does not apply.

#### Verification tests

Add a new test module under `tests/config/`. The module exercises three cases.

* `test_synthesizer_entry_loads` — `BaseAgentConfig` validates the synthesizer entry without error.
* `test_synthesizer_prompt_path_exists` — the `prompt:` field resolves to a readable file under the repo root.
* `test_synthesizer_tools_empty` — `tools == []` is asserted explicitly with a comment pointing at the per-invocation MCP rationale, so a future contributor doesn't "fix" the empty list by adding global tool names.

#### Inline rationale comment

If `config/agents.yaml` does not already document why `synthesizer.tools` is `[]`, add a one-line YAML comment above the synthesizer entry pointing to the per-invocation MCP wiring decision in the parent issue.

## Out of scope

* Adding tools to the entry — the harness wires its own per-invocation MCP server.
* Authoring the prompt — story 09 covers verification of the existing prompt.
* Per-invocation MCP-server construction — story 08.
* Any change to the field values unless verification surfaces a discrepancy and the operator confirms.

## Acceptance criteria

- [ ] All five field values asserted in the verification test.
- [ ] `prompt:` path resolves to an existing file.
- [ ] `tools: []` is documented inline in `agents.yaml` (one-line comment) explaining the per-invocation MCP rationale, if not already.
- [ ] If verification surfaces a discrepancy, pause and surface to the operator before editing the file.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.