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
(``duration_s``, ``input_tokens``, ``cache_read_tokens``,
``cache_write_tokens``, ``output_tokens``, ``tool_calls``,
``stop_reason``) is fixed at the Protocol level per parent issue
ALP-493 § Pre-resolved (B) — the smallest set that answers
"stuck or working?", "cost in budget?", and "tool-using or thinking?".

The three input-side counts are emitted separately rather than summed
because the Anthropic API ``usage`` payload splits the prompt across
``input_tokens`` (non-cached delta), ``cache_read_input_tokens``
(cache hit — the bulk of an AlphaMind prompt), and
``cache_creation_input_tokens`` (cache write). An operator tailing the
JSONL needs the split to distinguish "context assembled, cached
correctly" from "context-assembly path broken" (ALP-701).
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

    def agent_retrying(
        self, *, phase: str, agent: str, model: str, attempt: int, reason: str
    ) -> None:
        """One recoverable-transient retry of an agent call (ALP-756).

        Emitted between SDK attempts when a harness re-invokes after a
        retryable failure (today: the synthesizer's empty, non-overflow
        ``end_turn`` response). ``attempt`` is the 1-based index of the
        attempt that just failed; ``reason`` is a short machine token
        (``"empty_response"``). Distinct from ``agent_request`` so an
        operator tailing the stream can tell a retry apart from a fresh
        call.

        Note for a future SSE wiring: the schema-level ``AgentRetryingEvent``
        numbers ``attempt`` from 2 (the upcoming attempt) and types
        ``reason`` as ``_FailureMode``; a bridge that emits it must translate
        this signal's failed-attempt index (``attempt + 1``) and reason
        vocabulary accordingly.
        """
        ...

    def agent_response(  # noqa: PLR0913 - kwargs-only Protocol contract: 3 routing kwargs (phase/agent/model) + the 7-field response set fixed by ALP-493 § (B) and the SDK ``usage`` split (ALP-701)
        self,
        *,
        phase: str,
        agent: str,
        model: str,
        duration_s: float,
        input_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
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

    def agent_retrying(self, **fields: Any) -> None:
        pass

    def agent_response(self, **fields: Any) -> None:
        pass


# Module-level singleton — stateless, safe to share across call sites and
# safe to use as a function-argument default without triggering ``B008``.
NOOP_PROGRESS_EMITTER: Final[ProgressEmitter] = NoOpProgressEmitter()
