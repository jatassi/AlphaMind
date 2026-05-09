"""SQLAlchemy mapping for the ``activity_log`` table (story 03 / ALP-357).

One row per semantic mutation event. Polymorphic over event type — the
``event_type`` column is a discriminator and ``detail_json`` carries the
per-event-type Pydantic detail payload serialized via ``model_dump_json()``.

Foreign key on ``invocation_id`` with ``ON DELETE RESTRICT`` — invocation
rows are append-only, and a parent row referenced by an activity-log entry
cannot be deleted without first removing the entry.

``position_id`` / ``order_id`` / ``thesis_id`` are nullable plain TEXT
columns at this story; the FKs to ``positions`` / ``orders`` / ``theses``
land in a follow-up integration migration once stories 04a-04b ship.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.portfolio_state.events.activity_log import (
    EventGroup,
    EventSource,
    EventType,
)

# Vocabulary lists drive the CHECK constraints. Each enum's
# ``__members__.values()`` is the single source of truth — no duplication of
# the catalog inside this module.
_EVENT_TYPES = tuple(member.value for member in EventType)
_EVENT_GROUPS = tuple(member.value for member in EventGroup)
_EVENT_SOURCES = tuple(member.value for member in EventSource)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class ActivityLogRow(Base):
    """Forward-only per-event semantic-changelog row.

    Mirrors the field list in
    ``docs/design/05-execution-layer/state-persistence.md`` § Activity log
    entries. CHECK constraints encode the same enum vocabularies the typed
    ``ActivityLogEntry`` enforces, so a future direct-SQL writer faces the
    same fail-closed guarantees.
    """

    __tablename__ = "activity_log"

    entry_id: Mapped[str] = mapped_column(Text, primary_key=True)
    invocation_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("invocations.invocation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    entry_at: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    event_group: Mapped[str] = mapped_column(Text, nullable=False)
    position_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    thesis_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            _check_in("event_type", _EVENT_TYPES),
            name="ck_activity_log_event_type",
        ),
        CheckConstraint(
            _check_in("event_group", _EVENT_GROUPS),
            name="ck_activity_log_event_group",
        ),
        CheckConstraint(
            _check_in("source", _EVENT_SOURCES),
            name="ck_activity_log_source",
        ),
        Index("ix_activity_log_invocation_id", "invocation_id"),
        Index("ix_activity_log_event_type_invocation_id", "event_type", "invocation_id"),
        Index("ix_activity_log_position_id_entry_at", "position_id", "entry_at"),
    )
