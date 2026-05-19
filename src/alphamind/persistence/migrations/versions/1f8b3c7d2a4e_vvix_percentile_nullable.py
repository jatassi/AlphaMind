"""vvix_percentile_nullable

Revision ID: 1f8b3c7d2a4e
Revises: 05a4f2c8d691
Create Date: 2026-05-19 00:00:00.000000

Make ``distillation_regime_state.vvix_percentile`` nullable per ALP-571.

The VVIX collector does not yet exist; the prior implementation stamped
``50.0`` as a "neutral" placeholder, which downstream agents could not
distinguish from a live median-vol percentile reading. The publish layer
now emits ``None`` when the underlying VVIX series is unavailable or has
insufficient observations to rank against trailing history, surfacing the
missing-data state through the calibration vocabulary (ALP-540) instead of
through a numeric default.

SQLite recreates the table via ``batch_alter_table`` to alter column
nullability — the column itself is unchanged otherwise.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f8b3c7d2a4e"
down_revision: str | Sequence[str] | None = "05a4f2c8d691"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Relax ``distillation_regime_state.vvix_percentile`` to nullable."""
    with op.batch_alter_table("distillation_regime_state") as batch_op:
        batch_op.alter_column(
            "vvix_percentile",
            existing_type=sa.Float(),
            nullable=True,
        )


def downgrade() -> None:
    """Restore the non-null constraint.

    Any rows the new publish layer wrote with ``vvix_percentile IS NULL``
    are first set to ``50.0`` so the constraint reinstates cleanly. The
    downgrade is lossy with respect to the new semantic — ``50.0`` aliases
    "VVIX unavailable" to "mid-percentile" — but matches the original
    NOT NULL storage convention.
    """
    op.execute(
        sa.text(
            "UPDATE distillation_regime_state "
            "SET vvix_percentile = 50.0 "
            "WHERE vvix_percentile IS NULL"
        )
    )
    with op.batch_alter_table("distillation_regime_state") as batch_op:
        batch_op.alter_column(
            "vvix_percentile",
            existing_type=sa.Float(),
            nullable=False,
        )
