# 09b — Lift `scheduler/orchestrator.py` layer-spanning helpers into feature packages; consolidate Mode enum

## Goal

`scheduler/orchestrator.py` has grown to 1085 LOC with 32 helper functions reaching into 30+ first-party modules across analysis, decision, distillation, execution, portfolio_state, risk_guardrails — a layer-spanning swamp of private helpers carrying inline "pre-review triage simplification" / "deferred follow-up" comments at lines 178, 196, 599, 779-789, 842-849. Lift each into the feature package it belongs to, dropping the orchestrator to ~600 LOC of clean composition.

Also consolidate the three parallel `Mode`-translation helpers (`pipeline/decision.py:138-146`, `scheduler/orchestrator.py:427`, `scheduler/invocation.py:52`) into a single `Mode` enum with translator methods (folded in from former story 12c).

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L8 (orchestrator god module) + Worth Knowing W5 (Mode helpers)
* `src/alphamind/scheduler/orchestrator.py` — the 1085-LOC file; line refs 178, 196, 599, 779-789, 842-849 mark the candidates
* `src/alphamind/pipeline/decision.py:138-146` — `_ANALYST_MODE_FOR_PIPELINE`, `_STRATEGIST_MODE_FOR_PIPELINE` dicts
* `src/alphamind/scheduler/orchestrator.py:427` — `_mode_to_decision_literal`
* `src/alphamind/scheduler/invocation.py:52` — `_mode_to_active_mode_literal`
* Story 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/regime.py` already exists; the `Mode` enum may live there or in `_kernel/mode.py`

## Depends on

* 02a (<issue id="81e17dca-4621-40e0-ba40-2a38c590d3a3">ALP-457</issue>) — `_kernel/` exists for the Mode enum's home

## Scope

In scope: identify each layer-spanning helper in `orchestrator.py`, lift to its feature package; introduce single `Mode` enum with translator methods; update consumers. Tests update accordingly.

### 1\. Lift orchestrator helpers

Per audit, three groups for lift:

* `_build_active_risk_parameters`, `_build_synthetic_regime_output`, `_load_prior_active_risk_parameters` → `risk_guardrails/regime_adaptation/` (these are regime-adaptation policy, not orchestration)
* `_active_sectors_from_resolved`, `_build_sector_resolver`, `_ticker_scope_from_assets`, `_sectors_config_from_assets` → new `config/assets_views.py` (these all unpack `resolved.assets.sectors`)
* `_make_repository_providers`, `_evaluate_pre_event_decision`, `_evaluate_stress_decision` → likely belong in `decision/` or `risk_guardrails/` — judgment call per helper

The orchestrator imports the lifted helpers from their new homes; line count drops to ~600.

### 2\. Single `Mode` enum

`src/alphamind/_kernel/mode.py`:

```python
class PipelineMode(StrEnum):
    NORMAL = "normal"
    HALT = "halt"
    DEFENSIVE_POSTURE = "defensive_posture"

    def to_analyst_pipeline_mode(self) -> Literal["normal", "halt"]: ...
    def to_strategist_pipeline_mode(self) -> Literal["normal", "halt"]: ...
    def to_decision_literal(self) -> str: ...
    def to_active_mode_literal(self) -> str: ...
```

Replace the three parallel dicts/functions with calls into `PipelineMode.to_*()`. Add typed coverage: if a new `Mode` value is added, `mypy --strict` catches every `to_*` site that doesn't handle it.

### 3\. Update consumers

Every site that previously called `_mode_to_*_literal()` now calls `mode.to_*_literal()`. Decision-layer consumers of `_ANALYST_MODE_FOR_PIPELINE` retarget to `mode.to_analyst_pipeline_mode()`.

### Out of scope

Other orchestrator clean-up beyond the lift (e.g., re-organizing `run_invocation` itself) is left to future work. This story is the lifts + Mode consolidation.

## Acceptance criteria

- [ ] `scheduler/orchestrator.py` is ≤700 LOC.
- [ ] Layer-spanning helpers `_build_active_risk_parameters`, `_build_synthetic_regime_output`, `_load_prior_active_risk_parameters`, `_active_sectors_from_resolved`, `_build_sector_resolver`, `_ticker_scope_from_assets`, `_sectors_config_from_assets` are lifted to their feature packages.
- [ ] `src/alphamind/_kernel/mode.py` (or `_kernel/regime.py` extension) defines `PipelineMode` with `to_*()` translator methods.
- [ ] `pipeline/decision.py:138-146`, `scheduler/orchestrator.py:427`, `scheduler/invocation.py:52` Mode-translation helpers are deleted; consumers call `Mode.to_*()`.
- [ ] The inline "pre-review triage simplification" / "deferred follow-up" comments at [orchestrator.py:178](http://orchestrator.py:178), 196, 599, 779-789, 842-849 are resolved (the code they referred to has moved).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`wc -l src/alphamind/scheduler/orchestrator.py` shows ≤700. `grep -rn "_mode_to_\|_ANALYST_MODE_FOR_PIPELINE\|_STRATEGIST_MODE_FOR_PIPELINE" src/` returns zero hits. Test: instantiate `PipelineMode.HALT.to_analyst_pipeline_mode()` returns the same value as the old `_ANALYST_MODE_FOR_PIPELINE["halt"]`.