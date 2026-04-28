---
status: done
completed_date: 2026-04-27
commit_id: 6f3a354
---

# 06a — Cross-reference validators

## Goal

Implement the cross-reference validation layer per `configuration-management.md § Validation`. Cross-reference checks span multiple YAML files and assert that every name reference resolves: every `active_profile` names an existing profile file, every rule ID in a profile's `rule_values` exists in `guardrails.yaml`, every `*_env` resolves to a key in `.env`, every tool name in an agent's allowlist is a registered tool, every trigger key in `scheduler.yaml` has a matching `run_types/{key}.yaml`, and so on.

These checks live separately from per-file Pydantic validation (stories 03*–04*) because each Pydantic model can only see its own file's content. Cross-reference checks need every file at once and run at config-load time after all per-file parsing has succeeded.

## Reading

- `docs/design/configuration-management.md` § Validation — full enumeration of cross-reference checks
- `src/alphamind/config/models/main.py`, `profiles.py`, `regimes.py`, `modes.py`, `overlays.py`, `run_types.py`, `agents.py`, `guardrails.py`, `assets.py`, `scheduler.py`, `venue.py` (post-stories 03b–03i, 04a–04e)
- `src/alphamind/config/resolver.py` (post-story-05) — the resolver assumes inputs are validated; this story is one of two that satisfies that assumption
- `src/alphamind/config/loaders.py` (post-stories 04a–04e) — bundle-mapping accessors
- `dotenv.dotenv_values` from `python-dotenv` — already a dependency; this story uses it to enumerate `.env` keys

## Depends on

- 03a–03i (every flat-tail model)
- 04a–04e (every bundle directory)
- 05 (resolver — the cross-reference validator runs after the cascade in 08, but uses the same model objects)

## Scope

In scope:
- `src/alphamind/config/validation/cross_reference.py` defining `validate_cross_references(...)` — a single entry point that runs every cross-reference check against the loaded YAML tree. Signature:
  ```python
  def validate_cross_references(
      *,
      main: MainConfig,
      scheduler: SchedulerConfig,
      venue: VenueConfig,
      guardrails: GuardrailsConfig,
      agents: AgentsConfig,
      assets: AssetsConfig,
      profiles: dict[Profile, ProfileConfig],
      regimes: dict[Regime, RegimeConfig],
      modes: dict[Mode, ModeConfig],
      overlays: dict[Overlay, PreEventOverlay | StressOverlay],
      run_types: dict[RunType, RunTypeConfig],
      env_keys: frozenset[str],
      registered_tools: frozenset[str],
  ) -> None
  ```
- A `CrossReferenceError(Exception)` class. Each check that fails raises a `CrossReferenceError` whose message names the offending key, the file it appears in, and what reference failed.
- The function aggregates failures rather than fast-failing — it runs every check and raises one `CrossReferenceError` carrying every failure as a list. Operators editing the YAML want all issues at once.
- Concrete checks (one per bullet — match the design doc's `§ Validation` enumeration):
  1. `main.active_profile` is a key in `profiles` (the loaded `dict[Profile, ProfileConfig]` mapping is the source of truth).
  2. For every `Profile p` in `profiles`: every entry in `profiles[p].active_sectors` is a key in `assets.sectors`.
  3. For every `Profile p`: every key in `profiles[p].rule_values` is a `RuleEntry.id` in `guardrails.rules`.
  4. For every `Regime r` in `regimes`: every key in `regimes[r].multipliers` is a `RuleEntry.id` in `guardrails.rules`.
  5. For every `Regime r`: every rule ID present in any `profiles[p].rule_values` (across all profiles) is a key in `regimes[r].multipliers`. (Regime multiplier tables must cover the union of rules used by any profile — a profile that uses options rules requires every regime to define multipliers for them.)
  6. For every overlay: every key in its `multipliers` is a `RuleEntry.id` in `guardrails.rules`.
  7. For every provider in `venue.alpaca`: `api_key_env` and `api_secret_env` are present in `env_keys`.
  8. For every `AgentName a` in `agents.agents`: every tool name in `agents.agents[a].tools` (and, for the adaptive researcher, every key in `tool_caps`) is a member of `registered_tools`.
  9. For every trigger key in `scheduler.triggers`: there is a matching `RunType` in `run_types` (the `RunType` enum closes the set, so a trigger key not in the enum will already have been caught at parse time; this check verifies the YAML files for every enum member are present).
  10. For every `RunType rt` in `run_types`: every `AgentName a` in `run_types[rt].agents.enabled` is a key in `agents.agents` (already enforced by the `AgentName` enum at parse time, but re-asserted here as a defensive check covering YAML edits that bypass parsing).
  11. For every `RunType rt` and every override agent `a` in `run_types[rt].agents.overrides`: every override field is a recognized field on `agents.agents[a]`'s Pydantic model. Already enforced at parse time per story 04e; re-asserted here.
- A loader helper `read_env_keys(env_path: Path) -> frozenset[str]` (in `src/alphamind/config/loaders.py`) that reads `.env` via `dotenv.dotenv_values` and returns the key set. Used by the validator's caller.
- A registered-tool registry placeholder: `src/alphamind/config/tools.py` defining `REGISTERED_TOOLS: frozenset[str]` containing the tool names from the existing decision/adaptive specs (`retrieve_brief`, `validate_guardrail`, `get_thesis_components`, `submit_envelope`, `news_search`, `ticker_deep_pull`, `social_sentiment`, `prediction_markets`, `options_flow`, `sec_lending`, `short_interest`, `earnings_calendar`, `macro_data`). The set is module-level constant; runtime tool registration is out of scope.
- Re-export `validate_cross_references`, `CrossReferenceError`, `read_env_keys`, `REGISTERED_TOOLS` from `models/__init__.py` / `loaders` / `tools` namespaces.
- Unit tests covering one happy-path call (every shipped YAML passes) and one failure case per check listed above. The failure-case fixtures construct minimal Pydantic-valid model instances with one cross-reference broken; assert the validator raises with a message naming the broken reference.

Out of scope:
- Semantic self-test invariants (story 06b).
- The `.env` file itself — the validator reads keys, not values.
- Runtime tool dispatch — `REGISTERED_TOOLS` is a static constant for validation; the runtime tool harness owns dispatch.

## Notes

**Failure aggregation pattern.** The function builds a `list[str]` of failure messages, one per failing check. After running all checks, if the list is non-empty, raise `CrossReferenceError("\n".join(failures))`. This is more useful than fast-failing — operators want to see every broken reference at once.

**Why `env_keys` and `registered_tools` are parameters, not module-level reads.** Pure-function discipline (mirrors story 05's resolver). The caller (story 08's loader) reads `.env` and passes the keys in.

**Defensive re-checks for already-enforced invariants.** Several checks listed (10, 11) are already enforced at Pydantic parse time via the closed `AgentName` enum. Re-asserting them in cross-reference is cheap insurance: if a future YAML edit bypasses parse-time validation (unlikely but possible — e.g., a hand-crafted Pydantic instance fed directly), the cross-reference layer still catches the divergence.

**`REGISTERED_TOOLS` content.** The thirteen tool names listed in Scope come from `analyst.md`, `pm.md`, `strategist.md`, and `adaptive-research.md`. Verify against those four docs at implementation time; do not transcribe blindly — the analysis of which tools are registered is the source of truth, not the configuration-management worked example. The registered-tool set may grow in future stories; this story freezes the v1 set.

**`tool_caps` keys for adaptive researcher.** Per story 03i, `AdaptiveAgentConfig.tool_caps` is a dict keyed by tool name. Cross-reference checks every tool-cap key, in addition to the entries in `AgentsConfig.agents[a].tools`.

## Acceptance criteria

- [ ] `src/alphamind/config/validation/cross_reference.py` exists and defines `CrossReferenceError` and `validate_cross_references(...)`.
- [ ] `src/alphamind/config/loaders.py` exposes `read_env_keys(env_path: Path) -> frozenset[str]`.
- [ ] `src/alphamind/config/tools.py` exists and defines `REGISTERED_TOOLS: frozenset[str]` carrying the thirteen tool names listed in Scope.
- [ ] `models/__init__.py` re-exports `validate_cross_references`, `CrossReferenceError`, `REGISTERED_TOOLS`.
- [ ] A unit test asserts the shipped YAML tree passes `validate_cross_references` without raising.
- [ ] A unit test asserts `main.active_profile` set to a profile not in `profiles` raises `CrossReferenceError`.
- [ ] A unit test asserts an `active_sectors` entry not present in `assets.sectors` raises.
- [ ] A unit test asserts a `rule_values` key not present in `guardrails` raises.
- [ ] A unit test asserts a regime's `multipliers` missing a rule used by some profile raises.
- [ ] A unit test asserts an overlay's `multipliers` key not in `guardrails` raises.
- [ ] A unit test asserts a `venue.alpaca.paper.api_key_env` not in the `env_keys` set raises.
- [ ] A unit test asserts an agent's `tools` entry not in `REGISTERED_TOOLS` raises.
- [ ] A unit test asserts an `adaptive_researcher.tool_caps` key not in `REGISTERED_TOOLS` raises.
- [ ] A unit test asserts the validator aggregates failures: feeding multiple broken references at once produces a single `CrossReferenceError` whose message names every one.
- [ ] A unit test asserts `read_env_keys` reads a fixture `.env` and returns the expected key set.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
