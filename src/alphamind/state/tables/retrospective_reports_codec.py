"""Round-trip codec between ``RetrospectiveReportRecord`` and
``RetrospectiveReportsRow`` (ALP-875 / story 02c).

All timestamps are stored as ISO 8601 text with UTC ``Z`` suffix.
"""

from __future__ import annotations

from alphamind.feedback_loop.retrospective.records import (
    ReportId,
    RetrospectiveReportRecord,
)
from alphamind.state.tables._singleton_codec import datetime_to_iso_z, iso_z_to_datetime
from alphamind.state.tables.retrospective_reports import RetrospectiveReportsRow


def record_to_row(record: RetrospectiveReportRecord) -> RetrospectiveReportsRow:
    """Encode a ``RetrospectiveReportRecord`` into a ``RetrospectiveReportsRow``."""
    return RetrospectiveReportsRow(
        report_id=str(record.report_id),
        window_start=datetime_to_iso_z(record.window_start, field_name="window_start"),
        window_end=datetime_to_iso_z(record.window_end, field_name="window_end"),
        generated_at=datetime_to_iso_z(record.generated_at, field_name="generated_at"),
        generated_by_session_id=record.generated_by_session_id,
        report_file_ref=record.report_file_ref,
    )


def row_to_record(row: RetrospectiveReportsRow) -> RetrospectiveReportRecord:
    """Decode a ``RetrospectiveReportsRow`` back into a ``RetrospectiveReportRecord``."""
    return RetrospectiveReportRecord(
        report_id=ReportId(row.report_id),
        window_start=iso_z_to_datetime(row.window_start),
        window_end=iso_z_to_datetime(row.window_end),
        generated_at=iso_z_to_datetime(row.generated_at),
        generated_by_session_id=row.generated_by_session_id,
        report_file_ref=row.report_file_ref,
    )


__all__ = ["record_to_row", "row_to_record"]
