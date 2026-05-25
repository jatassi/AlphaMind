## Symptom

`scripts/verify_debug_e2e.py --archive-root .archive/verify-debug-e2e` (run 2026-05-17, archive `inv-20260517T171854Z-b7f886d2`) crashes inside the pre-processor's first `evaluate_proposals` call:

```
File "src/alphamind/decision/proposal_pre_processor/observations.py", line 180, in compute_combined_set_impact
    library_output = evaluate_proposals(...)
File "src/alphamind/risk_guardrails/guardrail_evaluation/evaluate.py", line 104, in evaluate_proposals
    projections = project_all(...)
File "src/alphamind/risk_guardrails/guardrail_evaluation/rules/__init__.py", line 103, in project_all
    zones=config.escalation_zones[spec.effective_limit_key],
KeyError: 'borrow_cost_budget_pct_per_day'
```

The `KeyError` is the visible failure. Two silent failures live in the same construction site.

## Root cause

`src/alphamind/scheduler/orchestrator.py:279-287` (the `_build_decision_kwargs` builder threaded into `run_decision_pipeline`) constructs `LibraryConfig` directly:

```python
library_config = LibraryConfig(
    effective_limits=resolved.rule_values,
    escalation_zones={},  # populated by upstream guardrail composition; minimal default here
    feature_flags=feature_flags,
    active_sectors=tuple(sorted(active_sectors_from_resolved(resolved))),
    active_regime=resolved.regime_label,
    active_profile=resolved.profile_label,
    conservative_buffer_pct=0.0,
)
```

The inline comment promises "upstream guardrail composition" populates `escalation_zones`, but nothing does. `conservative_buffer_pct=0.0` is similarly a placeholder while `resolved.execution.conservative_delta_buffer_pct=10.0`.

The canonical adapter `from_resolved_config` at `src/alphamind/risk_guardrails/guardrail_evaluation/effective_limits.py:44` builds both `effective_limits` and `escalation_zones` from the same iteration over `resolved.rule_values`, and reads the correct buffer from `resolved.execution.conservative_delta_buffer_pct`. The continuous-monitor's `production_substrate.py:212` calls it correctly. The scheduler bypasses it.

Introduced 2026-05-11 in PR #44 ([ALP-431](https://linear.app/alphamind-jatassi/issue/ALP-431/pipeline-scheduler), scheduler initial implementation), commit `9cf9ff9`.

## Downstream impacts — three failures, only one visible

**(1) Visible KeyError.** `rules/__init__.py:103` does `config.escalation_zones[spec.effective_limit_key]` — subscript, not `.get` — and crashes on the first rule (`borrow_cost_budget_pct_per_day`, alphabetically).

**(2) Silent zero conservative buffer.** `delta_adjusted.py:_effective_buffer_fraction` multiplies `conservative_buffer_pct * regime_multiplier` and returns 0. Every options-pricing-with-buffer call in `compute_delta_adjusted_exposure` runs but under-protects.

**(3) Silent NORMAL classifications.** `risk_budget.py:87` documents "rules missing from `config.escalation_zones` default to `RiskZone.NORMAL`" — every rule in scheduler-driven risk-budget snapshots classifies as NORMAL regardless of actual consumption. Analyst/strategist/PM input bundles see this every invocation.

## Why it surfaced now

The pre-processor crashed earlier on two upstream bugs: [ALP-503](https://linear.app/alphamind-jatassi/issue/ALP-503/risk-budget-never-populated-end-to-end-analyst-crashes-on-empty) (analyst-side empty risk_budget, PR #65) and [ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae) (`STRATEGY requires option_legs` validator, PR #66). With both fixed, `project_all` is now reachable and the subscript fails on the first rule.

The same broken `library_config` is also threaded to every decision-layer agent's `validate_guardrail` tool (`pipeline/decision.py:340,376,412,435`). They don't crash because Sonnet/Opus agents don't always call `validate_guardrail`, and the harness probably reports tool errors back to the agent rather than raising. The pre-processor's `compute_combined_set_impact` calls `evaluate_proposals` unconditionally — first path to surface this.

## Why tests don't catch it

Unit tests construct `LibraryConfig` directly with explicit `escalation_zones`. Library-layer tests use `from_resolved_config`. The scheduler's `_build_decision_kwargs` is exercised only in production or `verify_debug_e2e.py`.

## Scope

**Library-side change** (`src/alphamind/scheduler/orchestrator.py:279-287`): replace the inline `LibraryConfig(...)` construction with a call to `from_resolved_config(pipeline_config.resolved)`. If `_build_decision_kwargs` needs `active_sectors=tuple(sorted(...))` (the adapter preserves profile order), apply that as a post-adapter override rather than re-constructing the dataclass.

**Defensive-tolerance cleanup** (`risk_guardrails/guardrail_evaluation/risk_budget.py:73-75`): the `None`-zones → `NORMAL` fallback was added to compensate for this bug. Once the scheduler is fixed, the fallback should raise — silent NORMAL classifications hide real consumption breaches.

## Acceptance criteria

* Regression test that asserts `library_config.escalation_zones.keys() == library_config.effective_limits.keys()` and `library_config.conservative_buffer_pct == 10.0` (matching resolved config).
* `risk_budget.py:build_risk_budget_consumption`'s defensive `None`-zones branch raises instead of returning NORMAL.
* `uv run ruff check . && uv run ruff format . && uv run mypy && uv run lint-imports && uv run pytest -n auto` all green.

## Related

* Related: [ALP-503](https://linear.app/alphamind-jatassi/issue/ALP-503/risk-budget-never-populated-end-to-end-analyst-crashes-on-empty) (PR #65), [ALP-504](https://linear.app/alphamind-jatassi/issue/ALP-504/close-on-optionsstrategy-rule-contributions-read-empty-proposal-dae) (PR #66) — upstream bugs that masked this.
* Surfaced by: `scripts/verify_debug_e2e.py` archive `inv-20260517T171854Z-b7f886d2`.

## Reading

* `src/alphamind/scheduler/orchestrator.py:254-315` — `_build_decision_kwargs`.
* `src/alphamind/risk_guardrails/guardrail_evaluation/effective_limits.py:44-106` — canonical `from_resolved_config`.
* `src/alphamind/execution/continuous_monitor/breach_loop/production_substrate.py:212` — correct usage pattern.
* `src/alphamind/risk_guardrails/guardrail_evaluation/rules/__init__.py:97-108` — `project_all`'s subscript that crashes.
* `src/alphamind/risk_guardrails/guardrail_evaluation/risk_budget.py:64-90` — defensive tolerance.
* `src/alphamind/risk_guardrails/guardrail_evaluation/delta_adjusted.py:179-181` — `_effective_buffer_fraction`.