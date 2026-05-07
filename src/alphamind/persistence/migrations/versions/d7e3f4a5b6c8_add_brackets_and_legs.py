"""add_brackets_and_legs

Revision ID: d7e3f4a5b6c8
Revises: c6e3f4d5b8a9
Create Date: 2026-05-07 05:30:00.000000

Adds the ``brackets`` (parent) + ``bracket_legs`` (child) tables (story
04d / ALP-361). The bracket bridges the thesis (which justifies each exit
condition) and the orders (which implement the conditions mechanically).

Mirrors the column shape defined in
``src/alphamind/execution/state_persistence/tables/brackets.py`` and
``src/alphamind/execution/state_persistence/tables/bracket_legs.py`` and
the design doc at ``docs/design/05-execution-layer/state-persistence.md``
§ Brackets. CHECK constraints encode the same enum vocabularies the
SQLAlchemy ``__table_args__`` install, so a future direct-SQL writer
faces the same fail-closed guarantees the typed records enforce on the
application path.

The FK on ``bracket_legs.bracket_id`` targets ``brackets.bracket_id``
with ``ON DELETE RESTRICT`` — bracket rows are append-only.

Out of scope at this story: FK constraints from ``brackets.position_id``
to ``positions`` and from ``brackets.entry_order_id`` /
``bracket_legs.order_id`` to ``orders``. Those tables don't exist yet
(stories 04a-04b); the columns are plain TEXT here. Follow-up integration
migrations wire the FKs once those tables ship.
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op

from alphamind.portfolio_state.records.orders import (
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketStatus,
)

# revision identifiers, used by Alembic.
revision: str = "d7e3f4a5b6c8"
down_revision: str | Sequence[str] | None = "c6e3f4d5b8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create ``brackets`` and ``bracket_legs`` plus their indexes."""
    op.create_table(
        "brackets",
        sa.Column("bracket_id", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("entry_order_id", sa.Text(), nullable=False),
        sa.Column("entry_window_deadline", sa.Text(), nullable=True),
        sa.Column("corporate_action_cancellation_reason", sa.Text(), nullable=True),
        sa.Column("modification_history_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bracket_id"),
        sa.CheckConstraint(
            _check_in("status", BracketStatus),
            name="ck_brackets_status",
        ),
    )
    op.create_index(
        "ix_brackets_position_id",
        "brackets",
        ["position_id"],
        unique=True,
    )
    op.create_index("ix_brackets_status", "brackets", ["status"])

    op.create_table(
        "bracket_legs",
        sa.Column("bracket_leg_id", sa.Text(), nullable=False),
        sa.Column("bracket_id", sa.Text(), nullable=False),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("leg_type", sa.Text(), nullable=False),
        sa.Column("order_id", sa.Text(), nullable=True),
        sa.Column("trigger_kind", sa.Text(), nullable=False),
        sa.Column("trigger_payload_json", sa.Text(), nullable=False),
        sa.Column("pl_anchor_json", sa.Text(), nullable=True),
        sa.Column("enforcement", sa.Text(), nullable=False),
        sa.Column("leg_status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("bracket_leg_id"),
        sa.ForeignKeyConstraint(
            ["bracket_id"],
            ["brackets.bracket_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _check_in("leg_type", BracketLegType),
            name="ck_bracket_legs_leg_type",
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('PRICE', 'TIME', 'EVENT')",
            name="ck_bracket_legs_trigger_kind",
        ),
        sa.CheckConstraint(
            _check_in("enforcement", BracketLegEnforcement),
            name="ck_bracket_legs_enforcement",
        ),
        sa.CheckConstraint(
            _check_in("leg_status", BracketLegStatus),
            name="ck_bracket_legs_leg_status",
        ),
    )
    op.create_index("ix_bracket_legs_bracket_id", "bracket_legs", ["bracket_id"])
    op.create_index(
        "uq_bracket_legs_bracket_id_leg_index",
        "bracket_legs",
        ["bracket_id", "leg_index"],
        unique=True,
    )


def downgrade() -> None:
    """Drop ``bracket_legs`` then ``brackets`` (FK order matters)."""
    op.drop_index("uq_bracket_legs_bracket_id_leg_index", table_name="bracket_legs")
    op.drop_index("ix_bracket_legs_bracket_id", table_name="bracket_legs")
    op.drop_table("bracket_legs")
    op.drop_index("ix_brackets_status", table_name="brackets")
    op.drop_index("ix_brackets_position_id", table_name="brackets")
    op.drop_table("brackets")
