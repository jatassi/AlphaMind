"""SQLAlchemy mapping for the ``thesis_pnl_ledger`` Intent table (ALP-843 / W0a).

Per-thesis realized PnL + cost basis with provenance — a unit of Intent
(CONTEXT.md): authored by the pipeline (single writer, ADR-0005), never
overwritten by a broker snapshot. Story 03c derives the figures from the
broker-event log; this story defines the durable shape only.

Keyed by ``thesis_id`` (one ledger entry per thesis), which FKs to ``theses``
with ``ON DELETE RESTRICT`` DEFERRABLE INITIALLY DEFERRED — matching the cyclic
writeback convention. ``realized_pnl_usd`` / ``cost_basis_usd`` use
``DecimalText`` for exact ``Money`` round-trips (realized PnL may be negative).

``last_derived_event_seq`` (ALP-865) is the per-thesis re-derivation watermark —
the max ``broker_event_log.event_seq`` (rowid) folded into this row's figures.
``NULL`` means the thesis was never derived (so it is always dirty); the dirty
set the post-poll rederive recomputes is the theses carrying an event newer than
their watermark, so a long-closed thesis with no new events is skipped and its
ledger left exactly as the prior derivation wrote it.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.state.tables._money_column import DecimalText


class ThesisPnlLedgerRow(Base):
    """Per-thesis realized-PnL / cost-basis row. Single-writer = pipeline."""

    __tablename__ = "thesis_pnl_ledger"

    thesis_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey(
            "theses.thesis_id",
            name="fk_thesis_pnl_ledger_thesis_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        primary_key=True,
    )
    realized_pnl_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    cost_basis_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    provenance_json: Mapped[str] = mapped_column(Text, nullable=False)
    derived_from_invocation_id: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey(
            "invocations.invocation_id",
            name="fk_thesis_pnl_ledger_invocation_id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        nullable=True,
    )
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)
    # Per-thesis re-derivation watermark (ALP-865): max event_seq folded into the
    # figures above. NULL = never derived → always dirty.
    last_derived_event_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
