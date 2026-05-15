"""SQLAlchemy mapping for the ``drawdown_state`` singleton table (story 04e / ALP-362).

A single mutable row representing the portfolio's running drawdown state —
equity high-water mark, current drawdown percentage, duration since the
HWM was last established, and the per-source contribution map.

Per :issue:`ALP-119` Pre-resolved decision (D), the running high-water mark
is hard to recompute from history, so it lives in this singleton table
rather than being recomputed at every snapshot read. Historical
reconstruction (P/L time series, drawdown history) replays the activity
log; this table only holds current state.

The singleton is enforced by a CHECK constraint pinning ``id = 'current'``.
The persisted column set mirrors the running-state subset of
:class:`alphamind.portfolio_state.aggregates.DrawdownState`; computed
read-time fields (zones, tier, intraday drawdown) derive from these
running fields plus current snapshot context.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

# Singleton sentinel: every read/write targets this row. The CHECK constraint
# below is the schema-level guard against accidental multi-row state.
DRAWDOWN_STATE_SINGLETON_ID = "current"


class DrawdownStateRow(Base):
    """Singleton row mirroring the running-state subset of ``DrawdownState``."""

    __tablename__ = "drawdown_state"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    equity_high_water_mark_usd: Mapped[float] = mapped_column(Float, nullable=False)
    current_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_duration_hours: Mapped[float] = mapped_column(Float, nullable=False)
    lifetime_max_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    drawdown_by_source_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"id = '{DRAWDOWN_STATE_SINGLETON_ID}'",
            name="ck_drawdown_state_singleton_id",
        ),
    )
