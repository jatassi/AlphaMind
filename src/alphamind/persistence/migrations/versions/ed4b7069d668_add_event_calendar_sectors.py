"""add_event_calendar_sectors

Adds the comma-separated TEXT ``sectors`` column to ``event_calendar`` so
per-sector event filtering can read the full event row without an external
join. NULL is the cross-sector default (e.g. FOMC).

Revision ID: ed4b7069d668
Revises: 3fbbfd3049f5
Create Date: 2026-05-02

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ed4b7069d668"
down_revision: str | Sequence[str] | None = "3fbbfd3049f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a nullable ``sectors`` TEXT column to ``event_calendar``."""
    op.add_column(
        "event_calendar",
        sa.Column("sectors", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove the ``sectors`` column from ``event_calendar``."""
    op.drop_column("event_calendar", "sectors")
