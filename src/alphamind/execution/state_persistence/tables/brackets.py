"""SQLAlchemy mapping for the ``brackets`` table (story 04d / ALP-361).

One row per bracket — 1:1 with the parent position. The bracket bridges
the thesis (which justifies each exit condition) and the orders (which
implement the conditions mechanically).

``modification_history_json`` carries an ordered JSON array of modification
events (``BracketLegModification`` Pydantic instances) serialized via
``model_dump_json()``.

``position_id`` and ``entry_order_id`` are plain TEXT on this mapper; the
FKs to ``positions`` / ``orders`` are enforced at the schema level by
migration ``e9d2c4f7b3a1_tighten_state_persistence_fks``
(DEFERRABLE INITIALLY DEFERRED, ``ON DELETE RESTRICT``). They are not
declared on this mapper — see ALP-369 for the follow-up that adds
mapper-level declarations. The 1:1 invariant with positions is encoded
via a UNIQUE index on ``position_id``.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.records.orders import BracketStatus

_BRACKET_STATUSES = tuple(member.value for member in BracketStatus)


class BracketRow(Base):
    """Forward-only per-bracket row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Brackets.
    The CHECK constraint on ``status`` and the typed ``BracketRecord``
    enforce the same vocabulary, so a future direct-SQL writer faces the
    same fail-closed guarantees the application path enforces.
    """

    __tablename__ = "brackets"

    bracket_id: Mapped[str] = mapped_column(Text, primary_key=True)
    position_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    entry_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    entry_window_deadline: Mapped[str | None] = mapped_column(Text, nullable=True)
    corporate_action_cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    modification_history_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _BRACKET_STATUSES)})",
            name="ck_brackets_status",
        ),
        Index("ix_brackets_position_id", "position_id", unique=True),
        Index("ix_brackets_status", "status"),
    )
