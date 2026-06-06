"""PM decision event details — verdicts, abandonments, envelope rejections."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Literal

from alphamind.portfolio_state.events.types import (
    EventGroup,
    EventType,
    PMVerdict,
)


@dataclass(frozen=True, slots=True)
class PMDecisionDetail:
    """Detail payload for PM_DECISION events."""

    envelope_id: str
    source_provenance_json: dict[str, Any]
    evaluation_json: dict[str, Any]
    modifications_json: list[dict[str, Any]]
    resulting_command_ids: tuple[str, ...]
    verdict: PMVerdict
    # ALP-557: the full body of the originating proposal — the analyst
    # Recommendation (pm_analyst envelopes) or the strategist PositionAssessment /
    # PendingOrderAssessment (pm_strategist envelopes), as ``model_dump(mode="json")``.
    # The counterfactual-replay engine (ALP-129) reconstructs entry_order / target /
    # invalidation_legs / time_expectation_hours / position_size / instrument from it.
    originating_proposal_json: dict[str, Any]
    # ALP-765: execution-layer enter-now reprices that happened after the PM
    # authored the verdict. Empty for envelopes with no enter-now limit entries.
    reprice_markers_json: list[dict[str, Any]] = dataclasses.field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CommandAbandonedDetail:
    """Detail payload for COMMAND_ABANDONED events."""

    envelope_id: str
    command_id: str
    originating_agent: str
    command_type: Literal["OPEN", "CLOSE", "ADD", "ADJUST", "CANCEL"]
    failure_reason: str
    retry_attempt_count: int


@dataclass(frozen=True, slots=True)
class EnvelopeParseFailedDetail:
    """Detail payload for ENVELOPE_PARSE_FAILED events.

    Persists the Layer-1 (Pydantic) parse failure surfaced by the
    ``submit_envelope`` MCP wrapper before any per-command processing runs.
    Mirrors the in-memory ``FailedSubmissionEntry`` so the audit trail
    survives process restart.
    """

    attempted_envelope_id: str
    attempted_command_id: str
    validation_error_repr: str
    raw_args_json: str


@dataclass(frozen=True, slots=True)
class EnvelopeRejectionDetail:
    """Detail payload for ENVELOPE_REJECTED events.

    Persists Layer-2/3 rejection of a parsed envelope by ``validate_pm_envelope``
    — invariant violations or cross-command coherence failures. Symmetric with
    :class:`EnvelopeParseFailedDetail` (Layer-1 parse failures); both event
    types are PM_DECISION-grouped envelope-level forensics.

    The envelope's ``position_id`` is preserved here as ``referenced_position_id``
    for operator forensics; the activity_log row's ``position_id`` column is
    nullified by the emitter to honor the FK constraint when the orphan id is
    itself the rejection cause (criterion ``position_id_resolves``).
    """

    envelope_id: str
    referenced_position_id: str | None
    attempted_command_count: int
    blocking_criteria: tuple[str, ...]
    validation_errors_json: str


_REGISTRY: list[tuple[EventType, type, EventGroup]] = [
    (EventType.PM_DECISION, PMDecisionDetail, EventGroup.PM_DECISION),
    (EventType.COMMAND_ABANDONED, CommandAbandonedDetail, EventGroup.PM_DECISION),
    (
        EventType.ENVELOPE_PARSE_FAILED,
        EnvelopeParseFailedDetail,
        EventGroup.PM_DECISION,
    ),
    (
        EventType.ENVELOPE_REJECTED,
        EnvelopeRejectionDetail,
        EventGroup.PM_DECISION,
    ),
]


__all__ = [
    "CommandAbandonedDetail",
    "EnvelopeParseFailedDetail",
    "EnvelopeRejectionDetail",
    "PMDecisionDetail",
]
