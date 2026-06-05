"""SQLAlchemy mapping for the ``position_greeks`` side table (ALP-843 / W0a).

Single-writer (monitor-owned) per-position greeks, keyed by ``position_id``
(ADR-0005). The greeks live here — in their own table — precisely so the
monitor never RMW's the pipeline-owned ``positions`` row: the redesign adds
**no** greeks columns to ``positions``. ``position_id`` is both the PK and an
``ON DELETE RESTRICT`` DEFERRABLE INITIALLY DEFERRED FK to ``positions``,
giving one greeks row per position.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base


class PositionGreeksRow(Base):
    """Per-position greeks row. Single-writer = monitor; keyed by position_id."""

    __tablename__ = "position_greeks"

    position_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "positions.position_id",
            name="fk_position_greeks_position_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )
    delta: Mapped[float] = mapped_column(Float, nullable=False)
    gamma: Mapped[float] = mapped_column(Float, nullable=False)
    theta: Mapped[float] = mapped_column(Float, nullable=False)
    vega: Mapped[float] = mapped_column(Float, nullable=False)
    iv: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
