"""add_theses_and_thesis_components

Revision ID: c6e3f4d5b8a9
Revises: b5d2e3f4c6a7
Create Date: 2026-05-07 05:30:00.000000

Adds the ``theses`` parent + ``thesis_components`` child tables (story
04b / ALP-359). The parent-child split is mandated by the design's three
consumption modes — programmatic-query metadata, single-component LLM
evaluation, full-thesis evaluation. Both tables ship in one migration
because the FK on ``thesis_components.thesis_id`` requires the parent
``theses`` to exist within the same migration.

Mirrors the column shape defined in
``src/alphamind/execution/state_persistence/tables/theses.py`` and
``thesis_components.py``. CHECK constraints encode the same enum
vocabularies the SQLAlchemy ``__table_args__`` install, so a future
direct-SQL writer faces the same fail-closed guarantees the typed
``ThesisRecord`` enforces on the application path.

The FK on ``thesis_components.thesis_id`` targets ``theses.thesis_id``
with ``ON DELETE RESTRICT``. The FK from ``theses.position_id`` to
``positions`` is deferred to the integration migration, since the
positions table ships in a parallel-wave story (04a).
"""

from collections.abc import Sequence
from enum import StrEnum

import sqlalchemy as sa
from alembic import op

from alphamind.portfolio_state.records.theses import (
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)

# revision identifiers, used by Alembic.
revision: str = "c6e3f4d5b8a9"
down_revision: str | Sequence[str] | None = "c8e3f4a2b1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _check_in(column: str, members: type[StrEnum]) -> str:
    rendered = ", ".join(repr(m.value) for m in members)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create ``theses`` and ``thesis_components`` plus their indexes."""
    op.create_table(
        "theses",
        sa.Column("thesis_id", sa.Text(), nullable=False),
        sa.Column("position_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("resolution_timestamp", sa.Text(), nullable=True),
        sa.Column("resolution_category", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("time_expectation_hours", sa.Float(), nullable=True),
        sa.Column("position_size_rationale", sa.Text(), nullable=True),
        sa.Column("generation_timestamp", sa.Text(), nullable=False),
        sa.Column("narrative_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("thesis_id"),
        sa.CheckConstraint(
            _check_in("status", ThesisRecordStatus),
            name="ck_theses_status",
        ),
        sa.CheckConstraint(
            "resolution_category IS NULL OR "
            + _check_in("resolution_category", ThesisResolutionCategory),
            name="ck_theses_resolution_category",
        ),
    )
    op.create_index("ix_theses_status", "theses", ["status"])
    op.create_index("ix_theses_position_id", "theses", ["position_id"])
    op.create_index(
        "ix_theses_resolution_timestamp",
        "theses",
        ["resolution_timestamp"],
    )

    op.create_table(
        "thesis_components",
        sa.Column("component_id", sa.Text(), nullable=False),
        sa.Column("thesis_id", sa.Text(), nullable=False),
        sa.Column("component_type", sa.Text(), nullable=False),
        sa.Column("linked_bracket_leg", sa.Text(), nullable=True),
        sa.Column("instrument_reference", sa.Text(), nullable=True),
        sa.Column("narrative", sa.Text(), nullable=False),
        sa.Column("key_assumptions_json", sa.Text(), nullable=False),
        sa.Column("supporting_signals_json", sa.Text(), nullable=False),
        sa.Column("resolution_outcome", sa.Text(), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("component_id"),
        sa.ForeignKeyConstraint(
            ["thesis_id"],
            ["theses.thesis_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            _check_in("component_type", ThesisComponentType),
            name="ck_thesis_components_component_type",
        ),
        sa.CheckConstraint(
            "resolution_outcome IS NULL OR "
            + _check_in("resolution_outcome", ThesisComponentOutcome),
            name="ck_thesis_components_resolution_outcome",
        ),
    )
    op.create_index(
        "ix_thesis_components_thesis_id",
        "thesis_components",
        ["thesis_id"],
    )


def downgrade() -> None:
    """Drop ``thesis_components`` then ``theses`` (FK order matters)."""
    op.drop_index("ix_thesis_components_thesis_id", table_name="thesis_components")
    op.drop_table("thesis_components")
    op.drop_index("ix_theses_resolution_timestamp", table_name="theses")
    op.drop_index("ix_theses_position_id", table_name="theses")
    op.drop_index("ix_theses_status", table_name="theses")
    op.drop_table("theses")
