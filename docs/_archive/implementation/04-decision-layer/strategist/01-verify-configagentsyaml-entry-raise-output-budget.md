# 01 — Verify `config/agents.yaml` entry + raise output budget

## Goal

Round-trip the existing `strategist` entry in `config/agents.yaml` against the strategist design contract and the harness story (06) shape. The entry was authored 2026-04-27 during the configuration-management work tree's "Land analysis-layer prompts" quick win; this story confirms the values are intentional, surfaces any discrepancy to the operator before harness work begins, AND raises `output_token_budget` from 2000 to 4000 per parent Issue decision (A) so full-system portfolio outputs (1,200–2,500 tokens per design doc) plus retry-headroom fit within budget.

## Reading

* `config/agents.yaml` — the strategist entry to verify and edit.
* `docs/design/04-decision-layer/strategist.md` § Token budget — design-doc target ranges (primary 300–600; full-system 1,200–2,500 tokens output).
* `docs/design/cost-and-rate-limit-modeling.md` — model selection rationale (Opus) and per-invocation aggregate.
* `src/alphamind/config/models/agents.py` — `BaseAgentConfig` model + `AgentName` enum.
* `tests/config/test_agents_config.py` — existing analyst-entry test pattern; add a strategist-entry test mirroring it.
* `config/agents.yaml` `analyst:` block — the sibling-pattern for `output_token_budget: 4000`.

## Depends on

* None (Wave 1 of work tree).

## Scope

In scope: `config/agents.yaml` and the strategist-entry validation test under `tests/config/`.

### 1\. Verify the existing strategist entry

Confirm each field matches the design contract:

* `model: claude-opus-4-7` (Opus per cost-and-rate-limit-modeling.md)
* `prompt: prompts/decision/strategist.md` (matches the existing prompt file)
* `latency_budget_seconds: 180` (Opus reasoning budget, matches analyst)
* `context_token_budget: 8000` (matches analyst)
* `tools: [retrieve_brief, validate_guardrail]` (matches the harness story's MCP wiring; both tools are used in both modes per parent Issue decision (I))

If any value is unintentional or substantively wrong, surface to the operator per the parent Issue's surfacing conditions before changing.

### 2\. Raise `output_token_budget` from 2000 to 4000

Per parent Issue decision (A), edit the strategist entry to raise `output_token_budget` from `2000` to `4000`.

### 3\. Add a strategist-entry validation test

Test path: `tests/config/test_agents_config.py` (existing module). Add a test mirroring the existing analyst-entry test:

* Load `AgentsConfig` from the canonical path.
* Resolve the strategist entry by `AgentName.strategist`.
* Assert each of the six fields above against the loaded `BaseAgentConfig` instance.

### Out of scope

* Authoring or editing `prompts/decision/strategist.md` — story 02 owns prompt verification.
* Changes to other agent entries (analyst, portfolio_manager, sector researchers).
* Changes to `context_token_budget` — flagged as a surfacing condition in the parent Issue if story 08 reveals overflow, not changed here.

## Acceptance criteria

- [ ] `config/agents.yaml` strategist entry's `output_token_budget` is `4000`.
- [ ] `config/agents.yaml` strategist entry's other fields match the verified values (`model: claude-opus-4-7`, `prompt: prompts/decision/strategist.md`, `latency_budget_seconds: 180`, `context_token_budget: 8000`, `tools: [retrieve_brief, validate_guardrail]`).
- [ ] `tests/config/test_agents_config.py` includes a strategist-entry test asserting all six fields, mirroring the analyst-entry test.
- [ ] `uv run pytest tests/config/ -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

```bash
uv run pytest tests/config/ -n auto
```

Inspect `config/agents.yaml` to confirm `output_token_budget: 4000` under the `strategist:` block.
