"""add monitor_halt_mode singleton table

Revision ID: f1e2a3b4c5d6
Revises: e2f7a1c3b8d9
Create Date: 2026-05-26 00:00:00.000000

Adds the ``monitor_halt_mode`` singleton table — the persistent backing for
the ``halt_mode_engaged`` portfolio state field per
``docs/design/monitor-control-and-events-schema.md`` § Notes on cross-field
invariants. The continuous monitor's loopback
``POST /control/set_halt_mode`` verb (story 01c / ALP-665) reads + writes this
row so operator-set halt mode survives monitor restarts.

Singleton-row discipline mirrors ``drawdown_state``: ``id = 'current'`` enforced
by a CHECK constraint; ``enabled`` constrained to ``0`` / ``1``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1e2a3b4c5d6"
down_revision: str | Sequence[str] | None = "e2f7a1c3b8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``monitor_halt_mode`` singleton table."""
    op.create_table(
        "monitor_halt_mode",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "id = 'current'",
            name="ck_monitor_halt_mode_singleton_id",
        ),
        sa.CheckConstraint(
            "enabled IN (0, 1)",
            name="ck_monitor_halt_mode_enabled_bool",
        ),
    )


def downgrade() -> None:
    """Drop the ``monitor_halt_mode`` table."""
    op.drop_table("monitor_halt_mode")
