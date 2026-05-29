"""extend ck_positions_status with CANCELLED + backfill stranded rows (ALP-744)

Revision ID: b6e1f9a4c3d2
Revises: e2c4a8f1d5b3
Create Date: 2026-05-29 18:30:00.000000

ALP-744 adds the terminal ``PositionStatus.CANCELLED`` value: a position whose
entry order never filled and was then cancelled (bracket dissolved) had no
terminal state of its own — the fill path only does PENDING → OPEN → CLOSED, so
such a position stranded in PENDING forever (never priced by the OPEN-only
underlying-quote stream, perpetually warning in the snapshot assembler).

The application enum + the cancel/dissolve write path now drive PENDING →
CANCELLED, but the schema-layer ``ck_positions_status`` CHECK constraint —
installed by ``c8e3f4a2b1d6_add_positions`` from the then-current vocabulary —
still rejects ``'CANCELLED'``. The first attempt to persist a cancelled
position in production would trip the constraint and the cancel writeback would
fail. This migration widens the constraint to admit the new value.

It also backfills the two rows already stranded in production (ALP-744 prod
evidence, 2026-05-29): both never filled (empty execution_history, NULL
entry_timestamp), both with a DISSOLVED bracket and all orders CANCELLED — the
exact never-opened-then-cancelled shape the new terminal state represents. The
``status = 'PENDING'`` guard makes the backfill idempotent and a no-op on any
deploy where the rows have already moved on.

SQLite does not support ``ALTER TABLE DROP CONSTRAINT``, so the constraint is
swapped via :func:`op.batch_alter_table` (table copy-and-rename), mirroring
``c3b7d5a8e4f2`` / ``b8c9d2e4f7a1``. ``orders`` / ``brackets`` / ``theses`` carry
``ON DELETE RESTRICT`` FKs *into* ``positions``; the intermediate ``DROP TABLE``
the recreate performs is safe because the migration environment (``env.py``)
disables FK enforcement for the whole run (see ``d3f6a1c7e9b2``).

The downgrade reverses the constraint but refuses to run if any row still
carries ``status='CANCELLED'`` — otherwise the older constraint would silently
forbid a value the upgraded constraint had been admitting, leaving the schema
inconsistent (same posture as ``c3b7d5a8e4f2``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6e1f9a4c3d2"
down_revision: str | Sequence[str] | None = "e2c4a8f1d5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the PositionStatus vocabulary prior to this migration (the set
# ``c8e3f4a2b1d6`` installed). Adding new members later requires extending both
# the application enum AND shipping a new migration on top of this one.
_STATUSES_BEFORE = ("PENDING", "OPEN", "CLOSED")
_NEW_STATUS = "CANCELLED"

# The two positions stranded in PENDING in production (ALP-744 evidence,
# 2026-05-29) — both never filled, both with a DISSOLVED bracket.
_STRANDED_POSITION_IDS = (
    "POS-SCHW-3241748bbea254ca86e646eb1022adb7",
    "POS-ZS-ecd62928cd9e5e6e9e7c14e9971a7d08",
)


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Widen ck_positions_status with CANCELLED, then backfill stranded rows."""
    new_statuses = (*_STATUSES_BEFORE, _NEW_STATUS)
    with op.batch_alter_table("positions") as batch_op:
        batch_op.drop_constraint("ck_positions_status", type_="check")
        batch_op.create_check_constraint(
            "ck_positions_status",
            _check_in("status", new_statuses),
        )

    # Backfill the known stranded rows. The WHERE clause mirrors the runtime
    # cancel path's guard (``_cancel_never_filled_position``): only a PENDING row
    # with an empty execution_history becomes CANCELLED. CANCELLED means
    # never-filled, and the PositionRecord validator rejects a CANCELLED row that
    # carries fills — so were a named row to have gained a fill between the
    # 2026-05-29 evidence snapshot and the deploy, skipping it here avoids
    # writing a row the read codec would later reject (the ALP-731 class of
    # unreadable-row corruption). The status guard also keeps the statement
    # idempotent. Variable values are bound (the id list / status literal),
    # matching the downgrade's parameterized style.
    bind = op.get_bind()
    id_params = {f"pid{idx}": pid for idx, pid in enumerate(_STRANDED_POSITION_IDS)}
    placeholders = ", ".join(f":{name}" for name in id_params)
    bind.execute(
        sa.text(
            "UPDATE positions SET status = :new_status "
            "WHERE status = 'PENDING' "
            "AND execution_history_json IN ('[]', '') "
            f"AND position_id IN ({placeholders})"
        ),
        {"new_status": _NEW_STATUS, **id_params},
    )


def downgrade() -> None:
    """Revert to the pre-CANCELLED status vocabulary.

    Refuses to downgrade when any row still carries ``status='CANCELLED'``, so a
    routine ``alembic downgrade -1`` cannot leave rows that violate the
    reinstated constraint.
    """
    bind = op.get_bind()
    surviving = bind.execute(
        sa.text("SELECT COUNT(*) FROM positions WHERE status = :st"),
        {"st": _NEW_STATUS},
    ).scalar_one()
    if surviving:
        raise RuntimeError(
            f"refusing to downgrade revision {revision}: "
            f"{surviving} position row(s) with status='{_NEW_STATUS}' would be "
            "invalidated by the older CHECK constraint. Resolve them manually "
            "before re-running the downgrade."
        )
    with op.batch_alter_table("positions") as batch_op:
        batch_op.drop_constraint("ck_positions_status", type_="check")
        batch_op.create_check_constraint(
            "ck_positions_status",
            _check_in("status", _STATUSES_BEFORE),
        )
