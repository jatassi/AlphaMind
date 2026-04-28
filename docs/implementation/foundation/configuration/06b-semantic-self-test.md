---
status: not_started
completed_date:
commit_id:
---

# 06b — Semantic self-test invariants

## Goal

Implement the semantic self-test layer per `configuration-management.md § Validation`. Semantic self-tests are computed invariants that go beyond per-file types and beyond cross-reference name resolution: drawdown progressive tiers monotonic, no regime multiplier drives a rule limit to zero or negative for any profile, escalation zones ordered, capital ranges non-overlapping, ticker uniqueness across sectors and benchmarks, `last_full_validation` no later than today, feature-flag closure (no residue), and the `digest.yaml` numerical sanity invariants. Failures here would parse-validate and cross-reference-validate cleanly but would produce wrong behavior at runtime.

## Reading

- `docs/design/configuration-management.md` § Validation — the semantic-self-test enumeration
- `docs/design/configuration-management.md` § Feature flag semantics — closure rule
- `docs/design/asset-universe-validation.md` § Config-load invariants — ticker uniqueness, format regex, `last_full_validation` ≤ today, sector non-emptiness
- `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown progressive tiers — monotonicity requirement
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Parameter sets per regime — design rationale for "drawdown does not loosen in low-vol" and similar non-numeric invariants (these are operator-judgment, not enforced)
- `src/alphamind/config/resolver.py` (post-story-05) — `compose_config` produces the resolved snapshot the closure test runs against
- `src/alphamind/config/models/*` (post-stories 03b–03i, 04a–04e)

## Depends on

- 03a–03i (every flat-tail model)
- 04a–04e (every bundle directory)
- 05 (resolver — feature-flag closure tests run against composed `ResolvedConfig`)

## Scope

In scope:
- `src/alphamind/config/validation/semantic.py` defining `validate_semantic_invariants(...)` — a single entry point that runs every semantic self-test against the loaded YAML tree plus the resolver. Signature:
  ```python
  def validate_semantic_invariants(
      *,
      guardrails: GuardrailsConfig,
      assets: AssetsConfig,
      digest: DigestConfig,
      profiles: dict[Profile, ProfileConfig],
      regimes: dict[Regime, RegimeConfig],
      composed_configs: dict[tuple[Profile, Regime, Mode, tuple[Overlay, ...], RunType], ResolvedConfig],
      today: date,
  ) -> None
  ```
- A `SemanticInvariantError(Exception)` class. Each failing invariant raises with a message naming the offending field(s) and the violated rule. Failures are aggregated like cross-reference (one error carrying every violation).
- Concrete invariants — match the `configuration-management.md § Validation` enumeration:
  1. **Cumulative-drawdown progressive tiers monotonic.** For the `cumulative_drawdown_pct` rule entry in `guardrails.rules`: the `progressive_tiers` list is monotonically increasing in `trigger_pct`.
  2. **No regime multiplier drives a rule limit to zero or negative for any profile.** For every `(profile, regime)` pair: for every rule ID in the profile's `rule_values`: `profile.rule_values[rule_id] * regime.multipliers[rule_id] > 0`. (Combined with overlays, the `composed_configs` map already encodes this via stage-5 cascade — the test reads `composed_configs` and asserts every numeric value is `> 0`.)
  3. **Escalation zones ordered.** For every `RuleEntry r` in `guardrails.rules`: `r.escalation_zones.warning < r.escalation_zones.critical < r.escalation_zones.hard_block`. (Already enforced at parse time by story 03f's model validator; re-asserted here as a defensive check.)
  4. **Capital ranges non-overlapping.** For every distinct pair of profiles `p, q`: their `capital_range_usd` ranges do not overlap.
  5. **Ticker uniqueness.** Across `assets.sectors` (every sector's tickers) and `assets.benchmarks` (every key): no ticker appears more than once.
  6. **Ticker format.** Every ticker in `assets.sectors` and `assets.benchmarks` matches `^[A-Z][A-Z0-9.]*$`. (Already enforced by `AssetsConfig` per story 03a; re-asserted defensively.)
  7. **Sector non-emptiness.** Every sector listed in any profile's `active_sectors` has `len(assets.sectors[sector]) ≥ 1`.
  8. **`last_full_validation` ≤ today.** When `assets.last_full_validation is not None`: `assets.last_full_validation <= today`.
  9. **Feature-flag closure.** For every profile `p` with any feature flag disabled: the resolved configs derived from `p` (at any regime, mode, overlay, run-type combination) contain no rule, agent, or tool that depends on the disabled feature. The check is structural: if `p.feature_flags.options_enabled is False`, `composed_configs[(p, ...)].rule_values` must contain no key starting with `options_`, no key matching `portfolio_theta_*` or `portfolio_vega_*`, no rule whose ID is in the options-rule set defined by the rule registry.
  10. **`digest.yaml` numerical bounds.** Every `baseline_window_weeks ≥ 1`; `anti_pattern_spike.multiplier_vs_baseline > 1.0`; `anti_pattern_spike.min_occurrences_this_week ≥ 0`; `sector_underperform.median_offset_sigma > 0`; every `delta_pp_threshold ∈ (0, 100]`; `validation_window_end.days_before_due ≥ 0`. (All already enforced at parse time per story 03h; re-asserted defensively.)
- A helper `enumerate_compositions(...)` in `src/alphamind/config/validation/semantic.py` that returns the closure-test composition matrix: every (profile × regime × mode × overlays × run_type) combination relevant for the closure check. The helper invokes `compose_config` from story 05; the matrix size is bounded (4 profiles × 4 regimes × 2 modes × subset of 2 overlays × 6 run_types = 384 compositions worst case — fast). The validator's caller (story 08) is free to enumerate compositions via this helper or a narrower set; the validator function itself accepts a precomputed map.
- `src/alphamind/config/validation/options_rule_set.py` (or inlined in `semantic.py`) defining the closed set of options-related rule IDs: `{position_max_loss_options_pct, options_delta_pct, portfolio_theta_pct_per_day, portfolio_vega_pct_per_iv_point}`. The closure test reads this set when checking `options_enabled: false`. A parallel set covers shorts: `{net_short_pct, total_short_pct, single_short_max_pct, borrow_cost_budget_pct_per_day}` — with the note that `gross_exposure_pct` is **not** in the shorts set (it's binding even with shorts disabled, just trivially since gross == net long).
- Re-export `validate_semantic_invariants`, `SemanticInvariantError`, `enumerate_compositions` from `models/__init__.py` / `validation` namespace.
- Unit tests covering the happy path (shipped YAML passes) and one failure case per invariant (1–10).

Out of scope:
- Cross-reference checks (story 06a).
- Operator-judgment "should-be" invariants (e.g., "drawdown should not loosen in low-vol") — design-rationale notes, not enforced.
- The runtime that calls the validator each invocation — story 08.
- Threshold-calibration semantic checks for `distillation.yaml` — owned by the separate distillation-layer story `02-config-schema.md`; this story does not duplicate them.

## Notes

**Aggregating failures, defensive re-checks.** Same pattern as story 06a: build a list, raise once. Several invariants are already enforced at Pydantic parse time and are re-asserted here as defensive insurance — they cost almost nothing to run (single-pass dict iteration) and provide a guarantee that no parse-bypassing edit slips through.

**Closure check requires composed configs.** Feature-flag closure cannot be checked from raw YAML alone — it asserts that the *resolved* config has dropped the disabled-feature rules. Story 05's resolver does the dropping; this story checks the resolver's output. The dependency on story 05 is explicit.

**Composition matrix size.** 4 × 4 × 2 × 4 × 6 = 768 worst-case compositions (4 overlay subsets: `()`, `(pre_event,)`, `(stress,)`, `(pre_event, stress)`). Per-composition validation is O(rule count) ≈ O(20) — total work ≈ 15K dict iterations. Fast enough to run on every config load. If empirical perf matters, narrow the matrix to "every profile + every regime, fixed mode/overlays/run-type" — the closure invariant is invariant under mode/overlay/run-type since those don't add rules to the resolved config.

**`enumerate_compositions` is a helper, not a validator.** It returns a map; the validator accepts a pre-computed map. Story 08 calls `enumerate_compositions` and passes the result to `validate_semantic_invariants`. Keeping enumeration separate makes the validator's signature self-contained and testable in isolation.

**`today: date` parameter.** Pure-function discipline — no `date.today()` call inside the validator. The caller passes the current date; tests can pin a fixed date.

## Acceptance criteria

- [ ] `src/alphamind/config/validation/semantic.py` exists and defines `SemanticInvariantError` and `validate_semantic_invariants(...)`.
- [ ] `src/alphamind/config/validation/semantic.py` (or a sibling module) defines `enumerate_compositions(...)` returning the (profile, regime, mode, overlays, run_type) → ResolvedConfig map.
- [ ] `models/__init__.py` re-exports `validate_semantic_invariants`, `SemanticInvariantError`, `enumerate_compositions`.
- [ ] A unit test asserts the shipped YAML tree (composed across the full matrix) passes `validate_semantic_invariants` without raising.
- [ ] A unit test asserts a `cumulative_drawdown_pct` `progressive_tiers` with non-monotonic `trigger_pct` raises (synthetic fixture).
- [ ] A unit test asserts a regime multiplier of `0.0` for some rule used by some profile raises.
- [ ] A unit test asserts profiles `micro: capital_range_usd: [1000, 10000]` and `small: capital_range_usd: [5000, 15000]` raise (overlap).
- [ ] A unit test asserts a duplicate ticker between two sectors raises.
- [ ] A unit test asserts a duplicate ticker between a sector and a benchmark raises.
- [ ] A unit test asserts a sector listed in any profile's `active_sectors` with an empty ticker list raises.
- [ ] A unit test asserts `assets.last_full_validation = date(2099, 1, 1)` raises when `today = date(2026, 4, 27)`.
- [ ] A unit test asserts the micro profile's resolved configs (across the matrix) contain no `options_*`, `portfolio_theta_*`, `portfolio_vega_*` rules; if a synthetic resolver leaves a residue (zero-valued options rule), the validator raises.
- [ ] A unit test asserts `enumerate_compositions` produces 768 entries (4 profiles × 4 regimes × 2 modes × 4 overlay subsets × 6 run_types) at the full matrix.
- [ ] A unit test asserts the validator aggregates failures.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.
