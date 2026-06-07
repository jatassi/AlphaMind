"""activity_log_source_account_activities — widen the source CHECK (ALP-918)

Revision ID: c918aa0000ff
Revises: b899tr0000ff
Create Date: 2026-06-07 12:00:00.000000

ALP-918 makes the option-lifecycle poll emit ``POSITION_CLOSED`` activity-log
entries on an option expiry / assignment / exercise under the new
``EventSource.ACCOUNT_ACTIVITIES_PROCESSOR`` source (so the closed-position
thesis resolver can resolve the thesis instead of leaving it ``ACTIVE`` forever).
The ``activity_log.source`` CHECK (``ck_activity_log_source``) is built from the
live enum, so a DB whose ``activity_log`` was created before this member existed
carries a CHECK that rejects ``ACCOUNT_ACTIVITIES_PROCESSOR`` — this migration
widens it.

**Parented on** ``b899tr0000ff`` (the ALP-899 source-widen) — re-confirmed as
the single head so the chain stays linear (two Alembic heads break
``upgrade head``).

**Idempotent + drift-free on a fresh DB.** The genesis baseline
(``a000000000aa``) is metadata-driven (``Base.metadata.create_all`` against the
*live* models), so on any fresh DB it already builds the CHECK with the full live
vocabulary — ``ACCOUNT_ACTIVITIES_PROCESSOR`` included. ``upgrade`` therefore
guards on whether the persisted CHECK already lists the member and **returns
without rebuilding** when it does, so a fresh ``upgrade head`` is a no-op that
leaves the metadata-built table untouched. The table rebuild runs only on a
pre-existing DB whose CHECK is still narrow.

The vocab is **hardcoded as of this revision** (not read from the live enum —
the migration CHECK-vocab trap): ``downgrade`` must narrow the CHECK back to
exactly the pre-``ACCOUNT_ACTIVITIES_PROCESSOR`` set, which a live-enum read
could not produce once the member exists. Tested via downgrade→reject→upgrade→
accept (a forward-only upgrade can't demonstrate the widen, since the baseline
already builds the wide CHECK on a fresh test DB).

A SQLite CHECK change is a table rebuild, which renumbers the implicit ``rowid``
ALP-870 surfaces as ``activity_log.event_seq``. No persisted watermark tracks
this column (the emergency receiver re-derives ``max(event_seq)`` at each process
start), so there is nothing to reset; the rebuild runs only on the narrow-CHECK
path anyway.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c918aa0000ff"
down_revision: str | Sequence[str] | None = "b899tr0000ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "activity_log"
_CHECK_NAME = "ck_activity_log_source"
_NEW_MEMBER = "ACCOUNT_ACTIVITIES_PROCESSOR"

# The source vocabulary frozen as of this revision (the CHECK-vocab trap: never
# read from the live enum, or downgrade could not narrow the constraint).
_OLD_SOURCES: tuple[str, ...] = (
    "FILL_PROCESSOR",
    "COMMAND_EXECUTOR",
    "BRACKET_MANAGER",
    "MARGIN_MONITOR",
    "BORROW_ACCRUAL_MONITOR",
    "GUARDRAIL_LAYER",
    "CORPORATE_ACTION_PROCESSOR",
    "CONFIG_RELOAD",
    "OPERATOR_CONSOLE",
    "DISTILLATION_ORCHESTRATOR",
    "ANALYSIS_PIPELINE",
)
_NEW_SOURCES: tuple[str, ...] = (*_OLD_SOURCES, _NEW_MEMBER)


def _persisted_check_has_member(bind: sa.engine.Connection, member: str) -> bool:
    """True if ``activity_log``'s persisted CHECK DDL already lists *member*."""
    sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _TABLE},
    ).scalar_one_or_none()
    return sql is not None and f"'{member}'" in sql


def _check_condition(values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"source IN ({rendered})"


def _rebuild_source_check(values: tuple[str, ...]) -> None:
    """Recreate ``activity_log`` with the ``source`` CHECK over *values*.

    SQLite cannot ALTER a CHECK in place, so this is a table rebuild via
    ``batch_alter_table``: drop the named CHECK and add it back over the target
    vocabulary. Migrations run with ``PRAGMA foreign_keys=OFF`` (see the alembic
    env), so the intermediate ``DROP TABLE`` does not trip the incoming FKs.
    """
    with op.batch_alter_table(_TABLE, recreate="always") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, _check_condition(values))


def upgrade() -> None:
    """Widen the CHECK to include ``ACCOUNT_ACTIVITIES_PROCESSOR`` (no-op if present)."""
    if _persisted_check_has_member(op.get_bind(), _NEW_MEMBER):
        return
    _rebuild_source_check(_NEW_SOURCES)


def downgrade() -> None:
    """Narrow the CHECK to the pre-``ACCOUNT_ACTIVITIES_PROCESSOR`` set (no-op if narrow)."""
    if not _persisted_check_has_member(op.get_bind(), _NEW_MEMBER):
        return
    _rebuild_source_check(_OLD_SOURCES)
