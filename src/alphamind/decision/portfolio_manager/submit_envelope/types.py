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

ALP-476 (story 10c): :class:`SubmitEnvelopeState` is now ``frozen=True,
slots=True`` per the audit's L8 mutable-dataclass remediation. Mutation sites
use :func:`dataclasses.replace` and thread the new state through return values;
the MCP closure in :mod:`.server` rebinds via ``nonlocal state``.
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


@dataclass(frozen=True, slots=True)
class SubmitEnvelopeState:
    """Frozen per-invocation cumulative state for the submit_envelope tool.

    Each transition produces a new instance via :func:`dataclasses.replace`:
    ``validation_state`` advances on every accepted command via
    ``with_accepted_proposal(delta)``; ``submission_log`` appends one entry per
    call that parsed to a :class:`PMEnvelope`; ``failed_submission_log``
    appends one entry per Layer-1 (Pydantic) parse failure. Synthetic
    command_ids derive their ordinal from the envelope's command index and
    ``attempt_seq`` from ``post_rejection`` modification count — both
    deterministic from the envelope alone, so no counter cell is needed.

    ``invocation_id`` is required (non-empty) — it is interpolated into every
    synthetic command_id via :func:`alphamind.execution.oms.command_ids.derive_pm_command_id`;
    an empty value would surface there as malformed IDs like
    ``inv-.{envelope_id}.0.0``.
    """

    validation_state: ValidationToolState
    invocation_id: str
    submission_log: tuple[SubmissionLogEntry, ...] = ()
    failed_submission_log: tuple[FailedSubmissionEntry, ...] = ()

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

    ``starting_validation_state`` seeds the submit_envelope cumulative state
    from the same initial :class:`ValidationToolState` the PM's
    :func:`validate_guardrail` MCP tool starts from, but the two advance
    INDEPENDENTLY thereafter: ``ValidationToolState`` is immutable, so the
    standalone validation tool mutates its own ``_ValidationStateCell`` on each
    pre-submission PASS while submit_envelope advances ``validation_state`` via
    :func:`dataclasses.replace` on each accepted command. A pre-submission
    ``validate_guardrail`` PASS therefore does NOT credit the submit-time
    cumulative state — only commands actually accepted through
    ``submit_envelope`` (surviving guardrails *and* broker routing) do
    (ALP-743).
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
