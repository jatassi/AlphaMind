# 07f — scheduler progress-order + SSE per-type wire coverage

## Goal

Two scheduler consolidations where a tautological-looking test guards one genuinely-unique fact the verifier flagged. Drop the tautology; preserve the unique coverage by relocating it to a cheaper level.

## Reading

* `tests/scheduler/test_orchestrator_progress.py` — `test_run_invocation_records_nine_agent_request_response_pairs` (L547–611, incl. `expected_agents_in_order` L586–600), `test_run_invocation_records_every_phase_boundary` (L497).
* `tests/analysis/test_harness_core.py` — `test_invoke_sdk_emits_agent_request_and_response` (L932), `…_on_failure` (L981) — the real per-harness emit.
* `tests/scheduler/control/test_app.py` — `TestSSEFramingForEachEventType::test_each_event_type_frames_correctly` (L281–375, 9 parametrized cases).
* `tests/scheduler/control/test_events.py` — emitter-level name-mapping tests; `format_sse_record`.
* Parent `ALP-783`.

## Depends on

* `05f` ([ALP-793](https://linear.app/alphamind-jatassi/issue/ALP-793/05f-create-testsschedulerconftestpy-for-shared-scheduler-fixtures)) — the scheduler conftest hoist lands first (same directory).

## Scope

**(1)** `test_orchestrator_progress.py`**:** the emission-count/field assertions in `test_run_invocation_records_nine_agent_request_response_pairs` are tautological — the test's own stubs (`_emit_analysis_pipeline_progress` / `_emit_decision_pipeline_progress`) hard-code the 9 `_emit_agent_call`s, and the real emit is covered by `test_harness_core.py` (L932/L981). **PRESERVE** the canonical-order spec: extract `expected_agents_in_order` (distillation → 3 sectors → qual → adaptive → synth → analyst → strategist → pm) into a focused test asserting the agent dependency-DAG order (this ordering is not reproduced by any other test). Drop the tautological count/field assertions.

**(2)** `test_app.py` **SSE framing:** keep ONE Uvicorn-boot representative for the end-to-end transport path (it's identical across types). The per-type `type(event).model_validate(records[0][1])` wire round-trip (L373) is unique — `model_validate` of a wire payload appears nowhere else. **PRESERVE** the 9-way wire-serialization coverage by ADDING a cheap per-type `model_validate` round-trip at the emitter / `format_sse_record` level (no server boot), so the per-type `datetime`/`Decimal` payload-field serialization stays covered.

## Acceptance criteria

- [ ] The tautological 9-pair emission-count/field assertions are gone; a focused test asserts the canonical 9-agent DAG order.
- [ ] SSE framing keeps one Uvicorn representative; a per-type `model_validate` wire round-trip exists at the emitter/`format_sse_record` level covering all 9 event types.
- [ ] `coverage report` for `src/alphamind/scheduler/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/scheduler -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms the agent-order assertion and a per-type `model_validate` round-trip both still exist.