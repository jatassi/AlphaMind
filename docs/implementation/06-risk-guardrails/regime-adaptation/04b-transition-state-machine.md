---
status: not_started
completed_date:
commit_id:
---

# 04b — Transition state machine

## Goal

Land the pure function that, given the previously-persisted `RegimeAdaptationState` and the freshly-classified `Regime` for the current invocation, computes the new transition state — `STABLE`, `TIGHTENING`, or `LOOSENING` — together with `transition_invocations_remaining` and the populated-during-loosening fields (`transition_started_invocation_id`, `transition_origin_regime`). Encodes the asymmetric mechanics from `regime-adaptation.md`: tightening is immediate (one-step transition to `STABLE` after the new regime is in effect); loosening is gradual over 3 invocations (linear interpolation; tightening during loosening aborts the interpolation).

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Transition mechanics — the full mechanical contract: tightening immediate, loosening over 3 invocations, tightening always overrides loosening
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Position handling when tightening creates breaches — defers position remedies to strategist + PM, but the transition state machine itself does not handle positions; this story is the state-of-the-machine function
- `src/alphamind/config/models/regimes.py` — `Regime` StrEnum (the four guardrail regimes); the regime ordering for tighten-vs-loosen detection (`low_vol < normal < elevated < crisis`) is the canonical volatility-increase direction
- `src/alphamind/portfolio_state/records/capital.py` — `RegimeTransitionState` (`STABLE`, `TIGHTENING`, `LOOSENING`)
- `src/alphamind/distillation/regime.py` § `compute_transition_state` — the *distillation*-side transition state machine for reference; this story is the *guardrail*-side machine and is structurally different (different state space, different invariants)
- `02-package-skeleton-and-types.md` — `RegimeAdaptationState` typed record and its invariants (`STABLE` → `remaining == 0`, `LOOSENING` → `origin` and `started` populated); the transition machine produces records that respect those invariants
- `04a-regime-adaptation-state-persistence.md` — the persisted shape this function consumes (prior) and produces (new)

## Depends on

- 02 (package skeleton + types)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/transition_machine.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_transition_machine.py`.

### 1. Public function

```python
def compute_next_transition(
    *,
    new_regime: Regime,
    invocation_id: str,
    prior_state: RegimeAdaptationState | None,
) -> NextTransitionDecision
```

Returns a small typed record:

```python
@dataclass(frozen=True, slots=True)
class NextTransitionDecision:
    """The transition-state-machine output the orchestrator persists."""
    active_regime: Regime
    prior_regime: Regime | None                       # passthrough of new_regime's predecessor for audit
    transition_state: RegimeTransitionState
    transition_invocations_remaining: int
    transition_started_invocation_id: str | None
    transition_origin_regime: Regime | None
```

The orchestrator (story 09) constructs the full `RegimeAdaptationState` by combining this decision with the auxiliary passthrough fields (`distillation_regime_label`, `distillation_vix_level`, `regime_skip_emergency`, `active_overlays`, `as_of`).

### 2. Regime ordering helper

```python
_REGIME_VOLATILITY_LADDER: tuple[Regime, ...] = (
    Regime.low_vol,
    Regime.normal,
    Regime.elevated,
    Regime.crisis,
)


def regime_ladder_index(regime: Regime) -> int:
    """Return the regime's position in the increasing-volatility ladder."""
    return _REGIME_VOLATILITY_LADDER.index(regime)
```

Higher index = higher volatility. Tightening = move to higher index; loosening = move to lower index.

### 3. Decision rules

The function's logic, in order:

1. **Bootstrap (no prior state).** Return `NextTransitionDecision(active_regime=new_regime, prior_regime=None, transition_state=STABLE, transition_invocations_remaining=0, transition_started_invocation_id=None, transition_origin_regime=None)`.

2. **No regime change.**
   - If `prior_state.transition_state == LOOSENING`: continue the loosening interpolation. Decrement `transition_invocations_remaining` by 1; if it reaches `0`, the next state is `STABLE` (interpolation complete); else `LOOSENING` with the decremented count. The `transition_started_invocation_id` and `transition_origin_regime` carry forward unchanged during the countdown; they reset to `None` on the `STABLE` step.
   - If `prior_state.transition_state == TIGHTENING`: tightening is a single-step transition. The next state is `STABLE` with `remaining=0`, both Optional fields `None`.
   - If `prior_state.transition_state == STABLE`: stay `STABLE`.

3. **Tightening transition** (`new_regime` index > `prior_state.active_regime` index):
   - Return `NextTransitionDecision(active_regime=new_regime, prior_regime=prior_state.active_regime, transition_state=TIGHTENING, transition_invocations_remaining=0, transition_started_invocation_id=None, transition_origin_regime=None)`.
   - **Tightening overrides loosening.** If `prior_state.transition_state == LOOSENING` and the new regime is *higher* than `prior_state.active_regime`, the loosening interpolation is abandoned and the new state is `TIGHTENING`. The previously-pending loosening leaves no residue.
   - **Tightening overrides tightening.** A second tightening event on top of an already-tightening (or already-stable-after-tightening) prior state simply moves to the new (higher) regime with `transition_state=TIGHTENING`. The prior_regime field captures the immediate predecessor.

4. **Loosening transition** (`new_regime` index < `prior_state.active_regime` index):
   - The loosening interpolation runs over 3 invocations starting *now*. The first invocation's state is `LOOSENING` with `transition_invocations_remaining=3` (the interpolation primitive in 04c interprets the remaining count to compute the per-rule interpolation step).
   - Return `NextTransitionDecision(active_regime=new_regime, prior_regime=prior_state.active_regime, transition_state=LOOSENING, transition_invocations_remaining=3, transition_started_invocation_id=invocation_id, transition_origin_regime=prior_state.active_regime)`.
   - **Loosening during loosening** (e.g., `elevated → normal → low_vol` over consecutive invocations): the second loosening *resets* the interpolation. New origin = current `active_regime` of the prior state (which was already partway through an interpolation). New `transition_invocations_remaining=3`. New `transition_started_invocation_id=invocation_id`. The prior interpolation's residual is discarded.
   - **Loosening during tightening** (rare — a prior state of `TIGHTENING` then a loosen back): the prior `TIGHTENING` was a one-step transition, so the prior `active_regime` is the higher regime. A loose-back simply starts a new loosening interpolation from that higher regime to the new lower regime. Same return shape.

### 4. Implementation requirements

- The function is pure: equal inputs produce equal outputs; no global state read or written.
- The function does not raise on any well-typed input. The typed-record invariants (story 02) and the persistence-layer CHECK constraints (story 04a) catch malformed states upstream and downstream.
- The function does not look at `transition_invocations_remaining` of a prior `STABLE` or `TIGHTENING` state — those are always `0` by invariant. It looks at `transition_invocations_remaining` only when the prior state is `LOOSENING`.

### 5. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_transition_machine.py`:

- **Bootstrap:** `compute_next_transition(new_regime=Regime.normal, invocation_id="INV-1", prior_state=None)` returns `STABLE`, `remaining=0`, both Optional fields `None`, `prior_regime=None`.
- **No-change stable:** prior state STABLE, `Regime.normal`; new `Regime.normal`. Returns STABLE, `remaining=0`, both Optionals `None`, `prior_regime=Regime.normal`.
- **No-change tightening → stable:** prior state TIGHTENING, `Regime.elevated`; new `Regime.elevated`. Returns STABLE, `remaining=0`.
- **No-change loosening countdown:** prior state LOOSENING, `active_regime=Regime.normal`, `remaining=3`, `origin=Regime.elevated`, `started_invocation="INV-1"`; new `Regime.normal`. Returns LOOSENING, `remaining=2`, `origin=Regime.elevated`, `started="INV-1"`.
- **Loosening countdown to 1:** prior LOOSENING `remaining=2`; same regime. Returns LOOSENING `remaining=1`, fields carry forward.
- **Loosening countdown completing:** prior LOOSENING `remaining=1`; same regime. Returns STABLE `remaining=0`, both Optionals `None`.
- **Tightening transition from STABLE:** prior STABLE `Regime.normal`; new `Regime.elevated`. Returns TIGHTENING, `remaining=0`, both Optionals `None`, `prior_regime=Regime.normal`.
- **Tightening transition skipping a band:** prior STABLE `Regime.low_vol`; new `Regime.crisis`. Returns TIGHTENING, `prior_regime=Regime.low_vol`.
- **Tightening overrides loosening:** prior LOOSENING `active_regime=Regime.normal`, `remaining=2`, `origin=Regime.elevated`, `started="INV-1"`; new `Regime.elevated`. Returns TIGHTENING, `remaining=0`, both Optionals `None`, `prior_regime=Regime.normal`. The prior interpolation's residue is dropped.
- **Tightening from TIGHTENING:** prior TIGHTENING `active_regime=Regime.elevated`; new `Regime.crisis`. Returns TIGHTENING, `prior_regime=Regime.elevated`.
- **Loosening transition from STABLE:** prior STABLE `Regime.elevated`; new `Regime.normal`, `invocation_id="INV-2"`. Returns LOOSENING, `remaining=3`, `started_invocation_id="INV-2"`, `origin=Regime.elevated`.
- **Loosening skipping a band:** prior STABLE `Regime.crisis`; new `Regime.low_vol`. Returns LOOSENING, `remaining=3`, `origin=Regime.crisis`.
- **Loosening during loosening (resets interpolation):** prior LOOSENING `active_regime=Regime.normal`, `remaining=2`, `origin=Regime.elevated`, `started="INV-1"`; new `Regime.low_vol`, `invocation_id="INV-3"`. Returns LOOSENING, `remaining=3`, `started="INV-3"`, `origin=Regime.normal`. (Origin is the *prior active*, not the prior origin.)
- **Loosening from TIGHTENING:** prior TIGHTENING `active_regime=Regime.crisis`; new `Regime.elevated`, `invocation_id="INV-4"`. Returns LOOSENING, `remaining=3`, `started="INV-4"`, `origin=Regime.crisis`.
- **`regime_ladder_index` ordering:** assert `regime_ladder_index(low_vol) < regime_ladder_index(normal) < regime_ladder_index(elevated) < regime_ladder_index(crisis)`.
- **`regime_ladder_index` covers every member:** asserting via iteration over `Regime` confirms no member is missing from the ladder.
- **Determinism:** identical inputs produce identical outputs; the prior_state argument is not mutated.
- **`NextTransitionDecision` invariants:** every produced decision satisfies `STABLE → remaining=0 AND both Optionals None` and `LOOSENING → both Optionals populated`. Verified by a property-style test that runs every test scenario and asserts the invariants on the result.

Out of scope:

- Computing the *interpolated effective limits* — that is story 04c's pure function. This story produces only the state-machine decision; the interpolation primitive consumes the LOOSENING countdown.
- Persisting the decision — story 04a's `insert_state` is the writer.
- Detecting overlay activation changes — overlays activate independently of regime transitions; stories 06a/06b cover those.
- Detecting *whether* a regime change should trigger an emergency invocation — the regime-skip flag is computed by the distillation layer (`regime_skip_emergency` on the distillation block) and flows through the orchestrator unchanged.

## Notes

The "tightening overrides loosening" rule is the core asymmetry. A real-world example: regime moves `elevated → normal` (loosening starts). Two invocations later, while the loosening interpolation has the limits two-thirds of the way to `normal`, a flash crash drives the regime back to `elevated`. The state machine immediately returns `TIGHTENING` with `prior_regime=normal` — the loosening's residual two-thirds-of-the-way limits are abandoned, and the next invocation operates at the full `elevated` regime values.

Per `feedback_no_inventing_component_names.md`, `RegimeTransitionState` (STABLE/TIGHTENING/LOOSENING) is the existing portfolio-state enum; this story does not introduce a sibling enum. The `NextTransitionDecision` typed record is the only new name; it mirrors the structure of the existing state record minus the passthrough fields the orchestrator wires in.

Per `feedback_avoid_numeric_anchors.md`, the function uses one numeric constant (`3` invocations of loosening interpolation), and that constant lives in the regime-adaptation design doc and is mirrored in `regimes/*.yaml`'s `transition.loosen_on_exit: linear_over_invocations_3`. The state machine reads it from a module-level constant `_LOOSENING_INVOCATIONS = 3` rather than from YAML — the value is mechanical, not operator-tunable per regime, and matches the spec name. If a future regime config wants a different loosening duration per regime, the constant becomes a parameter; the function signature accepts the change without breaking callers.

Per `feedback_simplify_before_building.md`, the state machine does not implement a "freeze loosening on overlay activation" or "extend loosening duration during high stress" mechanism. Such heuristics would mask the underlying signal — the design says loosening is mechanical and stress overlays apply on top. If real operating data later argues for blending the two, that is a follow-up design decision, not a preemptive feature.

The countdown semantic — `remaining=3` on the first loosening invocation, decrementing to `0` (which transitions to STABLE) — is what the interpolation primitive (04c) reads to compute the linear interpolation fraction. Specifically, the interpolation fraction is `(LOOSENING_INVOCATIONS - remaining + 1) / LOOSENING_INVOCATIONS`:
- `remaining=3` → fraction `1/3` (one-third toward new limit)
- `remaining=2` → fraction `2/3`
- `remaining=1` → fraction `3/3 = 1` (full new limit)
- After `remaining=0` (transition to STABLE), the limit is the full new limit by definition

The `remaining=1` invocation is the last invocation operating under interpolation; the next invocation transitions to `STABLE` and uses the full new limit. This matches the design's "Full new (looser) limit" at "+2 invocations" timing.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/transition_machine.py` exists and defines `compute_next_transition`, `NextTransitionDecision`, `regime_ladder_index`, `_REGIME_VOLATILITY_LADDER`.
- [ ] `compute_next_transition` and `NextTransitionDecision` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] Bootstrap (no prior state) returns `STABLE`, `remaining=0`, both Optionals `None`.
- [ ] No-change `STABLE` returns `STABLE`, `remaining=0`.
- [ ] No-change `TIGHTENING` returns `STABLE`, `remaining=0`.
- [ ] No-change `LOOSENING` decrements `remaining` by 1; carries `origin` and `started` forward; transitions to `STABLE` when `remaining` reaches 0.
- [ ] Tightening transition produces `TIGHTENING` with `remaining=0`, both Optionals `None`, `prior_regime` correctly populated.
- [ ] Tightening overrides a prior `LOOSENING` — the new state is `TIGHTENING`, the prior interpolation residue is discarded.
- [ ] Loosening transition produces `LOOSENING` with `remaining=3`, `started=invocation_id`, `origin=prior active_regime`.
- [ ] Loosening during loosening resets the interpolation — new `remaining=3`, new `started=invocation_id`, new `origin=prior active_regime` (not prior origin).
- [ ] `regime_ladder_index` returns indices in the order `low_vol(0) < normal(1) < elevated(2) < crisis(3)`.
- [ ] Every produced `NextTransitionDecision` satisfies the invariants `STABLE → remaining=0 AND both Optionals None` and `LOOSENING → both Optionals populated`.
- [ ] The function is pure: equal inputs produce equal outputs; the input record is not mutated.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.
