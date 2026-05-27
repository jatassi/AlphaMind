"""Submission-log entry types — the per-call records the submit_envelope wrapper keeps.

The PM-side submit_envelope wrapper
(:mod:`alphamind.decision.portfolio_manager.submit_envelope`) appends one
:class:`SubmissionLogEntry` per ``submit_envelope`` call that parsed cleanly
to a :class:`PMEnvelope`, and one :class:`FailedSubmissionEntry` per call
that failed Layer-1 (Pydantic) coercion. Both logs are surfaced on the
harness's ``HarnessSuccess`` and consumed by the execution-side Phase 2
write path (:mod:`alphamind.execution.write_paths.phase2`).

Living in :mod:`alphamind.commands` lets both decision-side producers and
execution-side consumers reference these shapes without re-opening the
decision↔execution import cycle that ALP-458 closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alphamind.commands.pm_envelope import PMEnvelope
from alphamind.commands.submission_results import SubmissionResult

__all__ = [
    "FailedSubmissionEntry",
    "SubmissionLogEntry",
]


@dataclass(frozen=True, slots=True)
class SubmissionLogEntry:
    """One ``submit_envelope`` call's record — envelope + per-command results.

    ``dispatch_results`` carries the per-command broker outcome (one entry per
    command, aligned with ``submission_results``) when the submit_envelope
    wrapper routed accepted commands through the broker (ALP-711). The field
    is typed ``tuple[Any, ...] | None`` because ``alphamind.commands`` is a
    leaf package per ``.importlinter``'s ``commands-leaf`` contract and may
    not import ``alphamind.execution.oms.broker_dispatch.BrokerDispatchResult``;
    consumers downstream (the Phase 2 writeback) read the structural
    ``.alpaca_order_id`` attribute and cast back to the typed shape at
    their boundary. ``None`` (the default) signals the legacy / debug-e2e
    log-only path that never routed through a broker — Phase 2 writeback
    falls back to synthetic ``alp-{order_id}`` placeholders in that case.

    ``abandoned_entries`` (also ALP-711) carries per-broker-failure markers
    the scheduler's ``dispatch_phase2`` uses to emit ``COMMAND_ABANDONED``
    activity-log rows. Same ``Any`` rationale: the originating dataclass
    (``_AbandonedCommandEntry``) lives in the decision layer; downstream
    consumers read ``.command_id`` / ``.command_type`` / ``.failure_reason``
    / ``.retry_attempt_count`` structurally.
    """

    envelope: PMEnvelope
    submission_results: tuple[SubmissionResult, ...]
    dispatch_results: tuple[Any, ...] | None = None
    abandoned_entries: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class FailedSubmissionEntry:
    """One ``submit_envelope`` call that failed Layer-1 (Pydantic) parsing.

    ``submission_log`` only records calls that produced a parsed
    :class:`PMEnvelope`; this parallel log preserves the raw payload, the
    Pydantic error text, and the synthetic ``command_id`` for every Layer-1
    rejection so post-hoc forensics can reconstruct attempts that never
    reached command processing.
    """

    raw_args: dict[str, Any]
    validation_error_repr: str
    command_id: str
