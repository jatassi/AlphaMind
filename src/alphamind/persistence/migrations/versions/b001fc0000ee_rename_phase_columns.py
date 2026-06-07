"""rename_phase_columns — execution phase1/phase2 → fill_collection/command_execution (ALP-901)

Revision ID: b001fc0000ee
Revises: a556rp0000dd
Create Date: 2026-06-07 00:00:00.000000

Renames the two completion-timestamp columns on ``invocations`` that marked the
ordinal "Phase 1" and "Phase 2" of the two-transaction invocation lifecycle:

    phase1_completed_at   → fill_collection_completed_at
    phase2_completed_at   → command_execution_completed_at

The ``fill_collection_summary_json`` / ``command_execution_summary_json`` columns
already use the behavior-named vocabulary (introduced with the invocations table) —
this migration finishes the half-applied convention so the table is internally
consistent.

No CHECK constraint references these columns (only trigger_type / trigger_source /
active_mode / staleness_flag carry CHECKs), so no constraint rebuild is required.
Data is preserved in-place; no backfill is needed.

**SQLite mechanics.** SQLite ≥ 3.25 supports ``ALTER TABLE … RENAME COLUMN …`` natively.
Alembic's ``batch_alter_table`` context wraps the ALTER in a way that works on both
SQLite and the hypothetical future migration to a server-side DB — batch mode is the
canonical path for SQLite column renames per the Alembic docs.

**Idempotency.** On a fresh DB (genesis baseline ``a000000000aa`` with
``Base.metadata.create_all``), the ORM model already names these columns with the new
names, so on any fresh DB the column names are already correct — ``upgrade`` guards
against the idempotency concern by attempting the rename only when the old column name
still exists. This ensures ``alembic upgrade head`` on a metadata-built DB is a no-op.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "b001fc0000ee"
down_revision: str | None = "a556rp0000dd"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "invocations"


def _column_names() -> set[str]:
    """Return the current column names on the invocations table."""
    bind = op.get_bind()
    return {c["name"] for c in inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    """Rename phase1_completed_at → fill_collection_completed_at and
    phase2_completed_at → command_execution_completed_at."""
    cols = _column_names()
    with op.batch_alter_table(_TABLE) as batch_op:
        if "phase1_completed_at" in cols:
            batch_op.alter_column(
                "phase1_completed_at",
                new_column_name="fill_collection_completed_at",
            )
        if "phase2_completed_at" in cols:
            batch_op.alter_column(
                "phase2_completed_at",
                new_column_name="command_execution_completed_at",
            )


def downgrade() -> None:
    """Restore fill_collection_completed_at → phase1_completed_at and
    command_execution_completed_at → phase2_completed_at."""
    cols = _column_names()
    with op.batch_alter_table(_TABLE) as batch_op:
        if "fill_collection_completed_at" in cols:
            batch_op.alter_column(
                "fill_collection_completed_at",
                new_column_name="phase1_completed_at",
            )
        if "command_execution_completed_at" in cols:
            batch_op.alter_column(
                "command_execution_completed_at",
                new_column_name="phase2_completed_at",
            )
