"""rename invocations.trigger_source 'pre_open' -> 'market_open' (ALP-822)

Revision ID: a7e3f1c9d2b4
Revises: c1b2a3d4e5f6
Create Date: 2026-06-02 13:00:00.000000

ALP-822 renamed the first-of-day scheduler run-type from ``pre_open`` to
``market_open``. As of 2026-06-02 it fires at 09:35 ET — *after* the 09:30
NYSE open — so ``pre_open`` had become a misnomer. ``invocations.trigger_source``
stores the run-type label and is gated by ``ck_invocations_trigger_source``,
so the rename has to land at the schema layer too.

This migration (a) rewrites every historical ``trigger_source='pre_open'``
row to ``'market_open'`` so past invocations stay queryable under the new
label, and (b) drops + recreates ``ck_invocations_trigger_source`` with
``pre_open`` replaced by ``market_open`` in the allowed vocabulary.

The three steps run in the order drop-CHECK → rewrite-rows → recreate-CHECK
deliberately: the pre-rename constraint forbids ``market_open`` (so the
UPDATE would be rejected while it is still attached), and the post-rename
constraint forbids ``pre_open`` (so the rows must already carry the new
value before the constraint is recreated and the table is copied).

The downgrade is the exact inverse: remap ``market_open`` rows back to
``pre_open`` and restore the pre-rename vocabulary. No refusal guard is
needed — unlike the additive ``borrow_accrual`` migration, the downgrade
remaps the renamed value rather than leaving it stranded under a stricter
constraint.

Mirrors the SQLite-compatibility posture of the prior ``trigger_source``
migrations: :func:`op.batch_alter_table` is used because SQLite does not
support ``ALTER TABLE DROP CONSTRAINT``.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "a7e3f1c9d2b4"
down_revision: str | Sequence[str] | None = "c1b2a3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the recognized ``trigger_source`` vocabulary *after* the
# ALP-822 rename (``pre_open`` -> ``market_open``). Keep in sync with
# ``src/alphamind/state/tables/invocations.py``'s ``_TRIGGER_SOURCES``
# tuple — renaming a value requires editing both this snapshot AND the
# application-layer tuple, then shipping this migration that rewrites the
# rows and recreates the CHECK with the renamed vocabulary.
_TRIGGER_SOURCES_AFTER = (
    "cli",
    "debug_e2e_cli",
    "operator_console",
    "continuous_monitor",
    "borrow_accrual",
    "replay",
    "market_open",
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
# ``d3e7a9c1b502`` pinned (still ``pre_open``). The downgrade recreates
# this set.
_TRIGGER_SOURCES_BEFORE = (
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


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Rewrite ``pre_open`` rows to ``market_open`` and swap the CHECK vocab."""
    bind = op.get_bind()
    # Drop the old CHECK first — it forbids 'market_open', so the row
    # rewrite below would otherwise be rejected while it is attached.
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.drop_constraint("ck_invocations_trigger_source", type_="check")
    bind.execute(
        text(
            "UPDATE invocations SET trigger_source = 'market_open' "
            "WHERE trigger_source = 'pre_open'"
        )
    )
    # Recreate with the renamed vocabulary; rows already carry 'market_open',
    # so the table-copy the batch performs passes the new constraint.
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.create_check_constraint(
            "ck_invocations_trigger_source",
            _check_in("trigger_source", _TRIGGER_SOURCES_AFTER),
        )


def downgrade() -> None:
    """Restore the pre-rename CHECK and remap ``market_open`` rows back.

    The mirror of :func:`upgrade`: drop the renamed constraint, remap the
    renamed rows back to ``pre_open`` (the pre-rename constraint forbids
    ``market_open``), then recreate the pre-rename constraint.
    """
    bind = op.get_bind()
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.drop_constraint("ck_invocations_trigger_source", type_="check")
    bind.execute(
        text(
            "UPDATE invocations SET trigger_source = 'pre_open' "
            "WHERE trigger_source = 'market_open'"
        )
    )
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.create_check_constraint(
            "ck_invocations_trigger_source",
            _check_in("trigger_source", _TRIGGER_SOURCES_BEFORE),
        )
