"""extend distillation_event_history.event_kind with correlation_divergence

Revision ID: 8a8d4e44b305
Revises: 0aa4fc8b5647
Create Date: 2026-04-27 00:00:00.000000

Story 02-distillation-layer/08d adds the ``correlation_divergence`` value to
``distillation_event_history.event_kind``. The event records when a sector-
pair correlation breaks at or above 1.5 sigma from its long-window baseline; the resolution
outcome is logged when correlation returns to within one sigma of baseline.

SQLite does not support direct CHECK-constraint alteration, so the upgrade
recreates the table with the extended constraint and copies rows over. The
downgrade reverses the operation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8a8d4e44b305"
down_revision: str | Sequence[str] | None = "0aa4fc8b5647"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_EVENT_KINDS = "'gap', 'extended_hours'"
_NEW_EVENT_KINDS = "'gap', 'extended_hours', 'correlation_divergence'"


def upgrade() -> None:
    """Extend the event_kind CHECK constraint to include correlation_divergence.

    The recreate-and-copy pattern is the SQLite-compatible idiom for CHECK
    constraint alteration; ``op.batch_alter_table`` handles the dance.
    """
    with op.batch_alter_table("distillation_event_history") as batch_op:
        batch_op.drop_constraint(
            "ck_distillation_event_history_event_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_distillation_event_history_event_kind",
            f"event_kind IN ({_NEW_EVENT_KINDS})",
        )


def downgrade() -> None:
    """Revert the event_kind CHECK constraint to the original two values.

    Rows whose ``event_kind`` is ``correlation_divergence`` would violate
    the older constraint. Rather than silently deleting accumulated
    correlation-divergence history, refuse to downgrade and require the
    operator to clear them explicitly — a routine ``alembic downgrade -1``
    on a long-running database otherwise would obliterate weeks of
    detected events without warning.
    """
    bind = op.get_bind()
    surviving = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM distillation_event_history "
            "WHERE event_kind = 'correlation_divergence'"
        )
    ).scalar_one()
    if surviving:
        raise RuntimeError(
            f"refusing to downgrade revision 8a8d4e44b305: "
            f"{surviving} 'correlation_divergence' row(s) in "
            "distillation_event_history would be invalidated by the older "
            "CHECK constraint. Clear them manually before re-running the "
            "downgrade: "
            "DELETE FROM distillation_event_history "
            "WHERE event_kind = 'correlation_divergence';"
        )
    with op.batch_alter_table("distillation_event_history") as batch_op:
        batch_op.drop_constraint(
            "ck_distillation_event_history_event_kind",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_distillation_event_history_event_kind",
            f"event_kind IN ({_OLD_EVENT_KINDS})",
        )
