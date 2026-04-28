---
status: not_started
completed_date:
commit_id:
---

# 04e — `run_types/` bundle (six firing triggers)

## Goal

Land the six run-type files under `config/run_types/` and a Pydantic model that validates each one. Run-types are scheduler-resolved (the firing trigger key passed in by APScheduler determines which file loads). Each file scopes the agent roster (which agents fire on this trigger) and the budget envelope (per-agent latency / token caps, adaptive-research tool-call and token caps, qualitative news-digest depth) for that invocation. Run-types are deterministic-only — they do not inject prompt instructions per `configuration-management.md § run_types/<trigger>.yaml`.

## Reading

- `docs/design/configuration-management.md` § `run_types/<trigger>.yaml` — schema, worked examples for `pre_open` and `off_hours_rolling`, override-semantics paragraph, scope paragraph
- `docs/design/configuration-management.md` § Composition model — confirms run-type is the last cascade dimension applied
- `docs/design/README.md` § Pipeline scheduling — full per-trigger description, the six trigger names, the parallelism intent
- `docs/design/cost-and-rate-limit-modeling.md` § Workload model — the budget envelopes the run-type clamps must respect
- `src/alphamind/config/models/agents.py` (post-story-03i) — `AgentName` enum the model references
- `src/alphamind/config/models/scheduler.py` (post-story-03c) — `triggers:` map keys must match the run-type filenames
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)
- 03c (scheduler.yaml — trigger names that filenames must match)
- 03i (agents.yaml — `AgentName` enum the run-type roster references)

## Scope

In scope:
- Six files under `config/run_types/`: `pre_open.yaml`, `market_hours_rolling.yaml`, `pre_close.yaml`, `off_hours_rolling.yaml`, `weekend_saturday.yaml`, `weekend_sunday.yaml`. The filename stems match the trigger keys declared in `scheduler.yaml`.
- Each file declares:
  - `agents:` block with:
    - `enabled: list[AgentName]` — the agent roster active on this trigger
    - `overrides: dict[AgentName, dict[str, Any]]` — shallow-override map; keys are agent names, values are a subset of `agents.yaml` fields to override (e.g., `latency_budget_seconds`, `output_token_budget`, `cumulative_tool_call_limit`, `cumulative_tool_token_budget`)
  - `qualitative_researcher:` block with:
    - `news_digest:` block with `top_n_per_sector: int`, `top_n_high_priority: int`
- Population per the table in `configuration-management.md § run_types/<trigger>.yaml`:
  - `pre_open`: full roster (9 agents); adaptive override `cumulative_tool_call_limit: 25`, `cumulative_tool_token_budget: 4000`, `latency_budget_seconds: 300`; news digest `top_n_per_sector: 5`, `top_n_high_priority: 3`
  - `market_hours_rolling`: full roster; adaptive override `cumulative_tool_call_limit: 20`, `cumulative_tool_token_budget: 3000`; news digest `top_n_per_sector: 5`
  - `pre_close`: full roster; adaptive override `cumulative_tool_call_limit: 15`, `cumulative_tool_token_budget: 2500`; news digest `top_n_per_sector: 4`
  - `off_hours_rolling`: roster omits `adaptive_researcher`; news digest `top_n_per_sector: 3`
  - `weekend_saturday`: roster omits `adaptive_researcher`; news digest `top_n_per_sector: 3`
  - `weekend_sunday`: full roster; adaptive override `cumulative_tool_call_limit: 20`, `cumulative_tool_token_budget: 3000`; news digest `top_n_per_sector: 5`
- The three parallel-fire sector researchers, the synthesizer, and the three decision-layer agents are present in every run type's `agents.enabled` list per `configuration-management.md § run_types`.
- `src/alphamind/config/models/run_types.py` defining:
  - `RunType` (StrEnum: the six trigger names — closed set; member values match filename stems)
  - `NewsDigestConfig` (BaseModel: `top_n_per_sector: int = Field(ge=1)`, `top_n_high_priority: int = Field(ge=0)`)
  - `QualitativeResearcherSection` (BaseModel: `news_digest: NewsDigestConfig`)
  - `AgentsSection` (BaseModel: `enabled: list[AgentName]`, `overrides: dict[AgentName, dict[str, Any]] = Field(default_factory=dict)`)
  - `RunTypeConfig` (BaseModel: `agents: AgentsSection`, `qualitative_researcher: QualitativeResearcherSection`)
- A model validator on `AgentsSection.enabled` enforcing:
  - Non-empty list
  - No duplicate agent names
  - The decision-layer trio (`analyst`, `strategist`, `portfolio_manager`) and the synthesizer are present (run-types may not disable the decision layer per `configuration-management.md § run_types`)
- A model validator on `AgentsSection.overrides` enforcing:
  - Every key in `overrides` is also present in `enabled` (no overriding a disabled agent)
  - For each `(agent, override)` pair, every key in the `override` dict is one of the recognized override fields: `latency_budget_seconds`, `context_token_budget`, `output_token_budget`, `cumulative_tool_call_limit`, `cumulative_tool_token_budget`. Unknown keys raise.
  - `cumulative_tool_call_limit` and `cumulative_tool_token_budget` may only appear under the `adaptive_researcher` override (they are adaptive-only fields per story 03i)
- A loader helper `load_run_types(config_dir: Path) -> dict[RunType, RunTypeConfig]` (extending `src/alphamind/config/loaders.py`) that reads every `run_types/{name}.yaml` for every `RunType` enum member, validates each, and returns a frozen mapping.
- Re-export `RunTypeConfig`, `RunType`, `NewsDigestConfig`, `QualitativeResearcherSection`, `AgentsSection`, and `load_run_types` from `models/__init__.py` / `loaders` namespace.
- Unit tests covering: every shipped run-type file parses cleanly; a YAML omitting `analyst` from `enabled` raises; a YAML with `overrides.adaptive_researcher.unknown_field` raises; a YAML with `overrides.analyst.cumulative_tool_call_limit` raises (adaptive-only field on non-adaptive agent); a YAML with `overrides.qualitative_researcher` declared while `qualitative_researcher` is not in `enabled` raises.

Out of scope:
- Cross-reference — every `RunType` member has a matching `triggers:` entry in `scheduler.yaml` (story 06a).
- Composition — applying the run-type's roster + budget overrides to the resolved config (story 05).
- Run-type prompt injection — explicitly out of scope per `configuration-management.md § run_types`. The story does not encode any "run_type" string into agent prompts.
- Coupling between `news_digest` knobs and the qualitative researcher's behavior — the model carries the values; the researcher consumes them.

## Notes

**Strict closed roster.** The `enabled` list is `list[AgentName]` (the closed enum from story 03i), so adding a new run-type-eligible agent requires editing the enum first. This is by design: the agent roster is small and stable, and silently accepting an unknown name would cause the resolver to drop it.

**Override field allow-list.** `AgentsSection.overrides` is typed as `dict[AgentName, dict[str, Any]]` because Pydantic's structural validation of nested dict values would force every override to declare every field. The model validator restricts keys to the allow-list at validation time. This is the right tradeoff between expressiveness (operators can set just one field) and safety (typos raise).

**Why `qualitative_researcher` is its own block** rather than nested under `agents`: the design doc structures it that way to make news-digest depth a first-class operator-facing knob, distinct from the agent-runner overrides. Mirror the doc's structure.

**Decision-layer presence requirement.** The validator that asserts `analyst`, `strategist`, `portfolio_manager`, `synthesizer` are in `enabled` encodes a structural rule: run types **may not** disable the decision layer or the synthesizer. The three sector researchers likewise fire on every run type, but the validator does not enforce this — that is policy, not invariant; the `04e` run_type table in `configuration-management.md` is the authoritative source for which agents fire on which trigger.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] All six run-type YAML files exist under `config/run_types/`.
- [ ] `pre_open.yaml`, `market_hours_rolling.yaml`, `pre_close.yaml`, `weekend_sunday.yaml` declare `adaptive_researcher` in `enabled`; `off_hours_rolling.yaml` and `weekend_saturday.yaml` omit it.
- [ ] Every run-type carries `analyst`, `strategist`, `portfolio_manager`, `synthesizer` in `enabled`.
- [ ] News-digest values per the table in `configuration-management.md § run_types`.
- [ ] `src/alphamind/config/models/run_types.py` defines `RunType`, `RunTypeConfig`, `NewsDigestConfig`, `QualitativeResearcherSection`, `AgentsSection`.
- [ ] `src/alphamind/config/loaders.py` defines `load_run_types(config_dir: Path) -> dict[RunType, RunTypeConfig]`.
- [ ] `models/__init__.py` re-exports the names.
- [ ] A unit test asserts every shipped run-type file parses cleanly via `load_run_types()` and the resulting mapping has six entries.
- [ ] A unit test asserts a run-type YAML omitting `analyst` from `enabled` raises `ValidationError`.
- [ ] A unit test asserts a run-type YAML omitting `synthesizer` from `enabled` raises `ValidationError`.
- [ ] A unit test asserts `enabled` containing duplicates raises `ValidationError`.
- [ ] A unit test asserts `overrides.adaptive_researcher.unknown_field: 1` raises `ValidationError`.
- [ ] A unit test asserts `overrides.analyst.cumulative_tool_call_limit: 25` raises `ValidationError`.
- [ ] A unit test asserts `overrides.adaptive_researcher.latency_budget_seconds: 300` is accepted (recognized override field).
- [ ] A unit test asserts an `overrides:` key not present in `enabled` raises `ValidationError`.
- [ ] A unit test asserts `news_digest.top_n_per_sector: 0` raises `ValidationError`.
- [ ] A unit test asserts `load_run_types()` raises when a run-type file is missing.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
