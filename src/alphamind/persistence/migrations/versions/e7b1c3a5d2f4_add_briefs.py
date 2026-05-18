"""add_briefs

Revision ID: e7b1c3a5d2f4
Revises: d9e2f5a7c3b4
Create Date: 2026-05-17 22:35:00.000000

Adds the ``briefs`` table per ``docs/architecture/data-and-state.md``
§ Brief store. One row per (invocation_id, brief_kind); the rendered
brief body and its ``CR-N`` → block_id reference index serialize into
two TEXT columns so a cross-process consumer (replay harness,
command-center diagnostic) can hydrate the full brief in one read.

Phase 7 of :func:`alphamind.distillation.orchestrator.run_external_distillation`
writes one row per invocation (``brief_kind='correlation_regime'``); the
hot-path keeps the in-process passthrough via
``DistillationOutputs.correlation_regime_brief``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b1c3a5d2f4"
down_revision: str | Sequence[str] | None = "d9e2f5a7c3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``briefs`` table."""
    op.create_table(
        "briefs",
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("brief_kind", sa.Text(), nullable=False),
        sa.Column("reference_index_json", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("invocation_id", "brief_kind"),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.invocation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "brief_kind IN ('correlation_regime')",
            name="ck_briefs_brief_kind",
        ),
    )


def downgrade() -> None:
    """Drop the ``briefs`` table."""
    op.drop_table("briefs")
