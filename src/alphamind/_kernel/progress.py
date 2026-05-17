"""Per-invocation progress event sink — Protocol + no-op default.

Pure, dependency-free Protocol the seven LLM harnesses (4 analysis + 3
decision) and the orchestrator/composition runners share. Lives in
``_kernel`` rather than ``scheduler`` so the import graph honours the
composition-root layering contract: harnesses sit below the kernel and
must not reach upward into ``scheduler``. The kernel re-exports the
public surface from ``alphamind.scheduler.progress`` so the design-doc-
named home is preserved at the call sites the orchestrator owns.

Story ALP-495 introduced the Protocol. Story ALP-497 (this one) routes
the seven harnesses through :func:`alphamind.analysis._harness_core.invoke_sdk`
which emits ``agent_request`` / ``agent_response`` per SDK call;
moving the Protocol into ``_kernel`` keeps the import graph clean.

The ``agent_response`` field set
(``duration_s``, ``input_tokens``, ``output_tokens``, ``tool_calls``,
``stop_reason``) is fixed at the Protocol level per parent issue
ALP-493 § Pre-resolved (B) — the smallest set that answers
"stuck or working?", "cost in budget?", and "tool-using or thinking?".
"""

from __future__ import annotations

from typing import Any, Final, Protocol, runtime_checkable

__all__ = ["NOOP_PROGRESS_EMITTER", "NoOpProgressEmitter", "ProgressEmitter"]


@runtime_checkable
class ProgressEmitter(Protocol):
    """Per-invocation progress event sink.

    Implementations: :class:`NoOpProgressEmitter` (production default)
    and ``JsonlProgressEmitter`` (debug-e2e mode, story 02c).

    ``@runtime_checkable`` enables structural ``isinstance`` checks the
    tests rely on; production wiring is type-checked statically.
    """

    def phase_start(self, phase: str) -> None: ...

    def phase_done(self, phase: str, **fields: Any) -> None: ...

    def agent_request(self, *, phase: str, agent: str, model: str) -> None: ...

    def agent_response(
        self,
        *,
        phase: str,
        agent: str,
        model: str,
        duration_s: float,
        input_tokens: int,
        output_tokens: int,
        tool_calls: int,
        stop_reason: str | None,
    ) -> None: ...


class NoOpProgressEmitter:
    """No-op default the production daemon uses.

    Every method accepts the Protocol's declared shape and returns
    ``None``. ``phase_done`` / ``agent_request`` / ``agent_response``
    use ``**fields: Any`` so the no-op transparently absorbs every
    keyword variant the Protocol may declare — keeping this class
    insulated from future field-set evolution.

    Prefer the module-level :data:`NOOP_PROGRESS_EMITTER` singleton over
    constructing a fresh instance per call: it is stateless and reusing
    one instance sidesteps the ``B008`` mutable-default-argument lint at
    every call site that defaults ``progress`` to no-op behaviour.
    """

    def phase_start(self, phase: str) -> None:
        pass

    def phase_done(self, phase: str, **fields: Any) -> None:
        pass

    def agent_request(self, **fields: Any) -> None:
        pass

    def agent_response(self, **fields: Any) -> None:
        pass


# Module-level singleton — stateless, safe to share across call sites and
# safe to use as a function-argument default without triggering ``B008``.
NOOP_PROGRESS_EMITTER: Final[ProgressEmitter] = NoOpProgressEmitter()
