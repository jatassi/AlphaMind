"""SQLAlchemy mapping for the ``unattributed_fills`` table (ALP-763).

A transient holding table for raw broker fill events that arrive before the
local ``orders`` row is committed (deferred Phase-2 writeback). The continuous
monitor's fill-stream consumer cannot resolve ``fill_records.order_id`` (a NOT
NULL FK) for such fills, so it parks the raw ``FillReport`` here and retries on
a later drain instead of silently dropping the fill.

This is a self-draining retry QUEUE, **not** a parallel fill ledger: once the
order materializes, the drain replays ``raw_report_json`` through the normal
``fill_records`` path and deletes the row. Deliberately carries **no
ForeignKey** to ``orders`` — the whole point is that the order may not exist
yet.

Identity is the ``broker_fill_key``: a deterministic id derived from the
broker-fill identity tuple ``(alpaca_order_id, fill_timestamp, fill_quantity,
fill_price)`` — the ``fill_records`` dedupe key minus ``order_id``. The
consumer computes it; this layer only stores it as the primary key so retries
and reconnect-driven backfill converge to a single row.
"""

from __future__ import annotations

from sqlalchemy import Float, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base


class UnattributedFillRow(Base):
    """One row per broker fill event awaiting attribution to a local order.

    ``alerted`` is stored as ``Integer`` (0/1) for SQLite portability, matching
    the storage-layer bool convention (see ``monitor_halt_mode.enabled`` /
    ``positions.corporate_action_adjustment_needed``).

    ``event_type`` is free text (``filled`` / ``partially_filled`` /
    ``stopped``) with no CHECK constraint: an early CHECK built from a live enum
    retroactively changes on fresh DBs whenever the enum gains a member.
    """

    __tablename__ = "unattributed_fills"

    broker_fill_key: Mapped[str] = mapped_column(Text, primary_key=True)
    alpaca_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_order_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    fill_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    fill_price: Mapped[float] = mapped_column(Float, nullable=False)
    fill_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    raw_report_json: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_retry_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    alerted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_unattributed_fills_alpaca_order_id", "alpaca_order_id"),)
