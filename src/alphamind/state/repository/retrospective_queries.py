"""Read/write helpers for the ``retrospective_reports`` and
``retrospective_decisions`` tables (ALP-875 / story 02c).

Kept in its own module (not ``sql_repository.py``) to avoid wave-collision
with sibling story table work. All helpers use a synchronous ``Session``
(matching the ``validation_queries`` pattern); async helpers are not needed
at this tier.

Helpers:
* :func:`insert_retrospective_report` — encode and queue a
  ``RetrospectiveReportRecord`` for insert.
* :func:`read_retrospective_report` — fetch a single
  ``RetrospectiveReportRecord`` by report ID.
* :func:`insert_retrospective_decision` — encode and queue a
  ``RetrospectiveDecisionRecord`` for insert.
* :func:`read_decisions_for_report` — return all decisions for a given
  report, ordered by ``captured_at`` ascending.
* :func:`read_unresolved_followup_decisions` — return follow-up decisions
  that have no ``linked_validation_id`` (unresolved per the rollback evidence
  protocol in ``feedback-loop.md § Rollback evidence protocol``).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.feedback_loop.retrospective.records import (
    ReportId,
    RetrospectiveDecisionRecord,
    RetrospectiveReportRecord,
)
from alphamind.state.tables.retrospective_decisions import RetrospectiveDecisionsRow
from alphamind.state.tables.retrospective_decisions_codec import (
    record_to_row as decision_record_to_row,
)
from alphamind.state.tables.retrospective_decisions_codec import (
    row_to_record as decision_row_to_record,
)
from alphamind.state.tables.retrospective_reports import RetrospectiveReportsRow
from alphamind.state.tables.retrospective_reports_codec import (
    record_to_row as report_record_to_row,
)
from alphamind.state.tables.retrospective_reports_codec import (
    row_to_record as report_row_to_record,
)

# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------


def insert_retrospective_report(
    session: Session,
    record: RetrospectiveReportRecord,
) -> None:
    """Encode and queue a ``RetrospectiveReportRecord`` for insert.

    Adds the row to *session*; the primary-key uniqueness constraint is
    enforced when the unit of work flushes — duplicate ``report_id``
    surfaces as :class:`sqlalchemy.exc.IntegrityError` at the next
    ``flush`` / ``commit``.
    """
    session.add(report_record_to_row(record))


def insert_retrospective_decision(
    session: Session,
    record: RetrospectiveDecisionRecord,
) -> None:
    """Encode and queue a ``RetrospectiveDecisionRecord`` for insert.

    Adds the row to *session*; the primary-key uniqueness constraint is
    enforced when the unit of work flushes — duplicate ``decision_id``
    surfaces as :class:`sqlalchemy.exc.IntegrityError` at the next
    ``flush`` / ``commit``.
    """
    session.add(decision_record_to_row(record))


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def read_retrospective_report(
    session: Session,
    report_id: ReportId,
) -> RetrospectiveReportRecord | None:
    """Return the ``RetrospectiveReportRecord`` for *report_id*, or ``None``."""
    row = session.get(RetrospectiveReportsRow, str(report_id))
    if row is None:
        return None
    return report_row_to_record(row)


def read_decisions_for_report(
    session: Session,
    report_id: ReportId,
) -> tuple[RetrospectiveDecisionRecord, ...]:
    """Return all decisions for *report_id*, ordered by ``captured_at`` ascending.

    Returns an empty tuple when no decisions exist for the report.
    """
    stmt = (
        select(RetrospectiveDecisionsRow)
        .where(RetrospectiveDecisionsRow.report_id == str(report_id))
        .order_by(RetrospectiveDecisionsRow.captured_at.asc())
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(decision_row_to_record(row) for row in rows)


def read_unresolved_followup_decisions(
    session: Session,
) -> tuple[RetrospectiveDecisionRecord, ...]:
    """Return follow-up decisions that have no linked validation.

    A follow-up decision is unresolved when:
    * ``decision_type`` = ``'follow_up'``, AND
    * ``linked_validation_id`` IS NULL (no validation has been spawned).

    Used by the ``optional_pending_retrospective`` rollback evidence
    protocol (``feedback-loop.md § Rollback evidence protocol``) to check
    whether any outstanding follow-ups should gate a rollback decision.

    Results are ordered by ``captured_at`` ascending.
    """
    stmt = (
        select(RetrospectiveDecisionsRow)
        .where(
            RetrospectiveDecisionsRow.decision_type == "follow_up",
            RetrospectiveDecisionsRow.linked_validation_id.is_(None),
        )
        .order_by(RetrospectiveDecisionsRow.captured_at.asc())
    )
    rows = session.execute(stmt).scalars().all()
    return tuple(decision_row_to_record(row) for row in rows)


__all__ = [
    "insert_retrospective_decision",
    "insert_retrospective_report",
    "read_decisions_for_report",
    "read_retrospective_report",
    "read_unresolved_followup_decisions",
]
