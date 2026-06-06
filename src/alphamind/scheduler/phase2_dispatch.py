"""Phase 2 envelope dispatcher.

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
from typing import TYPE_CHECKING, Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.commands.submission_log import SubmissionLogEntry
from alphamind.execution.write_paths.phase2 import (
    persist_command_abandoned,
    persist_envelope_outcome,
)
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_from_row,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.orders import OrderRow

if TYPE_CHECKING:
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult

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


async def _envelope_already_persisted(
    session: AsyncSession,
    *,
    invocation_id: str,
    envelope_id: str,
    accepted_command_ids: tuple[str, ...],
) -> bool:
    """Return whether this envelope's Phase-2 outcome was already persisted in-turn.

    Primary key (ALP-836 / scope G): a pre-committed ``orders`` row keyed by the
    deterministic ``client_order_id`` (= an accepted command_id). The broker-active
    PM turn commits each command's durable order row BEFORE its broker dispatch
    (atomicity-first), so a present row is the load-bearing signal that the in-turn
    writeback ran — and it survives a lost ``pm_decision`` finalize commit, which
    keying solely off the ``PM_DECISION`` marker (the prior ALP-763 approach) would
    not. Re-running ``persist_envelope_outcome`` here would PK-collide on those
    rows + double the capital reservation, so we skip.

    Fallback: the ``PM_DECISION`` activity-log row scoped to
    ``(invocation_id, envelope_id)`` — covers an all-rejected envelope (no order
    rows) and any legacy path that emits the marker without a client_order_id row.
    ``envelope_id`` is read off the rehydrated typed detail rather than matched
    against the encoded JSON so the key never couples to the serialization format.
    """
    if accepted_command_ids:
        order_stmt = (
            select(OrderRow.order_id)
            .where(OrderRow.client_order_id.in_(accepted_command_ids))
            .limit(1)
        )
        if (await session.execute(order_stmt)).first() is not None:
            return True
    stmt = select(ActivityLogRow).where(
        ActivityLogRow.event_type == EventType.PM_DECISION.value,
        ActivityLogRow.invocation_id == invocation_id,
    )
    for row in (await session.execute(stmt)).scalars():
        entry = activity_log_entry_from_row(row)
        if getattr(entry.detail, "envelope_id", None) == envelope_id:
            return True
    return False


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
        # ALP-763 — the broker-active PM turn writes + commits each envelope's
        # outcome IN-TURN (right after broker dispatch) to close the fast-fill
        # race against the deferred order-row commit. When that already
        # happened, re-running ``persist_envelope_outcome`` here would PK-collide
        # on the order/position rows and double the (non-idempotent) capital
        # reservation — so skip the writeback for an already-persisted envelope.
        # The in-turn writeback commits the FULL envelope outcome, including one
        # ``COMMAND_ABANDONED`` audit row per abandoned entry; so on the broker-
        # active path the abandoned-audit loop below is gated off the same
        # ``already_persisted`` flag to avoid double-emitting those rows. On the
        # non-broker deferred path (debug-e2e / log-only) the in-turn write never
        # ran, ``already_persisted`` is False, and ``dispatch_phase2`` is the sole
        # emitter of both the writeback and the abandoned audit. The summary
        # counts are derived purely from ``submission_results`` and run regardless.
        async with session_factory() as session:
            handle = InvocationHandle(session=session, invocation_id=invocation_id)
            accepted_command_ids = tuple(
                r.command_id for r in entry.submission_results if r.status == "accepted"
            )
            already_persisted = await _envelope_already_persisted(
                session,
                invocation_id=invocation_id,
                envelope_id=str(entry.envelope.envelope_id),
                accepted_command_ids=accepted_command_ids,
            )
            if not already_persisted:
                # ALP-711 scope (C) — when the submit_envelope wrapper routed
                # accepted commands through the broker, ``entry.dispatch_results``
                # carries the per-command :class:`BrokerDispatchResult` payload
                # so this writeback persists the broker's real ``alpaca_order_id``.
                # ``None`` (the debug-e2e / log-only path) falls through to the
                # synthetic-ID fallback inside :func:`persist_envelope_outcome`.
                await persist_envelope_outcome(
                    handle,
                    entry.envelope,
                    entry.submission_results,
                    config=state_persistence_config,
                    dispatch_results=cast(
                        "tuple[BrokerDispatchResult | None, ...] | None",
                        entry.dispatch_results,
                    ),
                    reprice_markers=entry.reprice_markers,
                    originating_proposal_json=entry.originating_proposal_json,
                )
                await session.commit()
        # ALP-711 — when broker routing returned ``GatewaySubmissionFailed``
        # for one or more accepted commands, the wrapper appended an
        # ``_AbandonedCommandEntry`` per failure onto the log entry. Emit
        # one ``COMMAND_ABANDONED`` activity-log row per entry on a fresh
        # session so the audit trail survives the per-envelope rollback the
        # broker rejection implicitly performs (the rejected command never
        # wrote orders so there is no rollback artifact, but the
        # design-doc contract is "abandoned audit lands on a fresh
        # session" regardless). ALP-763 — gated on ``not already_persisted``:
        # the broker-active in-turn writeback already emitted + committed these
        # rows, so re-emitting here would produce duplicate audit rows. Only the
        # deferred path (no in-turn write) emits them here.
        if not already_persisted:
            for abandoned in entry.abandoned_entries:
                async with session_factory() as session:
                    handle = InvocationHandle(session=session, invocation_id=invocation_id)
                    await persist_command_abandoned(
                        handle,
                        envelope_id=str(entry.envelope.envelope_id),
                        command_id=str(abandoned.command_id),
                        originating_agent=str(entry.envelope.source_provenance),
                        command_type=abandoned.command_type,
                        failure_reason=str(abandoned.failure_reason),
                        retry_attempt_count=int(abandoned.retry_attempt_count),
                    )
                    await session.commit()
        for result in entry.submission_results:
            if result.status == "accepted":
                submitted += 1
            elif result.status == "rejected":
                rejected += 1

    return Phase2Summary(commands_submitted=submitted, commands_rejected=rejected)
