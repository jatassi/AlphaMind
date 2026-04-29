---
status: not_started
completed_date:
commit_id:
---

# 08 — Active risk parameter set assembler

## Goal

Land the pure function that produces the typed `ActiveRiskParameterSet` record (raw state §4d) from the orchestrator's resolved inputs — the active regime, the new transition decision (story 04b), the resolved effective limits (post-interpolation, post-overlay), the active overlays, and the prior `ActiveRiskParameterSet` for the `parameter_change_flag` computation. Bridges the regime-adaptation feature's outputs to the portfolio-state schema the rest of the system consumes.

## Reading

- `docs/design/01-data-layer/internal/portfolio-state.md` § 4d — the `ActiveRiskParameterSet` schema and its semantic; `regime_label`, `transition_state`, `transition_invocations_remaining`, `parameter_change_flag`, `entries`, `active_overlays`
- `src/alphamind/portfolio_state/records/capital.py` — the existing `ActiveRiskParameterSet`, `ActiveRiskParameterEntry`, `RegimeLabel`, `RegimeTransitionState` Pydantic models; this story wires the data into them
- `src/alphamind/portfolio_state/computations/risk_budget.py` § `compute_parameter_change_flag` — the existing helper that compares two `ActiveRiskParameterSet` instances; this story calls it
- `02-package-skeleton-and-types.md` — `RegimeAdaptationOutput` carries the `active_risk_parameter_set` field this story populates
- `04b-transition-state-machine.md` — `NextTransitionDecision` carries `active_regime`, `transition_state`, `transition_invocations_remaining`; this story consumes those
- `04c-loosening-interpolation.md` — `resolve_active_multipliers` produces the per-rule effective multipliers; this story consumes them combined with the base profile values

## Depends on

- 02 (package skeleton + types)
- 03 (regime mapping — for the `RegimeLabel` value bridging guardrail `Regime` to portfolio-state `RegimeLabel`)
- 04b (transition state machine — for `NextTransitionDecision`)
- 04c (interpolation — for `resolve_active_multipliers`)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/parameter_set.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_parameter_set.py`.

### 1. Public function

```python
def assemble_active_risk_parameter_set(
    *,
    next_transition: NextTransitionDecision,
    base_profile_rule_values: Mapping[str, float],          # the profile's base values, pre-multiplier
    active_regime_multipliers: Mapping[str, float],         # the active regime's multiplier table
    interpolated_multipliers: Mapping[str, float],          # post-interpolation multipliers (== regime when STABLE/TIGHTENING)
    overlay_multipliers_composed: Mapping[str, float],      # product of all active overlay multipliers per rule (1.0 when no overlays touch the rule)
    active_overlays: tuple[Overlay, ...],
    rule_metadata: Mapping[str, RuleMetadata],              # rule_id → label/unit; from RuleRegistry
    prior_parameter_set: ActiveRiskParameterSet | None,
) -> ActiveRiskParameterSet
```

Returns a fully-populated `ActiveRiskParameterSet`. The orchestrator (09) then assembles `RegimeAdaptationOutput` around this, the breach detector output, and the new persisted state.

### 2. Field-by-field assembly

#### 2a. `regime_label` (portfolio-state enum)

The portfolio-state `RegimeLabel` enum (`LOW_VOL`, `NORMAL`, `ELEVATED`, `CRISIS`) and the configuration `Regime` enum (`low_vol`, `normal`, `elevated`, `crisis`) are name-equivalent up to case. The assembler maps:

```python
_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL: Mapping[Regime, RegimeLabel] = MappingProxyType({
    Regime.low_vol: RegimeLabel.LOW_VOL,
    Regime.normal: RegimeLabel.NORMAL,
    Regime.elevated: RegimeLabel.ELEVATED,
    Regime.crisis: RegimeLabel.CRISIS,
})
```

Module-level constant. Compile-time `assert` confirms every member is mapped.

#### 2b. `transition_state` (portfolio-state enum)

The portfolio-state `RegimeTransitionState` (`STABLE`, `TIGHTENING`, `LOOSENING`) is the same vocabulary as the regime-adaptation transition machine. Direct passthrough from `next_transition.transition_state`.

#### 2c. `transition_invocations_remaining`

Direct passthrough from `next_transition.transition_invocations_remaining`. The `ActiveRiskParameterSet` validator enforces `STABLE → 0` (mirrors story 02's invariant).

#### 2d. `parameter_change_flag`

Compute via the existing helper:

```python
parameter_change_flag = compute_parameter_change_flag(
    current=tentative_parameter_set,    # constructed without flag for the comparison
    prior=prior_parameter_set,
)
```

`compute_parameter_change_flag` returns `False` when `prior is None` (bootstrap), `True` when any parameter changed since prior, `False` otherwise. The helper compares regime label, the (rule_id, value) pairs, and the active_overlays.

The assembler constructs a tentative `ActiveRiskParameterSet` first (with `parameter_change_flag=False` as a placeholder), runs the helper, then rebuilds the final record with the computed flag. Pydantic v2 `frozen=True` requires reconstruction rather than mutation.

#### 2e. `entries` — the per-rule values

For each rule_id in `interpolated_multipliers`:
1. `base_value = base_profile_rule_values[rule_id]`
2. `regime_multiplier = active_regime_multipliers[rule_id]`
3. `interpolated_multiplier = interpolated_multipliers[rule_id]`
4. `overlay_multiplier = overlay_multipliers_composed[rule_id]` (default 1.0 when missing)
5. `effective_value = base_value * interpolated_multiplier * overlay_multiplier`
6. `regime_multiplier_applied = interpolated_multiplier * overlay_multiplier` (the *combined* multiplier — matches `ActiveRiskParameterEntry.regime_multiplier_applied`'s field semantic of "the total multiplier vs. base")
7. Look up `rule_metadata[rule_id]` for `label` and `unit`.
8. Construct `ActiveRiskParameterEntry(rule_id=rule_id, rule_label=label, value=effective_value, unit=unit, regime_multiplier_applied=regime_multiplier_applied, base_value=base_value)`.

The entry list is sorted alphabetically by `rule_id`. Determinism — and the existing `_assert_unique_rule_ids` validator on `ActiveRiskParameterSet` rejects duplicates anyway.

#### 2f. `active_overlays` — strings, not enum

The portfolio-state `ActiveRiskParameterSet.active_overlays: tuple[str, ...]` is typed as plain strings (the portfolio-state package does not import the `Overlay` enum). The assembler passes `tuple(sorted(overlay.value for overlay in active_overlays))` — alphabetically sorted, string-encoded.

### 3. Validation

- Raises `ValueError` if `interpolated_multipliers` has a key not in `base_profile_rule_values` (or vice versa). The two maps must align; the orchestrator (09) is responsible for ensuring alignment.
- Raises `ValueError` if `overlay_multipliers_composed` has a key not in `interpolated_multipliers`. The overlay composition is partial (overlays touch only some rules), so missing keys default to `1.0`; *extra* keys are an error.
- Raises `ValueError` if any rule_id in `interpolated_multipliers` is missing from `rule_metadata`.
- Raises `ValueError` if `next_transition.active_regime` is not in the `_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL` mapping. (Catches enum drift if a future `Regime` member is added without updating the mapping.)

### 4. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_parameter_set.py`:

- **Bootstrap (no prior set):** `prior_parameter_set=None`, normal regime, no overlays. Returns an `ActiveRiskParameterSet` with `regime_label=NORMAL`, `transition_state=STABLE`, `transition_invocations_remaining=0`, `parameter_change_flag=False` (bootstrap), `entries` matching the base profile values × regime multipliers, `active_overlays=()`.
- **Same as prior:** prior set identical to current; `parameter_change_flag=False`.
- **Regime change since prior:** prior set in `NORMAL`; current set in `ELEVATED`; `parameter_change_flag=True`.
- **Overlay activated since prior:** prior set with no overlays; current set with `(stress,)`; `parameter_change_flag=True`.
- **Overlay multiplier composition:** active overlay tightens `sector_concentration_pct` by 0.85; the entry's `value` reflects `base * regime_multiplier * 0.85` and `regime_multiplier_applied` reflects `regime_multiplier * 0.85`.
- **Multiple overlays compose multiplicatively:** `(pre_event, stress)` both touching `position_max_size_pct` (pre_event=0.80) — though stress doesn't touch this rule in the shipped config, the test uses synthetic overlays with overlapping rules to verify multiplication.
- **Loosening interpolation reflected:** `transition_state=LOOSENING`, `interpolated_multipliers["position_max_size_pct"]=0.60` (one-third interpolated from crisis 0.40 to normal 1.00); the entry's `regime_multiplier_applied=0.60`.
- **`regime_label` mapping is correct for every Regime value:** parametrized test covers all four members.
- **`transition_state` passthrough:** parametrized test covers STABLE, TIGHTENING, LOOSENING.
- **`transition_invocations_remaining` passthrough:** parametrized test covers 0 (STABLE), 1, 2, 3 (LOOSENING).
- **Sorted `entries`:** entries appear in alphabetical order by `rule_id`.
- **Sorted `active_overlays`:** input `(Overlay.stress, Overlay.pre_event)` → output `("pre_event", "stress")`.
- **Mismatched key sets between `interpolated_multipliers` and `base_profile_rule_values` raise `ValueError`.**
- **Extra key in `overlay_multipliers_composed` raises `ValueError`.**
- **Missing rule in `rule_metadata` raises `ValueError`.**
- **Validator inherited from `ActiveRiskParameterSet`:** constructing an entry with mismatched `STABLE` + non-zero `transition_invocations_remaining` raises (the Pydantic validator on the upstream model catches it).
- **Validator inherited from `ActiveRiskParameterSet`:** duplicate `rule_id` in entries raises (the model's existing `_validate_unique_entry_rule_ids`).
- **Determinism / purity:** identical inputs produce identical outputs; input mappings are not mutated.

Out of scope:

- Persisting the `ActiveRiskParameterSet` — portfolio state owns its persistence and the orchestrator (09) hands the assembled record to the portfolio-state assembler.
- Reading `RuleRegistry` — the assembler accepts `rule_metadata` as input. Translation happens in the orchestrator.
- Fan-out of `sector_concentration_pct` to per-sector ids — that's the configuration resolver's job (already handled in `compose_config`); the assembler reads whatever per-sector ids are in `interpolated_multipliers`.
- Computing `regime_multiplier_applied` as a *single regime* multiplier (excluding overlays) — the field's name might suggest that, but the existing portfolio-state field semantics are "total multiplier vs. base", which folds in interpolation and overlays. If a future consumer needs the regime-only multiplier separated from overlays, that is a portfolio-state schema additive.

## Notes

The `regime_multiplier_applied` field combines the interpolation multiplier and the overlay multipliers. This is a deliberate choice — the field's purpose is "show the operator the total scaling vs. base"; splitting into separate fields would force the renderer to reassemble for display. The base value is already separately captured in `base_value`.

Per `feedback_no_inventing_component_names.md`, `ActiveRiskParameterSet` and `ActiveRiskParameterEntry` are the existing portfolio-state types. The assembler's function name `assemble_active_risk_parameter_set` mirrors the design's "assemble" terminology used throughout state-delivery.

Per `feedback_simplify_before_building.md`, the assembler does not implement caching or memoization. It is invoked once per pipeline invocation and produces a fresh record. If a future per-call cost emerges (e.g., the rule-metadata map becomes expensive to construct), the orchestrator caches; the assembler stays pure.

Per `feedback_avoid_numeric_anchors.md`, no numeric thresholds. Default overlay multiplier `1.0` is the multiplicative identity, not a tunable.

Per `feedback_per_producer_schema.md`, the assembler emits one record. The `RegimeAdaptationOutput` (story 02) wraps this record alongside the orchestrator's other outputs.

The `_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL` constant is the only place in the regime-adaptation package that bridges between the configuration-side `Regime` enum and the portfolio-state-side `RegimeLabel` enum. The two enums exist because their respective packages have independent ownership; the bridge lives here as the consumer of both. A compile-time `assert` catches drift if either enum gains a member.

The `compute_parameter_change_flag` reuse means the assembler does not re-implement the comparison logic. The helper already encodes the "regime, rule values, overlays" comparison; the assembler trusts it. If the helper later adds another field to the comparison (e.g., transition_state), the assembler benefits without change.

The two-pass construction (build tentative without `parameter_change_flag`, run helper, rebuild final) is mildly awkward but unavoidable with frozen Pydantic models. An alternative would be to compute the flag *outside* `ActiveRiskParameterSet` and inject it; the helper's signature requires two `ActiveRiskParameterSet` instances, so the two-pass approach is the cleanest fit.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/parameter_set.py` exists and defines `assemble_active_risk_parameter_set`, `_GUARDRAIL_REGIME_TO_PORTFOLIO_STATE_LABEL`.
- [ ] `assemble_active_risk_parameter_set` is re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] Bootstrap (`prior_parameter_set=None`) returns an `ActiveRiskParameterSet` with `parameter_change_flag=False`.
- [ ] Same-as-prior input returns `parameter_change_flag=False`.
- [ ] Regime change from prior returns `parameter_change_flag=True`.
- [ ] Overlay activation since prior returns `parameter_change_flag=True`.
- [ ] Overlay multiplier composition is multiplicative: entry's `value = base * interpolated_multiplier * overlay_multiplier`; `regime_multiplier_applied = interpolated_multiplier * overlay_multiplier`.
- [ ] Loosening interpolation is reflected: when `interpolated_multipliers != active_regime_multipliers`, the entry's `value` uses the interpolated value.
- [ ] `regime_label` mapping covers all four `Regime` members (verified by parametrized test).
- [ ] `transition_state` and `transition_invocations_remaining` pass through from `NextTransitionDecision`.
- [ ] Entries are sorted alphabetically by `rule_id`.
- [ ] `active_overlays` are sorted alphabetically as strings.
- [ ] Mismatched `interpolated_multipliers` / `base_profile_rule_values` keys raise `ValueError`.
- [ ] Extra keys in `overlay_multipliers_composed` raise `ValueError`.
- [ ] Missing rule in `rule_metadata` raises `ValueError`.
- [ ] The function is pure: equal inputs produce equal outputs; the input mappings are not mutated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
