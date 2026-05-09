"""SQLAlchemy mapping for the ``positions`` table (story 04a / ALP-358).

One row per position — the central business object every Tier 1 / Tier 2
entity references. Polymorphic over instrument type via the
``instrument_type`` discriminator + ``details_json`` payload (the per-variant
``PositionDetailsPayload`` model's ``model_dump_json()``).

The status state machine (``PENDING`` → ``OPEN`` → ``CLOSED``) is encoded as
a CHECK constraint. ``thesis_id`` and ``bracket_id`` are nullable plain TEXT
columns on this mapper; the FKs to ``theses`` / ``brackets`` (and
``parent_position_id`` to ``positions``) are enforced at the schema level by
migration ``e9d2c4f7b3a1_tighten_state_persistence_fks``
(DEFERRABLE INITIALLY DEFERRED, ``ON DELETE RESTRICT``). They are not
declared on this mapper — see ALP-369 for the follow-up that adds
mapper-level declarations.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionStatus,
)

# Vocabulary lists drive the CHECK constraints. Each enum's
# ``__members__.values()`` is the single source of truth — no duplication of
# the catalog inside this module.
_STATUSES = tuple(member.value for member in PositionStatus)
_DIRECTIONS = tuple(member.value for member in Direction)
_INSTRUMENT_TYPES = tuple(member.value for member in InstrumentType)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class PositionRow(Base):
    """Forward-mutable per-position row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Positions and
    the typed ``PositionRecord`` Pydantic shape. CHECK constraints encode the
    same enum vocabularies the typed record enforces, so a future direct-SQL
    writer (e.g. a backfill script) faces the same fail-closed guarantees.
    """

    __tablename__ = "positions"

    position_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thesis_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    bracket_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(Text, nullable=False)
    entry_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    instrument_type: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False)
    execution_history_json: Mapped[str] = mapped_column(Text, nullable=False)
    realized_pnl_to_date_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    corporate_action_adjustment_needed: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_position_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(_check_in("status", _STATUSES), name="ck_positions_status"),
        CheckConstraint(_check_in("direction", _DIRECTIONS), name="ck_positions_direction"),
        CheckConstraint(
            _check_in("instrument_type", _INSTRUMENT_TYPES),
            name="ck_positions_instrument_type",
        ),
        Index("ix_positions_status", "status"),
        Index("ix_positions_thesis_id", "thesis_id"),
    )
