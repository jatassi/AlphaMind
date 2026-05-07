"""SQLAlchemy mapping for the ``cash_ledger`` singleton table (story 04e / ALP-362).

A single mutable row representing the portfolio's current cash state — current
cash, settled cash, reserved capital, available buying power, margin held,
unsettled proceeds with per-transaction settlement dates. Historical
reconstruction comes from the activity log, not from this table.

The singleton is enforced by a CHECK constraint pinning ``id = 'current'``.
The persisted column set mirrors the persistent subset of
:class:`alphamind.portfolio_state.records.CashLedger`; computed read-time
fields (``cash_pct_of_portfolio``, ``true_deployable_capital_usd``,
``regt_excess_*``) are not stored — they are recomputed at delivery time.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Float, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base

# Singleton sentinel: every read/write targets this row. The CHECK constraint
# below is the schema-level guard against accidental multi-row state.
CASH_LEDGER_SINGLETON_ID = "current"


class CashLedgerRow(Base):
    """Singleton row mirroring the persisted subset of ``CashLedger``."""

    __tablename__ = "cash_ledger"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    current_cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    settled_cash_usd: Mapped[float] = mapped_column(Float, nullable=False)
    reserved_capital_usd: Mapped[float] = mapped_column(Float, nullable=False)
    available_buying_power_usd: Mapped[float] = mapped_column(Float, nullable=False)
    margin_held_usd: Mapped[float] = mapped_column(Float, nullable=False)
    unsettled_proceeds_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"id = '{CASH_LEDGER_SINGLETON_ID}'",
            name="ck_cash_ledger_singleton_id",
        ),
    )
