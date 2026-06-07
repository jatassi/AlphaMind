"""SQLAlchemy mapping for the ``validation_outcomes`` table (ALP-874 / story 02b).

One row per validation evaluation written at EVALUATE time. The FK to
``validations.validation_id`` carries a UNIQUE constraint — exactly one
outcome per validation.

``posterior_summary`` serialises as JSON-text (per-metric shape resolved
by the metric the validation watched) — same pattern as ``narrative_json``
in ``ThesisRow``.

``evaluated_by_session_id`` is nullable — the review-session surface
(ALP-686) is deferred.

``rollback_status`` is persisted (not recomputed at read time) so the
derivation rule applied at EVALUATE time is preserved across schema or
threshold evolution.

No Alembic migration here — story 03 owns the combined migration.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.feedback_loop.validation.records import (
    RollbackStatus,
    Verdict,
)
from alphamind.persistence.models import Base


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


class ValidationOutcomesRow(Base):
    """One-per-validation evaluation row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Validation
    outcomes. The ``UniqueConstraint`` on ``validation_id`` enforces the
    one-outcome-per-validation invariant at the SQL layer.
    """

    __tablename__ = "validation_outcomes"

    outcome_id: Mapped[str] = mapped_column(Text, primary_key=True)
    validation_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "validations.validation_id",
            name="fk_validation_outcomes_validation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    evaluated_at: Mapped[str] = mapped_column(Text, nullable=False)
    evaluated_by_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    verdict: Mapped[str] = mapped_column(Text, nullable=False)
    # Per-metric pre/post comparison with explicit uncertainty — JSON-text.
    posterior_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    confounder_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    rollback_status: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "validation_id",
            name="uq_validation_outcomes_validation_id",
        ),
        CheckConstraint(
            _check_in("verdict", Verdict),
            name="ck_validation_outcomes_verdict",
        ),
        CheckConstraint(
            _check_in("rollback_status", RollbackStatus),
            name="ck_validation_outcomes_rollback_status",
        ),
        Index("ix_validation_outcomes_validation_id", "validation_id"),
        Index("ix_validation_outcomes_evaluated_at", "evaluated_at"),
    )
