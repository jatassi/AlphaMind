"""SQLAlchemy mapping for the ``validations`` table (ALP-874 / story 02b).

One row per pre-registered validation captured at REGISTER time. The
``validations`` table is the anchor of the validation-discipline system —
story 07b writes here at REGISTER time; 08b writes the supersession fields;
the digest (07a) reads active validations for its validation section.

``watched_metric_ids`` serialises as JSON-text (ordered list of
``MetricId`` strings) — same pattern as ``narrative_json`` in
``ThesisRow`` / ``key_assumptions_json`` in ``ThesisComponentRow``.

``registered_by_session_id``, ``superseded_at``, and ``superseded_reason``
are nullable:

* ``registered_by_session_id`` — the review-session surface (ALP-686) is
  deferred; populated once that surface ships.
* ``superseded_at`` / ``superseded_reason`` — null while the window is still
  readable; written by the supersession detector (story 08b).

No Alembic migration here — story 03 owns the combined migration.

``validation_id`` is the clean PK/FK target for:
* ``validation_outcomes.validation_id`` (unique FK, this story)
* ``retrospective_decisions.linked_validation_id`` (nullable FK, story 02c)
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.feedback_loop.validation.records import (
    ExpectedDirection,
    SupersededReason,
)
from alphamind.persistence.models import Base


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


class ValidationsRow(Base):
    """Append-only per-validation row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Validations.
    Status (``pending`` / ``superseded`` / ``evaluated``) is derived at read
    time — ``superseded`` when ``superseded_at`` is non-null, ``evaluated``
    when a joining ``ValidationOutcomesRow`` exists, ``pending`` otherwise.
    """

    __tablename__ = "validations"

    validation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    registered_at: Mapped[str] = mapped_column(Text, nullable=False)
    registered_by_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    edited_artifact: Mapped[str] = mapped_column(Text, nullable=False)
    pre_edit_version: Mapped[str] = mapped_column(Text, nullable=False)
    post_edit_version: Mapped[str] = mapped_column(Text, nullable=False)
    registered_regime: Mapped[str] = mapped_column(Text, nullable=False)
    registered_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Ordered list of MetricId strings serialised as JSON-text.
    watched_metric_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    window_length_days: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_direction: Mapped[str] = mapped_column(Text, nullable=False)
    expected_magnitude: Mapped[str] = mapped_column(Text, nullable=False)
    success_criterion: Mapped[str] = mapped_column(Text, nullable=False)
    failure_criterion: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_due_at: Mapped[str] = mapped_column(Text, nullable=False)
    superseded_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    superseded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _check_in("expected_direction", ExpectedDirection),
            name="ck_validations_expected_direction",
        ),
        CheckConstraint(
            "superseded_reason IS NULL OR " + _check_in("superseded_reason", SupersededReason),
            name="ck_validations_superseded_reason",
        ),
        Index("ix_validations_edited_artifact", "edited_artifact"),
        Index("ix_validations_registered_at", "registered_at"),
        Index("ix_validations_superseded_at", "superseded_at"),
    )
