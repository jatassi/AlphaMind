---
status: done
completed_date: 2026-04-27
commit_id: ff9f39d
---

# 03i — `agents.yaml` + Pydantic model

## Goal

Land `config/agents.yaml` (per-agent LLM configuration: model, prompt path, latency and token budgets, tool allowlist) and a Pydantic model. The file is the single source of truth for every LLM agent in the pipeline; downstream consumers (the agent runner, the cost/rate-limit monitor, the run-type filter from story 04e) read it through the typed model.

## Reading

- `docs/design/configuration-management.md` § `agents.yaml` — schema and worked example
- `docs/design/cost-and-rate-limit-modeling.md` § Workload model — full agent inventory and Opus/Sonnet assignments
- `docs/design/05-execution-layer/state-persistence.md` § Agent calls — canonical agent-name enumeration (used as foreign-key value across the system)
- `docs/architecture/llm-integration.md` — agent inventory and `system_prompt_path` convention (`prompts/{layer}/{agent}.md`)
- `docs/design/03-analysis-layer/adaptive-research.md` — adaptive researcher's `cumulative_tool_call_limit` and `cumulative_tool_token_budget` fields and the per-tool cap structure
- `prompts/decision/analyst.md`, `prompts/decision/strategist.md`, `prompts/decision/pm.md` — existing prompt files (verify path-existence validator finds them)
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/agents.yaml` populated with one entry per LLM agent in the pipeline. Use the canonical agent IDs from `state-persistence.md § Agent calls`. The 9 agents:
  - **Decision (Opus):** `analyst`, `strategist`, `portfolio_manager`
  - **Analysis (Sonnet):** `tech_semis_researcher`, `financials_researcher`, `energy_researcher`, `qualitative_researcher`, `adaptive_researcher`, `synthesizer`
- Each agent entry carries:
  - `model` — `claude-opus-4-7` or `claude-sonnet-4-6` per the cost-doc inventory
  - `prompt` — path to the agent's system prompt file, relative to the repo root (e.g., `prompts/decision/analyst.md`, `prompts/analysis/tech_semis_researcher.md`)
  - `latency_budget_seconds` — int, agent-specific (use design-doc default ranges; `180` for analyst, `300` for adaptive_researcher per the worked example)
  - `context_token_budget` — int, agent-specific
  - `output_token_budget` — int, agent-specific
  - `tools` — list of tool names (allowlist). Empty list is valid.
  - For `adaptive_researcher` only, two additional fields:
    - `cumulative_tool_call_limit` — int (≥ 1)
    - `cumulative_tool_token_budget` — int (≥ 1)
    - `tool_caps` — `dict[str, int]` mapping tool name → per-tool call cap
- `src/alphamind/config/models/agents.py` defining:
  - `AgentName` (StrEnum: the 10 canonical IDs above — closed enum so the 04* bundle stories can reference it)
  - `AllowedModel` (StrEnum: `claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5-20251001`) — the Haiku member is unused but present so the operator can switch under cap pressure per `cost-and-rate-limit-modeling.md § Cap-hit response`
  - `BaseAgentConfig` (BaseModel: `model: AllowedModel`, `prompt: str`, `latency_budget_seconds: int = Field(ge=1)`, `context_token_budget: int = Field(ge=1)`, `output_token_budget: int = Field(ge=1)`, `tools: list[str]`)
  - `AdaptiveAgentConfig` (BaseAgentConfig + `cumulative_tool_call_limit: int = Field(ge=1)`, `cumulative_tool_token_budget: int = Field(ge=1)`, `tool_caps: dict[str, int]`)
  - `AgentsConfig` (BaseModel: `agents: dict[AgentName, BaseAgentConfig | AdaptiveAgentConfig]`)
- A field-level validator on `BaseAgentConfig.prompt` calling `Path.is_file()` against the repo root and raising if the file does not exist. The check makes typos a parse-time error rather than a runtime SDK failure.
- A field-level validator on `BaseAgentConfig.tools` and `AdaptiveAgentConfig.tool_caps` keys asserting every tool name matches `^[a-z][a-z0-9_]*$` (the registered-tool naming convention from the analysis-layer specs).
- A model validator on `AgentsConfig` enforcing that `agents:` covers every member of `AgentName`. Missing or extra keys raise.
- A model validator on `AgentsConfig` enforcing that the `adaptive_researcher` entry uses `AdaptiveAgentConfig` (carries the three adaptive-only fields) and every other entry uses `BaseAgentConfig` (does not carry them).
- Re-export `AgentsConfig`, `BaseAgentConfig`, `AdaptiveAgentConfig`, `AgentName`, `AllowedModel` from `models/__init__.py`.
- Unit tests covering: shipped `config/agents.yaml` parses cleanly; missing agent (e.g., omitting `synthesizer`) raises; extra agent name not in `AgentName` raises; non-existent prompt path raises; unknown `model` raises; an `analyst` entry carrying `cumulative_tool_call_limit` raises; an `adaptive_researcher` entry missing `cumulative_tool_call_limit` raises.

Out of scope:
- The tool registry — every tool name must be a registered tool somewhere in code; that cross-reference belongs to the runtime when the agent fires. Schema validation here is name-format only.
- Cross-reference — every agent name in any run-type's `agents.enabled` (story 04e) exists in this file; story 06a.
- Per-profile token-budget overrides — those live in `profiles/<profile>.yaml` per `configuration-management.md § profiles/medium.yaml` worked example.
- Wiring `AgentsConfig` into the loader aggregate (story 08).

## Notes

**Closed enum vs. open dict.** `AgentsConfig.agents` is keyed by the closed `AgentName` enum to make the per-agent slots a contract — adding a new agent requires editing the enum, which forces touches on every consuming story (04c modes, 04e run_types). Open dictionaries silently accept new keys, which is the wrong default for a roster of fixed agents.

**Discriminated union.** Pydantic v2 supports tagged unions natively, but the discriminator here (adaptive-only fields) is structural rather than tagged. The model validator approach is simpler than declaring a Pydantic discriminator and equivalent for the validation goal.

**Path resolution.** `Path.is_file()` runs against the repo root resolved by walking up from `models/agents.py` four levels (matching the existing `models/data_sources.py` pattern for `.env.example`). Test fixtures must accommodate this — a fixture YAML referencing a non-existent prompt file should raise; a fixture YAML referencing one of the real `prompts/decision/*.md` files should pass.

**Latency and token budgets** are shipped values, not enforced runtime caps. The model carries them; the agent runner applies them when constructing `ClaudeAgentOptions`. Engineering rationale (ranges, scaling per profile) is not validated here.

**The `tools` list** is a flat list of tool names — no per-tool metadata. The adaptive researcher's `tool_caps` map carries per-tool call counts because of its structured tool-loop budget; other agents have no per-tool overrides and use the runner's default of "one call per tool slot".

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/agents.yaml` exists with entries for all 10 canonical agent IDs.
- [ ] Each entry carries `model`, `prompt`, `latency_budget_seconds`, `context_token_budget`, `output_token_budget`, `tools`.
- [ ] The `adaptive_researcher` entry additionally carries `cumulative_tool_call_limit`, `cumulative_tool_token_budget`, `tool_caps`.
- [ ] Every `prompt` path resolves to an existing file under `prompts/`.
- [ ] `config/agents.yaml` parses cleanly via `yaml.safe_load` and validates against `AgentsConfig`.
- [ ] `src/alphamind/config/models/agents.py` defines `AgentName`, `AllowedModel`, `BaseAgentConfig`, `AdaptiveAgentConfig`, `AgentsConfig`.
- [ ] `models/__init__.py` re-exports the five names.
- [ ] A unit test asserts the shipped `config/agents.yaml` parses and exposes all 9 agents.
- [ ] A unit test asserts a YAML missing an agent (e.g., omitting `synthesizer`) raises `ValidationError`.
- [ ] A unit test asserts a YAML containing an extra agent name not in `AgentName` raises `ValidationError`.
- [ ] A unit test asserts a `prompt` value pointing at a non-existent path raises `ValidationError`.
- [ ] A unit test asserts an unknown `model` (e.g., `claude-haiku-3-5`) raises `ValidationError`.
- [ ] A unit test asserts the `analyst` entry carrying `cumulative_tool_call_limit` raises `ValidationError`.
- [ ] A unit test asserts the `adaptive_researcher` entry missing `cumulative_tool_call_limit` raises `ValidationError`.
- [ ] A unit test asserts an `analyst` entry with `tools: [Retrieve_Brief]` (capital letter) raises `ValidationError`.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
