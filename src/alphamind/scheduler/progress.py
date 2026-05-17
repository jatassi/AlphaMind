"""Re-export of the kernel-level ``ProgressEmitter`` Protocol + NoOp default.

Story ALP-495 introduced the Protocol in this module. Story ALP-497
(harness emitter wiring) moved the implementation into
:mod:`alphamind._kernel.progress` so the seven harnesses (4 analysis +
3 decision) can depend on it without breaching the composition-root
layering contract (``scheduler`` sits above ``analysis`` / ``decision``).

The public seam stays here so the orchestrator's call sites, the
debug-e2e ``JsonlProgressEmitter`` (story 02c), and the
``RunInvocationContext.debug_e2e.emitter_factory`` plumbing keep
importing from ``alphamind.scheduler.progress`` per the design doc.
"""

from __future__ import annotations

from alphamind._kernel.progress import (
    NOOP_PROGRESS_EMITTER,
    NoOpProgressEmitter,
    ProgressEmitter,
)

__all__ = ["NOOP_PROGRESS_EMITTER", "NoOpProgressEmitter", "ProgressEmitter"]
