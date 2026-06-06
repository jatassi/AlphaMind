# 02a — Harness + composition-runner emitter wiring

## Goal

Thread `progress: ProgressEmitter = NoOpProgressEmitter()` through the seven harness functions (4 analysis + 3 decision), the two pipeline composition runners (`run_analysis_pipeline`, `run_decision_pipeline`), and the orchestrator's phase-boundary emit sites. The single emit point for `agent_request` / `agent_response` lives inside `_harness_core.invoke_sdk` — wrapping the SDK call there means each harness function just threads one extra kwarg to the shared driver. The orchestrator gains 12 `phase_start` / `phase_done` pairs per parent Issue Pre-resolved decision § (D); the `seed` phase event is emitted by story 04's CLI helper, not the orchestrator.

## Reading

* `docs/design/debug-e2e-mode.md` § 6.5 (Harness instrumentation: explicit parameter vs. contextvar)
* `src/alphamind/analysis/_harness_core.py` § `invoke_sdk` — the shared SDK driver where the emit pair lands
* `src/alphamind/analysis/{adaptive_research,domain_researchers,qualitative_research,synthesizer}/harness.py` — 4 analysis harness files to extend
* `src/alphamind/decision/{analyst,strategist,portfolio_manager}/harness.py` — 3 decision harness files to extend
* `src/alphamind/pipeline/analysis.py` § `run_analysis_pipeline` — composition runner to extend
* `src/alphamind/pipeline/decision.py` § `run_decision_pipeline` — composition runner to extend
* `src/alphamind/scheduler/orchestrator.py` § `run_invocation` — phase-boundary emit sites
* `ALP-495` (01b) — provides `ProgressEmitter` + `NoOpProgressEmitter`
* Parent issue `ALP-493` Pre-resolved decisions §§ (B), (D), (E), (K) — `agent_response` fields, phase list, SDK call count, runner kwarg name

## Depends on

* `ALP-495` (01b — Progress Protocol + RunInvocationContext debug_e2e field)

## Scope

In scope: `src/alphamind/analysis/_harness_core.py`, the 7 harness files under `analysis/` and `decision/`, the 2 composition runners under `pipeline/`, and `scheduler/orchestrator.py`. Tests under `tests/analysis/`, `tests/decision/`, `tests/pipeline/`, `tests/scheduler/`.

### 1\. Single SDK-emit point in `_harness_core.invoke_sdk`

Extend `invoke_sdk(...)` with `progress: ProgressEmitter` and `phase: str` keyword parameters; wrap the SDK call:

```python
async def invoke_sdk(
    *,
    sdk_query_fn, prompt, options, diag, budget_seconds, ...,
    progress: ProgressEmitter = NoOpProgressEmitter(),
    phase: str,
) -> SDKOutcome:
    progress.agent_request(phase=phase, agent=diag.agent_name, model=diag.model)
    outcome = ...  # existing SDK driver loop
    progress.agent_response(
        phase=phase, agent=diag.agent_name, model=diag.model,
        duration_s=wall_elapsed,
        input_tokens=outcome.tokens_used.input,
        output_tokens=outcome.tokens_used.output,
        tool_calls=outcome.tool_calls,
        stop_reason=outcome.stop_reason,
    )
    return outcome
```

`phase` is the pipeline stage name (e.g., `"domain_researchers"`, `"analyst"`); `agent` (read from `diag.agent_name`) is the agent name within the stage (e.g., `"tech_semis_researcher"`). For single-agent phases they coincide.

### 2\. Thread `progress` + `phase` through 7 harness functions

Each of `invoke_synthesizer`, `invoke_adaptive_researcher`, `invoke_qualitative_researcher`, `invoke_domain_researcher` (called per-sector by the orchestrator), `invoke_analyst`, `invoke_strategist`, `invoke_portfolio_manager` gains `progress: ProgressEmitter = NoOpProgressEmitter()` and `phase: str` keyword parameters and passes both to `invoke_sdk`. The phase string comes from the caller (the composition runner).

### 3\. Thread `progress` through 2 composition runners

`run_analysis_pipeline(..., progress: ProgressEmitter = NoOpProgressEmitter())` emits `phase_start("distillation")` / `phase_done` around `run_external_distillation`; `phase_start("domain_researchers")` / `phase_done` around the TaskGroup for the 3 sectors; `phase_start("qualitative")` / `phase_done` around the qualitative call (in parallel with `domain_researchers` under the same TaskGroup — emit boundaries are OK to overlap); `phase_start("adaptive")` / `phase_done` around the adaptive call; `phase_start("synthesizer")` / `phase_done` around the synthesizer call. Each harness call receives the same `progress` and its phase name (`"domain_researchers"` for all 3 sector calls; `"qualitative"`, `"adaptive"`, `"synthesizer"` for the singletons).

`run_decision_pipeline(..., progress: ProgressEmitter = NoOpProgressEmitter())` emits `phase_start("analyst")` / `phase_done` and `phase_start("strategist")` / `phase_done` around the TaskGroup; `phase_start("pre_processor")` / `phase_done` around `run_proposal_pre_processor` (no SDK call inside — pre-processor is deterministic); `phase_start("pm")` / `phase_done` around `run_portfolio_manager`.

### 4\. Orchestrator emit sites

`run_invocation` in `scheduler/orchestrator.py`:

* Reads `progress = context.debug_e2e.emitter_factory(invocation_id) if context.debug_e2e is not None else NoOpProgressEmitter()` once at the top.
* Emits `phase_start("phase1")` before opening the Phase 1 session; `phase_done("phase1", fills_processed=phase1_summary.fills_processed)` after Phase 1 commits.
* Emits `phase_start("snapshot_assembly")` before `_assemble_phase1_snapshot`; `phase_done("snapshot_assembly")` after.
* Passes `progress=progress` to `run_analysis_pipeline` and `run_decision_pipeline`.
* Emits `phase_start("phase2")` / `phase_done("phase2", commands_submitted=phase2_summary.commands_submitted)` around Phase 2.

The `seed` phase event is NOT emitted by the orchestrator — story 04's CLI helper emits it before `run_invocation` is called.

### 5\. Test substitute

Ship a small `RecordingProgressEmitter` test fake in `tests/scheduler/test_progress.py` (extending what 01b leaves there):

```python
class RecordingProgressEmitter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def phase_start(self, phase: str) -> None:
        self.events.append(("phase_start", {"phase": phase}))
    # ... and so on for the other three methods
```

The integration test under `tests/scheduler/test_orchestrator_progress.py` drives `run_invocation` against a `RecordingProgressEmitter` substitute (injected via a debug-style `RunInvocationContext` with `debug_e2e` set, the orchestrator-side wiring landed in this story); asserts every expected phase boundary + every expected agent_request/agent_response pair appears.

### Out of scope

* `JsonlProgressEmitter` (story 02c).
* `wipe_and_seed`'s phase event (story 04 emits it from `__main__.py` before `run_invocation`).
* Changing the SDK driver loop's behavior beyond adding the emit pair.

## Acceptance criteria

- [ ] `invoke_sdk` in `_harness_core.py` accepts `progress: ProgressEmitter` (default `NoOpProgressEmitter()`) and `phase: str` keyword parameters; emits `agent_request` before the SDK call and `agent_response` after, with the field set per parent issue § (B).
- [ ] All 7 harness functions (`invoke_synthesizer`, `invoke_adaptive_researcher`, `invoke_qualitative_researcher`, `invoke_domain_researcher`, `invoke_analyst`, `invoke_strategist`, `invoke_portfolio_manager`) accept `progress` + `phase` kwargs defaulting to `NoOpProgressEmitter()` / required, pass both to `invoke_sdk`.
- [ ] `run_analysis_pipeline` and `run_decision_pipeline` accept `progress: ProgressEmitter = NoOpProgressEmitter()`; emit `phase_start` / `phase_done` for each phase in parent issue § (D)'s phase list (excluding `seed`, `phase1`, `snapshot_assembly`, `phase2`); thread `progress` + the phase string into each harness call.
- [ ] `run_invocation` reads `context.debug_e2e.emitter_factory(invocation_id)` when `context.debug_e2e is not None`; passes the emitter to both composition runners; emits `phase_start` / `phase_done` for `phase1`, `snapshot_assembly`, `phase2`.
- [ ] A new `tests/scheduler/test_orchestrator_progress.py` test exercises `run_invocation` with a `RecordingProgressEmitter` substitute and asserts the recorded events include every phase boundary in the parent § (D) list (excluding `seed`) plus 9 `agent_request` / `agent_response` pairs in dependency order.
- [ ] `RecordingProgressEmitter` is shipped as a test fake in `tests/scheduler/test_progress.py` (or a conftest-level fixture) so other tests can import it.
- [ ] Existing tests that construct harness functions or composition runners without `progress` continue to pass — default kwarg.
- [ ] `uv run pytest -n auto` passes the full suite; full linter chain clean.

## Verification

`uv run pytest tests/scheduler/test_orchestrator_progress.py tests/pipeline/ tests/analysis/ tests/decision/ -n auto` passes. Full linter chain clean.