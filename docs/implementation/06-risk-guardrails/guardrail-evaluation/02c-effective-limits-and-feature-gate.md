---
status: in_progress
completed_date:
commit_id:
---

# 02c — Effective-limit adapter and feature-flag gate

## Goal

Two related primitives that bridge the upstream configuration resolver to the library's input shape:

1. **Effective-limit adapter** — a pure function that consumes a `ResolvedConfig` (produced by `compose_config` in `src/alphamind/config/resolver.py`) and returns the library's `LibraryConfig` shape (defined in story 01). This is the "active regime parameter resolution" primitive from `guardrail-evaluation.md` § Primitives, realized as a thin read of the already-cascaded `rule_values` plus the per-rule `escalation_zones` from `guardrails.yaml` plus the `feature_flags` carved to the library's narrower view.
2. **Feature-flag gate** — a per-proposal classifier that returns the canonical `feature_disabled` rejection for proposed deltas referencing instrument classes the active profile disables (`asset_type=OPTION` on `options_enabled=False`, `direction=SHORT` on `short_selling_enabled=False`). This is the "feature-flag early-exit" primitive from the design doc.

Together these two primitives define which rules are in scope for the current invocation and which proposals never reach the projection layer.

## Reading

- `docs/design/06-risk-guardrails/guardrail-evaluation.md` § Primitives (Active regime parameter resolution, Feature-flag early-exit) — primitive contracts
- `docs/design/configuration-management.md` § Composition model — `ResolvedConfig.rule_values` is the cascaded output the library reads
- `docs/design/configuration-management.md` § Feature flag semantics — closure (drop, not zero) discipline; the adapter's read is "what's in `rule_values`" — it does not re-derive
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Per-rule enforcement summary — canonical rule IDs the library is in scope for
- `docs/design/06-risk-guardrails/breach-behavior.md` § Escalation model — the warning/critical/hard-block percentages live in `guardrails.yaml`'s `escalation_zones` per rule
- `docs/design/06-risk-guardrails/state-delivery.md` § Guardrail validation tool — the validation tool's output uses `feature_disabled` exactly per the design's reason string
- `src/alphamind/config/resolver.py` — `ResolvedConfig` dataclass
- `src/alphamind/config/models/guardrails.py` — `GuardrailsConfig` rule registry (escalation zones live here)
- `src/alphamind/config/models/profiles.py` — `FeatureFlags`, `ProfileConfig.active_sectors`
- `src/alphamind/risk_guardrails/guardrail_evaluation/types.py` — `LibraryConfig`, `EscalationZones`, `FeatureFlagsView`, `FeatureDisabledRejection`, `ProposedDelta`, `Direction`, `AssetType`, `Action` (defined in 01)

## Depends on

- 01 (canonical types)

## Scope

In scope:

- `src/alphamind/risk_guardrails/guardrail_evaluation/effective_limits.py` defining `from_resolved_config(...)`:
  ```python
  def from_resolved_config(resolved: ResolvedConfig) -> LibraryConfig:
  ```
  Reads `resolved.rule_values`, `resolved.guardrails.rules`, `resolved.feature_flags`, `resolved.profile.active_sectors`, `resolved.regime_label`, `resolved.profile_label`, and `resolved.execution.conservative_delta_buffer_pct` to produce a `LibraryConfig`. Behavior:
  - **`effective_limits`.** Copy `rule_values` verbatim. The cascade is upstream's responsibility; the adapter does not re-multiply or override.
  - **`escalation_zones`.** For every rule ID present in `effective_limits`, look up its `escalation_zones` block in `resolved.guardrails.rules` and convert to the library's `EscalationZones` dataclass. Rules in `effective_limits` but missing from `guardrails.rules` are a structural error (raises `EffectiveLimitAdapterError`) — should not happen post-cross-reference validation.
  - **`feature_flags`.** Carve `FeatureFlagsView(options_enabled=resolved.feature_flags.options_enabled, short_selling_enabled=resolved.feature_flags.short_selling_enabled)` — narrower than `ResolvedConfig.feature_flags` (which carries `fractional_shares_required` the library does not consult).
  - **`active_sectors`.** Copy `resolved.profile.active_sectors` as a `tuple[str, ...]`. Order preserved.
  - **`active_regime` / `active_profile`.** Copy `resolved.regime_label` / `resolved.profile_label` as plain strings.
  - **`conservative_buffer_pct`.** Copy `resolved.execution.conservative_delta_buffer_pct`.
  - The adapter is pure — equal `ResolvedConfig` produces equal `LibraryConfig`.
- `EffectiveLimitAdapterError(Exception)` — raised when the adapter detects a structural inconsistency (rule in `rule_values` missing from `guardrails.rules`, sector in `active_sectors` missing from `assets.sectors`, conservative buffer outside `[0, 100]`). Each instance carries a single failure with a message naming the offending field.
- `src/alphamind/risk_guardrails/guardrail_evaluation/feature_gate.py` defining:
  - `classify_feature_gate(proposal: ProposedDelta, config: LibraryConfig) -> FeatureDisabledRejection | None`. Returns `None` if the proposal is allowed; `FeatureDisabledRejection` with the canonical reason string if blocked. Reason strings:
    - `"options_disabled"` when `proposal.asset_type ∈ {OPTION, STRATEGY}` and `config.feature_flags.options_enabled is False`.
    - `"shorts_disabled"` when `proposal.direction == SHORT` and `config.feature_flags.short_selling_enabled is False`.
    - When both apply (a short option), `"options_disabled"` takes precedence (more fundamental — the instrument cannot be opened at all). The single-reason output keeps the error surface uniform; the engine's downstream rejection logic does not need to enumerate combined cases.
    - `disabled_feature: str` is `"options"` or `"shorts"` matching the reason.
  - The function only inspects `proposal.asset_type` and `proposal.direction`; it does not inspect `proposal.action`. A `CLOSE` of an existing options position on a profile that has flipped to `options_enabled=False` is a misconfiguration — but the library does not own that policy. Detailed handling (allow CLOSE-only on disabled features, etc.) lives at the entry point in story 05; this primitive is purely structural.
  - The function is total over `(proposal, config)` — returns `None` or the rejection; never raises.
- Re-export `from_resolved_config`, `EffectiveLimitAdapterError`, `classify_feature_gate` from `guardrail_evaluation/__init__.py`.
- Unit tests:
  - **Adapter happy path.** Build a synthetic `ResolvedConfig` (medium profile, normal regime, no overlays, normal mode); assert the adapter produces `LibraryConfig` with `effective_limits` matching the input `rule_values`, `escalation_zones` populated for every rule, `feature_flags=FeatureFlagsView(True, True)`, `active_sectors=("tech", "semis", "financials", "energy")`, `conservative_buffer_pct=10`.
  - **Adapter cascade transparency.** Build `ResolvedConfig` for medium × elevated × `pre_event` overlay. Assert `effective_limits["sector_concentration_pct"]` matches `0.25 × 0.80 × 1.0` (no overlay multiplier on this rule) — the adapter copies `rule_values` and the cascade was upstream's job. Pinning this prevents the adapter from silently re-cascading.
  - **Missing escalation zone.** Construct a `ResolvedConfig` where `rule_values` has a rule absent from `guardrails.rules`; assert `from_resolved_config` raises `EffectiveLimitAdapterError` naming the rule. (Construct via direct `ResolvedConfig(...)` instantiation in the test; do not mutate shipped YAML.)
  - **Conservative-buffer bounds.** A `conservative_delta_buffer_pct` of `-5` or `150` raises `EffectiveLimitAdapterError`.
  - **Feature gate, options disabled.** `LibraryConfig(feature_flags=FeatureFlagsView(False, True))` — proposal `asset_type=OPTION`, `direction=LONG` returns `FeatureDisabledRejection(reason="options_disabled", disabled_feature="options")`.
  - **Feature gate, shorts disabled.** `LibraryConfig(feature_flags=FeatureFlagsView(True, False))` — proposal `asset_type=EQUITY`, `direction=SHORT` returns `FeatureDisabledRejection(reason="shorts_disabled", disabled_feature="shorts")`.
  - **Feature gate, both disabled (short option).** `FeatureFlagsView(False, False)` — proposal `asset_type=OPTION`, `direction=SHORT` returns `reason="options_disabled"` (precedence rule).
  - **Feature gate, allowed.** `FeatureFlagsView(True, True)` — every proposal returns `None`.
  - **Feature gate, equity long with shorts disabled.** `FeatureFlagsView(True, False)` — `asset_type=EQUITY`, `direction=LONG` returns `None`.
  - **Action does not influence the gate.** A `CLOSE` on an option proposal with `options_enabled=False` returns the same `FeatureDisabledRejection` as an `OPEN`. The gate is structural; entry-point policy (story 05) decides how `CLOSE` interacts with disabled features.
  - **Adapter purity.** Calling `from_resolved_config` twice on the same `ResolvedConfig` produces equal `LibraryConfig` objects (`==`). Hash equality holds via the frozen dataclass contract from 01.

Out of scope:

- Re-cascading the rule_values dict. The configuration resolver does the cascade; this story reads it.
- The cumulative-tracking wrapper used by the validation tool. That's `state-delivery.md`'s responsibility — it composes this gate primitive plus per-call state.
- Engine-side handling of rejected proposals. The engine consumes `LibraryOutput.feature_disabled` via story 05's entry point and decides whether to log, alert, or surface to the PM — outside this story's scope.
- Combining feature-disabled with rule-projection breach reporting. The library treats them as disjoint surfaces; a feature-disabled proposal never enters per-rule projection.

## Notes

**Single-reason rejections, not enum lists.** A short option could legitimately be flagged for "options_disabled AND shorts_disabled," but reporting both is cosmetic — the proposal is rejected either way. Single-reason output keeps the contract simple. The precedence (options before shorts) is documented and tested; the feedback loop sees `options_disabled` consistently for short-option proposals on profiles that disable both.

**The adapter is the only point that reads `ResolvedConfig.execution.conservative_delta_buffer_pct`.** Story 03 (delta-adjusted exposure) reads `LibraryConfig.conservative_buffer_pct`. The adapter is the seam.

**The library's `LibraryConfig.active_regime` is a string, not an enum.** Regime labels are owned by `regimes.yaml` filenames; the library accepts whatever the resolver produces. Story 03 maps the regime label to a per-regime buffer multiplier; that mapping (`{"normal": 1.0, "elevated": 1.2, "crisis": 1.5, "low-vol": 0.8}` or whatever the design dictates) lives in story 03, not here.

**Feature-flag closure is upstream's discipline.** Per `configuration-management.md § Feature flag semantics`, when `options_enabled=False`, `rule_values` already contains no options-related rules — the resolver dropped them. The adapter does not re-check closure; the semantic-self-test (`config/validation/semantic.py` story 06b) does. The library trusts the resolver.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/effective_limits.py` exists and defines `from_resolved_config(...)` and `EffectiveLimitAdapterError`.
- [ ] `src/alphamind/risk_guardrails/guardrail_evaluation/feature_gate.py` exists and defines `classify_feature_gate(...)`.
- [ ] `from_resolved_config`, `EffectiveLimitAdapterError`, `classify_feature_gate` are re-exported from `guardrail_evaluation/__init__.py`.
- [ ] A unit test asserts the adapter produces a `LibraryConfig` whose `effective_limits` equals the input `ResolvedConfig.rule_values` (medium × normal × no overlays).
- [ ] A unit test asserts the adapter copies `escalation_zones` from `guardrails.rules` per rule ID.
- [ ] A unit test asserts the adapter does not re-cascade — for a `ResolvedConfig` constructed with a known `rule_values` map, `effective_limits` is byte-identical and no regime multiplier is reapplied.
- [ ] A unit test asserts `EffectiveLimitAdapterError` is raised when a rule in `rule_values` is missing from `guardrails.rules`.
- [ ] A unit test asserts `EffectiveLimitAdapterError` is raised when `conservative_delta_buffer_pct` is negative or exceeds 100.
- [ ] A unit test asserts the adapter is pure: two calls on equal `ResolvedConfig` inputs produce equal `LibraryConfig` outputs (equality and hash).
- [ ] A unit test asserts `classify_feature_gate` returns `FeatureDisabledRejection(reason="options_disabled", disabled_feature="options")` for an option proposal under `options_enabled=False`.
- [ ] A unit test asserts `classify_feature_gate` returns `FeatureDisabledRejection(reason="shorts_disabled", disabled_feature="shorts")` for an equity short proposal under `short_selling_enabled=False`.
- [ ] A unit test asserts `classify_feature_gate` returns `reason="options_disabled"` for a short-option proposal under both flags disabled (precedence).
- [ ] A unit test asserts `classify_feature_gate` returns `None` for every proposal when both flags are enabled.
- [ ] A unit test asserts `classify_feature_gate` returns `None` for an equity-long proposal under shorts-disabled.
- [ ] A unit test asserts the gate is action-agnostic — a `CLOSE` on an option under `options_enabled=False` returns the same rejection as `OPEN`.
- [ ] A unit test asserts `classify_feature_gate` does not raise under any combination of inputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
