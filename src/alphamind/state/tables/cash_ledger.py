"""SQLAlchemy mapping for the ``cash_ledger`` singleton table (story 04e / ALP-362).

A single mutable row representing the portfolio's current cash state — current
cash, settled cash, reserved capital, margin held, unsettled proceeds with
per-transaction settlement dates. Historical reconstruction comes from the
activity log, not from this table.

The singleton is enforced by a CHECK constraint pinning ``id = 'current'``.
The persisted column set mirrors the persistent subset of
:class:`alphamind.portfolio_state.records.CashLedger`; computed read-time
fields (``cash_pct_of_portfolio``, ``true_deployable_capital_usd``,
``regt_excess_*``) are not stored — they are recomputed at delivery time.

``available_buying_power_usd`` is also a derived field — fill-collection / command-execution
write paths do not maintain it and the persisted column carries whatever
the seed left there. The snapshot assembler overwrites it on read using
the canonical formula ``settled_cash - reserved - margin_held`` (the same
formula ``compute_true_deployable_capital_usd`` uses).
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from alphamind.persistence.models import Base
from alphamind.state.tables._money_column import DecimalText

# Singleton sentinel: every read/write targets this row. The CHECK constraint
# below is the schema-level guard against accidental multi-row state.
CASH_LEDGER_SINGLETON_ID = "current"


class CashLedgerRow(Base):
    """Singleton row mirroring the persisted subset of ``CashLedger``.

    ALP-462 — USD columns use :class:`DecimalText` (text-encoded ``Decimal``)
    so the codec round-trips ``Money`` values exactly. SQLite's ``NUMERIC``
    affinity collapses to ``REAL`` on fractional values, re-introducing
    binary-float drift; storing the canonical Decimal repr sidesteps that.
    """

    __tablename__ = "cash_ledger"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    current_cash_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    settled_cash_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    reserved_capital_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    # Static-persisted / seed-only; assembler recomputes at read time. Dashboard only.
    available_buying_power_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    margin_held_usd: Mapped[Decimal] = mapped_column(DecimalText, nullable=False)
    unsettled_proceeds_json: Mapped[str] = mapped_column(Text, nullable=False)
    last_updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            f"id = '{CASH_LEDGER_SINGLETON_ID}'",
            name="ck_cash_ledger_singleton_id",
        ),
    )
