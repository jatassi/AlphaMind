---
status: not_started
completed_date:
commit_id:
---

# 02 — Agent configuration scaffolding

## Goal

Land the YAML configuration the three domain researcher agents read at invocation start: per-agent model assignment, system-prompt path, token budget, allowed-tools allowlist (empty for domain researchers), plus the sector-to-ticker assignment that scopes each agent's input bundle.

## Reading

- `docs/design/configuration-management.md` — particularly `config/agents.yaml` and the cross-reference / semantic-self-test invariants
- `docs/architecture/llm-integration.md` § Agent inventory — model column for analysis-layer agents (Sonnet for sector analysts) and `Tool access: None`
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Output — token budget 400–800
- `docs/design/03-analysis-layer/domain-researchers/financials.md` § Output — token budget 300–600
- `docs/design/03-analysis-layer/domain-researchers/energy.md` § Output — token budget 300–600
- `docs/design/asset-universe-validation.md` — universe configuration shape; `config/universe.yaml` is the authoritative ticker list
- `config/universe.yaml` — already exists; do not modify (sector membership lives there if present, or a sibling sector-mapping file)

## Depends on

None.

## Scope

In scope:
- `config/agents.yaml` — the registry doc names this file in the configuration-management spec but it does not yet exist. Create it with one entry per agent in the analysis-layer agent inventory (initial entries cover the three domain researchers; sibling agents — qualitative, adaptive, synthesizer, plus decision-layer agents — are added by their respective work trees as they land). Each domain researcher entry must specify:
  - `model` — `sonnet` per the inventory.
  - `system_prompt_path` — relative to repo root, pointing at the file produced by stories 09a/9b/9c (`prompts/analysis/tech_semis_researcher.md`, `prompts/analysis/financials_researcher.md`, `prompts/analysis/energy_researcher.md`).
  - `output_token_budget` — `800` for tech/semis, `600` for financials, `600` for energy (upper end of the per-agent range; the LLM generates within budget).
  - `allowed_tools` — empty list (domain researchers receive their inputs as a static bundle and emit a single brief; no tool calls).
  - `sector` — one of `tech_semis` | `financials` | `energy`.
- `config/sectors.yaml` (new) — mapping of sector to ticker list. Source the assignments from the design docs' opening lines (~35 tech/semis, ~15 financials, ~15 energy). Each entry: `sector: { tickers: [LIST], description: str }`. The actual ticker membership comes from `config/universe.yaml`'s sector tagging if that file already carries `sector_classification` keys; otherwise this file is the authoritative source. Document which is authoritative in a top-of-file comment.
- Pydantic schema models under `src/alphamind/config/models.py` (extend the existing module from the data-layer collector work tree, not a new module) — one model per new YAML file:
  - `AgentConfig` and `AgentRegistry` for `agents.yaml`.
  - `SectorConfig` and `SectorRegistry` for `sectors.yaml`.
- Loader integration: extend `_common.load_config()` (from the data-layer collector work tree) to read both new YAML files and expose them on the aggregate config object.
- Cross-reference validators implemented in the Pydantic models (per `configuration-management.md` § Cross-reference):
  - Every domain researcher agent's `system_prompt_path` resolves to an existing file (path check at config-load time, not just string check).
  - Every `sector` value in `agents.yaml` matches a key in `sectors.yaml`.
  - Every ticker listed under a sector in `sectors.yaml` exists in `config/universe.yaml`'s ticker list. (Resolves the universe → sector consistency invariant.)
  - Every `model` is one of the allowed-models list (`sonnet`, `opus`, `haiku`).
- Unit tests verifying each YAML parses cleanly, cross-validators reject the documented violations (missing prompt file, unknown sector, unknown ticker, unknown model), and the aggregate config object exposes the new sections.

Out of scope:
- Other agent entries (qualitative, adaptive, synthesizer, analyst, strategist, PM) — added by their respective implementation work trees as they land.
- The `config/universe.yaml` schema itself — owned by the asset-universe-validation work (already specified, not yet implemented as Pydantic).
- Runtime loading of system prompts (story 07).
- Per-invocation feature-flag composition — `agents.yaml` is universe-wide flat configuration, not part of the profile/regime/mode/overlay composition resolver.

## Notes

The Pydantic models live under the existing `src/alphamind/config/models.py` rather than a new file because every config file's models live there per story 03a of the collector work tree. Adding new model classes to the existing module is consistent with that pattern.

Whether `config/universe.yaml` carries sector tagging today: if it does, `sectors.yaml` mirrors it (with a comment naming `universe.yaml` as authoritative); if it doesn't, `sectors.yaml` is authoritative and an out-of-scope follow-up consolidates the two. The Pydantic cross-validator should detect drift either way.

The system_prompt path-resolution check is a Pydantic validator that opens the file (or calls `Path.is_file()`) at config-load time — not a string check. This catches typos and missing files before the agent invocation tries to load the prompt and fails inside the SDK call.

`output_token_budget` is the SDK's `max_tokens` parameter. The design docs name a range (e.g., 400–800 tokens for tech/semis); use the upper end so the agent has headroom and only hits the cap on truncation. Story 07's harness uses this value when constructing `ClaudeAgentOptions`.

The `allowed_tools` field exists for the schema's sake — domain researchers have no tools — and is consumed by the harness when constructing the options. Future work-trees building the qualitative / adaptive / decision agents populate the same field with their tool inventories.

## Acceptance criteria

- [ ] `config/agents.yaml` exists with three domain researcher entries (`tech_semis_researcher`, `financials_researcher`, `energy_researcher`) carrying `model`, `system_prompt_path`, `output_token_budget`, `allowed_tools`, `sector`.
- [ ] `config/sectors.yaml` exists with three sector entries (`tech_semis`, `financials`, `energy`) carrying `tickers` and `description`.
- [ ] `src/alphamind/config/models.py` defines `AgentConfig`, `AgentRegistry`, `SectorConfig`, `SectorRegistry`.
- [ ] `_common.load_config()` returns an aggregate config object exposing both `agents` and `sectors` sections.
- [ ] Pydantic validators reject an `agents.yaml` entry whose `system_prompt_path` does not resolve to an existing file.
- [ ] Pydantic validators reject an `agents.yaml` entry whose `sector` is not a key in `sectors.yaml`.
- [ ] Pydantic validators reject a `sectors.yaml` entry whose `tickers` list contains a ticker not present in `config/universe.yaml`.
- [ ] Pydantic validators reject an unknown `model` value in `agents.yaml`.
- [ ] Unit tests cover all of the above using fixture YAML files written under a temp directory.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
