# 02 — compose_phase_1_enforcement orchestrator

## Goal

Ship `compose_phase_1_enforcement` and `Phase1EnforcementResult` under `src/alphamind/execution/guardrail_enforcement/orchestrator.py` — the per-invocation Phase 1 entry point that wraps a regime adaptation output with the drawdown tier composition primitive (story 01) and bundles the result. Pure function; caller is responsible for invoking `resolve_regime_adaptation` and reading `DrawdownState`. Consumed by stories 03a / 03b in this work tree, by the eventual decision-layer pipeline composition ([ALP-310](https://linear.app/alphamind-jatassi/issue/ALP-310/decision-layer-pipeline-composition-wiring)), and by `verify_guardrail_enforcement.py` (story 04).

## Reading

* `docs/design/05-execution-layer/architecture.md` § 3. Guardrail enforcement layer.
* `docs/design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload — Source of truth — names this Phase 1 step.
* `src/alphamind/risk_guardrails/regime_adaptation/orchestrator.py` § `resolve_regime_adaptation` — produces `RegimeAdaptationOutput.active_risk_parameter_set` (regime-resolved + overlay-applied; pre-tier-override).
* `src/alphamind/risk_guardrails/regime_adaptation/types.py` § `RegimeAdaptationOutput` — input shape consumed.
* `src/alphamind/portfolio_state/records/capital.py` § `DrawdownState`, `ActiveRiskParameterSet`.
* `src/alphamind/risk_guardrails/breach_behavior/types.py` § `DrawdownTier` enum.
* `src/alphamind/execution/guardrail_enforcement/composition.py` (story 01) — the primitive this orchestrator wraps.

## Depends on

* [ALP-394](https://linear.app/alphamind-jatassi/issue/ALP-394/01-compose-active-risk-parameters-primitive) (this work tree, story 01) — for `compose_active_risk_parameters`.

## Scope

In scope, all under `src/alphamind/execution/guardrail_enforcement/`. Tests at `tests/execution/guardrail_enforcement/`.

### 1\. `Phase1EnforcementResult` typed record

`src/alphamind/execution/guardrail_enforcement/orchestrator.py` (new file). Frozen Pydantic model.

```python
class Phase1EnforcementResult(BaseModel):
    """Per-invocation Phase 1 enforcement-layer output.

    Bundles the canonical ``ActiveRiskParameterSet`` (regime-resolved +
    drawdown-tier-overridden) consumed by state-delivery renderers, the
    validation tool, and the engine T3 check, plus the classified
    drawdown tier for downstream halt-mode and emergency-trigger logic.
    """

    model_config = ConfigDict(frozen=True)

    active_risk_parameters: ActiveRiskParameterSet
    drawdown_tier: DrawdownTier | None
```

### 2\. `compose_phase_1_enforcement` function

In the same module:

```python
def compose_phase_1_enforcement(
    *,
    regime_output: RegimeAdaptationOutput,
    drawdown_state: DrawdownState,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> Phase1EnforcementResult:
    """Compose Phase 1 enforcement output from regime + drawdown inputs.

    Extracts ``regime_output.active_risk_parameter_set`` as the regime-resolved
    starting point, calls :func:`compose_active_risk_parameters` to apply
    progressive-tier overrides, and bundles the result.

    Pure function — same inputs always produce identical outputs. Caller
    is responsible for invoking ``resolve_regime_adaptation`` and reading
    ``DrawdownState`` from the repository.
    """
```

### 3\. Public surface

In `src/alphamind/execution/guardrail_enforcement/__init__.py`:

```python
from alphamind.execution.guardrail_enforcement.orchestrator import (
    Phase1EnforcementResult,
    compose_phase_1_enforcement,
)
```

### Out of scope

* Calling `resolve_regime_adaptation` — caller's job.
* Reading `DrawdownState` from the repository — caller's job.
* Loading `progressive_tiers` from `BreachBehaviorConfig` — caller's job (one-shot at config-load time).
* Wiring into the repository's `active_risk_parameters_provider` — story 03a.
* Migrating verify scripts and decision-layer fixtures to use this orchestrator — story 03b.
* Including `regime_skip_emergency` or other regime-output fields in `Phase1EnforcementResult` — callers can read those directly from the input `RegimeAdaptationOutput`. Keep the result narrow.

## Acceptance criteria

- [ ] `compose_phase_1_enforcement` and `Phase1EnforcementResult` are importable from `alphamind.execution.guardrail_enforcement`.
- [ ] `Phase1EnforcementResult` is a frozen Pydantic model with the documented two fields.
- [ ] Given a `regime_output` whose `active_regime` is normal and `drawdown_state.current_drawdown_pct == 0.0`: `result.active_risk_parameters` equals `regime_output.active_risk_parameter_set`; `result.drawdown_tier is None`.
- [ ] Given a `drawdown_state` clearing the first non-halt tier: `result.active_risk_parameters` reflects the CONSTRAINED override; `result.drawdown_tier == DrawdownTier.CONSTRAINED`.
- [ ] Given a `drawdown_state` clearing the second non-halt tier: `result.drawdown_tier == DrawdownTier.HEAVILY_CONSTRAINED`.
- [ ] Given a `drawdown_state` clearing the full-halt tier: `result.drawdown_tier == DrawdownTier.FULL_HALT` and the `cumulative_drawdown_tier_3` overlay tag is appended to `result.active_risk_parameters.active_overlays`.
- [ ] Determinism guard: the function called five times with the same inputs returns identical outputs.
- [ ] No DB reads, no mutation of inputs (regression test imports `RegimeAdaptationOutput`/`DrawdownState` fixtures, calls the function, and asserts the input objects are unchanged via field-by-field equality).
- [ ] All tier values referenced come from `load_breach_behavior_config(config/breach_behavior.yaml).progressive_tiers` — no hard-coded numerics in test bodies.
- [ ] `tests/execution/guardrail_enforcement/test_orchestrator.py` exists and passes under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/guardrail_enforcement/test_orchestrator.py -n auto -v` — all new tests pass.
* Run `uv run pytest -n auto` — full suite green.
* Lint clean per CLAUDE.md.