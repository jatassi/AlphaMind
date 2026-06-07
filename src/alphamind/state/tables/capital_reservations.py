"""SQLAlchemy mapping for the ``capital_reservations`` Intent table (ALP-843 / W0a).

A per-thesis capital reservation — a unit of Intent (CONTEXT.md): written by the
Command-execution OPEN path when capital is reserved for a thesis (single writer =
pipeline, ADR-0005), read by the projection / PnL derivations. Never overwritten
by a broker snapshot.

``reservation_id`` is the PK; ``thesis_id`` FKs to ``theses`` with
``ON DELETE RESTRICT`` DEFERRABLE INITIALLY DEFERRED. ``reserved_capital_usd``
uses ``DecimalText`` for exact ``Money`` round-trips. ``released_at`` is null
while the reservation is live.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.state.tables._money_column import DecimalText


class CapitalReservationRow(Base):
    """Per-thesis capital-reservation row. Single-writer = pipeline."""

    __tablename__ = "capital_reservations"

    reservation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    thesis_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "theses.thesis_id",
            name="fk_capital_reservations_thesis_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=False,
    )
    reserved_capital_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    reserved_by_invocation_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "invocations.invocation_id",
            name="fk_capital_reservations_invocation_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    reserved_at: Mapped[str] = mapped_column(Text, nullable=False)
    released_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_capital_reservations_thesis_id", "thesis_id"),)
