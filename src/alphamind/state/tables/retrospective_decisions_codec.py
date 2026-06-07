"""Round-trip codec between ``RetrospectiveDecisionRecord`` and
``RetrospectiveDecisionsRow`` (ALP-875 / story 02c).

All timestamps are stored as ISO 8601 text with UTC ``Z`` suffix.
``linked_validation_id`` is nullable and passes through as-is.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.feedback_loop.retrospective.records import (
    DecisionId,
    DecisionType,
    ReportId,
    RetrospectiveDecisionRecord,
    Verdict,
)
from alphamind.state.tables._singleton_codec import datetime_to_iso_z
from alphamind.state.tables.retrospective_decisions import RetrospectiveDecisionsRow


def _parse_isoformat(text: str) -> datetime:
    return datetime.fromisoformat(text)


def record_to_row(record: RetrospectiveDecisionRecord) -> RetrospectiveDecisionsRow:
    """Encode a ``RetrospectiveDecisionRecord`` into a ``RetrospectiveDecisionsRow``."""
    return RetrospectiveDecisionsRow(
        decision_id=str(record.decision_id),
        report_id=str(record.report_id),
        captured_at=datetime_to_iso_z(record.captured_at, field_name="captured_at"),
        decision_type=record.decision_type.value,
        item_identifier=record.item_identifier,
        verdict=record.verdict.value,
        rationale=record.rationale,
        linked_validation_id=record.linked_validation_id,
    )


def row_to_record(row: RetrospectiveDecisionsRow) -> RetrospectiveDecisionRecord:
    """Decode a ``RetrospectiveDecisionsRow`` back into a ``RetrospectiveDecisionRecord``."""
    return RetrospectiveDecisionRecord(
        decision_id=DecisionId(row.decision_id),
        report_id=ReportId(row.report_id),
        captured_at=_parse_isoformat(row.captured_at),
        decision_type=DecisionType(row.decision_type),
        item_identifier=row.item_identifier,
        verdict=Verdict(row.verdict),
        rationale=row.rationale,
        linked_validation_id=row.linked_validation_id,
    )


__all__ = ["record_to_row", "row_to_record"]
