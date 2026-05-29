"""Phase 2 command-execution write path (story 08 / ALP-366).

Public entry points the ``submit_envelope`` MCP wrapper invokes
once an :class:`InvocationHandle` is supplied:

* :func:`persist_envelope_outcome` — accepted envelope: per-command writebacks
  plus the envelope-level ``pm_decision`` audit entry.
* :func:`persist_envelope_parse_failure` — Layer-1 parse failure
  (``envelope_parse_failed`` entry).
* :func:`persist_envelope_rejection` — Layer-2/3 envelope rejection
  (``envelope_rejected`` entry).
* :func:`persist_engine_envelope_outcome` — engine-originated protective CLOSE
  (story 04 / ALP-375).
* :func:`persist_command_abandoned` — post-rollback audit entry on a fresh
  ``InvocationHandle`` after the failed-command transaction rolls back.

All entry points join the open ``InvocationContext`` transaction; the
surrounding context commits on clean exit and rolls back on exception.

The implementation is decomposed by OMS command kind (story 06c / ALP-465):
:mod:`.open`, :mod:`.close`, :mod:`.adjust`, :mod:`.cancel`, :mod:`.add`,
plus :mod:`._shared` for cross-cutting helpers (activity-log emission,
cash-ledger primitives, protective-leg cancellation, generic order builders).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Literal

from alphamind.commands.command_models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    OMSCommand,
    OpenCommand,
)
from alphamind.commands.pm_envelope import PMEnvelope
from alphamind.commands.submission_log import FailedSubmissionEntry
from alphamind.commands.submission_results import SubmissionResult
from alphamind.commands.validation_results import ValidationError as PMValidationError
from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult
from alphamind.execution.write_paths.phase2._shared import _emit
from alphamind.execution.write_paths.phase2.add import _writeback_add
from alphamind.execution.write_paths.phase2.adjust import _writeback_adjust
from alphamind.execution.write_paths.phase2.cancel import (
    _writeback_cancel,
    persist_entry_window_cancel,
)
from alphamind.execution.write_paths.phase2.close import _writeback_close
from alphamind.execution.write_paths.phase2.open import _writeback_open
from alphamind.portfolio_state.events.activity_log import (
    CommandAbandonedDetail,
    EnvelopeParseFailedDetail,
    EnvelopeRejectionDetail,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.context import (
    InvocationHandle,
    stamp_phase_completion,
)

# Verdict mapping from PMEnvelope literal to PMVerdict StrEnum the activity log uses.
_VERDICT_TO_PM_VERDICT: dict[str, PMVerdict] = {
    "approve": PMVerdict.APPROVE,
    "approve_with_modification": PMVerdict.APPROVE_WITH_MODIFICATION,
    "reject": PMVerdict.REJECT,
    "override_with_corrective_action": PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION,
}


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def persist_envelope_outcome(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    submission_results: tuple[SubmissionResult, ...],
    *,
    config: StatePersistenceConfig,
    dispatch_results: tuple[BrokerDispatchResult | None, ...] | None = None,
) -> None:
    """Persist per-command writebacks + one ``pm_decision`` for an accepted envelope.

    When ``dispatch_results`` is supplied (broker-routing coordinated swap,
    story 03e / ALP-390), each accepted command's writeback consumes the
    matching :class:`BrokerDispatchResult` so the persisted entry / close /
    add / adjust order carries Alpaca's real ``alpaca_order_id`` rather than
    the synthetic ``alp-{order_id}`` placeholder. ``None`` entries (legacy
    callers and per-command failures) fall back to the synthetic id.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    if dispatch_results is None:
        dispatch_results = tuple(None for _ in submission_results)

    accepted_command_ids: list[str] = []
    for result, command, dispatch in zip(
        submission_results, envelope.commands, dispatch_results, strict=True
    ):
        if result.status != "accepted":
            continue
        await _dispatch_command_writeback(
            handle, command=command, result=result, dispatch_result=dispatch
        )
        accepted_command_ids.append(result.command_id)

    await _emit_pm_decision(handle, envelope=envelope, command_ids=tuple(accepted_command_ids))
    # Layer-1 parse rejections deliberately skip this — only an accepted
    # envelope's full writeback counts as a Phase 2 commit.
    await stamp_phase_completion(handle, column="phase2_completed_at")


async def persist_envelope_parse_failure(
    handle: InvocationHandle,
    failed_entry: FailedSubmissionEntry,
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist a Layer-1 parse failure as one ``envelope_parse_failed`` entry."""
    del config  # No knobs consumed at this story; signature is forward-shaped.

    detail = EnvelopeParseFailedDetail(
        attempted_envelope_id=str(failed_entry.raw_args.get("envelope_id", "ENV-REC-INVALID")),
        attempted_command_id=failed_entry.command_id,
        validation_error_repr=failed_entry.validation_error_repr,
        raw_args_json=json.dumps(failed_entry.raw_args),
    )
    _emit(
        handle,
        event_type=EventType.ENVELOPE_PARSE_FAILED,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.COMMAND_EXECUTOR,
    )


async def persist_envelope_rejection(
    handle: InvocationHandle,
    envelope: PMEnvelope,
    errors: tuple[PMValidationError, ...],
    *,
    config: StatePersistenceConfig,
) -> None:
    """Persist a Layer-2/3 envelope-level rejection as one ``envelope_rejected`` entry.

    Symmetric with ``persist_envelope_parse_failure`` — the envelope parsed
    cleanly at Layer-1 but failed an invariant or cross-command coherence
    check (per ``validate_pm_envelope``). Every error's ``criterion`` (when
    set) feeds ``blocking_criteria`` so a feedback-loop query can correlate
    envelope rejections back to their failing rules; the full error inventory
    serializes into ``validation_errors_json`` for operator forensics.

    The envelope's ``position_id`` is *not* forwarded into the activity_log
    row's ``position_id`` column. A common Layer-3 rejection criterion is
    ``position_id_resolves`` — the envelope's ``position_id`` is precisely
    the orphan id that has no row in ``positions``, and the FK constraint
    on ``activity_log.position_id`` (DEFERRABLE INITIALLY DEFERRED, validated
    at COMMIT) would roll back the entire invocation transaction. The
    envelope id and any referenced position id are preserved in the JSON
    detail's ``envelope_id`` and ``referenced_position_id`` fields for
    operator forensics; an orphan position_id is unqueryable against
    ``positions`` anyway, which is what that column is for.
    """
    del config  # No knobs consumed at this story; signature is forward-shaped.

    if not errors:
        msg = "persist_envelope_rejection requires at least one ValidationError"
        raise ValueError(msg)

    detail = EnvelopeRejectionDetail(
        envelope_id=envelope.envelope_id,
        referenced_position_id=envelope.position_id,
        attempted_command_count=len(envelope.commands),
        blocking_criteria=tuple(e.criterion for e in errors if e.criterion is not None),
        validation_errors_json=json.dumps([asdict(e) for e in errors]),
    )
    _emit(
        handle,
        event_type=EventType.ENVELOPE_REJECTED,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.GUARDRAIL_LAYER,
    )


async def persist_engine_envelope_outcome(
    handle: InvocationHandle,
    *,
    close_command: CloseCommand,
    command_id: str,
    extra_metadata: dict[str, Any],
    submitted_alpaca_order_id: str | None = None,
) -> None:
    """Persist the protective CLOSE for an engine-originated envelope.

    Sibling to :func:`persist_envelope_outcome` for the engine path
    (story 04 / ALP-375). Engine envelopes carry exactly one CLOSE command
    with no PM verdict — there is no ``pm_decision`` activity-log entry to
    emit. The function delegates to :func:`_writeback_close` with engine
    context threaded through ``extra_metadata`` (cascade_id,
    position_selection_rationale, rule_breached) and ``EventSource.BRACKET_MANAGER``
    as the activity-log source per ``oms-commands.md § Command origins``.

    When ``submitted_alpaca_order_id`` is supplied (broker-routing coordinated
    swap, story 03e / ALP-390), the persisted close order carries the broker's
    real Alpaca order id rather than the synthetic ``alp-{order_id}`` placeholder.

    The continuous monitor produced the envelope; the OMS owns persistence.
    The surrounding ``InvocationContext`` commits or rolls back atomically.
    """
    synthetic_result = SubmissionResult(
        command_ordinal=0,
        status="accepted",
        command_id=command_id,
        acknowledgment=None,
    )
    await _writeback_close(
        handle,
        command=close_command,
        result=synthetic_result,
        extra_metadata=extra_metadata,
        source=EventSource.BRACKET_MANAGER,
        submitted_alpaca_order_id=submitted_alpaca_order_id,
    )
    await stamp_phase_completion(handle, column="phase2_completed_at")


async def persist_command_abandoned(
    handle: InvocationHandle,
    *,
    envelope_id: str,
    command_id: str,
    originating_agent: str,
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"],
    failure_reason: str,
    retry_attempt_count: int,
) -> None:
    """Emit one ``command_abandoned`` entry post-rollback of the failed command txn.

    The caller opens this on a fresh ``InvocationHandle`` *after* rolling back
    the per-command transaction so the audit trail survives the rollback per
    the design doc's contract.
    """
    detail = CommandAbandonedDetail(
        envelope_id=envelope_id,
        command_id=command_id,
        originating_agent=originating_agent,
        command_type=command_type,
        failure_reason=failure_reason,
        retry_attempt_count=retry_attempt_count,
    )
    _emit(
        handle,
        event_type=EventType.COMMAND_ABANDONED,
        order_id=None,
        position_id=None,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
        source=EventSource.COMMAND_EXECUTOR,
    )


# ---------------------------------------------------------------------------
# Per-command-type dispatch
# ---------------------------------------------------------------------------


async def _dispatch_command_writeback(
    handle: InvocationHandle,
    *,
    command: OMSCommand,
    result: SubmissionResult,
    dispatch_result: BrokerDispatchResult | None = None,
) -> None:
    submitted_id = dispatch_result.alpaca_order_id if dispatch_result is not None else None
    if isinstance(command, OpenCommand):
        await _writeback_open(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    if isinstance(command, CloseCommand):
        await _writeback_close(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    if isinstance(command, AdjustCommand):
        await _writeback_adjust(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    if isinstance(command, CancelCommand):
        await _writeback_cancel(handle, command=command, result=result)
        return
    if isinstance(command, AddCommand):
        await _writeback_add(
            handle, command=command, result=result, submitted_alpaca_order_id=submitted_id
        )
        return
    msg = f"unsupported command variant: {type(command).__name__}"
    raise NotImplementedError(msg)


# ---------------------------------------------------------------------------
# Per-envelope PM decision emission
# ---------------------------------------------------------------------------


async def _emit_pm_decision(
    handle: InvocationHandle,
    *,
    envelope: PMEnvelope,
    command_ids: tuple[str, ...],
) -> None:
    """Emit one ``pm_decision`` capturing the full envelope provenance."""
    detail = PMDecisionDetail(
        envelope_id=envelope.envelope_id,
        source_provenance_json={
            "source_provenance": envelope.source_provenance,
            "source_recommendation_id": envelope.source_recommendation_id,
            "recommendation_type": envelope.recommendation_type,
            "position_id": envelope.position_id,
        },
        evaluation_json=envelope.evaluation.model_dump(mode="json"),
        modifications_json=[m.model_dump(mode="json") for m in envelope.modifications],
        resulting_command_ids=command_ids,
        verdict=_VERDICT_TO_PM_VERDICT[envelope.verdict],
    )
    _emit(
        handle,
        event_type=EventType.PM_DECISION,
        order_id=None,
        position_id=envelope.position_id,
        thesis_id=None,
        timestamp=datetime.now(UTC),
        detail=detail,
    )


__all__ = [
    "persist_command_abandoned",
    "persist_engine_envelope_outcome",
    "persist_entry_window_cancel",
    "persist_envelope_outcome",
    "persist_envelope_parse_failure",
    "persist_envelope_rejection",
]
