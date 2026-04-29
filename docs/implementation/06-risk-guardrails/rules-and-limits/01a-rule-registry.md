---
status: not_started
completed_date:
commit_id:
---

# 01a — Rule registry runtime accessor

## Goal

Land a small runtime accessor over `GuardrailsConfig.rules` so downstream guardrail features (state delivery, breach behavior, guardrail evaluation) can look up rule metadata by ID in O(1) and iterate rules by tier or by between-invocation monitoring requirement. The shipped configuration loads `guardrails.yaml` into a `list[RuleEntry]` (story 03f of `foundation/configuration/`); every consumer otherwise rebuilds the same dict. Centralizing the accessor here removes the duplication and gives the downstream features one stable lookup surface.

## Reading

- `docs/design/06-risk-guardrails/rules-and-limits.md` — particularly § Per-rule enforcement summary (the matrix that drives the tier and monitor lookups)
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Enforcement tier model — definitions of `T1`, `T2`, `T3`
- `docs/design/06-risk-guardrails/breach-behavior.md` § Per-rule classification — uses the same metadata (`breach_response`, `monitor_between_invocations`) the registry surfaces
- `src/alphamind/config/models/guardrails.py` — `GuardrailsConfig`, `RuleEntry`, `EnforcementTier`, `BreachResponse`, `EscalationZones`, `ProgressiveTier`, `EmergencyInvocation`
- `config/guardrails.yaml` — the shipped registry (19 rule entries plus `emergency_invocation`)
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` — currently empty; this story adds the first re-exports
- `src/alphamind/config/resolver.py` — `ResolvedConfig` (no edits required, but the registry will be used alongside `ResolvedConfig.guardrails`)

## Depends on

None. The configuration-management feature has already landed `GuardrailsConfig` and the shipped `config/guardrails.yaml`.

## Scope

In scope:

- `src/alphamind/risk_guardrails/rules_and_limits/registry.py` defining:
  - `RuleRegistry` (frozen, slots dataclass): an immutable lookup over a `Mapping[str, RuleEntry]` keyed by `RuleEntry.id`. Field: `_by_id: Mapping[str, RuleEntry]` — store via `MappingProxyType` so the dataclass is hashable and the underlying dict cannot be mutated.
  - `build_rule_registry(guardrails: GuardrailsConfig) -> RuleRegistry` — factory that reads `guardrails.rules` and builds the keyed view. Raises `ValueError` if the input contains duplicate `id` values (defensive — the parse-time validator already guarantees uniqueness, but the factory must surface a structural failure rather than silently dropping a duplicate).
  - `RuleRegistry.get(rule_id: str) -> RuleEntry` — raises `KeyError` naming the rule on miss.
  - `RuleRegistry.try_get(rule_id: str) -> RuleEntry | None` — returns `None` on miss.
  - `RuleRegistry.at_tier(tier: EnforcementTier) -> tuple[RuleEntry, ...]` — preserves the `guardrails.rules` declaration order; returns an empty tuple if no rules fire at that tier.
  - `RuleRegistry.requiring_monitor() -> tuple[RuleEntry, ...]` — same ordering; returns the rules where `monitor_between_invocations is True`.
  - `RuleRegistry.with_progressive_tiers() -> tuple[RuleEntry, ...]` — rules that carry a non-`None` `progressive_tiers` field. Currently exactly one (`cumulative_drawdown_pct`); the helper exists so a future second progressive-tier rule needs no consumer changes.
  - `RuleRegistry.__iter__()` — yields `RuleEntry` values in declaration order. Lets callers write `for rule in registry: ...` without reaching into `_by_id`.
  - `RuleRegistry.__len__()` — number of rules in the registry.
  - `RuleRegistry.__contains__(rule_id: str)` — `True` iff the rule ID is present.
- A small helper joining the registry with composed limits:
  - `ResolvedRule` (frozen, slots dataclass): pairs a `RuleEntry` with its currently-active limit. Fields: `metadata: RuleEntry`, `limit: float`.
  - `iter_active_rules(resolved: ResolvedConfig, registry: RuleRegistry) -> tuple[ResolvedRule, ...]` — for every `(rule_id, limit)` in `resolved.rule_values`, yield `ResolvedRule(metadata=registry.get(rule_id), limit=limit)`. Order matches `resolved.rule_values` iteration order. A rule present in `resolved.rule_values` but absent from the registry is a structural error (post-validation) — raise `KeyError` naming the rule.
- `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `RuleRegistry`, `build_rule_registry`, `ResolvedRule`, `iter_active_rules`. The existing empty `__init__.py` is replaced with this re-export list.
- Unit tests covering:
  - The shipped `config/guardrails.yaml` parses via `GuardrailsConfig.model_validate(...)` and `build_rule_registry(...)` returns a registry whose `len(...) == 19` and whose IDs match the canonical set in story 03f's Scope (the suffixed IDs).
  - `registry.get("sector_concentration_pct")` returns the matching `RuleEntry` with `enforcement_tiers == [T1, T2, T3]`, `breach_response == deferred_to_pm`, `monitor_between_invocations == True`.
  - `registry.get("nonexistent_rule")` raises `KeyError` with a message naming `nonexistent_rule`.
  - `registry.try_get("nonexistent_rule")` returns `None` (no exception).
  - `registry.at_tier(EnforcementTier.T1)` returns the canonical T1 rules per the per-rule enforcement summary in `rules-and-limits.md` — every rule with `T1` in `enforcement_tiers`. Assert membership for at least three known T1 rules (e.g., `position_max_size_pct`, `sector_concentration_pct`, `correlation_max`) and one known non-T1 rule's absence (e.g., `daily_drawdown_pct` is T2+T3 only).
  - `registry.at_tier(EnforcementTier.T3)` excludes `correlation_max` and `thesis_dependency_flag_pct` (the two T1+T2-only rules per the doc).
  - `registry.requiring_monitor()` includes `sector_concentration_pct` and excludes `position_max_size_pct` (which is command-time-only).
  - `registry.with_progressive_tiers()` returns exactly one rule in the shipped config: `cumulative_drawdown_pct`.
  - `len(registry) == 19`, `iter(registry)` preserves declaration order, `"sector_concentration_pct" in registry` is `True`.
  - `build_rule_registry` raises `ValueError` when handed a synthetic `GuardrailsConfig` with two `RuleEntry` instances sharing the same `id` (constructed via `model_construct` to bypass the parse-time validator that would otherwise reject the duplicate).
  - `iter_active_rules(resolved, registry)` returns tuples in `resolved.rule_values` iteration order; the joined `metadata.id` matches each yielded `ResolvedRule.metadata.id`; the joined `limit` equals `resolved.rule_values[rule_id]`.
  - `iter_active_rules` raises `KeyError` (with the offending rule ID in the message) when `resolved.rule_values` carries a rule not present in the registry. Construct the failure case by combining a real `ResolvedConfig` with a registry built from a `GuardrailsConfig` whose `rules` list has been narrowed to drop one rule.

Out of scope:

- Loading `GuardrailsConfig` from YAML — already shipped by `foundation/configuration/03f-guardrails-yaml-model.md`.
- The composition cascade producing `ResolvedConfig.rule_values` — already shipped by `foundation/configuration/05-composition-resolver.md`.
- Engine-side rejection or PM-side validation logic — owned by `guardrail-evaluation` and execution-layer features.
- A registry of *runtime values* (e.g., per-rule current state) — that is portfolio state's category 4c, owned elsewhere; this story exposes only the static registry and the composed limit.
- Caching of `build_rule_registry` results — the call is O(N rules) in 19 entries; callers can memoize at the pipeline boundary if they care.

## Notes

**Why a separate dataclass rather than monkey-patching `GuardrailsConfig`.** Pydantic models are frozen post-validation; adding lookup methods to `GuardrailsConfig` would require either subclassing (tying the registry to Pydantic) or a sidecar pattern. A standalone `RuleRegistry` dataclass keeps the configuration-management package focused on parse-and-validate and lets the runtime accessor live in the feature that owns the rule semantics.

**`MappingProxyType` over the inner dict.** Same pattern story 05's `ResolvedConfig` uses for `rule_values`. `RuleRegistry` is constructed once per `GuardrailsConfig`; the proxy makes the read-only contract explicit and lets the dataclass remain hashable without converting the dict to a tuple-of-items.

**Ordering preservation.** `at_tier`, `requiring_monitor`, `with_progressive_tiers`, and `__iter__` all preserve the `guardrails.rules` declaration order. The shipped `config/guardrails.yaml` already groups rules by category (position size, concentration, drawdown, ...) for human review; preserving that order in iteration keeps consumers' rendered output legible.

**`ResolvedRule` is intentionally minimal.** It pairs metadata with the currently-active limit. It does *not* carry current portfolio state, headroom, escalation zone, or any per-invocation signal — those join in at the consumer (state delivery joins them with portfolio state §4c; breach behavior joins them with the breach detector). Keeping `ResolvedRule` as just `(metadata, limit)` makes it reusable across consumers without becoming a god object.

**Registry duplicate-ID defensiveness.** The Pydantic validator (story 03f) already enforces `id` uniqueness in `GuardrailsConfig.rules`. The factory's defensive `ValueError` covers the case where a future caller constructs a `GuardrailsConfig` via `model_construct` (bypassing validators) or via direct dataclass instantiation in tests. Cheap insurance against silent drops.

**Tier 1/2/3 lookups are independent of feature flags.** `at_tier(T1)` returns every `RuleEntry` whose static metadata declares T1 — regardless of whether the active profile has the corresponding feature enabled. Filtering by feature flag is the consumer's job (consumers cross-reference against `ResolvedConfig.rule_values`, which is already feature-flag-closed by the resolver).

`model_config = ConfigDict(frozen=True)` is irrelevant here — these are stdlib `dataclass(frozen=True, slots=True)`, not Pydantic models.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/rules_and_limits/registry.py` exists and defines `RuleRegistry`, `build_rule_registry`, `ResolvedRule`, `iter_active_rules`.
- [ ] `RuleRegistry` and `ResolvedRule` are `dataclass(frozen=True, slots=True)`.
- [ ] `src/alphamind/risk_guardrails/rules_and_limits/__init__.py` re-exports `RuleRegistry`, `build_rule_registry`, `ResolvedRule`, `iter_active_rules`.
- [ ] A unit test asserts `build_rule_registry(GuardrailsConfig.model_validate(load_yaml('config/guardrails.yaml')))` returns a registry with `len() == 19` and the 19 canonical suffixed rule IDs.
- [ ] A unit test asserts `registry.get('sector_concentration_pct').enforcement_tiers == [T1, T2, T3]` and `registry.get('sector_concentration_pct').breach_response == BreachResponse.deferred_to_pm`.
- [ ] A unit test asserts `registry.get('nonexistent_rule')` raises `KeyError` whose message names `nonexistent_rule`.
- [ ] A unit test asserts `registry.try_get('nonexistent_rule')` returns `None` without raising.
- [ ] A unit test asserts `registry.at_tier(EnforcementTier.T1)` includes `position_max_size_pct`, `sector_concentration_pct`, `correlation_max` and excludes `daily_drawdown_pct`.
- [ ] A unit test asserts `registry.at_tier(EnforcementTier.T3)` excludes `correlation_max` and `thesis_dependency_flag_pct`.
- [ ] A unit test asserts `registry.requiring_monitor()` includes `sector_concentration_pct` and excludes `position_max_size_pct`.
- [ ] A unit test asserts `registry.with_progressive_tiers()` returns exactly one rule, `cumulative_drawdown_pct`.
- [ ] A unit test asserts `RuleRegistry.__iter__` yields rules in `guardrails.rules` declaration order, `__len__` returns 19, and `'sector_concentration_pct' in registry` is `True`.
- [ ] A unit test asserts `build_rule_registry` raises `ValueError` when given a `GuardrailsConfig` with duplicate `RuleEntry.id` values (constructed via `GuardrailsConfig.model_construct` or equivalent that bypasses the parse-time uniqueness validator).
- [ ] A unit test asserts `iter_active_rules(resolved, registry)` returns one `ResolvedRule` per entry in `resolved.rule_values`, with each `ResolvedRule.limit` equal to the corresponding composed limit and `ResolvedRule.metadata.id` matching.
- [ ] A unit test asserts `iter_active_rules` raises `KeyError` (with the offending rule ID in the message) when `resolved.rule_values` carries a rule absent from the registry.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
