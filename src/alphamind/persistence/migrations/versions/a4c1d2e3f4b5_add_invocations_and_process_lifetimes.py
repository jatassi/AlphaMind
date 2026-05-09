"""add_invocations_and_process_lifetimes

Revision ID: a4c1d2e3f4b5
Revises: c1f7d2e8a9b3
Create Date: 2026-05-07 04:30:00.000000

Adds the two state-persistence substrate tables (story 02b) — one row per
long-running process start (``process_lifetimes``) and one row per pipeline
invocation (``invocations``). The ``invocations.process_lifetime_id``
foreign key targets ``process_lifetimes.process_lifetime_id`` with
``ON DELETE RESTRICT``, so both tables must exist before the FK is wired —
they ship in a single migration.

Mirrors the column shape defined in
``src/alphamind/execution/state_persistence/tables/`` and the design doc at
``docs/design/05-execution-layer/state-persistence.md`` § Process lifetimes
and § Invocation records. CHECK constraints encode the same enum
vocabularies the SQLAlchemy ``__table_args__`` install, so a future
direct-SQL writer (e.g. a backfill script) faces the same fail-closed
guarantees the typed records enforce on the application path.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c1d2e3f4b5"
down_revision: str | Sequence[str] | None = "c1f7d2e8a9b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``process_lifetimes`` and ``invocations``."""
    op.create_table(
        "process_lifetimes",
        sa.Column("process_lifetime_id", sa.Text(), nullable=False),
        sa.Column("process_role", sa.Text(), nullable=False),
        sa.Column("process_start_at", sa.Text(), nullable=False),
        sa.Column("process_pid", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("git_sha", sa.Text(), nullable=False),
        sa.Column("git_branch", sa.Text(), nullable=False),
        sa.Column("git_dirty", sa.Integer(), nullable=False),
        sa.Column("python_version", sa.Text(), nullable=False),
        sa.Column("pip_freeze_hash", sa.Text(), nullable=False),
        sa.Column("pip_freeze_snapshot_path", sa.Text(), nullable=False),
        sa.Column("anthropic_sdk_version", sa.Text(), nullable=False),
        sa.Column("claude_agent_sdk_version", sa.Text(), nullable=False),
        sa.Column("os_release", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("process_lifetime_id"),
        sa.CheckConstraint(
            "process_role IN ('pipeline', 'monitor')",
            name="ck_process_lifetimes_process_role",
        ),
        sa.CheckConstraint(
            "git_dirty IN (0, 1)",
            name="ck_process_lifetimes_git_dirty",
        ),
    )

    op.create_table(
        "invocations",
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("process_lifetime_id", sa.Text(), nullable=False),
        sa.Column("start_at", sa.Text(), nullable=False),
        sa.Column("phase1_completed_at", sa.Text(), nullable=True),
        sa.Column("phase2_completed_at", sa.Text(), nullable=True),
        sa.Column("trigger_type", sa.Text(), nullable=False),
        sa.Column("trigger_source", sa.Text(), nullable=False),
        sa.Column("trigger_reason", sa.Text(), nullable=False),
        sa.Column("git_sha_at_invocation", sa.Text(), nullable=False),
        sa.Column("active_profile", sa.Text(), nullable=False),
        sa.Column("active_regime", sa.Text(), nullable=False),
        sa.Column("active_mode", sa.Text(), nullable=False),
        sa.Column("active_overlays_json", sa.Text(), nullable=False),
        sa.Column("resolved_config_hash", sa.Text(), nullable=False),
        sa.Column("resolved_config_snapshot_path", sa.Text(), nullable=False),
        sa.Column("feature_flags_snapshot_json", sa.Text(), nullable=False),
        sa.Column("data_calibration_state_snapshot_path", sa.Text(), nullable=False),
        sa.Column("data_source_freshness_json", sa.Text(), nullable=False),
        sa.Column("fill_collection_summary_json", sa.Text(), nullable=True),
        sa.Column("command_execution_summary_json", sa.Text(), nullable=True),
        sa.Column("staleness_flag", sa.Integer(), nullable=True),
        sa.Column("snapshot_metadata_json", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("invocation_id"),
        sa.ForeignKeyConstraint(
            ["process_lifetime_id"],
            ["process_lifetimes.process_lifetime_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('scheduled', 'emergency', 'manual')",
            name="ck_invocations_trigger_type",
        ),
        sa.CheckConstraint(
            "active_mode IN ('normal', 'defensive_posture', 'halted')",
            name="ck_invocations_active_mode",
        ),
        sa.CheckConstraint(
            "staleness_flag IS NULL OR staleness_flag IN (0, 1)",
            name="ck_invocations_staleness_flag",
        ),
    )


def downgrade() -> None:
    """Drop ``invocations`` then ``process_lifetimes`` (FK order matters)."""
    op.drop_table("invocations")
    op.drop_table("process_lifetimes")
