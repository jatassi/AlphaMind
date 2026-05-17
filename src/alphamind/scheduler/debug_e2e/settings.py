"""Debug-e2e runtime settings — broker overrides + progress-emitter factory.

Story ALP-497 introduces the minimal surface this module exposes:
:class:`DebugE2ESettings` with an ``emitter_factory`` field the orchestrator
reads at the top of :func:`alphamind.scheduler.orchestrator.run_invocation`
to swap the no-op default for a recording / JSONL emitter when the
``--debug-e2e`` flag is set.

Story 02c (ALP-499) will extend this dataclass with the JSONL-emitter
factory and the broker-adapter override fields the design doc names.
Until then the contract is intentionally narrow: callers may supply an
``emitter_factory: Callable[[invocation_id], ProgressEmitter]`` and the
orchestrator wires it through both composition runners.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from alphamind._kernel.progress import ProgressEmitter

__all__ = ["DebugE2ESettings"]


@dataclass(frozen=True, slots=True)
class DebugE2ESettings:
    """Settings bundle the orchestrator inspects in debug-e2e mode.

    ``emitter_factory`` is invoked once per invocation with the
    ``invocation_id`` so the JSONL emitter (story 02c) can open a fresh
    log file under the invocation's archive directory. Production
    callers leave :attr:`RunInvocationContext.debug_e2e` at ``None`` and
    the orchestrator falls back to :data:`NOOP_PROGRESS_EMITTER`.
    """

    emitter_factory: Callable[[str], ProgressEmitter]
