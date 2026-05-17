# 08b — Replace `asyncio.gather`/`create_task` with TaskGroup; fix async-without-await sites

## Goal

Apply Python 3.13 structured concurrency (`asyncio.TaskGroup`) across the legitimate async sites in the codebase. Two changes: (1) replace `asyncio.gather` calls in `analysis/domain_researchers/orchestrator.py:115` and `pipeline/analysis.py:148` with TaskGroup blocks; (2) replace the hand-rolled supervisors in `scheduler/supervisor.py` and `execution/continuous_monitor/supervisor.py` (each ~190 LOC pre-dating TaskGroup adoption) with TaskGroup-based supervisors (~80 LOC each). Also fix the ~7 real async-without-await sites (folded in from former story 12c).

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L10 parts 2-3
* `.claude/skills/python-architecture/references/runtime.md` § G2 — structured concurrency
* `src/alphamind/analysis/domain_researchers/orchestrator.py:115` — 3-way `asyncio.gather`
* `src/alphamind/pipeline/analysis.py:148` — 2-way `asyncio.gather` (orchestrator || qualitative)
* `src/alphamind/scheduler/supervisor.py:101-127` — hand-rolled supervisor + bare `except BaseException`
* `src/alphamind/execution/continuous_monitor/supervisor.py:108-127` — same pattern
* `src/alphamind/execution/broker_adapter/fill_stream.py:324` — `create_task` w/o TaskGroup
* `src/alphamind/execution/continuous_monitor/underlying_stream/task.py:243,244` — same
* Story 06d (<issue id="a0ce0f10-cf3a-4d14-8ad5-50a6e9b0e8b6">ALP-466</issue>) — `analysis/_harness_core.py` extraction; this story builds on its TaskGroup-eligible surface

## Depends on

* 06d (<issue id="a0ce0f10-cf3a-4d14-8ad5-50a6e9b0e8b6">ALP-466</issue>) — harness core must exist; the TaskGroup replacement in `analysis/orchestrator.py` builds on the extracted surface

## Scope

In scope: 5 `asyncio.gather`/`create_task` sites converted to `TaskGroup`; 2 hand-rolled supervisors replaced; ~7 real `async def`-without-await sites fixed (drop async or add real await).

### 1\. `analysis/domain_researchers/orchestrator.py:115` → TaskGroup

```python
async with asyncio.TaskGroup() as tg:
    tasks = [tg.create_task(invoke_*(sector)) for sector in (tech_semis, financials, energy)]
results = [t.result() for t in tasks]
```

The fail-closed semantic (any exception cancels siblings) is preserved automatically; multi-failure raises `ExceptionGroup`.

### 2\. `pipeline/analysis.py:148` → TaskGroup

Same pattern over (domain_researchers_orchestrator || qualitative_researcher).

### 3\. `scheduler/supervisor.py` rewrite

Replace the ~190-LOC hand-rolled supervisor (registry + `_run_one` + `_shutdown_registered_tasks` + bare `except BaseException:`) with a ~80-LOC TaskGroup-based supervisor. Signal handlers install via `loop.add_signal_handler` to call `_request_stop()`; the TaskGroup body awaits a stop event then cancels.

### 4\. `execution/continuous_monitor/supervisor.py` rewrite

Same TaskGroup pattern.

### 5\. Stream-task hosts

`broker_adapter/fill_stream.py:324`, `continuous_monitor/underlying_stream/task.py:243,244` — wrap the `create_task` calls in a parent TaskGroup at the lifecycle owner's level.

### 6\. Fix ~7 real async-without-await sites

* `execution/state_persistence/invocation_context/activity_log.py:89` `append_activity_log_entry` — verify whether DB write is sync; if so, drop `async`
* `execution/continuous_monitor/__main__.py:387` `_bootstrap_active_provider` — similar
* `execution/continuous_monitor/repository_provider.py:33` `_provider` — similar
* `scheduler/orchestrator.py:705,708` `_active_provider`, `_prior_provider` — closures satisfying a Protocol; either make the Protocol sync (if all impls are sync) or keep async with a comment
* Any other real sites — drop async or add the missing await

### Out of scope

The synchronous file I/O in `_DiagState.write` (story 06d already covers this if needed) — but `asyncio.to_thread(...)` wrapping is a separate concern not blocking this story.

## Acceptance criteria

- [ ] `analysis/domain_researchers/orchestrator.py:115` uses `asyncio.TaskGroup` (no `asyncio.gather`).
- [ ] `pipeline/analysis.py:148` uses `asyncio.TaskGroup`.
- [ ] `scheduler/supervisor.py` is ≤120 LOC and uses `asyncio.TaskGroup`; no `_RegisteredTask` mutable dataclass; no `except BaseException:` block.
- [ ] `execution/continuous_monitor/supervisor.py` same.
- [ ] All `asyncio.create_task` calls in `broker_adapter/fill_stream.py`, `continuous_monitor/underlying_stream/task.py` are inside a `TaskGroup` parent.
- [ ] The ~7 real async-without-await sites are fixed (sync or real await).
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "asyncio.gather\|asyncio.create_task" src/alphamind/{analysis,pipeline,scheduler,execution/continuous_monitor,execution/broker_adapter}/` returns hits only inside `TaskGroup` contexts (manual inspection). Continuous-monitor restart drill: cleanly cancellable on SIGTERM; no orphaned subprocess tasks.