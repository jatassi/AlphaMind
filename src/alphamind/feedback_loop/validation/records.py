"""Frozen record types for the validation-discipline data layer (ALP-874).

``ValidationRecord`` captures the pre-registered operator contract at REGISTER time.
``ValidationOutcomeRecord`` captures the one-per-validation evaluation written at
EVALUATE time. Both are immutable dataclasses (frozen=True) — the SQL row is the
mutable store; the record is what the rest of the system passes around.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

# ---------------------------------------------------------------------------
# New-type aliases for validation-layer IDs
# ---------------------------------------------------------------------------

ValidationId = NewType("ValidationId", str)
OutcomeId = NewType("OutcomeId", str)
MetricId = NewType("MetricId", str)


# ---------------------------------------------------------------------------
# Closed-set vocabularies
# ---------------------------------------------------------------------------


class ExpectedDirection(StrEnum):
    IMPROVED = "improved"
    UNCHANGED = "unchanged"
    DEGRADED = "degraded"


class SupersededReason(StrEnum):
    REGIME_TRANSITION = "regime_transition"
    MODEL_VERSION_CHANGE = "model_version_change"
    CONCURRENT_EDIT_ON_WATCHED_ARTIFACT = "concurrent_edit_on_watched_artifact"


class Verdict(StrEnum):
    IMPROVED = "improved"
    DEGRADED = "degraded"
    NO_CHANGE = "no_change"
    INCONCLUSIVE = "inconclusive"


class RollbackStatus(StrEnum):
    MANDATORY_CLEAN_FAILURE = "mandatory_clean_failure"
    OPTIONAL_PENDING_RETROSPECTIVE = "optional_pending_retrospective"
    NOT_APPLICABLE = "not_applicable"


# ---------------------------------------------------------------------------
# Frozen records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationRecord:
    """Pre-registered operator contract captured at REGISTER time.

    ``watched_metric_ids`` is an ordered tuple of ``MetricId`` strings — order
    is preserved through the codec's JSON-text round-trip.

    ``registered_by_session_id``, ``superseded_at``, and ``superseded_reason``
    are nullable: the session surface is deferred (ALP-686); supersession is
    absent until the detector fires.
    """

    validation_id: ValidationId
    registered_at: datetime
    registered_by_session_id: str | None
    edited_artifact: str
    pre_edit_version: str
    post_edit_version: str
    registered_regime: str
    registered_model_id: str
    watched_metric_ids: tuple[MetricId, ...]
    window_length_days: int
    expected_direction: ExpectedDirection
    expected_magnitude: str
    success_criterion: str
    failure_criterion: str
    evaluation_due_at: datetime
    superseded_at: datetime | None
    superseded_reason: SupersededReason | None


@dataclass(frozen=True)
class ValidationOutcomeRecord:
    """One-per-validation evaluation written at EVALUATE time.

    ``evaluated_by_session_id`` is nullable — the review-session surface is
    deferred (ALP-686).

    ``posterior_summary`` is a JSON-serialisable mapping; the codec stores it
    as JSON-text (same pattern as ``narrative_json`` in ``ThesisRow``).
    """

    outcome_id: OutcomeId
    validation_id: ValidationId
    evaluated_at: datetime
    evaluated_by_session_id: str | None
    verdict: Verdict
    posterior_summary: dict[str, object]
    confounder_notes: str | None
    narrative: str
    rollback_status: RollbackStatus


__all__ = [
    "ExpectedDirection",
    "MetricId",
    "OutcomeId",
    "RollbackStatus",
    "SupersededReason",
    "ValidationId",
    "ValidationOutcomeRecord",
    "ValidationRecord",
    "Verdict",
]
