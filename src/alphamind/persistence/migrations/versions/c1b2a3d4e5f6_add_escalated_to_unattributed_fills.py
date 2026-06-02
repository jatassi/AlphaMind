"""add_escalated_to_unattributed_fills

Revision ID: c1b2a3d4e5f6
Revises: a2f8c1d4e6b9
Create Date: 2026-06-02 00:00:00.000000

Adds ``escalated`` to the ``unattributed_fills`` retry queue (ALP-771).

A fill that never resolves (genuine out-of-band order) used to bump
``retry_count`` silently after the one-shot ``alerted`` warning fired.
This column enables a one-shot terminal ERROR escalation: once the fill's
age exceeds the configured TTL, the drain emits an ERROR and marks the row
``escalated=1`` so the alert does not repeat on subsequent drains.

SQLite supports ``ALTER TABLE … ADD COLUMN`` for columns with a server-side
default, so no batch recreate is required here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1b2a3d4e5f6"
down_revision: str | Sequence[str] | None = "a2f8c1d4e6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``escalated`` column (INTEGER NOT NULL DEFAULT 0) to ``unattributed_fills``."""
    op.add_column(
        "unattributed_fills",
        sa.Column("escalated", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    """Remove ``escalated`` column via batch table recreate (SQLite limitation)."""
    with op.batch_alter_table("unattributed_fills") as batch_op:
        batch_op.drop_column("escalated")
