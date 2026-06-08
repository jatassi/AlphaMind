"""SQLAlchemy mapping for the ``retrospective_decisions`` table (ALP-875 / story 02c).

One row per decision captured during a retrospective walkthrough (Phase 4).
Promotion-candidate accept/reject decisions and follow-up accept/reject
decisions both land here.

``report_id`` FK → ``retrospective_reports.report_id`` (ON DELETE RESTRICT).
``linked_validation_id`` nullable FK → ``validations.validation_id`` (ON DELETE
RESTRICT) — set only when the decision spawned a validation registration via
``/feedback-validate``; null otherwise.

Closed-set vocabularies:
* ``decision_type``: ``promotion_candidate | follow_up``
* ``verdict``: ``accepted | rejected``

No Alembic migration here — story 03 owns the combined migration.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.feedback_loop.retrospective.records import DecisionType, Verdict
from alphamind.persistence.models import Base


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


class RetrospectiveDecisionsRow(Base):
    """Append-only per-decision row for retrospective walkthrough decisions.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Retrospective
    decisions.
    """

    __tablename__ = "retrospective_decisions"

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    report_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "retrospective_reports.report_id",
            name="fk_retrospective_decisions_report_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    captured_at: Mapped[str] = mapped_column(Text, nullable=False)
    decision_type: Mapped[str] = mapped_column(Text, nullable=False)
    item_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    linked_validation_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "validations.validation_id",
            name="fk_retrospective_decisions_linked_validation_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            _check_in("decision_type", DecisionType),
            name="ck_retrospective_decisions_decision_type",
        ),
        CheckConstraint(
            _check_in("verdict", Verdict),
            name="ck_retrospective_decisions_verdict",
        ),
        Index("ix_retrospective_decisions_report_id", "report_id"),
        Index("ix_retrospective_decisions_captured_at", "captured_at"),
        Index("ix_retrospective_decisions_decision_type", "decision_type"),
        Index("ix_retrospective_decisions_linked_validation_id", "linked_validation_id"),
    )
