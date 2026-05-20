# 02a — Wire `compose_phase_1_enforcement` into the decision pipeline

## Goal

Centralize construction of the `active_risk_parameters_provider` consumed by `SqlPortfolioStateRepository` inside `run_decision_pipeline`, calling `compose_phase_1_enforcement` from regime resolution + `DrawdownState` + `progressive_tiers` and feeding the result through `make_active_risk_parameters_provider`. Today this composition exists at `src/alphamind/execution/guardrail_enforcement/` but is only consumed by verify scripts — the pipeline runner accepts a pre-built provider from callers. Closing this gap ensures the monitor's breach-evaluation loop (story 03b) and the pipeline's Phase 1 use one canonical entry point so the two cannot drift.

This is the "Phase 1 enforcement-layer composition wiring" Moderate to-do from `docs/project-tracker.md` flagged on the breach-behavior story-review audit (2026-04-28).

## Reading

* `src/alphamind/execution/guardrail_enforcement/__init__.py` — `compose_phase_1_enforcement`, `Phase1EnforcementResult`, `make_active_risk_parameters_provider`. Read both function bodies end-to-end.
* `src/alphamind/execution/guardrail_enforcement/orchestrator.py` — the orchestrator function that bundles the regime + drawdown inputs into the result.
* `src/alphamind/pipeline/decision.py` — the pipeline runner this story edits. Understand how `active_risk_parameters_provider` reaches the snapshot assembler today.
* `src/alphamind/portfolio_state/repository.py` (or equivalent) — `SqlPortfolioStateRepository`'s `active_risk_parameters_provider` slot contract (zero-arg awaitable returning `ActiveRiskParameterSet`).
* `src/alphamind/risk_guardrails/regime_adaptation/` — `resolve_regime_adaptation` and its `RegimeAdaptationOutput`. The pipeline already calls this (or its callers do); story 02a must not duplicate the call.
* `src/alphamind/scripts/verify_decision_pipeline.py`, `verify_state_persistence.py`, `verify_oms_commands.py` — current consumers that construct the provider themselves. They must continue to work after this refactor (either by accepting the new pipeline-internal construction, or by passing the same inputs through).
* Parent issue `ALP-123` § Pre-resolved decision (F) — the wiring belongs in this work tree by operator decision.
* `docs/design/06-risk-guardrails/state-delivery.md` § Portfolio state ingestion payload — the design contract the composed parameter set satisfies.

## Depends on

* [ALP-432](https://linear.app/alphamind-jatassi/issue/ALP-432/01-package-skeleton-monitor-entry-point-config-runtime-design-doc) (01) — package skeleton has nothing the pipeline needs, but the orchestrator dispatches in wave order; 01 lands first.

## Scope

Source under `src/alphamind/pipeline/decision.py` (refactor) and possibly `src/alphamind/pipeline/_shared.py` (extracted helper). Tests under `tests/pipeline/` and (smoke) the existing `tests/scripts/` verify-script regression coverage if present.

### 1\. Helper: `build_phase1_enforcement_inputs`

Author a helper that gathers the three inputs `compose_phase_1_enforcement` needs from the runtime context:

```python
async def build_phase1_enforcement_inputs(
    *,
    repository: PortfolioStateRepository,
    regime_output: RegimeAdaptationOutput,
    progressive_tiers: tuple[ProgressiveTier, ...],
) -> tuple[RegimeAdaptationOutput, DrawdownState, tuple[ProgressiveTier, ...]]:
    """Read the latest DrawdownState from the repository, return the tuple
    suitable for compose_phase_1_enforcement(**dict(zip([...], result))).
    """
```

Lives at `src/alphamind/pipeline/_shared.py` so analysis pipeline and other downstream consumers can reuse it if they need to compute the same view. Pure async; no caching.

### 2\. Pipeline runner integration

In `run_decision_pipeline`:

* Replace the externally-supplied `active_risk_parameters_provider` argument (or whatever the current shape is) with internal construction:
  1. Call `resolve_regime_adaptation(...)` (already present in or above the pipeline; the path to it must be visible).
  2. Call `build_phase1_enforcement_inputs(repository=..., regime_output=..., progressive_tiers=...)`.
  3. Call `compose_phase_1_enforcement(...)` with the unpacked inputs.
  4. Call `make_active_risk_parameters_provider(phase1_result)`.
  5. Inject the resulting awaitable into the snapshot assembler.
* Surface `phase1_result.drawdown_tier` on the bundled `DecisionPipelineResult` so downstream halt-mode / emergency-trigger callers do not redo the classification. Likely a new optional field `drawdown_tier: DrawdownTier | None`.
* Do NOT modify the analysis pipeline (`run_analysis_pipeline`) — Phase 1 wiring is decision-pipeline-only at this point.

### 3\. Verify-script migration

Refactor the three verify scripts that currently construct the provider:

* `src/alphamind/scripts/verify_decision_pipeline.py`
* `src/alphamind/scripts/verify_state_persistence.py`
* `src/alphamind/scripts/verify_oms_commands.py`

For each: stop inlining `compose_phase_1_enforcement` + `make_active_risk_parameters_provider`; instead pass the same inputs to `run_decision_pipeline` (or to whichever helper the script is exercising) and let the pipeline runner do the composition. The scripts retain their direct-construction tests in places where they verify the helpers in isolation, but the e2e flow uses the centralized path.

### 4\. Tests

Add unit + integration coverage at `tests/pipeline/test_decision_phase1_enforcement.py`:

* The pipeline runner, given a regime resolution + drawdown state + progressive tiers, composes the `Phase1EnforcementResult` and surfaces both the provider on the snapshot and the `drawdown_tier` on the bundled result.
* Determinism: identical inputs produce identical `phase1_result`.
* Override propagation: when `drawdown_state.current_drawdown_pct` clears a tier's `trigger_pct`, the resulting `ActiveRiskParameterSet` has the tier's overrides applied; the bundled `drawdown_tier` reflects the classification.
* No-override path: when drawdown is below the first tier, `drawdown_tier` is `None` and the regime-resolved parameters pass through untouched.
* Verify-script regression: each migrated verify script still passes its existing assertions.

### Out of scope

* Adding new fields to `Phase1EnforcementResult` — the bundle shape is settled by [ALP-394](https://linear.app/alphamind-jatassi/issue/ALP-394/01-compose-active-risk-parameters-primitive) / [ALP-125](https://linear.app/alphamind-jatassi/issue/ALP-125/guardrail-enforcement-layer).
* Wiring the monitor's continuous evaluation loop — that is story 03b, which independently calls `compose_phase_1_enforcement` against repository state, not via the pipeline runner.
* Changing the regime resolution shape or moving where `resolve_regime_adaptation` is called from.
* Halt-state activation logging — story 03b.

## Acceptance criteria

- [ ] `run_decision_pipeline` internally constructs the `active_risk_parameters_provider` via `compose_phase_1_enforcement` + `make_active_risk_parameters_provider`; callers no longer need to pass a pre-built provider.
- [ ] `build_phase1_enforcement_inputs` is exported from `alphamind.pipeline._shared` and is async; the helper reads `DrawdownState` from the repository.
- [ ] `DecisionPipelineResult` carries the classified `drawdown_tier: DrawdownTier | None` from the composition step.
- [ ] When `drawdown_state.current_drawdown_pct == 0.0`, the pipeline-provided provider yields the regime-resolved `ActiveRiskParameterSet` unchanged and `result.drawdown_tier is None`.
- [ ] When `drawdown_state.current_drawdown_pct` clears the first non-halt tier from `BreachBehaviorConfig.progressive_tiers`, the provider yields parameters with `cumulative_drawdown_tier_1` in `active_overlays` and the tier's overrides applied; `result.drawdown_tier == DrawdownTier.CONSTRAINED`.
- [ ] `tests/pipeline/test_decision_phase1_enforcement.py` covers the criteria above and passes under `uv run pytest tests/pipeline/ -n auto`.
- [ ] `scripts/verify_decision_pipeline.py`, `scripts/verify_state_persistence.py`, `scripts/verify_oms_commands.py` each pass under their existing invocation commands.
- [ ] `docs/project-tracker.md` "Phase 1 enforcement-layer composition wiring" Moderate entry is marked done in the same commit as the code change (or via a tracker-only follow-up commit; either is fine).
- [ ] `uv run pytest -n auto` (full suite) passes — no regressions in `tests/risk_guardrails/`, `tests/execution/`, `tests/portfolio_state/`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* `uv run pytest tests/pipeline/ -n auto -v` — every new test passes.
* `uv run python scripts/verify_decision_pipeline.py` (or the script's existing invocation) — passes.
* `uv run python scripts/verify_state_persistence.py` — passes.
* `uv run python scripts/verify_oms_commands.py` — passes.
* `uv run pytest -n auto` — full suite green.
* Lint clean per CLAUDE.md.