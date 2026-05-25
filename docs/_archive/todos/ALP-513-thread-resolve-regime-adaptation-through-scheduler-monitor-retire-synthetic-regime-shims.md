## Symptom

Neither the scheduler nor the continuous monitor invokes `resolve_regime_adaptation` (the regime-adaptation orchestrator). Both run synthetic shims that wrap the already-folded `compose_config` rule_values directly in `RegimeAdaptationOutput` / `ActiveRiskParameterSet`.

### Scheduler-side shim

`src/alphamind/risk_guardrails/regime_adaptation/active_parameters.py:1-130`. Helpers `build_active_risk_parameters` and `build_synthetic_regime_output` wrap rule_values. Module docstring lines 11-13 and function docstring lines 95-113 both say: *"When the regime-adaptation orchestrator is threaded through the pipeline scheduler (deferred follow-up), these helpers retire and resolve_regime_adaptation's real output flows through."*

### Monitor-side shim

`src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:602-680`. `make_regime_provider` builds a per-tick `RegimeAdaptationOutput` from the latest invocation's already-resolved parameter set. Docstring lines 619-621: *"When the regime-adaptation orchestrator is threaded through the monitor (deferred follow-up), this synthetic shim retires."*

[ALP-453](https://linear.app/alphamind-jatassi/issue/ALP-453/continuous-monitor-production-wire-deferred-placeholder-providers) covered the monitor's other placeholder providers (snapshot, library config, market hours, cascade dispatcher) but explicitly deferred the regime provider's retirement.

## Why this matters

The shims work as long as `compose_config` is the sole source of truth for rule_values across regime/overlay/feature-flag dimensions — they preserve the fold but lose four pieces of state the real resolver carries:

**(A) Active overlays bookkeeping.** Both shims return `active_overlays=()`. The real `resolve_regime_adaptation` would populate this from the matched overlay rules. State-delivery's overlay-display headers will show no active overlays even when one is active.

**(B) Parameter-change-flag computation.** The shims always set `parameter_change_flag=False` (scheduler) or read it from the latest persisted row (monitor). The real resolver compares prior vs. current and flips the flag on regime/overlay transitions.

**(C) Transition-state semantics.** Both shims hardcode `transition_state=STABLE`, `transition_invocations_remaining=0`. The real resolver carries forward transition state across invocations.

**(D) Regime-skip-emergency policy.** Both shims hardcode `regime_skip_emergency=False`. The real resolver consults the regime config.

## Scope

**(A) Scheduler integration.** Replace `build_synthetic_regime_output` in `scheduler/orchestrator.py` with a call to `resolve_regime_adaptation`. The resolver needs prior `ActiveRiskParameterSet` (load via the existing prior-parameter-provider), prior regime, current regime (from runtime dimensions), and profile/regime/overlay rule_values (from compose_config). Validate output's `entries` matches the synthetic version on a stable test fixture before swap.

**(B) Monitor integration.** Replace `make_regime_provider`'s synthetic-shim closure in `breach_loop/production_substrate.py` with a closure that invokes `resolve_regime_adaptation` per tick. The monitor must thread the same prior-parameter-provider and runtime-dimensions resolver the scheduler uses.

**(C) Retire shims.** Remove `build_active_risk_parameters` and `build_synthetic_regime_output` from `regime_adaptation/active_parameters.py` once both call sites swap. Update tests that consume the synthetic helpers.

## Acceptance criteria

- [ ] Scheduler calls `resolve_regime_adaptation` once per invocation; output flows into `compose_phase_1_enforcement` instead of `build_synthetic_regime_output`.
- [ ] Monitor's `regime_provider` closure calls `resolve_regime_adaptation` instead of building a synthetic output from the latest invocation row.
- [ ] `build_synthetic_regime_output` removed; `build_active_risk_parameters` either removed or repurposed as the resolver's internal helper.
- [ ] State-delivery overlay-display headers show active overlays when the resolver reports any.
- [ ] `parameter_change_flag` flips on regime/overlay transitions.
- [ ] `uv run pytest -n auto` passes; `uv run ruff check . && uv run mypy && uv run lint-imports` clean.

## Verification

* `uv run python scripts/verify_debug_e2e.py` returns exit 0.
* Manual smoke: run a synthetic regime transition scenario; observe `parameter_change_flag=True` in the resulting invocation row.
* `grep -n "build_synthetic_regime_output\|build_active_risk_parameters" src/alphamind/` shows zero hits (or only the resolver-internal helper).