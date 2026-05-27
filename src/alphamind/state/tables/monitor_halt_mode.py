"""SQLAlchemy mapping for the ``monitor_halt_mode`` singleton table (ALP-665).

The continuous monitor's loopback ``POST /control/set_halt_mode`` verb persists
its operator-set halt-mode flag into this row so the value survives monitor
restarts — the ``halt_mode_engaged`` portfolio state field per
``docs/design/monitor-control-and-events-schema.md`` § Notes on cross-field
invariants.

The singleton is enforced by a CHECK constraint pinning ``id = 'current'``,
mirroring the ``drawdown_state`` table convention.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

# Singleton sentinel: every read/write targets this row. The CHECK constraint
# below is the schema-level guard against accidental multi-row state.
MONITOR_HALT_MODE_SINGLETON_ID = "current"


class MonitorHaltModeRow(Base):
    """Singleton row tracking the operator-set halt-mode flag.

    ``enabled`` is stored as ``Integer`` (0/1) for SQLite portability — the
    SQLAlchemy ``Boolean`` type maps onto the same INTEGER affinity but the
    explicit shape matches the storage-layer convention in this codebase
    (see ``positions.corporate_action_adjustment_needed``).

    ``reason`` and ``applied_at`` are nullable so the disengaged default
    (``enabled=0`` on a fresh DB) does not invent values; the verb writes
    real values on every state transition.
    """

    __tablename__ = "monitor_halt_mode"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"id = '{MONITOR_HALT_MODE_SINGLETON_ID}'",
            name="ck_monitor_halt_mode_singleton_id",
        ),
        CheckConstraint(
            "enabled IN (0, 1)",
            name="ck_monitor_halt_mode_enabled_bool",
        ),
    )
