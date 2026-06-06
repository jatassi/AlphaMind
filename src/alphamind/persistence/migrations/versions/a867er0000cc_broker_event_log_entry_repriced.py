"""broker_event_log_entry_repriced — widen the event_type CHECK (ALP-867)

Revision ID: a867er0000cc
Revises: a865wm0000bb
Create Date: 2026-06-06 00:00:00.000000

ALP-867 relocates the monitor's entry-window reprice writeback to the pipeline by
adding a new ``BrokerEventType.ENTRY_REPRICED`` event the monitor appends and the
pipeline projects. The ``broker_event_log.event_type`` CHECK constraint is built
from the live enum, so a DB whose ``broker_event_log`` was created before this
member existed carries a CHECK that rejects ``ENTRY_REPRICED`` — this migration
widens it.

**Parented on** ``a865wm0000bb`` (ALP-865's watermark migration), not the genesis
baseline — a linear chain, so ``alembic upgrade head`` never branches into two
heads (ALP-865 landed first; per the coordination note this story re-parents onto
it).

**Idempotent + drift-free on a fresh DB.** The genesis baseline (``a000000000aa``)
is metadata-driven (``Base.metadata.create_all`` against the *live* models), so on
any fresh DB it already builds the CHECK with the full live vocabulary —
``ENTRY_REPRICED`` included. ``upgrade`` therefore guards on whether the persisted
CHECK already lists the member and **returns without rebuilding** when it does, so a
fresh ``upgrade head`` is a no-op that leaves the metadata-built table untouched
(the ``test_mapper_fk_autogenerate`` head-equals-metadata invariant holds). The
table rebuild runs only on a pre-existing DB whose CHECK is still narrow.

The vocab is **hardcoded as of this revision** (not read from the live enum — the
migration CHECK-vocab trap): ``downgrade`` must narrow the CHECK back to exactly the
pre-``ENTRY_REPRICED`` set, which a live-enum read could not produce once the member
exists. Tested via downgrade→reject→upgrade→accept (a forward-only upgrade can't
demonstrate the widen, since the baseline already builds the wide CHECK on a fresh
test DB).

**Note on the rebuild + ``event_seq``.** A SQLite CHECK change is a table rebuild,
which renumbers the implicit ``rowid`` ALP-865 surfaces as ``event_seq`` (the same
caveat as ``VACUUM`` — see ``persistence/CLAUDE.md``). This is benign here: the
rebuild only runs on the narrow-CHECK path, i.e. a DB created before ``ENTRY_REPRICED``
existed, which in this project means a combined ``upgrade head`` where ``a865wm0000bb``
created the rebuild watermarks moments earlier at 0 / NULL — a re-scan from the start
is correct. If this migration is ever applied standalone to a DB with *advanced*
watermarks, reset ``projection_rebuild_watermark.last_projected_event_seq`` (0) and
``thesis_pnl_ledger.last_derived_event_seq`` (NULL) afterward per that gotcha.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a867er0000cc"
down_revision: str | Sequence[str] | None = "a865wm0000bb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "broker_event_log"
_CHECK_NAME = "ck_broker_event_log_event_type"
_NEW_MEMBER = "ENTRY_REPRICED"

# The event_type vocabulary frozen as of this revision (the CHECK-vocab trap: never
# read from the live enum, or downgrade could not narrow the constraint).
_OLD_EVENT_TYPES: tuple[str, ...] = (
    "FILL",
    "OPEXP",
    "OPEXC",
    "OPASN",
    "OPTRD",
    "CA_CASH_DIVIDEND",
    "CA_STOCK_DIVIDEND",
    "CA_SPLIT",
    "CA_REVERSE_SPLIT",
    "CA_UNIT_SPLIT",
    "CA_MERGER",
    "CA_SPINOFF",
    "CA_NAME_CHANGE",
    "CA_SYMBOL_CHANGE",
    "CA_WORTHLESS_REMOVAL",
    "CA_REDEMPTION",
    "TERMINAL_ORDER_STATUS",
)
_NEW_EVENT_TYPES: tuple[str, ...] = (*_OLD_EVENT_TYPES, _NEW_MEMBER)


def _persisted_check_has_member(bind: sa.engine.Connection, member: str) -> bool:
    """True if ``broker_event_log``'s persisted CHECK DDL already lists *member*."""
    sql = bind.execute(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _TABLE},
    ).scalar_one_or_none()
    return sql is not None and f"'{member}'" in sql


def _check_condition(values: tuple[str, ...]) -> str:
    rendered = ", ".join(f"'{v}'" for v in values)
    return f"event_type IN ({rendered})"


def _rebuild_event_type_check(values: tuple[str, ...]) -> None:
    """Recreate ``broker_event_log`` with the ``event_type`` CHECK over *values*.

    SQLite cannot ALTER a CHECK in place, so this is a table rebuild via
    ``batch_alter_table``: drop the named CHECK and add it back over the target
    vocabulary. Migrations run with ``PRAGMA foreign_keys=OFF`` (see the alembic
    env), so the intermediate ``DROP TABLE`` does not trip the incoming FKs.
    """
    with op.batch_alter_table(_TABLE, recreate="always") as batch_op:
        batch_op.drop_constraint(_CHECK_NAME, type_="check")
        batch_op.create_check_constraint(_CHECK_NAME, _check_condition(values))


def upgrade() -> None:
    """Widen the CHECK to include ``ENTRY_REPRICED`` (no-op if already present)."""
    if _persisted_check_has_member(op.get_bind(), _NEW_MEMBER):
        return
    _rebuild_event_type_check(_NEW_EVENT_TYPES)


def downgrade() -> None:
    """Narrow the CHECK back to the pre-``ENTRY_REPRICED`` vocabulary (no-op if already narrow)."""
    if not _persisted_check_has_member(op.get_bind(), _NEW_MEMBER):
        return
    _rebuild_event_type_check(_OLD_EVENT_TYPES)
