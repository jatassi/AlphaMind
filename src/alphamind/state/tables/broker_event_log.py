"""SQLAlchemy mapping for the append-only ``broker_event_log`` (ALP-843 / W0a).

Every broker→local event that changes a Broker-Owned Fact — fills +
account-activities + corporate-actions + terminal order status — lands here as
one immutable row (ADR-0005: append-only, the only sanctioned cross-process
write). ``event_key`` carries a UNIQUE constraint so the websocket delivery and
a later REST recovery replay of the *same* event collapse to one row (the
idempotency guarantee ADR-0002's gap-free log relies on).

The broker-carried link columns (``thesis_id`` / ``invocation_id`` /
``position_id``) are the decoded FK that rides ``client_order_id``; they FK to
``theses`` / ``invocations`` / ``positions`` with ``ON DELETE RESTRICT`` and
DEFERRABLE INITIALLY DEFERRED, matching the cyclic-writeback convention the
other state tables use. ``position_id`` is nullable — a position-lifecycle
event carries no order link.

The ``event_type`` CHECK constraint carries the **full vocabulary up front**
(fills, ``OPEXP``/``OPEXC``/``OPASN``/``OPTRD``, corporate-action types,
terminal order status). Authoring it complete avoids the migration CHECK-vocab
trap — a constraint built from a partial enum would retroactively differ on a
fresh DB when a member is added later.

No update path is exposed by the codec — append-only by construction.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.state.records_broker_event_log import BrokerEventType

_EVENT_TYPES = tuple(member.value for member in BrokerEventType)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


class BrokerEventLogRow(Base):
    """Append-only per-event row. Captured once; never updated."""

    __tablename__ = "broker_event_log"

    event_key: Mapped[str] = mapped_column(Text, primary_key=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    thesis_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "theses.thesis_id",
            name="fk_broker_event_log_thesis_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    invocation_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "invocations.invocation_id",
            name="fk_broker_event_log_invocation_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    position_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "positions.position_id",
            name="fk_broker_event_log_position_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    raw_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    broker_timestamp: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("event_key", name="uq_broker_event_log_event_key"),
        CheckConstraint(
            _check_in("event_type", _EVENT_TYPES),
            name="ck_broker_event_log_event_type",
        ),
        Index("ix_broker_event_log_event_type", "event_type"),
        Index("ix_broker_event_log_thesis_id", "thesis_id"),
        Index("ix_broker_event_log_position_id", "position_id"),
    )
