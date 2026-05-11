"""Phase 2 envelope dispatcher (story 03b / ALP-445).

Iterates the PM's ``submission_log`` from the decision-pipeline result,
persists each envelope's outcome via :func:`persist_envelope_outcome`, and
aggregates per-command accept/reject counts into the :class:`Phase2Summary`
the orchestrator records on the invocation row.

Per the parent issue's fail-closed invariant, a persistence exception
propagates out so the surrounding :class:`InvocationContext` rolls back.
Broker dispatch lives in the OMS layer (story 03e ALP-390); this story
keeps the orchestrator dependency narrow — only Phase 2 writeback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from alphamind.execution.oms.submit_envelope_mcp import (
    SubmissionLogEntry,
)
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.write_paths.phase2 import (
    persist_envelope_outcome,
)

__all__ = ["PMResultLike", "Phase2Summary", "dispatch_phase2"]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Phase2Summary:
    """Aggregate envelope-dispatch outcome recorded on the invocation row."""

    commands_submitted: int
    commands_rejected: int


class PMResultLike(Protocol):
    """Structural shape ``dispatch_phase2`` consumes.

    The real :class:`~alphamind.decision.portfolio_manager.runner.PMResult`
    carries other fields; this dispatcher only reads ``submission_log``,
    so the protocol is intentionally narrow.
    """

    @property
    def submission_log(self) -> tuple[SubmissionLogEntry, ...]: ...


async def dispatch_phase2(
    *,
    handle: InvocationHandle,
    pm_result: PMResultLike,
    state_persistence_config: StatePersistenceConfig,
) -> Phase2Summary:
    """Persist every PM-submitted envelope and aggregate per-command counts.

    Each :class:`SubmissionLogEntry` is routed through
    :func:`persist_envelope_outcome`, which writes the per-command
    writebacks + the umbrella ``pm_decision`` entry and stamps
    ``phase2_completed_at`` on the invocation row. Submission counts:

    - ``commands_submitted`` — total per-command results with
      ``status="accepted"`` across every envelope.
    - ``commands_rejected`` — total with ``status="rejected"``.

    A persistence exception propagates out so the surrounding
    ``InvocationContext`` rolls back; partial-broker-state is the broker's
    idempotent-semantics problem per
    ``docs/design/mid-pipeline-failure-handling.md``.
    """
    submitted = 0
    rejected = 0
    for entry in pm_result.submission_log:
        await persist_envelope_outcome(
            handle,
            entry.envelope,
            entry.submission_results,
            config=state_persistence_config,
        )
        for result in entry.submission_results:
            if result.status == "accepted":
                submitted += 1
            elif result.status == "rejected":
                rejected += 1

    return Phase2Summary(commands_submitted=submitted, commands_rejected=rejected)
