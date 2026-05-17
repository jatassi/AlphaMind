"""Per-invocation progress event sink — Protocol + no-op default.

Story ALP-495 introduces this module as the seam the orchestrator + the
analysis/decision composition runners thread through. The Protocol's
no-op default keeps every call site branch-free (P3 — illegal states
unrepresentable): emitters are always non-``None``, so call sites never
guard with ``if emitter is not None``.

Production code uses :class:`NoOpProgressEmitter`. ``--debug-e2e`` mode
swaps in ``JsonlProgressEmitter`` (landed in story 02c / ALP-499) via
the ``DebugE2ESettings.emitter_factory`` attached to
:class:`~alphamind.scheduler.run_context.RunInvocationContext`.

The ``agent_response`` field set
(``duration_s``, ``input_tokens``, ``output_tokens``, ``tool_calls``,
``stop_reason``) is fixed at the Protocol level per parent issue
ALP-493 § Pre-resolved (B) — the smallest set that answers
"stuck or working?", "cost in budget?", and "tool-using or thinking?".
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["NoOpProgressEmitter", "ProgressEmitter"]


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
    """

    def phase_start(self, phase: str) -> None:
        pass

    def phase_done(self, phase: str, **fields: Any) -> None:
        pass

    def agent_request(self, **fields: Any) -> None:
        pass

    def agent_response(self, **fields: Any) -> None:
        pass
