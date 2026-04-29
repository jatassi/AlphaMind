---
status: in_progress
completed_date:
commit_id:
---

# 04c — Loosening interpolation

## Goal

Land the pure function that, during a `LOOSENING` transition, computes the *interpolated* per-rule effective limits at any point in the 3-invocation linear interpolation window. The state machine (04b) produces the `NextTransitionDecision` carrying `transition_invocations_remaining`; this primitive consumes that count plus the regime multipliers for the origin and destination regimes, and emits the per-rule effective multiplier the configuration resolver consumes for this invocation. Asymmetric to tightening, which has no interpolation step (the destination regime's multipliers apply immediately).

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Transition mechanics — the loosening interpolation contract: `Active limit = old_limit + (new_limit - old_limit) × (invocations_since_transition / 3)`. This story's primitive is the formula encoded
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Parameter sets per regime — the multiplier table; per-rule values for each regime; some rules have multiplier `1.00` (no change) — interpolation across an unchanging multiplier still produces `1.00` and the test suite confirms that
- `src/alphamind/config/models/regimes.py` — `RegimeConfig.multipliers: dict[str, float]`; the per-rule multiplier maps the interpolation primitive composes
- `02-package-skeleton-and-types.md` — `RegimeAdaptationState` carries the state-machine output the orchestrator consults to call this primitive
- `04b-transition-state-machine.md` — the `transition_invocations_remaining` semantic: `remaining=3` is the first interpolation invocation (fraction `1/3`); `remaining=1` is the last interpolation invocation (fraction `1`); after `remaining=0` the state transitions to `STABLE` and the primitive is no longer called

## Depends on

- 02 (package skeleton + types)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/interpolation.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_interpolation.py`.

### 1. Public function

```python
def interpolate_loosening_multipliers(
    *,
    origin_multipliers: Mapping[str, float],
    destination_multipliers: Mapping[str, float],
    transition_invocations_remaining: int,
    loosening_invocations: int = LOOSENING_INVOCATIONS,  # default 3, imported from types.py
) -> Mapping[str, float]
```

Returns a `Mapping[str, float]` of `rule_id → interpolated_multiplier` for every rule present in *both* `origin_multipliers` and `destination_multipliers`. The return is a `MappingProxyType` view over a fresh dict — read-only.

### 2. Interpolation formula

For each rule present in both maps, compute:

```
fraction = (loosening_invocations - transition_invocations_remaining + 1) / loosening_invocations
interpolated = origin_multiplier + (destination_multiplier - origin_multiplier) * fraction
```

Where:
- `transition_invocations_remaining=3, loosening_invocations=3` → `fraction = (3-3+1)/3 = 1/3`
- `transition_invocations_remaining=2, loosening_invocations=3` → `fraction = 2/3`
- `transition_invocations_remaining=1, loosening_invocations=3` → `fraction = 1` (full destination value)

Boundary semantics:
- `fraction=1/3` → one-third of the way from origin to destination
- `fraction=1` → full destination value (the last interpolation invocation operates at the new looser limits)

The formula is deliberately symmetric for an unchanging multiplier (`origin == destination` for a given rule, e.g., `daily_drawdown_pct` has multiplier `1.00` in `low-vol` and `1.00` in `normal` — no change): `interpolated = origin + (origin - origin) * fraction = origin`. The interpolation is a no-op for unchanged multipliers, which preserves the "drawdown limits don't loosen in low-vol" invariant from the design doc without special-casing.

### 3. Validation

- Raises `ValueError` if `transition_invocations_remaining` is not in `[1, loosening_invocations]`. Specifically, `remaining=0` is invalid — the orchestrator does not call this primitive when the prior state has already transitioned to `STABLE`. `remaining > loosening_invocations` is also invalid (would imply a fraction below `1/3`, outside the design's window).
- Raises `ValueError` if `loosening_invocations < 1`.
- Raises `ValueError` if `origin_multipliers` and `destination_multipliers` have different key sets (the function does not silently drop or invent rules; the caller must align the maps before calling).
- Raises `ValueError` if any multiplier value is non-positive (zero or negative — both invalid for a guardrail multiplier per the existing `RegimeConfig` validator).

### 4. `LOOSENING_INVOCATIONS` import

`LOOSENING_INVOCATIONS` is the canonical constant declared in `02-package-skeleton-and-types.md` § 2l. Imported here:

```python
from alphamind.risk_guardrails.regime_adaptation.types import LOOSENING_INVOCATIONS
```

Used as the default value for the `loosening_invocations` parameter on `interpolate_loosening_multipliers(...)`. The state machine (story 04b) imports the same constant — single source of truth, no duplication.

If a future per-regime loosening-duration policy lands, this constant becomes a default and the resolver passes the per-regime value through as the `loosening_invocations` argument. Until then, the literal `3` lives in `types.py` and only there.

### 5. Helper for the orchestrator

```python
def resolve_active_multipliers(
    *,
    transition_state: RegimeTransitionState,
    transition_invocations_remaining: int,
    active_regime_multipliers: Mapping[str, float],
    transition_origin_multipliers: Mapping[str, float] | None,
) -> Mapping[str, float]
```

Convenience wrapper the orchestrator (09) calls instead of dispatching on `transition_state` itself:

- `STABLE` or `TIGHTENING` → return `active_regime_multipliers` verbatim. No interpolation.
- `LOOSENING` → return `interpolate_loosening_multipliers(origin_multipliers=transition_origin_multipliers, destination_multipliers=active_regime_multipliers, transition_invocations_remaining=transition_invocations_remaining)`. Raises `ValueError` if `transition_origin_multipliers is None` (the LOOSENING invariant from story 02 requires the origin to be populated; this is the runtime check).

This wrapper keeps the orchestrator's call site uniform; it does not introduce a new state-machine concern.

### 6. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_interpolation.py`:

- **Linear interpolation at `remaining=3`** (first interpolation step):
  - `origin = {"position_max_size_pct": 0.40}` (crisis multiplier), `destination = {"position_max_size_pct": 1.00}` (normal multiplier), `remaining=3` → `interpolated = 0.40 + (1.00 - 0.40) * (1/3) = 0.60`.
- **Linear interpolation at `remaining=2`:** same maps, `remaining=2` → `interpolated = 0.40 + 0.60 * (2/3) = 0.80`.
- **Linear interpolation at `remaining=1`:** same maps, `remaining=1` → `interpolated = 0.40 + 0.60 * 1.0 = 1.00`. (Full destination value at the last interpolation step.)
- **Multiple rules interpolate independently:** `origin = {"position_max_size_pct": 0.70, "sector_concentration_pct": 0.80, "daily_drawdown_pct": 1.00}`, `destination = {"position_max_size_pct": 1.00, "sector_concentration_pct": 1.00, "daily_drawdown_pct": 1.00}`, `remaining=2` → `interpolated["position_max_size_pct"] = 0.90`, `interpolated["sector_concentration_pct"] = 0.867` (within `1e-9`), `interpolated["daily_drawdown_pct"] = 1.00` (no-op for unchanged multiplier).
- **Tightening multiplier (origin > destination)** is interpolated the same way; the function is direction-agnostic. `origin = {"x": 1.20}`, `destination = {"x": 0.40}`, `remaining=3` → `interpolated["x"] = 1.20 + (0.40 - 1.20) * (1/3) = 0.933` (within `1e-9`). The interpolation primitive itself does not enforce direction; the orchestrator only calls it when `transition_state == LOOSENING`, but the primitive is total over both directions for testability.
- **Mismatched key sets raise `ValueError`** naming the offending keys (the symmetric-difference set).
- **`remaining=0` raises `ValueError`.**
- **`remaining=4` (above `loosening_invocations=3`) raises `ValueError`.**
- **`loosening_invocations=0` raises `ValueError`.**
- **Negative multiplier in `origin_multipliers` raises `ValueError`.**
- **Zero multiplier in `destination_multipliers` raises `ValueError`.**
- **Custom `loosening_invocations`:** `loosening_invocations=2`, `remaining=2` → `fraction = (2-2+1)/2 = 1/2`. `loosening_invocations=2`, `remaining=1` → `fraction = 1`. Verifies the formula generalizes.
- **Determinism:** identical inputs produce identical outputs.
- **Output is a `MappingProxyType`:** assert `type(result).__name__ == "mappingproxy"` (or the equivalent type check). Mutations to the underlying dict are not visible to the caller.
- **`resolve_active_multipliers` (helper):**
  - `STABLE` returns the active map verbatim (same identity is fine; same dict contents required).
  - `TIGHTENING` returns the active map verbatim.
  - `LOOSENING` with `origin=None` raises `ValueError`.
  - `LOOSENING` with valid origin returns the interpolated map.

Out of scope:

- Reading the regime multipliers from `RegimeConfig`. The orchestrator (09) does the YAML→dict adaptation.
- Computing the *effective limits* (after applying overlay multipliers and feature-flag closure) — the configuration resolver (`compose_config` in `src/alphamind/config/resolver.py`) handles overlay composition; this primitive emits the regime-multiplier layer.
- Persisting the interpolation result.
- Detecting whether the transition is loosening vs. tightening — the state machine (04b) classified the transition; this primitive is invoked only for `LOOSENING`.

## Notes

The interpolation formula's `+1` offset (`(loosening_invocations - remaining + 1) / loosening_invocations`) makes the *last* interpolation invocation reach the full destination value (fraction `1`). The next invocation transitions to `STABLE` and uses the destination value directly. This means the design's "+2 invocations" timing is preserved: the transition-detected invocation is fraction `1/3`; +1 invocation is `2/3`; +2 invocations is `1`; from then on the regime is `STABLE` and no interpolation runs.

A reader noticing the `+1` offset and wondering whether the formula should be `(loosening_invocations - remaining) / loosening_invocations` (which would give `0`, `1/3`, `2/3` and never reach `1` during interpolation) should refer back to story 04b's countdown semantic: `remaining=1` is the *last* interpolation invocation, not a "two-thirds" intermediate; the next invocation is already `STABLE`. The `+1` offset is what makes that consistent.

Per `feedback_avoid_numeric_anchors.md`, the only literal in the module is the `LOOSENING_INVOCATIONS` default, and that constant lives canonically in `types.py` (story 02 § 2l), traceable to the design doc and to `regimes/*.yaml`'s `transition.loosen_on_exit` enum value. No other thresholds.

Per `feedback_simplify_before_building.md`, the function does not implement non-linear interpolation curves (sigmoid, ease-in-out, etc.). Linear is what the design specifies and the simplest option that satisfies the contract. If an operator later argues for a different curve, that is a follow-up — the function signature accommodates the change without breaking callers.

The `mismatched key sets raise ValueError` invariant catches the case where the orchestrator hands the primitive `origin_multipliers` from one regime and `destination_multipliers` from another, and the two regimes have different rule sets (e.g., a future regime that adds a rule the prior regime did not). Surfaces structural drift rather than silently dropping the unmatched rules.

Per `feedback_per_producer_schema.md`, the primitive returns the per-rule multiplier map (one rule → one float). It does not return per-rule rationale strings, audit metadata, or per-step records — those would conflate orchestration with computation. The orchestrator (09) layers audit metadata on top.

The `resolve_active_multipliers` helper is the only place in the orchestrator that needs to dispatch on `transition_state`; co-locating it here keeps the dispatch logic next to the interpolation formula. If a future caller (e.g., the proposal pre-processor) wants to reason about the interpolation independently, it imports `interpolate_loosening_multipliers` directly.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/interpolation.py` exists and defines `interpolate_loosening_multipliers` and `resolve_active_multipliers`. `LOOSENING_INVOCATIONS` is imported from `alphamind.risk_guardrails.regime_adaptation.types` (canonical declaration in story 02 § 2l); not redeclared here.
- [ ] `interpolate_loosening_multipliers` and `resolve_active_multipliers` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`. (`LOOSENING_INVOCATIONS` is already re-exported via story 02.)
- [ ] At `remaining=3, loosening=3`, the interpolation produces fraction `1/3` (one-third of the way from origin to destination).
- [ ] At `remaining=2`, fraction `2/3`.
- [ ] At `remaining=1`, fraction `1` (full destination value).
- [ ] Multiple rules interpolate independently — each rule's multiplier is computed against its own `(origin, destination)` pair.
- [ ] Unchanged multipliers (origin == destination for a rule) interpolate to the same value (no-op).
- [ ] The function is direction-agnostic — works for both loosening (origin > destination) and tightening (origin < destination), but the orchestrator only calls it for loosening.
- [ ] Mismatched key sets raise `ValueError` naming the symmetric-difference keys.
- [ ] `remaining=0`, `remaining > loosening_invocations`, `loosening_invocations < 1` all raise `ValueError`.
- [ ] Non-positive multiplier values in either input map raise `ValueError`.
- [ ] The output mapping is read-only (`MappingProxyType`); mutations to the underlying dict are not visible.
- [ ] `resolve_active_multipliers` returns the active map verbatim for `STABLE` and `TIGHTENING`.
- [ ] `resolve_active_multipliers` for `LOOSENING` with `origin=None` raises `ValueError`.
- [ ] `resolve_active_multipliers` for `LOOSENING` with valid origin returns the interpolated map.
- [ ] The function is pure: equal inputs produce equal outputs; the input mappings are not mutated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
