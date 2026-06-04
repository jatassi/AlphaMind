# 07a — Fold regime_adaptation orchestrator unit tests into the e2e timelines

## Goal

`test_orchestrator.py` (1,256 LOC) rebuilds the full `LoadedConfig`+`PositionView`+in-memory-DB scaffolding to assert single facts that the two e2e timeline files already assert per-step over the same orchestrator+DB path. Fold the subsumed lifecycle/overlay tests into the e2e files — but FIRST add the specific assertions the e2e files lack, so no guardrail is silently dropped (the verifier flagged several non-subsumed facts).

## Reading

* `tests/risk_guardrails/regime_adaptation/test_orchestrator.py` — the lifecycle + overlay classes.
* `tests/risk_guardrails/regime_adaptation/test_e2e_regime_lifecycle.py`, `test_e2e_overlay_overlap.py` — the per-step timelines (the fold target).
* `src/alphamind/risk_guardrails/regime_adaptation/` — the orchestrator.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

The core lifecycle facts (regime, transition_state, transition_invocations_remaining, audit presence/absence) ARE covered per-step by the e2e files — the subsumed unit classes (`TestStableContinuation`, `TestLooseningCountdown`, `TestLooseningCompletion`, and the overlay activation/composition/both-sorted cases) may fold in.

**Before deleting anything, ensure these assertions exist — add them to the e2e step assertions if absent, OR keep the named unit test (verifier — these are NOT in any e2e step today):**

* **TestBootstrap** cold-start: `audit_log_entries == ()`, `prior_regime is None`, `regime_transition_breaches == ()`, `regime_skip_emergency is False`, `active_risk_parameter_set.regime_label == LOW_VOL`.
* **TestLooseningFirstInvocation:** `new_state.transition_origin_regime == Regime.crisis` AND the loosening payload `direction == 'loosening'`.
* **TestTighteningTransition::test_emits_regime_transition_audit_with_tightening_direction:** payload `direction` / `prior_regime` / `new_regime`.
* **TestOverlayDeactivates:** the STRESS deactivation branch (e2e only ever deactivates `pre_event`; stress stays active throughout).
* **TestNoCompositeAlerts:** the stress-inactive negative branch (e2e always seeds stress active).

**KEEP as standalone units (not in any e2e):** `test_payload_carries_static_and_applied_multipliers` (multiplier-snapshot guard) and one of the stale/non-stale calendar tests.

## Acceptance criteria

- [ ] All five listed assertion-sets exist after the change — added to the e2e timelines or retained as focused unit tests.
- [ ] The genuinely-subsumed lifecycle/overlay unit classes are removed; `test_orchestrator.py` shrinks accordingly.
- [ ] The multiplier-snapshot test and one calendar test are retained.
- [ ] `coverage report` for `src/alphamind/risk_guardrails/regime_adaptation/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/risk_guardrails/regime_adaptation -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms `transition_origin_regime`, the bootstrap-empty-audit assertion, the stress-deactivation branch, and the multiplier-snapshot test all survive somewhere.