---
status: not_started
completed_date:
commit_id:
---

# 04a — `profiles/` bundle (micro / small / medium / large)

## Goal

Land the four profile files under `config/profiles/` and a Pydantic model that validates each one. Profiles are operator-pinned identity dimensions: each names which features are enabled, which sectors are active, which guardrail rule values apply, and what token-budget ranges decision agents observe at this capital tier.

## Reading

- `docs/design/configuration-management.md` § `profiles/medium.yaml` — schema and worked example for one profile (the others follow the same shape)
- `docs/design/06-risk-guardrails/rules-and-limits.md` — full per-profile rule values and feature flags for all four profiles (micro, small, medium, large) with rationale
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Profile comparison summary — the closed feature/rule matrix per profile
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Transitioning between profiles — `capital_range_usd` semantics
- `src/alphamind/config/models/guardrails.py` (post-story-03f) — `RuleEntry` and the canonical 19 rule IDs
- `src/alphamind/config/models/main.py` (post-story-03b) — `Profile` enum closing the set
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)
- 03b (main.yaml — `Profile` enum closes the set of valid filenames)
- 03f (guardrails.yaml — rule IDs the profile's `rule_values` keys must match)
- 03a (assets.yaml — `active_sectors` validates against sector keys at composition time, not parse time)
- 03i (agents.yaml — `agent_token_budgets` keys validate against `AgentName`)

## Scope

In scope:
- `config/profiles/micro.yaml`, `config/profiles/small.yaml`, `config/profiles/medium.yaml`, `config/profiles/large.yaml` — one file per `Profile` enum member, populated with the values from `rules-and-limits.md`.
- Each profile file declares:
  - `capital_range_usd: [int, int]` (lower, upper)
  - `risk_priority: str` (one of `signal_quality`, `concentration_management`, `exposure_management`, `exposure_and_execution_management` — see Notes)
  - `feature_flags:` block with `options_enabled: bool`, `short_selling_enabled: bool`, `fractional_shares_required: bool`
  - `active_sectors: list[str]` (subset of the master sector list maintained in `assets.yaml`)
  - `min_position_size_usd: int`
  - `rule_values:` map keyed by guardrail rule ID (the suffixed convention from story 03f), values numeric. Profiles MUST omit rules disabled by their feature flags (no zeroed-out residue) per `configuration-management.md § Feature flag semantics`.
  - `agent_token_budgets:` map keyed by decision agent name (`strategist`, `pm` from the worked example; analyst is also a decision agent and should be included for consistency), each value a `{context: [int, int], output: [int, int]}` block.
- `src/alphamind/config/models/profiles.py` defining:
  - `RiskPriority` (StrEnum: the four members listed above)
  - `FeatureFlags` (BaseModel: `options_enabled: bool`, `short_selling_enabled: bool`, `fractional_shares_required: bool`)
  - `TokenBudgetRange` (BaseModel: `context: tuple[int, int]`, `output: tuple[int, int]` — both with `(lower, upper)` semantics, lower ≤ upper, both ≥ 1)
  - `ProfileConfig` (BaseModel: `capital_range_usd: tuple[int, int]`, `risk_priority: RiskPriority`, `feature_flags: FeatureFlags`, `active_sectors: list[str]`, `min_position_size_usd: int = Field(ge=1)`, `rule_values: dict[str, float]`, `agent_token_budgets: dict[str, TokenBudgetRange]`)
- A model validator on `ProfileConfig` enforcing:
  - `capital_range_usd[0] < capital_range_usd[1]`, both ≥ 0
  - For every `TokenBudgetRange` value: `context[0] ≤ context[1]` and `output[0] ≤ output[1]`
  - `active_sectors` is non-empty and contains no duplicates; every entry matches `^[a-z][a-z0-9_]*$`
  - `rule_values` is non-empty; every key matches `^[a-z][a-z0-9_]*$`
- A loader helper `load_profiles(config_dir: Path) -> dict[Profile, ProfileConfig]` (in `src/alphamind/config/loaders.py`, a new module) that reads every `profiles/{name}.yaml` for every `Profile` enum member, validates each, and returns a frozen mapping. Missing files raise; the helper is the entry point story 05 (resolver) consumes.
- Re-export `ProfileConfig`, `RiskPriority`, `FeatureFlags`, `TokenBudgetRange`, and `load_profiles` from `models/__init__.py` (and `loaders` for `load_profiles`).
- Unit tests covering: every shipped profile parses cleanly; `capital_range_usd: [50000, 25000]` (inverted) raises; `agent_token_budgets.pm.context: [2500, 1500]` raises; an empty `active_sectors` raises; a `rule_values` key with capital letters raises.

Out of scope:
- Cross-reference — every key in `rule_values` exists in `guardrails.yaml`; every entry in `active_sectors` exists in `assets.yaml.sectors`; every key in `agent_token_budgets` is a member of `AgentName`. Story 06a.
- Semantic invariants — profiles' `capital_range_usd` ranges do not overlap; feature-flag closure (no options rules in a profile with `options_enabled: false`). Story 06b.
- Profile-transition mechanics (operator-pinned, manual edit) — owned by `rules-and-limits.md`, not implemented here.
- Composition arithmetic (apply regime multipliers etc.) — story 05.

## Notes

**Risk-priority StrEnum.** `rules-and-limits.md` describes risk priorities as full-prose phrases: "signal quality" (micro), "concentration management" (small), "exposure management" (medium), "exposure plus execution" (large). The enum member values are normalized snake_case (`signal_quality`, `concentration_management`, `exposure_management`, `exposure_and_execution_management`). The values are operator-facing — keep them readable.

**Profile rule sets are not uniform.** Micro and small omit options and short rules (those features are disabled). Medium and large carry all 19. The model does not enforce this — it is enforced at the cross-reference / feature-flag-closure layer (06a/06b). The model accepts any dict-shaped `rule_values`.

**`agent_token_budgets` shape.** The worked example shows ranges `[context_low, context_high]`. The model uses `tuple[int, int]` to capture the pair. The runner consumes the upper bound when sizing the SDK call; the lower bound is review-surface metadata. Do not narrow the model to a single int.

**Per-profile values from `rules-and-limits.md`.** The doc carries authoritative tables for each profile. Quote them verbatim — no rounding, no extrapolation. If a profile's table omits a rule (because the feature is disabled), omit the key from `rule_values` rather than zeroing it.

**`load_profiles` belongs in a new module** (`src/alphamind/config/loaders.py`) rather than in `_common.py` (the data-sources collector module) because the loaders are pipeline-side, not collector-side. Keep `_common.py` strictly within the data-sources scope. Story 08 ties everything together at a higher boundary.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/profiles/micro.yaml`, `small.yaml`, `medium.yaml`, `large.yaml` exist.
- [ ] Each profile file declares `capital_range_usd`, `risk_priority`, `feature_flags`, `active_sectors`, `min_position_size_usd`, `rule_values`, `agent_token_budgets`.
- [ ] Micro: `options_enabled: false`, `short_selling_enabled: false`, `active_sectors: [tech, semis]`. No options/short rules in `rule_values`.
- [ ] Small: `options_enabled: false`, `short_selling_enabled: false`, `active_sectors: [tech, semis, financials]`. No options/short rules.
- [ ] Medium: `options_enabled: true`, `short_selling_enabled: true`, `active_sectors: [tech, semis, financials, energy]`. All 19 rules present.
- [ ] Large: same flags and sectors as medium; all 19 rules; values match medium with proportional adjustments per `rules-and-limits.md`.
- [ ] `src/alphamind/config/models/profiles.py` defines `ProfileConfig`, `RiskPriority`, `FeatureFlags`, `TokenBudgetRange`.
- [ ] `src/alphamind/config/loaders.py` defines `load_profiles(config_dir: Path) -> dict[Profile, ProfileConfig]`.
- [ ] `models/__init__.py` re-exports the four model names; the new `loaders.py` symbol is importable as `alphamind.config.loaders.load_profiles`.
- [ ] A unit test asserts every shipped profile file parses cleanly via `load_profiles()` and the resulting mapping has four entries.
- [ ] A unit test asserts `capital_range_usd: [50000, 25000]` raises `ValidationError`.
- [ ] A unit test asserts `agent_token_budgets.pm.context: [2500, 1500]` raises `ValidationError`.
- [ ] A unit test asserts an empty `active_sectors` list raises `ValidationError`.
- [ ] A unit test asserts a `rule_values` key containing capital letters raises `ValidationError`.
- [ ] A unit test asserts duplicate entries in `active_sectors` raise `ValidationError`.
- [ ] A unit test asserts `load_profiles` raises when a profile file is missing from the directory.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
