"""SQLAlchemy mapping for the ``projection_rebuild_watermark`` singleton (ALP-865).

A single mutable row holding ``last_projected_event_seq`` — the max
``broker_event_log.event_seq`` (rowid) the terminal-status projection in
``write_paths/projection_rebuild.py`` has already folded onto the ``orders``
cache. Each Phase-1 rebuild scans only ``TERMINAL_ORDER_STATUS`` events with
``event_seq`` greater than this watermark, then advances it to the max scanned —
all inside the open Phase-1 write transaction, so the advance commits atomically
with the projection (a crash rolls both back and the next run re-scans).

The singleton is enforced by a CHECK constraint pinning ``id = 'current'``,
matching the ``cash_ledger`` / ``drawdown_state`` singleton convention. The first
run finds no row, treats the watermark as ``0`` (scan from the beginning once),
and writes the row when it advances.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

# Singleton sentinel: every read/write targets this row. The CHECK constraint
# below is the schema-level guard against accidental multi-row state.
PROJECTION_REBUILD_WATERMARK_SINGLETON_ID = "current"


class ProjectionRebuildWatermarkRow(Base):
    """Singleton row holding the part-1 terminal-status projection watermark."""

    __tablename__ = "projection_rebuild_watermark"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_projected_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"id = '{PROJECTION_REBUILD_WATERMARK_SINGLETON_ID}'",
            name="ck_projection_rebuild_watermark_singleton_id",
        ),
    )
