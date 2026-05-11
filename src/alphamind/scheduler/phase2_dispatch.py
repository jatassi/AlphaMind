"""Phase 2 envelope dispatcher (ALP-449 three-tx model).

Iterates the PM's ``submission_log`` from the decision-pipeline result and
persists each envelope's outcome via :func:`persist_envelope_outcome` **in
its own transaction** — one fresh session per envelope. Matches the
design's "each command's mutations commit atomically" guarantee
(``docs/design/05-execution-layer/state-persistence.md`` § Phase 2 write
path), so a mid-batch persistence failure leaves earlier envelopes' writes
durable. Aggregates per-command accept/reject counts into the
:class:`Phase2Summary` the orchestrator records.

Broker dispatch lives in the OMS layer (story 03e ALP-390); this module
keeps the orchestrator dependency narrow — only Phase 2 writeback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    session_factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
    pm_result: PMResultLike,
    state_persistence_config: StatePersistenceConfig,
) -> Phase2Summary:
    """Persist every PM-submitted envelope in its own transaction; aggregate counts.

    Each :class:`SubmissionLogEntry` opens a fresh session via
    ``session_factory`` and a fresh :class:`InvocationHandle` bound to
    that session, routes through :func:`persist_envelope_outcome`, and
    commits on clean exit. A persistence exception rolls back the
    in-flight envelope's transaction and propagates; envelopes that
    completed before the failure remain durable (the design's
    "commands submitted before the abort remain committed" semantic from
    ``docs/design/mid-pipeline-failure-handling.md``).

    Submission counts:
      * ``commands_submitted`` — total per-command results with
        ``status="accepted"`` across every envelope.
      * ``commands_rejected`` — total with ``status="rejected"``.
    """
    submitted = 0
    rejected = 0
    for entry in pm_result.submission_log:
        async with session_factory() as session:
            handle = InvocationHandle(session=session, invocation_id=invocation_id)
            await persist_envelope_outcome(
                handle,
                entry.envelope,
                entry.submission_results,
                config=state_persistence_config,
            )
            await session.commit()
        for result in entry.submission_results:
            if result.status == "accepted":
                submitted += 1
            elif result.status == "rejected":
                rejected += 1

    return Phase2Summary(commands_submitted=submitted, commands_rejected=rejected)
