"""migrate monetary columns from Float to text-encoded Decimal

Revision ID: d9e2f5a7c3b4
Revises: b2c4f7a3d9e8
Create Date: 2026-05-12 12:00:00.000000

Story 05b / ALP-462 migrates boundary money fields to ``Money``/``Price``
NewTypes over ``Decimal``. The codec layer is the durability boundary; the
SQLAlchemy column types must store and return ``Decimal`` exactly so the
codec can round-trip without binary-float drift entering the cash-ledger
accumulator or the fill records.

SQLite's ``NUMERIC`` affinity falls back to ``REAL`` (binary float) on
fractional values, silently re-introducing the drift the migration exists to
prevent (``Decimal('1234567.89')`` becomes ``Decimal('1234567.889999999898')``
on round-trip through ``Numeric``). The columns therefore migrate to ``Text``
so the application's :class:`DecimalText` type decorator can write the
canonical string repr (``"1234567.89"``) and parse it back via ``Decimal``.

Tables affected:

* ``cash_ledger`` — every persisted USD column (``current_cash_usd``,
  ``settled_cash_usd``, ``reserved_capital_usd``,
  ``available_buying_power_usd``, ``margin_held_usd``).
* ``fill_records`` — the three price/USD columns (``fill_price``,
  ``slippage_usd``, ``fees_usd``).

The upgrade uses ``op.batch_alter_table`` (which rebuilds the table on
SQLite). Existing Float-typed values cast to text via SQLite's implicit
conversion; downstream reads parse them via ``Decimal(text)`` so the legacy
float precision is preserved as-is (no new drift introduced).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e2f5a7c3b4"
down_revision: str | Sequence[str] | None = "b2c4f7a3d9e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CASH_LEDGER_MONEY_COLUMNS = (
    "current_cash_usd",
    "settled_cash_usd",
    "reserved_capital_usd",
    "available_buying_power_usd",
    "margin_held_usd",
)

_FILL_RECORDS_NON_NULL_MONEY_COLUMNS = (
    "fill_price",
    "fees_usd",
)
_FILL_RECORDS_NULLABLE_MONEY_COLUMNS = ("slippage_usd",)


def upgrade() -> None:
    """Rebuild cash_ledger and fill_records with text-encoded Decimal columns."""
    with op.batch_alter_table("cash_ledger") as batch_op:
        for name in _CASH_LEDGER_MONEY_COLUMNS:
            batch_op.alter_column(
                name,
                existing_type=sa.Float(),
                type_=sa.Text(),
                existing_nullable=False,
            )
    with op.batch_alter_table("fill_records") as batch_op:
        for name in _FILL_RECORDS_NON_NULL_MONEY_COLUMNS:
            batch_op.alter_column(
                name,
                existing_type=sa.Float(),
                type_=sa.Text(),
                existing_nullable=False,
            )
        for name in _FILL_RECORDS_NULLABLE_MONEY_COLUMNS:
            batch_op.alter_column(
                name,
                existing_type=sa.Float(),
                type_=sa.Text(),
                existing_nullable=True,
            )


def downgrade() -> None:
    """Revert monetary columns to Float."""
    with op.batch_alter_table("fill_records") as batch_op:
        for name in _FILL_RECORDS_NULLABLE_MONEY_COLUMNS:
            batch_op.alter_column(
                name,
                existing_type=sa.Text(),
                type_=sa.Float(),
                existing_nullable=True,
            )
        for name in _FILL_RECORDS_NON_NULL_MONEY_COLUMNS:
            batch_op.alter_column(
                name,
                existing_type=sa.Text(),
                type_=sa.Float(),
                existing_nullable=False,
            )
    with op.batch_alter_table("cash_ledger") as batch_op:
        for name in _CASH_LEDGER_MONEY_COLUMNS:
            batch_op.alter_column(
                name,
                existing_type=sa.Text(),
                type_=sa.Float(),
                existing_nullable=False,
            )
