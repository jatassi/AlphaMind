"""SQLAlchemy mapping for the ``retrospective_reports`` table (ALP-875 / story 02c).

One row per retrospective produced by ``/feedback-retrospective``. The record
is metadata only; the long-form markdown content lives on the filesystem at
``data/retrospective_reports/{report_id}/report.md`` and is referenced via
``report_file_ref``.

``generated_by_session_id`` is nullable — the review-session surface
(ALP-686) is deferred; populated once that surface ships.

No Alembic migration here — story 03 owns the combined migration.
"""

from __future__ import annotations

from sqlalchemy import Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base


class RetrospectiveReportsRow(Base):
    """Append-only per-report metadata row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Retrospective
    reports. Content lives at ``report_file_ref``; this row is the durable
    metadata handle the retrospective decision FK chains back to.
    """

    __tablename__ = "retrospective_reports"

    report_id: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[str] = mapped_column(Text, nullable=False)
    window_end: Mapped[str] = mapped_column(Text, nullable=False)
    generated_at: Mapped[str] = mapped_column(Text, nullable=False)
    generated_by_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_file_ref: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_retrospective_reports_generated_at", "generated_at"),
        Index("ix_retrospective_reports_window_start", "window_start"),
    )
