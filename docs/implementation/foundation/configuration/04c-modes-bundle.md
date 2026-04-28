---
status: done
completed_date: 2026-04-27
commit_id: 6c92ad3
---

# 04c — `modes/` bundle (normal / halt)

## Goal

Land the two mode files under `config/modes/` and a Pydantic model that validates each one. Modes are runtime-resolved by pipeline state — `halt` activates on daily-drawdown halt or cumulative-drawdown tier-3 full halt; `normal` is the default. Each file declares behavioral transforms applied to decision-layer agents: action-vocabulary restriction, output-mode flag, default treatment of pending orders.

## Reading

- `docs/design/configuration-management.md` § `modes/halt.yaml` — schema and worked example
- `docs/design/06-risk-guardrails/state-delivery.md` § Halt-mode header modifications — full per-agent behavioral contract
- `docs/design/06-risk-guardrails/breach-behavior.md` § Agent behavior during halt mode — narrative contract for what each agent does in halt
- `docs/design/04-decision-layer/strategist.md` § Halt mode and defensive-posture behavior — strategist's halt contract
- `docs/design/04-decision-layer/analyst.md` § Halt mode behavior (if present) — analyst's watchlist mode contract
- `src/alphamind/config/models/agents.py` (post-story-03i) — `AgentName` enum the model references
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)
- 03i (agents.yaml — `AgentName` enum closes the agent-keying set)

## Scope

In scope:
- `config/modes/normal.yaml`, `config/modes/halt.yaml`.
- `normal.yaml` is the **passthrough** mode: it declares per-agent blocks but every behavioral field is `null` or carries the unrestricted default. The model carries the explicit declaration so the resolver's mode-transform step is uniform across modes (no special-casing `normal`).
- `halt.yaml` populated with the design-doc worked example:
  - `analyst:` block with `output_mode: watchlist`
  - `strategist:` block with `output_mode: defensive_posture`, `allowed_actions: [hold, reduce, close, adjust-bracket]`, `pending_orders_default: cancel`
  - `pm:` block with `allowed_command_types: [CLOSE, ADJUST, CANCEL]`, `emphasis: capital_preservation`
- `src/alphamind/config/models/modes.py` defining:
  - `Mode` (StrEnum: `normal`, `halt` — closed set; member values match filename stems)
  - `AnalystOutputMode` (StrEnum: `proposals`, `watchlist`)
  - `StrategistOutputMode` (StrEnum: `normal`, `defensive_posture`)
  - `StrategistAction` (StrEnum: `hold`, `reduce`, `close`, `adjust-bracket`, `add`) — the closed action vocabulary
  - `PendingOrdersDefault` (StrEnum: `maintain`, `cancel`)
  - `CommandType` (StrEnum: `OPEN`, `CLOSE`, `ADJUST`, `CANCEL`, `ADD`) — uppercase per the OMS command schema
  - `PmEmphasis` (StrEnum: `normal`, `capital_preservation`)
  - `AnalystMode` (BaseModel: `output_mode: AnalystOutputMode`)
  - `StrategistMode` (BaseModel: `output_mode: StrategistOutputMode`, `allowed_actions: list[StrategistAction]`, `pending_orders_default: PendingOrdersDefault`)
  - `PmMode` (BaseModel: `allowed_command_types: list[CommandType]`, `emphasis: PmEmphasis`)
  - `ModeConfig` (BaseModel: `analyst: AnalystMode`, `strategist: StrategistMode`, `pm: PmMode`)
- A model validator on `StrategistMode.allowed_actions` and `PmMode.allowed_command_types` enforcing non-empty list, no duplicates.
- A loader helper `load_modes(config_dir: Path) -> dict[Mode, ModeConfig]` (extending `src/alphamind/config/loaders.py`) that reads every `modes/{name}.yaml` for every `Mode` enum member, validates each, and returns a frozen mapping.
- Re-export `ModeConfig`, `Mode`, the four output-mode/action/command/emphasis enums, the three per-agent sub-models, and `load_modes` from `models/__init__.py` / `loaders` namespace.
- Unit tests covering: every shipped mode file parses cleanly; halt mode's `pm.allowed_command_types` does not include `OPEN` or `ADD` per the design; an unknown action enum value raises; an empty `allowed_actions` list raises; duplicate action entries raise.

Out of scope:
- Cross-reference — the per-agent block names (analyst, strategist, pm) are model fields not validated against `AgentName` because the mode contract is decision-layer-specific (analysis-layer agents are not mode-affected per `state-delivery.md § Halt-mode header modifications`). The mismatch is intentional; do not enforce coverage.
- Composition — applying a mode's behavioral transform to the resolved config (story 05).
- Halt-mode trigger logic (which drawdown levels activate halt) — owned by `breach-behavior.md`, not config.
- Emergency-invocation header overlay — that is a separate concern from mode and is owned by `state-delivery.md § Emergency invocation header`; not encoded as a mode bundle.

## Notes

**Why `normal` is explicit.** Storing a passthrough `normal.yaml` keeps the resolver's mode application uniform and avoids `if mode == "normal": skip` branches. The cost is two files instead of one. Worth it for the simpler arithmetic.

**For `normal.yaml`, what values to use.**
- `analyst.output_mode: proposals` (the unrestricted default).
- `strategist.output_mode: normal`, `allowed_actions: [hold, reduce, close, adjust-bracket, add]` (full vocabulary), `pending_orders_default: maintain`.
- `pm.allowed_command_types: [OPEN, CLOSE, ADJUST, CANCEL, ADD]`, `emphasis: normal`.

**The decision-layer-only scope** is intentional. Halt mode does not transform analysis-layer agents — they continue running their normal pipeline; the synthesizer brief still feeds the decision agents. Halt-mode behavior is entirely a decision-layer concern.

**`StrategistAction` uses hyphenated `adjust-bracket`** to match the design doc's worked example (`allowed_actions: [hold, reduce, close, adjust-bracket]`). Pydantic StrEnum members can carry hyphens via `class StrategistAction(StrEnum): adjust_bracket = "adjust-bracket"`. The Python identifier is `adjust_bracket`, the YAML/string value is `adjust-bracket`.

**`CommandType` uppercase** matches the OMS command schema's command-type vocabulary (`OPEN` / `CLOSE` / `ADJUST` / `CANCEL` / `ADD`). Do not normalize to lowercase.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/modes/normal.yaml` and `config/modes/halt.yaml` exist.
- [ ] `normal.yaml` declares the passthrough/unrestricted defaults for each per-agent block.
- [ ] `halt.yaml` declares the restricted defaults from the design doc (watchlist for analyst, defensive_posture for strategist with reduced action vocabulary and `pending_orders_default: cancel`, restricted command types for PM with `emphasis: capital_preservation`).
- [ ] `src/alphamind/config/models/modes.py` defines `Mode`, `ModeConfig`, plus the seven enums and three sub-models listed in Scope.
- [ ] `src/alphamind/config/loaders.py` defines `load_modes(config_dir: Path) -> dict[Mode, ModeConfig]`.
- [ ] `models/__init__.py` re-exports the names.
- [ ] A unit test asserts both mode files parse cleanly via `load_modes()`.
- [ ] A unit test asserts halt mode's `pm.allowed_command_types` excludes `OPEN` and `ADD`.
- [ ] A unit test asserts `strategist.allowed_actions: []` raises `ValidationError`.
- [ ] A unit test asserts `strategist.allowed_actions: [hold, hold]` raises `ValidationError`.
- [ ] A unit test asserts `strategist.allowed_actions: [pivot]` raises `ValidationError`.
- [ ] A unit test asserts `pm.allowed_command_types: [open]` (lowercase) raises `ValidationError`.
- [ ] A unit test asserts `analyst.output_mode: trade` raises `ValidationError`.
- [ ] A unit test asserts the loader maps the YAML field `adjust-bracket` to enum member `StrategistAction.adjust_bracket`.
- [ ] A unit test asserts `load_modes()` raises when a mode file is missing.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
