"""Round-trip codec between ``ValidationOutcomeRecord`` and
``ValidationOutcomesRow`` (ALP-874).

``posterior_summary`` serialises as JSON-text; the round-trip preserves
the full nested structure.
"""

from __future__ import annotations

import json

from alphamind.feedback_loop.validation.records import (
    OutcomeId,
    RollbackStatus,
    ValidationId,
    ValidationOutcomeRecord,
    Verdict,
)
from alphamind.state.tables._singleton_codec import datetime_to_iso_z, iso_z_to_datetime
from alphamind.state.tables.validation_outcomes import ValidationOutcomesRow


def record_to_row(record: ValidationOutcomeRecord) -> ValidationOutcomesRow:
    """Encode a ``ValidationOutcomeRecord`` into a ``ValidationOutcomesRow``."""
    return ValidationOutcomesRow(
        outcome_id=str(record.outcome_id),
        validation_id=str(record.validation_id),
        evaluated_at=datetime_to_iso_z(record.evaluated_at, field_name="evaluated_at"),
        evaluated_by_session_id=record.evaluated_by_session_id,
        verdict=record.verdict.value,
        posterior_summary_json=json.dumps(record.posterior_summary),
        confounder_notes=record.confounder_notes,
        narrative=record.narrative,
        rollback_status=record.rollback_status.value,
    )


def row_to_record(row: ValidationOutcomesRow) -> ValidationOutcomeRecord:
    """Decode a ``ValidationOutcomesRow`` back into a
    ``ValidationOutcomeRecord``."""
    return ValidationOutcomeRecord(
        outcome_id=OutcomeId(row.outcome_id),
        validation_id=ValidationId(row.validation_id),
        evaluated_at=iso_z_to_datetime(row.evaluated_at),
        evaluated_by_session_id=row.evaluated_by_session_id,
        verdict=Verdict(row.verdict),
        posterior_summary=json.loads(row.posterior_summary_json),
        confounder_notes=row.confounder_notes,
        narrative=row.narrative,
        rollback_status=RollbackStatus(row.rollback_status),
    )


__all__ = ["record_to_row", "row_to_record"]
