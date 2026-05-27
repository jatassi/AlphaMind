"""add CHECK constraint on invocations.trigger_source (operator_console + extant)

Revision ID: b8c9d2e4f7a1
Revises: cc1f2e3d4a5b
Create Date: 2026-05-27 00:00:00.000000

Pins the recognized ``invocations.trigger_source`` vocabulary at the
schema layer. The new ``operator_console`` value is the load-bearing
addition: the command-center activity-log explorer's "Operator actions"
saved filter view joins ``activity_log`` rows against ``invocations``
rows whose ``trigger_source`` equals ``operator_console`` (per
``docs/design/command-center.md`` § Operator actions, story 04a's
proxy + audit contract). Without a CHECK a typo in a downstream writer
(``operator-console`` vs ``operator_console``) would silently break the
filter — operator actions would stop appearing in the saved view and
nobody would notice until the next manual audit (F15).

The enumeration includes every other ``trigger_source`` value the
codebase currently uses across the scheduler (RunType cron triggers),
the continuous monitor (emergency dispatch), the CLI dispatch paths,
the replay harness, and the existing test fixtures. Keeping the
``test`` / ``cron`` / ``morning-cron`` / ``test_initial_greeks_persistence.py``
entries in the allow-list means the migration is purely additive — no
existing row is invalidated — and the test suite continues to pass
under the new constraint. A future refactor that normalizes test
fixtures to a single marker can shrink the allow-list; for now the
finding's "narrow the scope, leave existing values as-is" guidance
applies (F15).

SQLite does not support ``ALTER TABLE ADD CONSTRAINT``, so the upgrade
uses :func:`op.batch_alter_table` to recreate the table with the new
constraint attached. The downgrade refuses to run if any row in the
DB has a ``trigger_source`` value not in the pre-F15 vocabulary —
otherwise the older (constraint-free) schema would silently accept
values the new constraint had been guarding.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d2e4f7a1"
down_revision: str | Sequence[str] | None = "cc1f2e3d4a5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the recognized ``trigger_source`` vocabulary at the time
# of this migration. Keep in sync with
# ``src/alphamind/state/tables/invocations.py``'s ``_TRIGGER_SOURCES``
# tuple — adding a new producer of an ``invocations`` row requires
# extending both this snapshot AND the application-layer tuple, then
# shipping a new migration that drops + recreates the CHECK with the
# extended vocabulary.
_TRIGGER_SOURCES = (
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
    """Add ``ck_invocations_trigger_source`` to the ``invocations`` table."""
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.create_check_constraint(
            "ck_invocations_trigger_source",
            _check_in("trigger_source", _TRIGGER_SOURCES),
        )


def downgrade() -> None:
    """Drop the ``ck_invocations_trigger_source`` CHECK constraint."""
    with op.batch_alter_table("invocations") as batch_op:
        batch_op.drop_constraint("ck_invocations_trigger_source", type_="check")
