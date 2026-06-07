"""Round-trip codec between ``ValidationRecord`` and ``ValidationsRow`` (ALP-874).

``watched_metric_ids`` serialises as a JSON-text array (ordered list of
``MetricId`` strings); the round-trip preserves insertion order.
"""

from __future__ import annotations

import json
from datetime import datetime

from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    MetricId,
    SupersededReason,
    ValidationId,
    ValidationRecord,
)
from alphamind.state.tables._singleton_codec import datetime_to_iso_z
from alphamind.state.tables.validations import ValidationsRow


def _parse_isoformat(text: str) -> datetime:
    return datetime.fromisoformat(text)


def record_to_row(record: ValidationRecord) -> ValidationsRow:
    """Encode a ``ValidationRecord`` into a ``ValidationsRow``."""
    return ValidationsRow(
        validation_id=str(record.validation_id),
        registered_at=datetime_to_iso_z(record.registered_at, field_name="registered_at"),
        registered_by_session_id=record.registered_by_session_id,
        edited_artifact=record.edited_artifact,
        pre_edit_version=record.pre_edit_version,
        post_edit_version=record.post_edit_version,
        registered_regime=record.registered_regime,
        registered_model_id=record.registered_model_id,
        watched_metric_ids_json=json.dumps([str(m) for m in record.watched_metric_ids]),
        window_length_days=record.window_length_days,
        expected_direction=record.expected_direction.value,
        expected_magnitude=record.expected_magnitude,
        success_criterion=record.success_criterion,
        failure_criterion=record.failure_criterion,
        evaluation_due_at=datetime_to_iso_z(
            record.evaluation_due_at, field_name="evaluation_due_at"
        ),
        superseded_at=(
            None
            if record.superseded_at is None
            else datetime_to_iso_z(record.superseded_at, field_name="superseded_at")
        ),
        superseded_reason=(
            None if record.superseded_reason is None else record.superseded_reason.value
        ),
    )


def row_to_record(row: ValidationsRow) -> ValidationRecord:
    """Decode a ``ValidationsRow`` back into a ``ValidationRecord``."""
    return ValidationRecord(
        validation_id=ValidationId(row.validation_id),
        registered_at=_parse_isoformat(row.registered_at),
        registered_by_session_id=row.registered_by_session_id,
        edited_artifact=row.edited_artifact,
        pre_edit_version=row.pre_edit_version,
        post_edit_version=row.post_edit_version,
        registered_regime=row.registered_regime,
        registered_model_id=row.registered_model_id,
        watched_metric_ids=tuple(MetricId(m) for m in json.loads(row.watched_metric_ids_json)),
        window_length_days=row.window_length_days,
        expected_direction=ExpectedDirection(row.expected_direction),
        expected_magnitude=row.expected_magnitude,
        success_criterion=row.success_criterion,
        failure_criterion=row.failure_criterion,
        evaluation_due_at=_parse_isoformat(row.evaluation_due_at),
        superseded_at=(None if row.superseded_at is None else _parse_isoformat(row.superseded_at)),
        superseded_reason=(
            None if row.superseded_reason is None else SupersededReason(row.superseded_reason)
        ),
    )


__all__ = ["record_to_row", "row_to_record"]
