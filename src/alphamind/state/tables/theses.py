"""SQLAlchemy mapping for the ``theses`` table (story 04b / ALP-359).

One row per thesis, one-to-one with a position. Carries the lifecycle
state machine (``ACTIVE`` → ``RESOLVED`` | ``CANCELLED``) plus the
non-component fields the design's three consumption modes need at
metadata-only granularity. The component bodies live in the sibling
``thesis_components`` table; ``narrative_json`` carries the parent
record's non-component fields preserved as JSON for faithful round-trip.

``position_id`` carries a DEFERRABLE INITIALLY DEFERRED FK to
``positions.position_id`` (``ON DELETE RESTRICT``), matching the
``e9d2c4f7b3a1_tighten_state_persistence_fks`` migration. The deferral
accommodates the positions↔theses cycle that Phase 2's OPEN writeback seeds
in a single transaction. CHECK constraints encode the same enum vocabularies
the typed ``ThesisRecord`` enforces, so a future direct-SQL writer faces the
same fail-closed guarantees.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, Float, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.theses import (
    ThesisRecordStatus,
    ThesisResolutionCategory,
)


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


class ThesisRow(Base):
    """Forward-only per-thesis row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Theses.
    Component bodies live in ``thesis_components``; this row carries only
    the parent-level fields plus a ``narrative_json`` blob that preserves
    the rest of the Pydantic shape losslessly for the round-trip codec.
    """

    __tablename__ = "theses"

    thesis_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "positions.position_id",
            name="fk_theses_position_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_category: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    time_expectation_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    position_size_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    narrative_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _check_in("status", ThesisRecordStatus),
            name="ck_theses_status",
        ),
        CheckConstraint(
            "resolution_category IS NULL OR "
            + _check_in("resolution_category", ThesisResolutionCategory),
            name="ck_theses_resolution_category",
        ),
        Index("ix_theses_status", "status"),
        Index("ix_theses_position_id", "position_id"),
        Index("ix_theses_resolution_timestamp", "resolution_timestamp"),
    )
