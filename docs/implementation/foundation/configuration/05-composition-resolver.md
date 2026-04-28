---
status: in_progress
completed_date:
commit_id:
---

# 05 — Composition resolver

## Goal

Implement the composition resolver: a pure function that takes the loaded YAML tree plus the runtime-resolved identity dimensions (active regime, active mode, active overlays, firing trigger) and produces a single `ResolvedConfig` snapshot. The resolver applies the cascade per `configuration-management.md § Composition model` — profile base → regime multipliers → overlay multipliers (multiplicative) → mode behavioral transform → run-type roster filter and budget clamp — and produces a frozen, hash-stable object the engine and agents consume for the duration of the invocation.

## Reading

- `docs/design/configuration-management.md` § Composition model — cascade order, scope of each dimension
- `docs/design/configuration-management.md` § Feature flag semantics — closure (not masking) requirement
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Parameter sets per regime — multiplicative arithmetic
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Override conditions — overlay arithmetic
- `docs/design/06-risk-guardrails/state-delivery.md` § Halt-mode header modifications — what the mode transform restricts
- `src/alphamind/config/models/profiles.py`, `regimes.py`, `modes.py`, `overlays.py`, `run_types.py`, `agents.py`, `guardrails.py` (post-stories 03f, 03i, 04a–04e)
- `src/alphamind/config/loaders.py` (extended across 04a–04e) — how the resolver acquires the bundle mappings

## Depends on

- 04a (profiles)
- 04b (regimes)
- 04c (modes)
- 04d (overlays)
- 04e (run_types)

## Scope

In scope:
- `src/alphamind/config/resolver.py` defining:
  - `ResolvedConfig` (frozen dataclass): the resolver's output. Fields:
    - `profile: Profile`
    - `regime: Regime`
    - `mode: Mode`
    - `active_overlays: tuple[Overlay, ...]` (ordered by application)
    - `run_type: RunType`
    - `feature_flags: FeatureFlags` (closed cascade per profile + mode)
    - `rule_values: dict[str, float]` (composed per-rule limits after profile × regime × overlay multiplication)
    - `agent_token_budgets: dict[AgentName, TokenBudgetRange]` (from profile, surfaced as a typed map)
    - `enabled_agents: tuple[AgentName, ...]` (from run-type roster, ordered)
    - `agent_overrides: dict[AgentName, dict[str, Any]]` (from run-type, validated keys)
    - `analyst_output_mode: AnalystOutputMode`, `strategist_output_mode: StrategistOutputMode`, `strategist_allowed_actions: tuple[StrategistAction, ...]`, `pending_orders_default: PendingOrdersDefault`, `pm_allowed_command_types: tuple[CommandType, ...]`, `pm_emphasis: PmEmphasis` (from mode bundle)
    - `news_digest_top_n_per_sector: int`, `news_digest_top_n_high_priority: int` (from run-type)
    - `paths: Paths`, `execution_mode: ExecutionMode` (from main.yaml)
    - `scheduler: SchedulerConfig`, `venue: VenueConfig`, `execution: ExecutionConfig`, `guardrails: GuardrailsConfig`, `llm_failure: LLMFailureConfig`, `digest: DigestConfig`, `assets: AssetsConfig`, `agents: AgentsConfig` (passed through unchanged from the corresponding flat-tail files)
  - `compose_config(...) -> ResolvedConfig` (the resolver entry point). Signature:
    ```python
    def compose_config(
        *,
        main: MainConfig,
        scheduler: SchedulerConfig,
        venue: VenueConfig,
        execution: ExecutionConfig,
        guardrails: GuardrailsConfig,
        llm_failure: LLMFailureConfig,
        digest: DigestConfig,
        assets: AssetsConfig,
        agents: AgentsConfig,
        profiles: dict[Profile, ProfileConfig],
        regimes: dict[Regime, RegimeConfig],
        modes: dict[Mode, ModeConfig],
        overlays: dict[Overlay, PreEventOverlay | StressOverlay],
        run_types: dict[RunType, RunTypeConfig],
        active_regime: Regime,
        active_mode: Mode,
        active_overlays: tuple[Overlay, ...],
        firing_trigger: RunType,
    ) -> ResolvedConfig:
    ```
- The composition arithmetic, in this exact order:
  1. **Profile base.** Look up `profiles[main.active_profile]` → `ProfileConfig`. Take its `rule_values` as the starting `dict[str, float]`.
  2. **Regime multiplier.** For every rule ID in the profile's `rule_values`: multiply by the matching multiplier from `regimes[active_regime].multipliers[rule_id]`. Rules absent from the regime's `multipliers` map (should not happen post-validation; this is a structural error and raises) are reported with the rule ID.
  3. **Overlay multipliers.** For each overlay in `active_overlays` in the order given, for every rule ID present in that overlay's `multipliers` (partial map): multiply the running rule value by the overlay multiplier. Rules absent from an overlay's map are unaffected. The order of `active_overlays` is significant only because multiplication is commutative for non-conflicting overlays — preserve operator-supplied order for review-surface stability.
  4. **Mode behavioral transform.** Apply `modes[active_mode]`'s per-agent restrictions to the corresponding `ResolvedConfig` fields (`analyst_output_mode`, `strategist_*`, `pm_*`).
  5. **Run-type filter and clamp.** Take `run_types[firing_trigger]`. Set `enabled_agents` from its `agents.enabled` list. Set `agent_overrides` from its `agents.overrides` map. Set `news_digest_*` from its `qualitative_researcher.news_digest` block.
  6. **Feature-flag closure.** Read `feature_flags` from the profile. The closure rule (per `configuration-management.md § Feature flag semantics`): for every disabled feature, drop the corresponding rules from `rule_values`, drop the corresponding agents/tools from `enabled_agents` (if any are feature-coupled — currently none are; this is a forward-looking placeholder). Closure is **drop**, not **zero-out**; if `options_enabled: false`, options rules must not appear in `rule_values` of the resolved config.
- The resolver is **pure** — no I/O, no logging side effects, no exception swallowing. Caller is responsible for handing in already-loaded objects.
- `ResolvedConfig` is hash-stable: equal inputs produce equal outputs. Use `dataclass(frozen=True, slots=True)`. Nested mutable types (dict, list) are converted to immutable forms (dict → frozen `MappingProxyType` or sorted tuple of items; list → tuple) at construction so the dataclass itself is hashable. Where Pydantic models are passed through unchanged, they are already frozen per their `model_config`.
- Unit tests covering: round-trip arithmetic for one canonical (medium, normal, [], pre_open) composition; a stricter composition (medium, elevated, [pre_event], pre_open) shows multiplicative tightening on `position_max_size_pct`; halt mode produces `analyst_output_mode == watchlist` and `pm_allowed_command_types` excluding `OPEN`; off_hours_rolling produces `enabled_agents` without `adaptive_researcher`; feature-flag closure verifies micro profile's `rule_values` contains no options keys.

Out of scope:
- Cross-reference and semantic-self-test validation (stories 06a, 06b) — the resolver assumes inputs are already validated.
- The runtime that actually invokes the resolver each invocation — owned by story 08.
- Snapshot persistence — owned by story 07.
- Determining `active_regime`, `active_mode`, `active_overlays` — those are computed by the distillation and pipeline-state layers; the resolver receives them as parameters.
- Profile transitions — `main.active_profile` is read; the operator owns transitions per `rules-and-limits.md § Transitioning between profiles`.

## Notes

**Why a pure function.** Snapshot persistence (story 07) hashes the resolver's output. If the resolver had side effects (logging, file I/O), the hash would not be deterministic across environments. The function-level purity is enforced by the test suite — call the resolver twice with the same arguments and compare outputs by hash.

**Multiplicative cascade.** Per `regime-adaptation.md`, regime multipliers and overlay multipliers compose multiplicatively. Two overlays both multiplying `position_max_size_pct` by `0.80` produce a final value of `base × regime × 0.80 × 0.80 = base × regime × 0.64`. This is the design intent — overlays compound, not max-take or last-wins.

**Mode is not multiplicative.** The mode's transform is *behavioral* (restricts action vocabulary, output mode, default treatment of pending orders) — not numeric. Apply it after numeric composition.

**Run-type is not multiplicative either.** The run-type filter trims the agent roster and clamps the budget envelope. Apply it last.

**`active_overlays` order.** Multiplicative composition is commutative for non-conflicting overlays (different rule IDs). For overlapping overlays (both touching the same rule), multiplication is also commutative. The order parameter is preserved in `ResolvedConfig.active_overlays` for the review surface (snapshot diff readability), not for arithmetic.

**Feature-flag closure as **drop**, not **zero**.** This is critical: if a feature is disabled, the resolved config must not contain the corresponding rules at zeroed values. The PM's guardrail-state header would otherwise show a `position_max_loss_options_pct` row with value 0 — which is misleading (it reads as "every options position fails the check") rather than absent ("options rules are not in scope for this profile"). The validator in story 06b enforces closure compliance; the resolver implements it.

**`MappingProxyType`** from `types` provides a read-only view over a dict. Use it to make the resolved-config dict fields immutable without converting them to tuple-of-items lists (which would lose the typed-dict ergonomics for callers).

**Dataclass with slots.** Python `dataclass(frozen=True, slots=True)` is the stable, performance-sensible choice over a Pydantic model here — the resolver's output is internal and has no validation needs (every Pydantic model in the input has already validated). Stick to dataclasses for the resolver output.

## Acceptance criteria

- [ ] `src/alphamind/config/resolver.py` exists and defines `ResolvedConfig` (frozen dataclass) and `compose_config(...)`.
- [ ] `compose_config` accepts the parameters listed in Scope and returns a `ResolvedConfig`.
- [ ] `ResolvedConfig` is a `dataclass(frozen=True, slots=True)`.
- [ ] `models/__init__.py` (or a sibling `__init__.py`) re-exports `ResolvedConfig` and `compose_config`.
- [ ] A unit test asserts a canonical composition (medium profile, normal regime, empty overlays, pre_open run-type) produces a `ResolvedConfig` whose `rule_values["position_max_size_pct"]` equals the medium profile's base value (multiplied by `1.0`).
- [ ] A unit test asserts (medium, elevated, [], pre_open) produces `rule_values["position_max_size_pct"] == base * 0.70` (per the regime multiplier table).
- [ ] A unit test asserts (medium, elevated, [pre_event], pre_open) produces `rule_values["position_max_size_pct"] == base * 0.70 * 0.80`.
- [ ] A unit test asserts halt mode produces `analyst_output_mode == AnalystOutputMode.watchlist`, `pm_allowed_command_types` excluding `OPEN` and `ADD`, and `pending_orders_default == PendingOrdersDefault.cancel`.
- [ ] A unit test asserts the off_hours_rolling run-type produces `enabled_agents` not containing `adaptive_researcher`.
- [ ] A unit test asserts the pre_open run-type produces `enabled_agents` containing `adaptive_researcher` and the resolved `agent_overrides[adaptive_researcher]["cumulative_tool_call_limit"] == 25`.
- [ ] A unit test asserts feature-flag closure: micro profile's resolved config contains no `options_*` keys in `rule_values` and no options rules of any name.
- [ ] A unit test asserts the resolver is pure — calling `compose_config(...)` twice with the same arguments produces equal outputs (`==`-equal and same hash).
- [ ] A unit test asserts a missing rule-ID in a regime's multiplier map (a structural error that should not happen post-validation but must surface clearly) raises with the rule ID named.
- [ ] No file I/O in the resolver — the test for purity covers this.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
