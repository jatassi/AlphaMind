"""Frozen record types for the retrospective data layer (ALP-875 / story 02c).

``RetrospectiveReportRecord`` is metadata + filesystem ref for the long-form
markdown produced in Phase 3 of ``/feedback-retrospective``.

``RetrospectiveDecisionRecord`` is one row per decision captured during a
retrospective walkthrough (Phase 4) — promotion-candidate or follow-up,
accept/reject — optionally linking a spawned validation.

Both are immutable dataclasses (frozen=True). The SQL row is the mutable
store; the record is what the rest of the system passes around.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import NewType

# ---------------------------------------------------------------------------
# New-type aliases
# ---------------------------------------------------------------------------

ReportId = NewType("ReportId", str)
DecisionId = NewType("DecisionId", str)


# ---------------------------------------------------------------------------
# Closed-set vocabularies
# ---------------------------------------------------------------------------


class DecisionType(StrEnum):
    PROMOTION_CANDIDATE = "promotion_candidate"
    FOLLOW_UP = "follow_up"


class Verdict(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Frozen records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetrospectiveReportRecord:
    """Metadata record for one retrospective report.

    The long-form markdown content lives on the filesystem at
    ``data/retrospective_reports/{report_id}/report.md``; ``report_file_ref``
    holds the path.

    ``generated_by_session_id`` is nullable — the review-session surface
    (ALP-686) is deferred; populated once that surface ships.
    """

    report_id: ReportId
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    generated_by_session_id: str | None
    report_file_ref: str


@dataclass(frozen=True)
class RetrospectiveDecisionRecord:
    """One decision captured during a retrospective walkthrough.

    ``decision_type`` is ``promotion_candidate`` or ``follow_up``.
    ``verdict`` is ``accepted`` or ``rejected``.

    ``linked_validation_id`` is nullable — set only when the decision spawned
    a validation registration via ``/feedback-validate``.
    """

    decision_id: DecisionId
    report_id: ReportId
    captured_at: datetime
    decision_type: DecisionType
    item_identifier: str
    verdict: Verdict
    rationale: str
    linked_validation_id: str | None


__all__ = [
    "DecisionId",
    "DecisionType",
    "ReportId",
    "RetrospectiveDecisionRecord",
    "RetrospectiveReportRecord",
    "Verdict",
]
