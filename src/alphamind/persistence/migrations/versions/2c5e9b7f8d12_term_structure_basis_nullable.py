"""term_structure_basis_nullable

Revision ID: 2c5e9b7f8d12
Revises: 1f8b3c7d2a4e
Create Date: 2026-05-19 12:00:00.000000

Make ``distillation_regime_state.term_structure_basis`` nullable per ALP-572.

The VIX-term-structure basis requires VX1 (front-month VIX futures) data,
which is not collected today (no series in ``macro_observations``, no
vendor adapter in ``data_sources/``). The prior implementation hard-coded
``vx1_minus_vix=0.0`` whenever VX1 was missing; downstream agents could
not distinguish that default from a live "flat term structure" reading.
The publish layer now emits ``None`` and surfaces the missing-data state
through the calibration vocabulary (ALP-540) rather than through a
numeric default.

SQLite recreates the table via ``batch_alter_table`` to alter column
nullability — the column itself is unchanged otherwise.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2c5e9b7f8d12"
down_revision: str | Sequence[str] | None = "1f8b3c7d2a4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Relax ``distillation_regime_state.term_structure_basis`` to nullable."""
    with op.batch_alter_table("distillation_regime_state") as batch_op:
        batch_op.alter_column(
            "term_structure_basis",
            existing_type=sa.Float(),
            nullable=True,
        )


def downgrade() -> None:
    """Restore the non-null constraint.

    Any rows the new publish layer wrote with ``term_structure_basis IS NULL``
    are first set to ``0.0`` so the constraint reinstates cleanly. The
    downgrade is lossy with respect to the new semantic — ``0.0`` aliases
    "VX1 unavailable" to "flat term structure" — but matches the original
    NOT NULL storage convention.
    """
    op.execute(
        sa.text(
            "UPDATE distillation_regime_state "
            "SET term_structure_basis = 0.0 "
            "WHERE term_structure_basis IS NULL"
        )
    )
    with op.batch_alter_table("distillation_regime_state") as batch_op:
        batch_op.alter_column(
            "term_structure_basis",
            existing_type=sa.Float(),
            nullable=False,
        )
