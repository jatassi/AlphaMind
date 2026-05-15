"""Phase-2 persistence helpers for the ``submit_envelope`` package — ALP-464.

After ALP-458 broke the decision↔execution cycle by relocating the engine-stub
to ``decision.portfolio_manager.submit_envelope``, the formerly-inline imports
of :mod:`alphamind.execution.write_paths.phase2` (cycle
workarounds) become normal top-level imports — exactly one entry at the head
of this module, used by every helper below.

Each helper wraps one Phase-2 entrypoint: ``persist_envelope_outcome``,
``persist_command_abandoned``, ``persist_envelope_parse_failure``,
``persist_envelope_rejection``. The wrappers exist so the orchestrator can
default the ``StatePersistenceConfig`` argument uniformly via
:func:`_stub_state_persistence_config` when callers don't supply one.
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

if TYPE_CHECKING:
    from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult


async def _persist_envelope_outcome_via_phase2(
    invocation_handle: Any,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    state_persistence_config: Any | None,
    *,
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None,
) -> None:
    """Dispatch to :func:`persist_envelope_outcome` with a default config."""
    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_outcome(
        invocation_handle,
        envelope,
        submission_results,
        config=config,
        dispatch_results=dispatch_results,
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

    Used by the engine-stub coordinated swap (story 03e / ALP-390) when a
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
    state_persistence_config: Any | None,
) -> None:
    """Dispatch to :func:`persist_envelope_parse_failure` with a default config."""
    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_parse_failure(invocation_handle, failed_entry, config=config)


async def _persist_envelope_rejection_via_phase2(
    invocation_handle: Any,
    envelope: PMEnvelope,
    errors: Sequence[Any],
    state_persistence_config: Any | None,
) -> None:
    """Dispatch to :func:`persist_envelope_rejection` with a default config."""
    config = state_persistence_config or _stub_state_persistence_config()
    await persist_envelope_rejection(invocation_handle, envelope, tuple(errors), config=config)


def _stub_state_persistence_config() -> Any:
    """Construct a no-op StatePersistenceConfig for callers that didn't supply one.

    Phase 2 doesn't read any knob in this story; the config is part of the
    forward-shaped signature only.
    """
    from alphamind.state.config import StatePersistenceConfig

    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 1,
            "snapshot_read_timeout_seconds": 1.0,
            "pip_freeze_snapshot_root": "/tmp",
            "invocation_provenance_root": "/tmp",
        }
    )


__all__ = [
    "_emit_command_abandoned_via_phase2",
    "_persist_envelope_outcome_via_phase2",
    "_persist_envelope_parse_failure_via_phase2",
    "_persist_envelope_rejection_via_phase2",
    "_stub_state_persistence_config",
]
