"""add_positions

Revision ID: c8e3f4a2b1d6
Revises: b5d2e3f4c6a7
Create Date: 2026-05-07 05:30:00.000000

Adds the ``positions`` table (story 04a / ALP-358) — one row per position,
the central business object every Tier 1 / Tier 2 entity references.
Polymorphic over instrument type via the ``instrument_type`` discriminator
+ ``details_json`` payload (the per-variant ``PositionDetailsPayload``
model's ``model_dump_json()``).

Mirrors the column shape defined in
``src/alphamind/execution/state_persistence/tables/positions.py`` and the
design doc at ``docs/design/05-execution-layer/state-persistence.md`` §
Positions. CHECK constraints encode the same enum vocabularies the
SQLAlchemy ``__table_args__`` install, so a future direct-SQL writer faces
the same fail-closed guarantees the typed ``PositionRecord`` enforces on
the application path.

Out of scope at this story: FK constraints to ``theses`` and ``brackets``.
Those tables don't exist yet (sibling parallel stories 04b / 04c); the
columns are nullable plain TEXT here. A follow-up integration migration
will add the FKs once those tables ship.
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op

from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionStatus,
)

# revision identifiers, used by Alembic.
revision: str = "c8e3f4a2b1d6"
down_revision: str | Sequence[str] | None = "b5d2e3f4c6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create ``positions`` and its two indexes."""
    op.create_table(
        "positions",
        sa.Column("position_id", sa.Text(), nullable=False),
        sa.Column("thesis_id", sa.Text(), nullable=True),
        sa.Column("bracket_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("entry_timestamp", sa.Text(), nullable=True),
        sa.Column("instrument_type", sa.Text(), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False),
        sa.Column("execution_history_json", sa.Text(), nullable=False),
        sa.Column("realized_pnl_to_date_usd", sa.Float(), nullable=True),
        sa.Column("corporate_action_adjustment_needed", sa.Integer(), nullable=False),
        sa.Column("parent_position_id", sa.Text(), nullable=True),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("position_id"),
        sa.CheckConstraint(_check_in("status", PositionStatus), name="ck_positions_status"),
        sa.CheckConstraint(
            _check_in("direction", Direction),
            name="ck_positions_direction",
        ),
        sa.CheckConstraint(
            _check_in("instrument_type", InstrumentType),
            name="ck_positions_instrument_type",
        ),
    )
    op.create_index("ix_positions_status", "positions", ["status"])
    op.create_index("ix_positions_thesis_id", "positions", ["thesis_id"])


def downgrade() -> None:
    """Drop ``positions`` and its indexes."""
    op.drop_index("ix_positions_thesis_id", table_name="positions")
    op.drop_index("ix_positions_status", table_name="positions")
    op.drop_table("positions")
