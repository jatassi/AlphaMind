"""positions_reprotection_needed — add reprotection_needed flag column (ALP-938)

Revision ID: f001rp0000aa
Revises: e001eg0000aa
Create Date: 2026-06-08 18:30:00.000000

Story ALP-938 (the deferred D + E of ALP-937) auto re-protects a partial-close
remainder. Fill collection sets ``positions.reprotection_needed = 1`` when a
PM-directed partial CLOSE leaves an equity position OPEN with all of its
broker-enforced protective legs already CANCELLED (ALP-937); the
post-fill-collection re-bracket step then re-submits a fresh standalone OCO for
the remaining shares at the original protective levels and clears the flag back
to 0. Persisting the trigger durably means a submit failure self-heals — the
flag survives and the next invocation's step retries.

**Schema changes:**

* ``positions.reprotection_needed`` — ``INTEGER NOT NULL DEFAULT 0``, mirroring
  the Integer-0/1 durable-flag shape of ``corporate_action_adjustment_needed``.
  The ``DEFAULT 0`` lets the incremental ``ADD COLUMN`` populate every existing
  row in one statement (SQLite requires a constant default to add a ``NOT NULL``
  column to a table that already has rows) and matches the live model's
  ``server_default`` so the create_all and incremental schemas agree.

**Idempotent on a fresh DB.** The genesis baseline (``a000000000aa``) is
metadata-driven (``Base.metadata.create_all`` against the live models), so on any
fresh DB the column already exists from ``PositionRow``; ``upgrade`` guards the
``add_column`` with a column-names check and is a no-op there.

**SQLite mechanics.** Adding a ``NOT NULL`` column with a constant ``DEFAULT``
does not require a table rebuild on SQLite (``ADD COLUMN`` accepts it directly).
``downgrade`` uses ``batch_alter_table`` because dropping a column requires a
rebuild on SQLite.

**Parented on** ``e001eg0000aa`` (the prior head) — re-confirmed as the single
head so the chain stays linear.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f001rp0000aa"
down_revision: str | Sequence[str] | None = "e001eg0000aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "positions"
_COLUMN = "reprotection_needed"


def _column_exists(bind: sa.engine.Connection) -> bool:
    return _COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    """Add positions.reprotection_needed (INTEGER NOT NULL DEFAULT 0)."""
    bind = op.get_bind()
    # On a fresh (create_all) DB the column already exists from the live model;
    # only the prod incremental path actually adds it.
    if not _column_exists(bind):
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    """Drop positions.reprotection_needed."""
    bind = op.get_bind()
    if _column_exists(bind):
        # SQLite requires a table rebuild to drop a column; batch_alter_table handles it.
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
