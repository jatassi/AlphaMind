"""add_regime_adaptation_state_table

Revision ID: 3fbbfd3049f5
Revises: 8a8d4e44b305
Create Date: 2026-04-29 00:00:00.000000

Adds the forward-only ``regime_adaptation_state`` table the orchestrator
(story 06-risk-guardrails/09) writes per invocation. Mirrors the
``distillation_regime_state`` shape: primary key on ``as_of``, indexed for
``order by as_of desc limit 1`` reads. CHECK constraints encode the same
invariants the typed-record ``__post_init__`` enforces, so a future
direct-SQL writer (e.g. a backfill script) faces the same fail-closed
guarantees.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3fbbfd3049f5"
down_revision: str | Sequence[str] | None = "8a8d4e44b305"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``regime_adaptation_state`` table and its index."""
    op.create_table(
        "regime_adaptation_state",
        sa.Column("as_of", sa.Text(), nullable=False),
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("active_regime", sa.Text(), nullable=False),
        sa.Column("prior_regime", sa.Text(), nullable=True),
        sa.Column("transition_state", sa.Text(), nullable=False),
        sa.Column("transition_invocations_remaining", sa.Integer(), nullable=False),
        sa.Column("transition_started_invocation_id", sa.Text(), nullable=True),
        sa.Column("transition_origin_regime", sa.Text(), nullable=True),
        sa.Column("active_overlays_csv", sa.Text(), nullable=False),
        sa.Column("distillation_regime_label", sa.Text(), nullable=False),
        sa.Column("distillation_vix_level", sa.Float(), nullable=False),
        sa.Column("regime_skip_emergency", sa.Integer(), nullable=False),
        sa.Column("ingested_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("as_of"),
        sa.CheckConstraint(
            "active_regime IN ('low_vol', 'normal', 'elevated', 'crisis')",
            name="ck_regime_adaptation_state_active_regime",
        ),
        sa.CheckConstraint(
            "transition_state IN ('STABLE', 'TIGHTENING', 'LOOSENING')",
            name="ck_regime_adaptation_state_transition_state",
        ),
        sa.CheckConstraint(
            "prior_regime IS NULL OR prior_regime IN ('low_vol', 'normal', 'elevated', 'crisis')",
            name="ck_regime_adaptation_state_prior_regime",
        ),
        sa.CheckConstraint(
            "transition_origin_regime IS NULL OR transition_origin_regime IN "
            "('low_vol', 'normal', 'elevated', 'crisis')",
            name="ck_regime_adaptation_state_origin_regime",
        ),
        sa.CheckConstraint(
            "regime_skip_emergency IN (0, 1)",
            name="ck_regime_adaptation_state_skip_emergency",
        ),
        sa.CheckConstraint(
            "(transition_state = 'STABLE' AND transition_invocations_remaining = 0) "
            "OR transition_state != 'STABLE'",
            name="ck_regime_adaptation_state_stable_zero_remaining",
        ),
        sa.CheckConstraint(
            "transition_state != 'LOOSENING' OR "
            "(transition_origin_regime IS NOT NULL AND "
            " transition_started_invocation_id IS NOT NULL)",
            name="ck_regime_adaptation_state_loosening_populated",
        ),
    )
    op.create_index(
        "ix_regime_adaptation_state_as_of_desc",
        "regime_adaptation_state",
        ["as_of"],
    )


def downgrade() -> None:
    """Drop the ``regime_adaptation_state`` table and its index."""
    op.drop_index(
        "ix_regime_adaptation_state_as_of_desc",
        table_name="regime_adaptation_state",
    )
    op.drop_table("regime_adaptation_state")
