"""extend_distillation_ticker_baseline_kinds

Revision ID: 0aa4fc8b5647
Revises: 71d9125161ee
Create Date: 2026-04-27 00:00:00.000000

Extends the ``distillation_ticker_baseline.baseline_kind`` CHECK constraint
to admit ``atm_iv`` alongside the existing ``volume`` / ``atr`` / ``spread``
/ ``sentiment`` kinds.

Story 02-distillation-layer/08b adds the IV-rank baseline state per ticker
(252-day rolling ATM-IV history). Per ``threshold-calibration.md`` the
existing ``distillation_ticker_baseline`` schema fits the IV-rank usage
natively (mean/stdev of trailing IV with the percentile derived on read);
extending the CHECK constraint avoids a new state-table family.

SQLite doesn't support ``ALTER TABLE ... DROP CONSTRAINT``, so the
upgrade rebuilds the table via ``batch_alter_table``. The downgrade
mirrors the rebuild in reverse, restricting back to the original four kinds.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0aa4fc8b5647"
down_revision: str | Sequence[str] | None = "71d9125161ee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_CHECK = "baseline_kind IN ('volume', 'atr', 'spread', 'sentiment')"
_NEW_CHECK = "baseline_kind IN ('volume', 'atr', 'spread', 'sentiment', 'atm_iv')"
_CONSTRAINT_NAME = "ck_distillation_ticker_baseline_baseline_kind"


def upgrade() -> None:
    """Replace the CHECK constraint to admit ``atm_iv``.

    ``batch_alter_table`` selects ``recreate`` automatically — letting
    alembic introspect the live table preserves the existing FK on
    ``ticker`` without an explicit re-declaration here.
    """
    with op.batch_alter_table("distillation_ticker_baseline") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            sa.text(_NEW_CHECK),
        )


def downgrade() -> None:
    """Restore the CHECK constraint to the original four kinds."""
    with op.batch_alter_table("distillation_ticker_baseline") as batch_op:
        batch_op.drop_constraint(_CONSTRAINT_NAME, type_="check")
        batch_op.create_check_constraint(
            _CONSTRAINT_NAME,
            sa.text(_OLD_CHECK),
        )
