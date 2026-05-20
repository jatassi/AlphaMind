"""add_borrow_cost_sweep_cursor

Revision ID: 5a70d23bd62f
Revises: 4f1e9b8c7a52
Create Date: 2026-05-19 23:11:36.089433

Adds the ``borrow_cost_sweep_cursor`` table (ALP-590). iBorrowDesk
444-blocks the collector's egress IP after a fixed number of requests per
run, so one ``collect_borrow_cost`` run only reaches part of the universe.
One row per collector records the ticker the next run should resume at, so
borrow-cost coverage rotates across the full universe over consecutive runs
instead of always restarting at the same head.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a70d23bd62f"
down_revision: str | Sequence[str] | None = "4f1e9b8c7a52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``borrow_cost_sweep_cursor`` table."""
    op.create_table(
        "borrow_cost_sweep_cursor",
        sa.Column("collector", sa.Text(), nullable=False),
        sa.Column("next_ticker", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("collector"),
    )


def downgrade() -> None:
    """Drop the ``borrow_cost_sweep_cursor`` table."""
    op.drop_table("borrow_cost_sweep_cursor")
