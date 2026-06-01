"""add_unattributed_fills

Revision ID: a2f8c1d4e6b9
Revises: b6e1f9a4c3d2
Create Date: 2026-06-01 09:00:00.000000

Adds the ``unattributed_fills`` transient retry queue (ALP-763).

A raw broker fill event can arrive before its local ``orders`` row is committed
(deferred Phase-2 writeback). The continuous monitor's fill-stream consumer
cannot resolve ``fill_records.order_id`` (a NOT NULL FK) for such fills, so it
parks the serialized ``FillReport`` here keyed by ``broker_fill_key`` (the
``fill_records`` dedupe key minus ``order_id``) and a later drain replays it
once the order materializes. Deliberately carries **no ForeignKey** to
``orders`` — the order may not exist yet. ``event_type`` is free text with no
CHECK constraint (an early CHECK from a live enum retroactively changes on
fresh DBs). The index on ``alpaca_order_id`` supports the order-arrival drain
lookup.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2f8c1d4e6b9"
down_revision: str | Sequence[str] | None = "b6e1f9a4c3d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``unattributed_fills`` and its ``alpaca_order_id`` index."""
    op.create_table(
        "unattributed_fills",
        sa.Column("broker_fill_key", sa.Text(), nullable=False),
        sa.Column("alpaca_order_id", sa.Text(), nullable=False),
        sa.Column("client_order_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("fill_timestamp", sa.Text(), nullable=False),
        sa.Column("fill_price", sa.Float(), nullable=False),
        sa.Column("fill_quantity", sa.Float(), nullable=False),
        sa.Column("raw_report_json", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.Text(), nullable=False),
        sa.Column("last_retry_at", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("alerted", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("broker_fill_key"),
    )
    op.create_index(
        "ix_unattributed_fills_alpaca_order_id",
        "unattributed_fills",
        ["alpaca_order_id"],
    )


def downgrade() -> None:
    """Drop ``unattributed_fills`` and its index."""
    op.drop_index(
        "ix_unattributed_fills_alpaca_order_id",
        table_name="unattributed_fills",
    )
    op.drop_table("unattributed_fills")
