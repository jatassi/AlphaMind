"""add_activity_log

Revision ID: b5d2e3f4c6a7
Revises: a4c1d2e3f4b5
Create Date: 2026-05-07 05:00:00.000000

Adds the ``activity_log`` table (story 03 / ALP-357) — one row per
semantic state-mutation event. Polymorphic over event type via the
``event_type`` discriminator + ``detail_json`` payload (the per-EventType
Pydantic detail's ``model_dump_json()``).

Mirrors the column shape defined in
``src/alphamind/execution/state_persistence/tables/activity_log.py`` and
the design doc at ``docs/design/05-execution-layer/state-persistence.md``
§ Activity log entries. CHECK constraints encode the same enum
vocabularies the SQLAlchemy ``__table_args__`` install, so a future
direct-SQL writer faces the same fail-closed guarantees the typed
``ActivityLogEntry`` enforces on the application path.

The FK on ``invocation_id`` targets ``invocations.invocation_id`` with
``ON DELETE RESTRICT`` — invocation rows are append-only.

Out of scope at this story: FK constraints to ``positions`` / ``orders`` /
``theses``. Those tables don't exist yet (stories 04a-04b); the columns
are nullable plain TEXT here. A follow-up integration migration will add
the FKs once those tables ship.
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op

from alphamind.portfolio_state.events.activity_log import (
    EventGroup,
    EventSource,
    EventType,
)

# revision identifiers, used by Alembic.
revision: str = "b5d2e3f4c6a7"
down_revision: str | Sequence[str] | None = "a4c1d2e3f4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create ``activity_log`` and its three indexes."""
    op.create_table(
        "activity_log",
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("entry_at", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_group", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Text(), nullable=True),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column("thesis_id", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("entry_id"),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.invocation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _check_in("event_type", EventType),
            name="ck_activity_log_event_type",
        ),
        sa.CheckConstraint(
            _check_in("event_group", EventGroup),
            name="ck_activity_log_event_group",
        ),
        sa.CheckConstraint(
            _check_in("source", EventSource),
            name="ck_activity_log_source",
        ),
    )
    op.create_index(
        "ix_activity_log_invocation_id",
        "activity_log",
        ["invocation_id"],
    )
    op.create_index(
        "ix_activity_log_event_type_invocation_id",
        "activity_log",
        ["event_type", "invocation_id"],
    )
    op.create_index(
        "ix_activity_log_position_id_entry_at",
        "activity_log",
        ["position_id", "entry_at"],
    )


def downgrade() -> None:
    """Drop ``activity_log`` and its indexes."""
    op.drop_index("ix_activity_log_position_id_entry_at", table_name="activity_log")
    op.drop_index("ix_activity_log_event_type_invocation_id", table_name="activity_log")
    op.drop_index("ix_activity_log_invocation_id", table_name="activity_log")
    op.drop_table("activity_log")
