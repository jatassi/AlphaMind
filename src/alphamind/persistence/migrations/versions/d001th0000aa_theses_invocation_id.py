"""theses_invocation_id — add nullable invocation_id column + back-populate (ALP-919)

Revision ID: d001th0000aa
Revises: c918aa0000ff
Create Date: 2026-06-07 12:00:00.000000

Story 02i gives every thesis a durable first-class link back to the pipeline
invocation that generated it, enabling the outcomes-conditioning surface to
populate ``ConditioningAttributes.regime`` and ``.time_of_day``.

**Schema changes:**

* ``theses.invocation_id`` — nullable TEXT column with a FK reference to
  ``invocations.invocation_id`` (``ON DELETE RESTRICT``, non-deferrable: the
  invocation row is durably inserted before the phase session opens, so no
  positions↔theses-style cycle applies) and an index
  ``ix_theses_invocation_id``.

**Back-population:**

The ``activity_log`` ``THESIS_CREATED`` event (emitted at OPEN write-back
time; present on the integration branch from story 03 / ALP-315) carries
``(invocation_id, thesis_id)`` for every thesis created after that emission
landed. The upgrade back-populates ``theses.invocation_id`` from the audit
trail for every thesis that has a matching ``THESIS_CREATED`` entry:

    UPDATE theses
       SET invocation_id = (
               SELECT al.invocation_id
                 FROM activity_log al
                WHERE al.thesis_id = theses.thesis_id
                  AND al.event_type = 'THESIS_CREATED'
                LIMIT 1
           )

Theses created before the ``THESIS_CREATED`` emission landed have no such
entry and remain ``NULL`` — that is correct and expected.

**Idempotent on a fresh DB.** The genesis baseline (``a000000000aa``) is
metadata-driven (``Base.metadata.create_all`` against the live models), so on
any fresh DB the column + index already exist. ``upgrade`` guards against the
``add_column`` with a column-names check and guards ``create_index`` via
``inspect``.

**SQLite mechanics.** Adding a nullable column with no server default does not
require a table rebuild on SQLite ≥ 3.20 (``ADD COLUMN`` without ``NOT NULL``).
Alembic's ``batch_alter_table`` is still used for the downgrade path (column
drop requires a rebuild on SQLite) and for idempotency consistency. The FK is
expressed as a ``ForeignKeyConstraint`` so the constraint name
``fk_theses_invocation_id`` is preserved across the rebuild.

**Parented on** ``c918aa0000ff`` (ALP-918's source-widen) — re-confirmed as
the single head so the chain stays linear.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d001th0000aa"
down_revision: str | Sequence[str] | None = "c918aa0000ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "theses"
_COLUMN = "invocation_id"
_INDEX = "ix_theses_invocation_id"
_FK_NAME = "fk_theses_invocation_id"


def _column_exists(bind: sa.engine.Connection) -> bool:
    return _COLUMN in {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _index_exists(bind: sa.engine.Connection) -> bool:
    return _INDEX in {idx["name"] for idx in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    """Add theses.invocation_id (nullable), FK, index, back-populate from activity_log."""
    bind = op.get_bind()

    # --- 1. Add column + FK (idempotent: skip if already present) ---
    # SQLite ``ALTER TABLE ADD COLUMN`` cannot attach a FOREIGN KEY, so on the
    # incremental path the column must be added via a full table rebuild
    # (``recreate="always"``) with the FK carried on the column. On a fresh DB the
    # genesis baseline already built the column + FK from the model, so this branch
    # is skipped entirely. Migrations run with ``PRAGMA foreign_keys=OFF`` (see the
    # alembic env), so the rebuild's intermediate ``DROP TABLE`` does not trip the
    # incoming ``ON DELETE RESTRICT`` foreign keys.
    if not _column_exists(bind):
        with op.batch_alter_table(_TABLE, recreate="always") as batch_op:
            batch_op.add_column(
                sa.Column(
                    _COLUMN,
                    sa.Text(),
                    sa.ForeignKey(
                        "invocations.invocation_id",
                        name=_FK_NAME,
                        ondelete="RESTRICT",
                    ),
                    nullable=True,
                ),
            )

    # --- 2. Back-populate from activity_log THESIS_CREATED entries ---
    # Run unconditionally: if the column already existed (fresh DB), it will
    # already be NULL for all rows (nothing to back-populate on a fresh DB),
    # so this is a safe no-op there.
    op.execute(
        sa.text(
            "UPDATE theses"
            " SET invocation_id = ("
            "  SELECT al.invocation_id"
            "  FROM activity_log al"
            "  WHERE al.thesis_id = theses.thesis_id"
            "    AND al.event_type = 'THESIS_CREATED'"
            "  LIMIT 1"
            " )"
            " WHERE invocation_id IS NULL"
        )
    )

    # --- 3. Add index (idempotent: skip if already present) ---
    if not _index_exists(bind):
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    """Drop ix_theses_invocation_id and the invocation_id column."""
    bind = op.get_bind()

    # Drop index first (SQLite batch rebuild will drop it anyway, but be explicit).
    if _index_exists(bind):
        op.drop_index(_INDEX, table_name=_TABLE)

    # SQLite requires a table rebuild to drop a column; batch_alter_table handles it.
    if _column_exists(bind):
        with op.batch_alter_table(_TABLE) as batch_op:
            batch_op.drop_column(_COLUMN)
