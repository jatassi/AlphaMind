"""Submission-log entry types — the per-call records the engine-stub keeps.

The PM-side engine-stub
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


@dataclass
class SubmissionLogEntry:
    """One ``submit_envelope`` call's record — envelope + per-command results."""

    envelope: PMEnvelope
    submission_results: tuple[SubmissionResult, ...]


@dataclass
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
