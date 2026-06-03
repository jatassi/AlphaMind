"""add orders.client_order_id + widen ck_orders_status with PENDING_SUBMIT (ALP-836)

Revision ID: b4d9f1a7c2e6
Revises: a7e3f1c9d2b4
Create Date: 2026-06-03 17:30:00.000000

ALP-836 makes order persistence atomicity-first: a durable ``orders`` row is
committed **before** the broker dispatch, keyed by the deterministic
``client_order_id`` (= the originating command_id) the broker fill carries, so a
lost post-submit commit can never strand a live broker order without a local row.
Two schema changes back this:

* ``orders.client_order_id`` — a nullable, unique-when-present column holding the
  broker client_order_id the primary order of a command was submitted under. A
  fill / reconcile pass resolves the durable pre-committed row by this key before
  the real ``alpaca_order_id`` has been backfilled. The unique index admits many
  NULLs (SQLite treats NULLs as distinct in a UNIQUE index) — native-bracket
  protective children carry Alpaca-generated client_order_ids and stay NULL — while
  enforcing one row per real key (the idempotent-replay backstop).

* ``ck_orders_status`` widened with ``PENDING_SUBMIT`` — the durable-intent status
  of an order committed locally but not yet accepted by the broker. The
  application enum + the new pre-dispatch write path drive PENDING_SUBMIT →
  PENDING; the schema CHECK installed by ``c6e3f4a5b8d9_add_orders`` from the
  then-current vocabulary still rejects it, so the first pre-commit in production
  would trip the constraint. This widens it. (Mind the CHECK-vocab-from-live-enum
  trap: ``c6e3f4a5b8d9`` builds the CHECK by iterating the live ``OrderStatus``
  enum, so a freshly-built DB already bakes PENDING_SUBMIT in — the existing
  production table, created when the enum lacked it, carries the narrow constraint
  this migration actually widens.)

SQLite supports neither ``ALTER TABLE DROP CONSTRAINT`` nor ``ALTER COLUMN``, so
both the column add and the constraint swap go through :func:`op.batch_alter_table`
(table copy-and-rename), mirroring ``b6e1f9a4c3d2`` / ``e2f7a1c3b8d9``. The
unique index is created outside the batch (the column must exist first).
``orders`` carries ``ON DELETE RESTRICT`` FKs into ``positions`` / ``brackets``;
the intermediate ``DROP TABLE`` the recreate performs is safe because the
migration environment (``env.py``) disables FK enforcement for the whole run.

The downgrade reverses both, refusing to run if any row still carries
``status='PENDING_SUBMIT'`` (the older constraint would silently forbid a value
the upgraded constraint had been admitting) — same posture as ``b6e1f9a4c3d2``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4d9f1a7c2e6"
down_revision: str | Sequence[str] | None = "a7e3f1c9d2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Snapshot of the OrderStatus vocabulary prior to this migration (the set
# ``c6e3f4a5b8d9_add_orders`` installed). Adding new members later requires
# extending both the application enum AND shipping a new migration on top of this.
_STATUSES_BEFORE = ("PENDING", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED", "REJECTED")
_NEW_STATUS = "PENDING_SUBMIT"
_INDEX_NAME = "ix_orders_client_order_id"


def _check_in(column: str, values: Sequence[str]) -> str:
    rendered = ", ".join(repr(v) for v in values)
    return f"{column} IN ({rendered})"


def upgrade() -> None:
    """Add client_order_id, widen ck_orders_status with PENDING_SUBMIT, index it."""
    new_statuses = (_NEW_STATUS, *_STATUSES_BEFORE)
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("client_order_id", sa.Text(), nullable=True))
        batch_op.drop_constraint("ck_orders_status", type_="check")
        batch_op.create_check_constraint("ck_orders_status", _check_in("status", new_statuses))
    op.create_index(_INDEX_NAME, "orders", ["client_order_id"], unique=True)


def downgrade() -> None:
    """Drop the index + column and narrow ck_orders_status back.

    Refuses to downgrade when any row still carries ``status='PENDING_SUBMIT'`` so a
    routine ``alembic downgrade -1`` cannot leave rows that violate the reinstated
    constraint.
    """
    bind = op.get_bind()
    surviving = bind.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE status = :st"),
        {"st": _NEW_STATUS},
    ).scalar_one()
    if surviving:
        raise RuntimeError(
            f"refusing to downgrade revision {revision}: "
            f"{surviving} order row(s) with status='{_NEW_STATUS}' would be "
            "invalidated by the older CHECK constraint. Resolve them manually "
            "(complete the broker backfill or cancel them) before re-running the "
            "downgrade."
        )
    op.drop_index(_INDEX_NAME, table_name="orders")
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_constraint("ck_orders_status", type_="check")
        batch_op.create_check_constraint("ck_orders_status", _check_in("status", _STATUSES_BEFORE))
        batch_op.drop_column("client_order_id")
