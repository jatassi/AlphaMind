# 01b — Progress Protocol + RunInvocationContext debug_e2e field

## Goal

Introduce `scheduler/progress.py` with the `ProgressEmitter` Protocol and `NoOpProgressEmitter` default; add the `debug_e2e: DebugE2ESettings | None = None` field on `RunInvocationContext`. The Protocol is the type the orchestrator and pipeline composition runners thread through (story 02a). `DebugE2ESettings` is forward-declared as a `TYPE_CHECKING`-only import to avoid the import-linter contract violation that story 04 lands — the actual class lives in story 03's `settings.py`.

## Reading

* `docs/design/debug-e2e-mode.md` §§ 3 (Package layout), 4 (Types — ProgressEmitter), 6.4 (ProgressEmitter as Protocol + NoOp default — why NoOp default vs. optional emitter)
* `src/alphamind/scheduler/run_context.py` — existing `RunInvocationContext` shape to extend
* Parent issue `ALP-493` Pre-resolved decision § (B) — `agent_response` event field set

## Depends on

* None — wave 1.

## Scope

In scope: `src/alphamind/scheduler/progress.py` (new) and `src/alphamind/scheduler/run_context.py` (extend with the `debug_e2e` field). Tests at `tests/scheduler/test_progress.py`.

### 1\. New module — `scheduler/progress.py`

```python
from typing import Any, Protocol


class ProgressEmitter(Protocol):
    """Per-invocation progress event sink.

    Default implementation is NoOpProgressEmitter; debug-e2e mode swaps in
    JsonlProgressEmitter (story 02c). Call sites never branch on presence —
    the emitter is always non-None (P3).
    """

    def phase_start(self, phase: str) -> None: ...
    def phase_done(self, phase: str, **fields: Any) -> None: ...
    def agent_request(self, *, phase: str, agent: str, model: str) -> None: ...
    def agent_response(
        self, *, phase: str, agent: str, model: str, duration_s: float,
        input_tokens: int, output_tokens: int,
        tool_calls: int, stop_reason: str | None,
    ) -> None: ...


class NoOpProgressEmitter:
    """No-op default the production daemon uses."""

    def phase_start(self, phase: str) -> None: ...
    def phase_done(self, phase: str, **fields: Any) -> None: ...
    def agent_request(self, **fields: Any) -> None: ...
    def agent_response(self, **fields: Any) -> None: ...
```

The `agent_response` field set (`duration_s`, `input_tokens`, `output_tokens`, `tool_calls`, `stop_reason`) is fixed at the Protocol level per parent issue's Pre-resolved decision § (B). `NoOpProgressEmitter` accepts via `**fields: Any` to absorb every shape the Protocol declares.

### 2\. Extend `RunInvocationContext`

Add a `debug_e2e` field defaulting to `None`. Use `TYPE_CHECKING` to break the import cycle so the runtime path never imports `debug_e2e`:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings

@dataclass(frozen=True, slots=True)
class RunInvocationContext:
    # ... existing fields unchanged ...
    debug_e2e: "DebugE2ESettings | None" = None
```

The string annotation keeps the forward reference; `TYPE_CHECKING` ensures the actual import never runs at module-load (the import-linter contract added in story 04 forbids it).

### Out of scope

* `JsonlProgressEmitter` (story 02c).
* The orchestrator's phase_start/phase_done emit sites + harness threading (story 02a).
* Wiring `--debug-e2e` to populate the context field (story 04).

## Acceptance criteria

- [ ] `src/alphamind/scheduler/progress.py` exists with `ProgressEmitter` Protocol and `NoOpProgressEmitter` class.
- [ ] `ProgressEmitter` declares methods `phase_start`, `phase_done`, `agent_request`, `agent_response` with the field shapes documented above (including the 5-field `agent_response`).
- [ ] `NoOpProgressEmitter` satisfies `ProgressEmitter` — verified by an instance check in tests (use `@runtime_checkable` on the Protocol if required for `isinstance` to work).
- [ ] `RunInvocationContext` has a new field `debug_e2e: DebugE2ESettings | None = None`, declared via forward-reference string annotation; `TYPE_CHECKING` guards the import.
- [ ] Existing constructors of `RunInvocationContext` (no `debug_e2e` arg) continue to work — default kwarg.
- [ ] `tests/scheduler/test_progress.py` instantiates `NoOpProgressEmitter`, calls each method (including kwargs-only `agent_request` / `agent_response`), asserts no exception.
- [ ] `uv run pytest tests/scheduler/ -n auto` passes.
- [ ] `uv run ruff check .` + `uv run ruff format .` + `uv run mypy` + `uv run lint-imports` all clean.

## Verification

`uv run pytest tests/scheduler/test_progress.py -n auto` passes. Existing `tests/scheduler/` tests continue to pass with the new `debug_e2e` field defaulting to `None`. Linter chain clean.