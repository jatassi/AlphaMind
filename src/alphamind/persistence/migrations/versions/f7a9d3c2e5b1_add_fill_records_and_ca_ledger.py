"""add_fill_records_and_ca_ledger

Revision ID: f7a9d3c2e5b1
Revises: c6e3f4a5b8d9
Create Date: 2026-05-07 07:00:00.000000

Adds the two append-only Tier-2 lifecycle tables (story 05 / ALP-363):

* ``fill_records`` — one row per fill event with the
  ``unprocessed`` / ``processed`` / ``quarantined`` status flag and Reg T
  attribution metadata. Continuous-monitor write path inserts new fills
  ``unprocessed``; Phase 1 marks them ``processed`` inside the integration
  transaction. Composite UNIQUE on
  ``(order_id, fill_timestamp, fill_quantity, fill_price)`` enforces the
  design-doc deduplication contract.
* ``corporate_action_integration_ledger`` — one row per Alpaca CA activity
  tracked by ``alpaca_activity_id``. Marks an activity as integrated;
  parallels the fill records' processing-status mechanism on Phase 1 retry.

Both tables FK to ``invocations.invocation_id``. ``fill_records`` additionally
FKs to ``orders.order_id`` (story 04c). Both FKs use ``ON DELETE RESTRICT`` —
the parent rows are append-only.
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op

from alphamind.execution.state_persistence.write_paths.records import (
    CorporateActionLedgerStatus,
    FillProcessingStatus,
)
from alphamind.portfolio_state.records.orders import OrderStatus

# revision identifiers, used by Alembic.
revision: str = "f7a9d3c2e5b1"
down_revision: str | Sequence[str] | None = "c6e3f4a5b8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create ``fill_records`` and ``corporate_action_integration_ledger``."""
    op.create_table(
        "fill_records",
        sa.Column("fill_id", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("fill_timestamp", sa.Text(), nullable=False),
        sa.Column("fill_price", sa.Float(), nullable=False),
        sa.Column("fill_quantity", sa.Float(), nullable=False),
        sa.Column("remaining_quantity_after", sa.Float(), nullable=False),
        sa.Column("order_status_after", sa.Text(), nullable=False),
        sa.Column("slippage_usd", sa.Float(), nullable=True),
        sa.Column("fees_usd", sa.Float(), nullable=False),
        sa.Column("execution_venue", sa.Text(), nullable=True),
        sa.Column("gateway_reference", sa.Text(), nullable=True),
        sa.Column("persistence_timestamp", sa.Text(), nullable=False),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.Column("processing_invocation_id", sa.Text(), nullable=True),
        sa.Column("processing_timestamp", sa.Text(), nullable=True),
        sa.Column("regt_attribution_json", sa.Text(), nullable=True),
        sa.Column("live_execution_estimate_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("fill_id"),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.order_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["processing_invocation_id"],
            ["invocations.invocation_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "order_id",
            "fill_timestamp",
            "fill_quantity",
            "fill_price",
            name="uq_fill_records_dedupe",
        ),
        sa.CheckConstraint(
            _check_in("order_status_after", OrderStatus),
            name="ck_fill_records_order_status_after",
        ),
        sa.CheckConstraint(
            _check_in("processing_status", FillProcessingStatus),
            name="ck_fill_records_processing_status",
        ),
    )
    op.create_index(
        "ix_fill_records_processing_status",
        "fill_records",
        ["processing_status"],
    )
    op.create_index("ix_fill_records_order_id", "fill_records", ["order_id"])

    op.create_table(
        "corporate_action_integration_ledger",
        sa.Column("alpaca_activity_id", sa.Text(), nullable=False),
        sa.Column("processing_invocation_id", sa.Text(), nullable=False),
        sa.Column("processing_timestamp", sa.Text(), nullable=False),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("alpaca_activity_id"),
        sa.ForeignKeyConstraint(
            ["processing_invocation_id"],
            ["invocations.invocation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _check_in("processing_status", CorporateActionLedgerStatus),
            name="ck_ca_ledger_processing_status",
        ),
    )
    op.create_index(
        "ix_ca_ledger_processing_invocation_id",
        "corporate_action_integration_ledger",
        ["processing_invocation_id"],
    )
    op.create_index(
        "ix_ca_ledger_processing_status",
        "corporate_action_integration_ledger",
        ["processing_status"],
    )


def downgrade() -> None:
    """Drop both tables and their indexes."""
    op.drop_index(
        "ix_ca_ledger_processing_status",
        table_name="corporate_action_integration_ledger",
    )
    op.drop_index(
        "ix_ca_ledger_processing_invocation_id",
        table_name="corporate_action_integration_ledger",
    )
    op.drop_table("corporate_action_integration_ledger")
    op.drop_index("ix_fill_records_order_id", table_name="fill_records")
    op.drop_index("ix_fill_records_processing_status", table_name="fill_records")
    op.drop_table("fill_records")
