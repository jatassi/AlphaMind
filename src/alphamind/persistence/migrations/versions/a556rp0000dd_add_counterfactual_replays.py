"""add_counterfactual_replays — durable substrate for the replay engine (ALP-556)

Revision ID: a556rp0000dd
Revises: a867er0000cc
Create Date: 2026-06-06 00:00:00.000000

Adds the ``counterfactual_replays`` table that persists one immutable record per
replay attempt produced by the counterfactual replay engine. Append-only;
one record per (``pm_decision_envelope_id``, ``replay_kind``) tuple enforced by
``uq_counterfactual_replays_envelope_kind``.

CHECK-constraint vocabularies are frozen at the design-doc tokens (lowercase
``.value``s from the enums in story 01) rather than read from the live enums —
the CHECK-vocab trap: ``downgrade`` must narrow back to exactly the set this
revision wrote, which a live-enum read cannot produce once new members are added.

**Idempotent on a fresh DB.** The genesis baseline (``a000000000aa``) is
metadata-driven (``Base.metadata.create_all``), so on any fresh DB it already
creates ``counterfactual_replays`` with the full schema this migration adds.
``upgrade`` guards ``create_table`` and each ``create_index`` with
``sa.inspect(op.get_bind())`` so ``alembic upgrade head`` on a baseline-built DB
is a clean no-op that leaves the metadata-built table untouched.

**Parented on** ``a867er0000cc`` (ALP-867's CHECK-widen), so the chain stays
linear (a single head).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a556rp0000dd"
down_revision: str | Sequence[str] | None = "a867er0000cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "counterfactual_replays"

# CHECK vocabularies frozen as of this revision (not read from live enums —
# the CHECK-vocab trap). All five members of each enum are listed from the
# start so no later story has to retroactively widen a CHECK on a deployed DB.
_REPLAY_KIND_VALUES: tuple[str, ...] = ("rejection", "modification_original_form")
_REPLAY_STATUS_VALUES: tuple[str, ...] = ("evaluated", "unevaluable")
_UNEVALUABLE_REASON_VALUES: tuple[str, ...] = (
    "unsupported_instrument",
    "unsupported_bracket_type",
    "data_missing",
    "corporate_action_in_window",
    "strategist_position_action_not_supported",
)
_EXIT_LEG_VALUES: tuple[str, ...] = (
    "target_hit",
    "stop_hit",
    "time_stop_fired",
    "entry_window_expired_unfilled",
    "strategist_close_at_proposal",
)
_CONFIDENCE_VALUES: tuple[str, ...] = ("high", "medium", "low")


def _check_in(column: str, values: tuple[str, ...]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Create the counterfactual_replays table + indexes (no-op if already present)."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("replay_id", sa.Text(), nullable=False),
            sa.Column("pm_decision_envelope_id", sa.Text(), nullable=False),
            sa.Column("replay_kind", sa.Text(), nullable=False),
            sa.Column("replay_status", sa.Text(), nullable=False),
            sa.Column("unevaluable_reason", sa.Text(), nullable=True),
            sa.Column("entered", sa.Boolean(), nullable=True),
            sa.Column("entry_price", sa.Text(), nullable=True),
            sa.Column("entry_timestamp", sa.Text(), nullable=True),
            sa.Column("entry_slippage", sa.Text(), nullable=True),
            sa.Column("entry_fees", sa.Text(), nullable=True),
            sa.Column("exit_leg", sa.Text(), nullable=True),
            sa.Column("exit_price", sa.Text(), nullable=True),
            sa.Column("exit_timestamp", sa.Text(), nullable=True),
            sa.Column("exit_slippage", sa.Text(), nullable=True),
            sa.Column("exit_fees", sa.Text(), nullable=True),
            sa.Column("realized_pl", sa.Text(), nullable=True),
            sa.Column("confidence", sa.Text(), nullable=True),
            sa.Column("replay_timestamp", sa.Text(), nullable=False),
            sa.Column("replay_data_window_start", sa.Text(), nullable=True),
            sa.Column("replay_data_window_end", sa.Text(), nullable=True),
            sa.Column("replay_engine_version", sa.Text(), nullable=False),
            sa.UniqueConstraint(
                "pm_decision_envelope_id",
                "replay_kind",
                name="uq_counterfactual_replays_envelope_kind",
            ),
            sa.CheckConstraint(
                _check_in("replay_kind", _REPLAY_KIND_VALUES),
                name="ck_counterfactual_replays_replay_kind",
            ),
            sa.CheckConstraint(
                _check_in("replay_status", _REPLAY_STATUS_VALUES),
                name="ck_counterfactual_replays_replay_status",
            ),
            sa.CheckConstraint(
                "unevaluable_reason IS NULL OR "
                + _check_in("unevaluable_reason", _UNEVALUABLE_REASON_VALUES),
                name="ck_counterfactual_replays_unevaluable_reason",
            ),
            sa.CheckConstraint(
                f"exit_leg IS NULL OR {_check_in('exit_leg', _EXIT_LEG_VALUES)}",
                name="ck_counterfactual_replays_exit_leg",
            ),
            sa.CheckConstraint(
                f"confidence IS NULL OR {_check_in('confidence', _CONFIDENCE_VALUES)}",
                name="ck_counterfactual_replays_confidence",
            ),
            sa.PrimaryKeyConstraint("replay_id"),
        )

    existing_indexes = (
        {i["name"] for i in inspector.get_indexes(_TABLE)} if inspector.has_table(_TABLE) else set()
    )

    if "ix_counterfactual_replays_envelope" not in existing_indexes:
        op.create_index(
            "ix_counterfactual_replays_envelope",
            _TABLE,
            ["pm_decision_envelope_id"],
        )
    if "ix_counterfactual_replays_status_timestamp" not in existing_indexes:
        op.create_index(
            "ix_counterfactual_replays_status_timestamp",
            _TABLE,
            ["replay_status", "replay_timestamp"],
        )


def downgrade() -> None:
    """Drop the counterfactual_replays table + indexes."""
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    existing_indexes = {i["name"] for i in inspector.get_indexes(_TABLE)}
    if "ix_counterfactual_replays_status_timestamp" in existing_indexes:
        op.drop_index("ix_counterfactual_replays_status_timestamp", table_name=_TABLE)
    if "ix_counterfactual_replays_envelope" in existing_indexes:
        op.drop_index("ix_counterfactual_replays_envelope", table_name=_TABLE)
    op.drop_table(_TABLE)
