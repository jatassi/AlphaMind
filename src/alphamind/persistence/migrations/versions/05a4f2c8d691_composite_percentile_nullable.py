"""composite_percentile_nullable

Revision ID: 05a4f2c8d691
Revises: c5d8e9a1f4b2
Create Date: 2026-05-18 18:00:00.000000

Make ``distillation_composite_state.percentile_60d`` nullable per ALP-545.

The funding-stress and market-liquidity composites publish a (raw_value,
percentile) pair. When the trailing distribution is empty or zero-variance
(common during bootstrap when every prior row's composite is the silent
default 0.0) the percentile is undefined. Storing a sentinel ``0.0`` made
the row indistinguishable from a real bottom-of-distribution reading;
stamping a ``100`` was equally wrong on the upper side. The publish layer
now emits ``None`` so the operator-visible block renders ``null`` and
downstream consumers don't conflate "no signal" with "extreme reading".

SQLite recreates the table via ``batch_alter_table`` to alter column
nullability — the column itself is unchanged otherwise.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "05a4f2c8d691"
down_revision: str | Sequence[str] | None = "c5d8e9a1f4b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Relax ``distillation_composite_state.percentile_60d`` to nullable."""
    with op.batch_alter_table("distillation_composite_state") as batch_op:
        batch_op.alter_column(
            "percentile_60d",
            existing_type=sa.Float(),
            nullable=True,
        )


def downgrade() -> None:
    """Restore the non-null constraint.

    Any rows the new publish layer wrote with ``percentile_60d IS NULL``
    are first set to ``0.0`` so the constraint reinstates cleanly. The
    downgrade is lossy with respect to the new semantic — ``0.0`` aliases
    "no signal" to "bottom of distribution" — but matches the original
    NOT NULL storage convention.
    """
    op.execute(
        sa.text(
            "UPDATE distillation_composite_state "
            "SET percentile_60d = 0.0 "
            "WHERE percentile_60d IS NULL"
        )
    )
    with op.batch_alter_table("distillation_composite_state") as batch_op:
        batch_op.alter_column(
            "percentile_60d",
            existing_type=sa.Float(),
            nullable=False,
        )
