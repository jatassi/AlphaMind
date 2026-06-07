"""Phase-2 persistence helpers for the ``submit_envelope`` package — ALP-464.

After ALP-458 broke the decision↔execution cycle by relocating the engine-stub
to ``decision.portfolio_manager.submit_envelope``, the formerly-inline imports
of :mod:`alphamind.execution.write_paths.phase2` (cycle
workarounds) become normal top-level imports — exactly one entry at the head
of this module, used by every helper below.

Each helper wraps one Phase-2 entrypoint: ``persist_envelope_outcome``,
``persist_command_abandoned``, ``persist_envelope_parse_failure``,
``persist_envelope_rejection``. The wrappers forward the orchestrator-supplied
:class:`~alphamind.state.config.StatePersistenceConfig` through unchanged so
every Phase-2 knob read inside the engine sees the operator's real config.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from alphamind._kernel.ids import CommandId, EnvelopeId
from alphamind.commands.pm_envelope import PMEnvelope
from alphamind.decision.portfolio_manager.submit_envelope.types import (
    FailedSubmissionEntry,
    SubmissionResult,
)
from alphamind.execution.write_paths.phase2 import (
    persist_command_abandoned,
    persist_envelope_outcome,
    persist_envelope_parse_failure,
    persist_envelope_rejection,
)
from alphamind.state.config import StatePersistenceConfig

if TYPE_CHECKING:
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult


async def _persist_envelope_outcome_via_phase2(
    invocation_handle: Any,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    state_persistence_config: StatePersistenceConfig,
    *,
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None,
    reprice_markers: tuple[Any, ...] = (),
    originating_proposal_json: dict[str, Any],
) -> None:
    """Dispatch to :func:`persist_envelope_outcome` with the supplied config."""
    await persist_envelope_outcome(
        invocation_handle,
        envelope,
        submission_results,
        config=state_persistence_config,
        dispatch_results=dispatch_results,
        reprice_markers=reprice_markers,
        originating_proposal_json=originating_proposal_json,
    )


async def _emit_command_abandoned_via_phase2(
    invocation_handle: Any,
    *,
    envelope_id: EnvelopeId,
    command_id: CommandId,
    originating_agent: str,
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"],
    failure_reason: str,
    retry_attempt_count: int,
) -> None:
    """Dispatch to :func:`persist_command_abandoned`.

    Used by the broker-routing coordinated swap (story 03e / ALP-390) when a
    broker dispatch returns ``GatewaySubmissionFailed`` — the command's
    writeback is skipped and the audit trail surfaces the failure.
    """
    await persist_command_abandoned(
        invocation_handle,
        envelope_id=envelope_id,
        command_id=command_id,
        originating_agent=originating_agent,
        command_type=command_type,
        failure_reason=failure_reason,
        retry_attempt_count=retry_attempt_count,
    )


async def _persist_envelope_parse_failure_via_phase2(
    invocation_handle: Any,
    failed_entry: FailedSubmissionEntry,
    state_persistence_config: StatePersistenceConfig,
) -> None:
    """Dispatch to :func:`persist_envelope_parse_failure` with the supplied config."""
    await persist_envelope_parse_failure(
        invocation_handle, failed_entry, config=state_persistence_config
    )


async def _persist_envelope_rejection_via_phase2(
    invocation_handle: Any,
    envelope: PMEnvelope,
    errors: Sequence[Any],
    state_persistence_config: StatePersistenceConfig,
) -> None:
    """Dispatch to :func:`persist_envelope_rejection` with the supplied config."""
    await persist_envelope_rejection(
        invocation_handle, envelope, tuple(errors), config=state_persistence_config
    )


__all__ = [
    "_emit_command_abandoned_via_phase2",
    "_persist_envelope_outcome_via_phase2",
    "_persist_envelope_parse_failure_via_phase2",
    "_persist_envelope_rejection_via_phase2",
]
