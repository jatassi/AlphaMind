"""extend ck_invocations_trigger_source with borrow_accrual (ALP-715 / ALP-718)

Revision ID: d3e7a9c1b502
Revises: b8c9d2e4f7a1
Create Date: 2026-05-27 12:00:00.000000

ALP-718 extended the application-layer ``_TRIGGER_SOURCES`` tuple in
``src/alphamind/state/tables/invocations.py`` with the new
``"borrow_accrual"`` value emitted by the continuous monitor's daily
borrow-accrual tick (ALP-715 § 4f). The previous migration
(``b8c9d2e4f7a1``) snapshotted the vocabulary *before* that addition, so
the CHECK constraint at the schema layer still rejects the new value.
A continuous-monitor process attempting to write its first
``trigger_source='borrow_accrual'`` row would trip
``ck_invocations_trigger_source`` and the daily tick would fail
silently in production.

The migration drops + recreates ``ck_invocations_trigger_source`` with
the extended vocabulary. The downgrade reverses the change, but
refuses to run if any row already has ``trigger_source='borrow_accrual'``
— otherwise the older (pre-borrow_accrual) constraint would silently
forbid values the upgraded constraint had been admitting, and the
downgrade would either fail mid-recreate or leave the schema
inconsistent.

Mirrors the SQLite-compatibility posture of the prior migration:
:func:`op.batch_alter_table` is used because SQLite does not support
``ALTER TABLE DROP CONSTRAINT``.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "d3e7a9c1b502"
down_revision: str | Sequence[str] | None = "b8c9d2e4f7a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the recognized ``trigger_source`` vocabulary *after* the
# ALP-718 addition. Keep in sync with
# ``src/alphamind/state/tables/invocations.py``'s ``_TRIGGER_SOURCES``
# tuple — adding a new producer of an ``invocations`` row requires
# extending both this snapshot AND the application-layer tuple, then
# shipping a new migration that drops + recreates the CHECK with the
# extended vocabulary.
_TRIGGER_SOURCES_AFTER = (
    "cli",
    "debug_e2e_cli",
    "operator_console",
    "continuous_monitor",
    "borrow_accrual",
    "replay",
    "pre_open",
    "market_hours_rolling",
    "pre_close",
    "off_hours_rolling",
    "weekend_saturday",
    "weekend_sunday",
    "emergency",
    "test",
    "cron",
    "morning-cron",
    "test_initial_greeks_persistence.py",
)


# Snapshot of the vocabulary *before* this migration — exactly what
# ``b8c9d2e4f7a1`` pinned. The downgrade recreates this set.
_TRIGGER_SOURCES_BEFORE = (
    "cli",
    "debug_e2e_cli",
    "operator_console",
    "continuous_monitor",
    "replay",
    "pre_open",
    "market_hours_rolling",
    "pre_close",
    "off_hours_rolling",
    "weekend_saturday",
    "weekend_sunday",
    "emergency",
    "test",
    "cron",
    "morning-cron",
    "test_initial_greeks_persistence.py",
)


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Drop + recreate ``ck_invocations_trigger_source`` with the extended vocabulary."""
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.drop_constraint("ck_invocations_trigger_source", type_="check")
        batch_op.create_check_constraint(
            "ck_invocations_trigger_source",
            _check_in("trigger_source", _TRIGGER_SOURCES_AFTER),
        )


def downgrade() -> None:
    """Restore the pre-borrow_accrual CHECK constraint.

    Refuses to run if any row carries ``trigger_source='borrow_accrual'`` —
    that value would be silently rejected by the older constraint, leaving
    the schema in an inconsistent state. Operator must purge or remap
    those rows before downgrading.
    """
    bind = op.get_bind()
    offending = bind.execute(
        text("SELECT COUNT(*) FROM invocations WHERE trigger_source = 'borrow_accrual'"),
    ).scalar_one()
    if offending:
        msg = (
            f"Refusing to downgrade: {offending} invocations row(s) carry "
            "trigger_source='borrow_accrual', which the pre-d3e7a9c1b502 "
            "CHECK constraint forbids. Purge or remap those rows first."
        )
        raise RuntimeError(msg)
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.drop_constraint("ck_invocations_trigger_source", type_="check")
        batch_op.create_check_constraint(
            "ck_invocations_trigger_source",
            _check_in("trigger_source", _TRIGGER_SOURCES_BEFORE),
        )
