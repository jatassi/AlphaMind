"""orders.direction NULLABLE for MLEG envelopes

Revision ID: e2f7a1c3b8d9
Revises: d8a3f2c7b9e4
Create Date: 2026-05-25 00:00:01.000000

ALP-614 makes the order-level direction honestly optional: a multi-leg
strategy MLEG envelope has no coherent envelope-level side because the
broker adapter sends per-leg ``side`` / ``position_intent`` from each
:class:`StrategyLeg`. The previous shape persisted the envelope with an
inert ``OrderDirection.BUY`` placeholder no consumer could meaningfully
read.

The upgrade:

* Drops the ``NOT NULL`` on ``orders.direction`` so MLEG rows persist
  ``NULL``.
* Backfills existing MLEG rows (``order_class = 'MLEG'``) to ``NULL`` —
  the prior BUY/SELL value was placeholder, not signal.

The downgrade refuses to run if any row still carries ``direction IS
NULL`` because a routine downgrade would silently violate the
NOT-NULL contract.

SQLite supports ``ALTER COLUMN`` only via the batch-alter helper.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2f7a1c3b8d9"
down_revision: str | Sequence[str] | None = "d8a3f2c7b9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the NOT NULL constraint, then backfill existing MLEG rows to NULL.

    Order matters: the UPDATE has to run against a nullable column, so the
    constraint flip comes first.
    """
    with op.batch_alter_table("orders") as batch_op:
        batch_op.alter_column(
            "direction",
            existing_type=sa.Text(),
            nullable=True,
        )
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE orders SET direction = NULL WHERE order_class = 'MLEG'"))


def downgrade() -> None:
    """Restore ``direction`` NOT NULL.

    Refuses to run if any ``direction IS NULL`` row survives, since the
    older NOT NULL constraint would reject them and existing MLEG envelopes
    have no honest non-NULL value to fall back to.
    """
    bind = op.get_bind()
    surviving = bind.execute(
        sa.text("SELECT COUNT(*) FROM orders WHERE direction IS NULL")
    ).scalar_one()
    if surviving:
        raise RuntimeError(
            f"refusing to downgrade revision {revision}: "
            f"{surviving} order(s) carry direction IS NULL which the older "
            "NOT NULL constraint would reject. Backfill manually before "
            "re-running the downgrade (e.g., "
            "``UPDATE orders SET direction = 'BUY' WHERE direction IS NULL``)."
        )
    with op.batch_alter_table("orders") as batch_op:
        batch_op.alter_column(
            "direction",
            existing_type=sa.Text(),
            nullable=False,
        )
