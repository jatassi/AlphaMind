"""SQLAlchemy mapping for the ``thesis_components`` table (story 04b / ALP-359).

One row per component (entry / target / invalidation rationale) of a
thesis. The parent-child split is mandated by the design's three
consumption modes — programmatic-query metadata, single-component LLM
evaluation, full-thesis evaluation — so component narratives can be
loaded selectively without rehydrating the whole thesis.

``thesis_id`` carries an FK to ``theses.thesis_id`` with
``ON DELETE RESTRICT`` (deleting a parent thesis with surviving children
is rejected at SQL time). CHECK constraints encode the
``ThesisComponentType`` and ``ThesisComponentOutcome`` vocabularies.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisComponentType,
)


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


class ThesisComponentRow(Base):
    """Forward-only per-component row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Thesis
    components. Key assumptions and supporting signals serialize as JSON
    arrays for round-trip with the typed Pydantic record.
    """

    __tablename__ = "thesis_components"

    component_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thesis_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("theses.thesis_id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_type: Mapped[str] = mapped_column(Text, nullable=False)
    linked_bracket_leg: Mapped[str | None] = mapped_column(Text, nullable=True)
    instrument_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    key_assumptions_json: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_signals_json: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _check_in("component_type", ThesisComponentType),
            name="ck_thesis_components_component_type",
        ),
        CheckConstraint(
            "resolution_outcome IS NULL OR "
            + _check_in("resolution_outcome", ThesisComponentOutcome),
            name="ck_thesis_components_resolution_outcome",
        ),
        Index("ix_thesis_components_thesis_id", "thesis_id"),
    )
