# 01 — compose_active_risk_parameters primitive

## Goal

Ship the pure function `compose_active_risk_parameters` under `src/alphamind/execution/guardrail_enforcement/composition.py` that applies cumulative-drawdown progressive-tier overrides on top of a regime-resolved `ActiveRiskParameterSet`. Wraps two existing breach-behavior primitives (`classify_cumulative_drawdown_tier` + `apply_progressive_tier_overrides`) into a single canonical entry point. Returns a tuple `(final_parameter_set, drawdown_tier)` so callers can render both. Consumed by story 02's orchestrator, and (eventually) by the continuous monitor ([ALP-123](https://linear.app/alphamind-jatassi/issue/ALP-123/continuous-monitor)) when recomputing parameters mid-session.

## Reading

* `docs/design/05-execution-layer/architecture.md` § 3. Guardrail enforcement layer — where this primitive lives architecturally.
* `docs/design/06-risk-guardrails/breach-behavior.md` § Cumulative drawdown response — tier semantics this primitive implements.
* `src/alphamind/risk_guardrails/breach_behavior/drawdown_tiers.py` — `classify_cumulative_drawdown_tier` and `apply_progressive_tier_overrides` are the two primitives this story composes; read both signatures end-to-end.
* `src/alphamind/risk_guardrails/breach_behavior/types.py` § `DrawdownTier` enum — `CONSTRAINED` / `HEAVILY_CONSTRAINED` / `FULL_HALT`.
* `src/alphamind/portfolio_state/records/capital.py` § `ActiveRiskParameterSet` and `DrawdownState` — typed records consumed.
* `src/alphamind/config/models/guardrails.py` § `ProgressiveTier` — config record describing tier triggers and overrides.
* `tests/risk_guardrails/breach_behavior/test_e2e_scenarios.py` § A8 cumulative drawdown tier 2 — the inlined composition this primitive replaces. Mirror its assertions in the new test module.

## Depends on

* (none — all upstream features are Done)

## Scope

In scope, all under `src/alphamind/execution/guardrail_enforcement/`. Tests at `tests/execution/guardrail_enforcement/`.

### 1\. `compose_active_risk_parameters` function

`src/alphamind/execution/guardrail_enforcement/composition.py` (new file). Pure synchronous function; no I/O.

```python
def compose_active_risk_parameters(
    *,
    regime_resolved_parameters: ActiveRiskParameterSet,
    drawdown_state: DrawdownState,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> tuple[ActiveRiskParameterSet, DrawdownTier | None]:
    """Apply cumulative-drawdown progressive-tier overrides on top of regime-resolved parameters.

    1. Classify the drawdown tier via ``classify_cumulative_drawdown_tier``.
    2. Apply the tier override via ``apply_progressive_tier_overrides`` (no-op when tier is None).
    3. Return the final parameter set + the classified tier.

    Pure function. No DB reads, no mutation of inputs. Same inputs always
    produce identical outputs.
    """
```

### 2\. Public surface

In `src/alphamind/execution/guardrail_enforcement/__init__.py`:

```python
from alphamind.execution.guardrail_enforcement.composition import (
    compose_active_risk_parameters,
)
```

### Out of scope

* Reading `DrawdownState` from the repository — caller's responsibility.
* Calling `resolve_regime_adaptation` to produce the input parameter set — caller's responsibility.
* Persistence of the composed parameter set — caller's responsibility (handled by the assembler's existing flow once the provider is wired in story 03a).
* Daily drawdown halt classification — that is `breach_behavior.compute_halt_state`, which is separate from progressive cumulative-drawdown tier overrides and not in this work tree's scope.
* Bundling into a wider result type — story 02 ships `Phase1EnforcementResult`; this story stays narrow.

## Acceptance criteria

- [ ] `compose_active_risk_parameters` is importable from `alphamind.execution.guardrail_enforcement`.
- [ ] When `drawdown_state.current_drawdown_pct == 0.0`, returns `(regime_resolved_parameters, None)` — input parameter set is returned unchanged (object equality is acceptable; the function should pass through, not deep-copy).
- [ ] When `current_drawdown_pct` clears the first non-halt tier's `trigger_pct` from the shipped `BreachBehaviorConfig`, returns parameters with `position_max_size_pct` and `gross_exposure_pct` entries clamped to the tier's override values, plus `cumulative_drawdown_tier_1` appended to `active_overlays`. Returned `DrawdownTier` is `CONSTRAINED`.
- [ ] When `current_drawdown_pct` clears the second non-halt tier's `trigger_pct`, returns parameters clamped to that tier's overrides plus `cumulative_drawdown_tier_2` overlay tag. Returned `DrawdownTier` is `HEAVILY_CONSTRAINED`.
- [ ] When `current_drawdown_pct` clears the full-halt tier's `trigger_pct`, returns parameters with the `cumulative_drawdown_tier_3` overlay tag appended (no per-rule overrides — full-halt is enforced via the action vocabulary, not parameter values). Returned `DrawdownTier` is `FULL_HALT`.
- [ ] The override never loosens: when `regime_resolved_parameters` already has a tighter value than the tier limit (e.g., crisis regime), the tighter value is preserved. Test asserts this for at least one rule × tier combination.
- [ ] Determinism guard: the function called five times with the same inputs returns identical outputs. (Mirror `_assert_deterministic` from `tests/risk_guardrails/breach_behavior/test_e2e_scenarios.py`.)
- [ ] One test reproduces the inlined composition pattern from `test_e2e_scenarios.py § A8` against the same fixtures, asserting the new primitive yields the same result.
- [ ] All tier values referenced come from `load_breach_behavior_config(config/breach_behavior.yaml).progressive_tiers` — no hard-coded numerics in test bodies.
- [ ] `tests/execution/guardrail_enforcement/test_composition.py` exists and passes under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/guardrail_enforcement/test_composition.py -n auto -v` — all new tests pass.
* Run `uv run pytest -n auto` — full suite green.
* Lint clean per CLAUDE.md.