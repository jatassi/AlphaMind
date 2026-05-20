"""positions_direction_nullable

Revision ID: d3f6a1c7e9b2
Revises: 5a70d23bd62f
Create Date: 2026-05-20 09:00:00.000000

Make ``positions.direction`` nullable per ALP-610 (parent ALP-591).

Position-level direction is a category error for a multi-leg options
strategy — an iron condor is neither long nor short. The typed
``PositionRecord.direction`` becomes ``Direction | None`` with a validator
tying ``direction is None`` to a strategy payload. The persisted column
follows: it relaxes to nullable, and a one-time backfill clears the inert
``'LONG'`` placeholder legacy strategy rows carry — without it,
``row_to_record``'s validator would fail closed on every legacy strategy row.

The ``ck_positions_direction`` CHECK (``direction IN ('LONG', 'SHORT')``)
already tolerates ``NULL``: SQLite evaluates ``NULL IN (...)`` to ``NULL``,
which a CHECK treats as a pass — so the constraint is preserved unchanged.

SQLite recreates the table via ``batch_alter_table`` to alter column
nullability: it builds a temp table, copies rows, ``DROP TABLE positions``,
then renames. ``orders`` / ``brackets`` / ``theses`` carry ``ON DELETE
RESTRICT`` foreign keys *into* ``positions`` (added by ``e9d2c4f7b3a1``), so on
a populated database that intermediate ``DROP TABLE`` would trip
``FOREIGN KEY constraint failed``. ``PRAGMA foreign_keys`` is silently ignored
while a transaction is pending and Alembic runs every migration inside one
connection-wide transaction, so the recreate cannot toggle it itself; the
migration environment (``env.py``) instead disables FK enforcement for the
whole run via a ``connect`` listener. The recreated table still declares the
incoming FKs, so referential integrity is unchanged once application
connections re-enable enforcement.

Downgrade re-fills strategy rows to ``'LONG'`` before restoring ``NOT NULL``
— lossy with respect to the new semantic (``'LONG'`` aliases the category
error back in) but matches the original storage convention.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3f6a1c7e9b2"
down_revision: str | Sequence[str] | None = "5a70d23bd62f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Relax ``positions.direction`` to nullable and clear strategy-row placeholders."""
    with op.batch_alter_table("positions") as batch_op:
        batch_op.alter_column(
            "direction",
            existing_type=sa.Text(),
            nullable=True,
        )
    op.execute(sa.text("UPDATE positions SET direction = NULL WHERE instrument_type = 'STRATEGY'"))


def downgrade() -> None:
    """Restore the non-null constraint.

    Strategy rows written with ``direction IS NULL`` are first re-filled with
    the inert ``'LONG'`` placeholder so the constraint reinstates cleanly.
    """
    op.execute(
        sa.text(
            "UPDATE positions SET direction = 'LONG' "
            "WHERE instrument_type = 'STRATEGY' AND direction IS NULL"
        )
    )
    with op.batch_alter_table("positions") as batch_op:
        batch_op.alter_column(
            "direction",
            existing_type=sa.Text(),
            nullable=False,
        )
