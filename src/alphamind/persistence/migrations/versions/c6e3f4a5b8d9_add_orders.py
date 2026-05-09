"""add_orders

Revision ID: c6e3f4a5b8d9
Revises: c8e1a4b6d3f2
Create Date: 2026-05-07 06:00:00.000000

Adds the ``orders`` table (story 04c / ALP-360) — the most write-heavy
entity in the OMS. The SQL row schema mirrors ``OrderRecord``'s non-null
shape: pre-submission orders are not representable as ``OrderRecord`` and
live in the activity log / PM-decision provenance instead, so
``alpaca_order_id``, ``bracket_id``, and ``submission_timestamp`` are
NOT NULL here.

CHECK constraints encode the same enum vocabularies the typed record
enforces, so a future direct-SQL writer faces the same fail-closed
guarantees on ``order_role`` / ``order_class`` / ``direction`` /
``order_type`` / ``duration`` / ``status``.

Out of scope at this story: FK constraints to ``positions`` /
``brackets``. Those tables don't exist yet (sibling stories 04a / 04d);
the FK-target columns are nullable plain TEXT here. A follow-up
integration migration will add the FKs once the parallel-wave stories
ship.
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op

from alphamind.portfolio_state.records.orders import (
    OrderClass,
    OrderDirection,
    OrderDuration,
    OrderRole,
    OrderStatus,
    OrderType,
)

# revision identifiers, used by Alembic.
revision: str = "c6e3f4a5b8d9"
down_revision: str | Sequence[str] | None = "c8e1a4b6d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create ``orders`` and its four indexes."""
    op.create_table(
        "orders",
        sa.Column("order_id", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Text(), nullable=True),
        sa.Column("bracket_id", sa.Text(), nullable=False),
        sa.Column("order_role", sa.Text(), nullable=False),
        sa.Column("order_class", sa.Text(), nullable=False),
        sa.Column("instrument_spec_json", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("price_parameters_json", sa.Text(), nullable=False),
        sa.Column("duration", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("alpaca_order_id", sa.Text(), nullable=False),
        sa.Column("alpaca_order_id_chain_json", sa.Text(), nullable=False),
        sa.Column("submission_timestamp", sa.Text(), nullable=False),
        sa.Column("last_update_timestamp", sa.Text(), nullable=False),
        sa.Column("filled_quantity", sa.Float(), nullable=False),
        sa.Column("average_fill_price", sa.Float(), nullable=True),
        sa.Column("remaining_quantity", sa.Float(), nullable=False),
        sa.Column("modification_count", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("order_id"),
        sa.CheckConstraint(_check_in("order_role", OrderRole), name="ck_orders_order_role"),
        sa.CheckConstraint(_check_in("order_class", OrderClass), name="ck_orders_order_class"),
        sa.CheckConstraint(_check_in("direction", OrderDirection), name="ck_orders_direction"),
        sa.CheckConstraint(_check_in("order_type", OrderType), name="ck_orders_order_type"),
        sa.CheckConstraint(_check_in("duration", OrderDuration), name="ck_orders_duration"),
        sa.CheckConstraint(_check_in("status", OrderStatus), name="ck_orders_status"),
    )
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_position_id", "orders", ["position_id"])
    op.create_index("ix_orders_bracket_id", "orders", ["bracket_id"])
    op.create_index("ix_orders_alpaca_order_id", "orders", ["alpaca_order_id"])


def downgrade() -> None:
    """Drop ``orders`` and its indexes."""
    op.drop_index("ix_orders_alpaca_order_id", table_name="orders")
    op.drop_index("ix_orders_bracket_id", table_name="orders")
    op.drop_index("ix_orders_position_id", table_name="orders")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_table("orders")
