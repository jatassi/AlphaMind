"""activity_log_event_group_distillation_anomaly — widen the event_group CHECK (ALP-933)

Revision ID: e001eg0000aa
Revises: d001th0000aa
Create Date: 2026-06-08 00:00:00.000000

The distillation orchestrator emits ``DISTILLATION_ANOMALY_FLAG`` activity-log
entries whose ``event_group`` is exactly ``EventGroup.DISTILLATION_ANOMALY``
(``build_activity_log_entry`` derives the group from ``EVENT_TYPE_TO_GROUP``).
The ``activity_log.event_group`` CHECK (``ck_activity_log_event_group``) is built
from the live enum, so a DB whose ``activity_log`` was created before this member
existed carries a CHECK that rejects ``DISTILLATION_ANOMALY`` — this migration
widens it.

**The omission this corrects.** Story 02e (ALP-877) added both
``EventGroup.DISTILLATION_ANOMALY`` and ``EventType.DISTILLATION_ANOMALY_FLAG``;
the combined feedback-loop migration ``c001fb0000ff`` (story 03 / ALP-879)
widened only the *event_type* CHECK, and ``b899tr0000ff`` / ``c918aa0000ff``
each widened only the *source* CHECK. No migration in the chain ever rebuilt the
*event_group* CHECK, so on an alembic-managed DB created before the model gained
the member, ``DISTILLATION_ANOMALY`` rows abort at ``session.commit()`` and every
distillation invocation fails before command execution.

**Parented on** ``d001th0000aa`` (story 02i's ``theses.invocation_id`` add) —
the current head; the already-applied ``c001fb0000ff`` cannot be edited in place.
Re-confirmed as the single head so the chain stays linear (two Alembic heads
break ``upgrade head``).

**Idempotent + drift-free on a fresh DB.** The genesis baseline
(``a000000000aa``) is metadata-driven (``Base.metadata.create_all`` against the
*live* models), so on any fresh DB it already builds the CHECK with the full live
vocabulary — ``DISTILLATION_ANOMALY`` included. ``upgrade`` therefore guards on
whether the persisted CHECK already lists the member and **returns without
rebuilding** when it does, so a fresh ``upgrade head`` is a no-op that leaves the
metadata-built table untouched (the head-equals-metadata autogenerate invariant
in ``tests/state/test_mapper_fk_autogenerate.py`` holds). The table rebuild runs
only on a pre-existing DB whose CHECK is still narrow.

The vocab is **hardcoded as of this revision** (not read from the live enum —
the migration CHECK-vocab trap): ``downgrade`` must narrow the CHECK back to
exactly the pre-``DISTILLATION_ANOMALY`` set, which a live-enum read could not
produce once the member exists. Tested via downgrade→reject→upgrade→accept (a
forward-only upgrade can't demonstrate the widen, since the baseline already
builds the wide CHECK on a fresh test DB).

A SQLite CHECK change is a table rebuild, which renumbers the implicit ``rowid``
ALP-870 surfaces as ``activity_log.event_seq``. No persisted watermark tracks
this column (the emergency receiver re-derives ``max(event_seq)`` at each process
start), so there is nothing to reset; the rebuild runs only on the narrow-CHECK
path anyway. Migrations run with ``PRAGMA foreign_keys=OFF`` (see the alembic
env), so the intermediate ``DROP TABLE`` does not trip the incoming FKs
(positions / orders / theses / invocations).
"""

from collections.abc import Sequence

from alembic import op

from alphamind.persistence.migrations._check_rebuild import (
    check_in,
    persisted_check_has_member,
    rebuild_check,
)

# revision identifiers, used by Alembic.
revision: str = "e001eg0000aa"
down_revision: str | Sequence[str] | None = "d001th0000aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "activity_log"
_CHECK_NAME = "ck_activity_log_event_group"
_NEW_MEMBER = "DISTILLATION_ANOMALY"

# The event_group vocabulary frozen as of this revision (the CHECK-vocab trap:
# never read from the live enum, or downgrade could not narrow the constraint).
_OLD_GROUPS: tuple[str, ...] = (
    "POSITION_LIFECYCLE",
    "ORDER_LIFECYCLE",
    "BRACKET",
    "THESIS",
    "CASH_AND_MARGIN",
    "RISK_AND_GUARDRAIL",
    "PM_DECISION",
    "CORPORATE_ACTION",
    "CONFIGURATION",
    "RECONCILIATION",
)
_NEW_GROUPS: tuple[str, ...] = (*_OLD_GROUPS, _NEW_MEMBER)


def _rebuild_event_group_check(values: tuple[str, ...]) -> None:
    """Recreate ``activity_log`` with the ``event_group`` CHECK over *values*.

    Routes the shared :func:`rebuild_check` mechanics over this migration's frozen
    *values*. Migrations run with ``PRAGMA foreign_keys=OFF`` (see the alembic env),
    so the intermediate ``DROP TABLE`` does not trip the incoming FKs.
    """
    rebuild_check(_TABLE, _CHECK_NAME, check_in("event_group", values))


def upgrade() -> None:
    """Widen the CHECK to include ``DISTILLATION_ANOMALY`` (no-op if already present)."""
    if persisted_check_has_member(op.get_bind(), _TABLE, _NEW_MEMBER):
        return
    _rebuild_event_group_check(_NEW_GROUPS)


def downgrade() -> None:
    """Narrow the CHECK to the pre-``DISTILLATION_ANOMALY`` set (no-op if already narrow)."""
    if not persisted_check_has_member(op.get_bind(), _TABLE, _NEW_MEMBER):
        return
    _rebuild_event_group_check(_OLD_GROUPS)
