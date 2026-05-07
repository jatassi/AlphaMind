"""add_cash_ledger_and_drawdown_state

Revision ID: c8e1a4b6d3f2
Revises: d7e3f4a5b6c8
Create Date: 2026-05-07 06:00:00.000000

Adds the two singleton-row state tables (story 04e / ALP-362) — one
``cash_ledger`` row and one ``drawdown_state`` row, each pinned by a
CHECK constraint to ``id = 'current'``. Historical reconstruction of cash
and drawdown over time replays the activity log; these tables only hold
current state.

Mirrors the column shape defined in
``src/alphamind/execution/state_persistence/tables/cash_ledger.py`` and
``drawdown_state.py``. The persisted column set tracks the running-state
subset of the typed ``CashLedger`` and ``DrawdownState`` records;
read-time computed fields (``cash_pct_of_portfolio``, drawdown zones,
etc.) live on the typed records but are not persisted.

The ``down_revision`` chains to story 03's ``activity_log`` migration.
The orchestrator may rechain parallel migrations (04a-04e) at integration
time; the per-story migration boundary stays unchanged.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8e1a4b6d3f2"
down_revision: str | Sequence[str] | None = "d7e3f4a5b6c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``cash_ledger`` and ``drawdown_state`` (both singletons)."""
    op.create_table(
        "cash_ledger",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("current_cash_usd", sa.Float(), nullable=False),
        sa.Column("settled_cash_usd", sa.Float(), nullable=False),
        sa.Column("reserved_capital_usd", sa.Float(), nullable=False),
        sa.Column("available_buying_power_usd", sa.Float(), nullable=False),
        sa.Column("margin_held_usd", sa.Float(), nullable=False),
        sa.Column("unsettled_proceeds_json", sa.Text(), nullable=False),
        sa.Column("last_updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "id = 'current'",
            name="ck_cash_ledger_singleton_id",
        ),
    )

    op.create_table(
        "drawdown_state",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("equity_high_water_mark_usd", sa.Float(), nullable=False),
        sa.Column("current_drawdown_pct", sa.Float(), nullable=False),
        sa.Column("drawdown_duration_hours", sa.Float(), nullable=False),
        sa.Column("lifetime_max_drawdown_pct", sa.Float(), nullable=False),
        sa.Column("drawdown_by_source_json", sa.Text(), nullable=False),
        sa.Column("last_updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "id = 'current'",
            name="ck_drawdown_state_singleton_id",
        ),
    )


def downgrade() -> None:
    """Drop both singleton tables."""
    op.drop_table("drawdown_state")
    op.drop_table("cash_ledger")
