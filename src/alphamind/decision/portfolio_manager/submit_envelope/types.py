"""Boundary types and state cell for the ``submit_envelope`` package — ALP-464.

This module hosts the per-invocation cumulative state cell
(:class:`SubmitEnvelopeState`) plus the public accessors
(:func:`build_initial_submit_envelope_state`, :func:`get_submission_log`,
:func:`get_failed_submission_log`) sibling submodules and external consumers
share.

The boundary Pydantic types — :class:`Acknowledgment`, :class:`RejectionPayload`,
:class:`SubmissionResult`, :class:`_PerRuleHeadroomEntry`,
:class:`_ValidationMetadata`, :class:`_BreachedRule` — live in
:mod:`alphamind.commands.submission_results` (the engine-side contract module
introduced by ALP-458). Log-entry types — :class:`SubmissionLogEntry`,
:class:`FailedSubmissionEntry` — live in :mod:`alphamind.commands.submission_log`.
We import them here so that the package's curated ``__init__.py`` can re-export
the names from a single source.

Story 10c's frozen-dataclass conversion of the internal Group C types is out of
scope; they remain Pydantic on the engine-side contract module.
"""

from __future__ import annotations

from dataclasses import dataclass

from alphamind.commands.submission_log import FailedSubmissionEntry, SubmissionLogEntry
from alphamind.commands.submission_results import (
    Acknowledgment,
    RejectionPayload,
    SubmissionResult,
    _BreachedRule,
    _PerRuleHeadroomEntry,
    _ValidationMetadata,
)
from alphamind.risk_guardrails.state_delivery.validation_tool import ValidationToolState


@dataclass
class SubmitEnvelopeState:
    """Mutable per-invocation cumulative state for the submit_envelope tool.

    The cell is mutated in place by the MCP closure: ``validation_state``
    advances on every accepted command via ``with_accepted_proposal(delta)``;
    ``submission_log`` appends one entry per call that parsed to a
    :class:`PMEnvelope`; ``failed_submission_log`` appends one entry per
    Layer-1 (Pydantic) parse failure; ``command_id_counter`` is not currently
    incremented (the synthetic ID format derives ordinal from the envelope's
    command index and ``attempt_seq`` from ``post_rejection`` modification
    count, both of which are deterministic from the envelope alone).

    ``invocation_id`` is required (non-empty) — it is interpolated into every
    synthetic command_id via :func:`alphamind.execution.oms.command_ids.derive_pm_command_id`;
    an empty value would surface there as malformed IDs like
    ``inv-.{envelope_id}.0.0``.
    """

    validation_state: ValidationToolState
    invocation_id: str
    submission_log: tuple[SubmissionLogEntry, ...] = ()
    failed_submission_log: tuple[FailedSubmissionEntry, ...] = ()
    command_id_counter: int = 0

    def __post_init__(self) -> None:
        if not self.invocation_id:
            msg = "SubmitEnvelopeState.invocation_id must be non-empty"
            raise ValueError(msg)


def build_initial_submit_envelope_state(
    *,
    invocation_id: str,
    starting_validation_state: ValidationToolState,
) -> SubmitEnvelopeState:
    """Construct a fresh :class:`SubmitEnvelopeState` for one invocation.

    The ``starting_validation_state`` is the same cell that backs the
    analyst's / strategist's / PM's :func:`validate_guardrail` MCP tool —
    sharing the cell keeps cumulative-impact tracking unified across
    pre-submission validation and submit-time re-validation.
    """
    return SubmitEnvelopeState(
        validation_state=starting_validation_state,
        invocation_id=invocation_id,
    )


def get_submission_log(state: SubmitEnvelopeState) -> tuple[SubmissionLogEntry, ...]:
    """Return the cumulative submission log for *state*."""
    return state.submission_log


def get_failed_submission_log(
    state: SubmitEnvelopeState,
) -> tuple[FailedSubmissionEntry, ...]:
    """Return the cumulative Layer-1 parse-failure log for *state*."""
    return state.failed_submission_log


__all__ = [
    "Acknowledgment",
    "FailedSubmissionEntry",
    "RejectionPayload",
    "SubmissionLogEntry",
    "SubmissionResult",
    "SubmitEnvelopeState",
    "_BreachedRule",
    "_PerRuleHeadroomEntry",
    "_ValidationMetadata",
    "build_initial_submit_envelope_state",
    "get_failed_submission_log",
    "get_submission_log",
]
