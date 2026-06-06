"""bound_projection_rebuild_scan — watermark table + per-thesis cursor (ALP-865)

Revision ID: a865wm0000bb
Revises: a000000000aa
Create Date: 2026-06-05 00:00:00.000000

Bounds the two O(all-history) projection-rebuild scans (ALP-865) with persisted
watermarks over ``broker_event_log``'s implicit-``rowid`` ``event_seq`` cursor
(no DDL — the rowid already exists). This delta adds the durable state the
watermarks read/advance:

* ``projection_rebuild_watermark`` — the part-1 singleton holding
  ``last_projected_event_seq`` (the terminal-status projection cursor).
* ``thesis_pnl_ledger.last_derived_event_seq`` — the per-thesis part-2 cursor
  (``NULL`` = never derived → always dirty).

Parented on the squashed genesis baseline ``a000000000aa`` (a linear chain — the
sibling ALP-867 migration must re-parent onto this one if it lands second, to
avoid two Alembic heads).

**Idempotent by construction.** The genesis baseline (``a000000000aa``) is
metadata-driven — it runs ``Base.metadata.create_all`` against the *live* models,
so on any fresh DB it already stands up these two objects (they are now part of
the models). This migration therefore guards each DDL with an inspection so a
fresh ``upgrade head`` (baseline → here) is a clean no-op, while a production DB
stamped at the baseline *before* these columns existed still gets them created.
Both paths converge on the same ``head`` schema, which must equal
``Base.metadata`` (the ``test_mapper_fk_autogenerate`` invariant).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a865wm0000bb"
down_revision: str | Sequence[str] | None = "a000000000aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WATERMARK_TABLE = "projection_rebuild_watermark"
_LEDGER_TABLE = "thesis_pnl_ledger"
_LEDGER_CURSOR_COLUMN = "last_derived_event_seq"


def _has_column(inspector: sa.Inspector, table: str, column: str) -> bool:
    return column in {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    """Create the watermark singleton + add the per-thesis derivation cursor."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_WATERMARK_TABLE):
        op.create_table(
            _WATERMARK_TABLE,
            sa.Column("id", sa.Text(), nullable=False),
            sa.Column("last_projected_event_seq", sa.Integer(), nullable=False),
            sa.CheckConstraint(
                "id = 'current'",
                name="ck_projection_rebuild_watermark_singleton_id",
            ),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_column(inspector, _LEDGER_TABLE, _LEDGER_CURSOR_COLUMN):
        op.add_column(
            _LEDGER_TABLE,
            sa.Column(_LEDGER_CURSOR_COLUMN, sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    """Drop the per-thesis cursor + the watermark singleton.

    ``op.drop_column`` uses SQLite's native ``ALTER TABLE … DROP COLUMN`` (no
    ``batch_alter_table``) — deliberately, because a batch table-rebuild of
    ``thesis_pnl_ledger`` reflects-and-recreates its DEFERRABLE FKs and can drop the
    deferrable attribute, drifting from ``Base.metadata``. Native DROP COLUMN needs
    SQLite ≥ 3.35 (2021), which the pinned Python 3.13 runtime's bundled SQLite
    satisfies everywhere (dev + Windows CI); the migration test exercises this exact
    downgrade on that runtime.
    """
    inspector = sa.inspect(op.get_bind())
    if _has_column(inspector, _LEDGER_TABLE, _LEDGER_CURSOR_COLUMN):
        op.drop_column(_LEDGER_TABLE, _LEDGER_CURSOR_COLUMN)
    if inspector.has_table(_WATERMARK_TABLE):
        op.drop_table(_WATERMARK_TABLE)
