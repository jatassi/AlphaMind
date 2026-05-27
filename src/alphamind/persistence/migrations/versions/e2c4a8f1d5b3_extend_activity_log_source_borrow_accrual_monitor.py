"""extend ck_activity_log_source with BORROW_ACCRUAL_MONITOR (ALP-715 / ALP-719)

Revision ID: e2c4a8f1d5b3
Revises: c3b7d5a8e4f2
Create Date: 2026-05-27 18:00:00.000000

ALP-715 review F9 introduced a dedicated ``EventSource.BORROW_ACCRUAL_MONITOR``
value so the strategist's source-classification logic stops conflating
daily borrow-accrual emissions with margin-call signals. The application
layer is updated in ``src/alphamind/portfolio_state/events/types.py``;
the schema-layer ``ck_activity_log_source`` CHECK constraint was last
snapshotted by ``b5d2e3f4c6a7_add_activity_log`` and still rejects the
new value. A continuous-monitor process attempting to insert its first
``source='BORROW_ACCRUAL_MONITOR'`` row would trip the constraint and
the daily borrow-accrual tick would fail silently in production.

The migration drops + recreates ``ck_activity_log_source`` with the
extended vocabulary. The downgrade reverses the change but refuses to
run if any row already carries ``source='BORROW_ACCRUAL_MONITOR'`` —
otherwise the older constraint would silently forbid values the upgraded
constraint had been admitting, leaving the schema inconsistent.

Mirrors the SQLite-compatibility posture of ``d3e7a9c1b502``:
:func:`op.batch_alter_table` is used because SQLite does not support
``ALTER TABLE DROP CONSTRAINT``.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "e2c4a8f1d5b3"
down_revision: str | Sequence[str] | None = "c3b7d5a8e4f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the recognized ``activity_log.source`` vocabulary *after*
# the ALP-715 review F9 addition. Keep in sync with
# ``src/alphamind/portfolio_state/events/types.py``'s ``EventSource``
# enum — adding a new value to the enum requires extending both this
# snapshot AND shipping a new migration that drops + recreates the
# CHECK with the extended vocabulary.
_EVENT_SOURCES_AFTER = (
    "FILL_PROCESSOR",
    "COMMAND_EXECUTOR",
    "BRACKET_MANAGER",
    "MARGIN_MONITOR",
    "BORROW_ACCRUAL_MONITOR",
    "GUARDRAIL_LAYER",
    "CORPORATE_ACTION_PROCESSOR",
    "CONFIG_RELOAD",
    "OPERATOR_CONSOLE",
)


# Snapshot of the vocabulary *before* this migration — the set the prior
# ``b5d2e3f4c6a7_add_activity_log`` migration installed. The downgrade
# recreates this set.
_EVENT_SOURCES_BEFORE = (
    "FILL_PROCESSOR",
    "COMMAND_EXECUTOR",
    "BRACKET_MANAGER",
    "MARGIN_MONITOR",
    "GUARDRAIL_LAYER",
    "CORPORATE_ACTION_PROCESSOR",
    "CONFIG_RELOAD",
    "OPERATOR_CONSOLE",
)


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Drop + recreate ``ck_activity_log_source`` with the extended vocabulary."""
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_source", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_source",
            _check_in("source", _EVENT_SOURCES_AFTER),
        )


def downgrade() -> None:
    """Restore the pre-BORROW_ACCRUAL_MONITOR CHECK constraint.

    Refuses to run if any row carries ``source='BORROW_ACCRUAL_MONITOR'`` —
    that value would be silently rejected by the older constraint, leaving
    the schema in an inconsistent state. Operator must purge or remap
    those rows before downgrading.
    """
    bind = op.get_bind()
    offending = bind.execute(
        text("SELECT COUNT(*) FROM activity_log WHERE source = 'BORROW_ACCRUAL_MONITOR'"),
    ).scalar_one()
    if offending:
        msg = (
            f"Refusing to downgrade: {offending} activity_log row(s) carry "
            "source='BORROW_ACCRUAL_MONITOR', which the pre-e2c4a8f1d5b3 "
            "CHECK constraint forbids. Purge or remap those rows first."
        )
        raise RuntimeError(msg)
    with op.batch_alter_table("activity_log") as batch_op:
        batch_op.drop_constraint("ck_activity_log_source", type_="check")
        batch_op.create_check_constraint(
            "ck_activity_log_source",
            _check_in("source", _EVENT_SOURCES_BEFORE),
        )
