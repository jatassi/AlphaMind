"""SQLAlchemy mapping for the ``activity_log`` table (story 03 / ALP-357).

One row per semantic mutation event. Polymorphic over event type — the
``event_type`` column is a discriminator and ``detail_json`` carries the
per-event-type Pydantic detail payload serialized via ``model_dump_json()``.

Foreign key on ``invocation_id`` with ``ON DELETE RESTRICT`` — invocation
rows are append-only, and a parent row referenced by an activity-log entry
cannot be deleted without first removing the entry.

``position_id`` / ``order_id`` / ``thesis_id`` are nullable TEXT columns
carrying DEFERRABLE INITIALLY DEFERRED FKs to ``positions`` / ``orders`` /
``theses`` (``ON DELETE RESTRICT``), matching the
``e9d2c4f7b3a1_tighten_state_persistence_fks`` migration. The deferral
accommodates command execution's writebacks, where activity-log emission can precede
parent-row commit when a single transaction seeds the full position +
thesis + bracket + orders cluster.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, literal_column
from sqlalchemy.orm import Mapped, column_property, mapped_column

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
    position_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "positions.position_id",
            name="fk_activity_log_position_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    order_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "orders.order_id",
            name="fk_activity_log_order_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    thesis_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "theses.thesis_id",
            name="fk_activity_log_thesis_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Read-only monotonic insertion cursor — SQLite's implicit ``rowid``,
    # surfaced as a mapped column so a poller (the emergency receiver, ALP-870)
    # can high-water-mark on the table's producer-independent insertion order
    # rather than the producer-formatted ``entry_id`` PK. Mirrors
    # ``BrokerEventLogRow.event_seq``: ``column_property`` over a
    # ``literal_column`` (not ``mapped_column``) so it emits **no DDL** — the
    # rowid already exists on this non-``WITHOUT ROWID`` table whose PK is the
    # TEXT ``entry_id``. The same two traps apply: a query referencing **only**
    # ``event_seq`` has no ``FROM`` anchor (co-select/co-filter a real column or
    # take the max in Python), and ``VACUUM`` would renumber rowids (this DB is
    # WAL-mode, append-only, never VACUUMed).
    event_seq: Mapped[int] = column_property(literal_column(f"{__tablename__}.rowid", Integer))

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
