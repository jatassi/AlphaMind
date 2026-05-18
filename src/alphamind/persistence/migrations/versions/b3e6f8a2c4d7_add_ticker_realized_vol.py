"""add_ticker_realized_vol

Revision ID: b3e6f8a2c4d7
Revises: e7b1c3a5d2f4
Create Date: 2026-05-18 06:00:00.000000

Adds the per-ticker realized-volatility substrate table (story ALP-530):

* ``ticker_realized_vol`` — one row per ``(ticker, as_of_date)`` carrying
  the trailing 30-trading-day realized vol, the producing
  ``invocation_id`` (FK to ``invocations.invocation_id``), and the
  wall-clock ``computed_at`` timestamp.

Same-day re-invocations upsert on the composite primary key
``(ticker, as_of_date)``; the index ``ix_ticker_realized_vol_as_of_date``
supports the "latest row per ticker" read path. ``ticker`` FKs to
``asset_universe.ticker`` with ``ON DELETE RESTRICT`` (parent rows are
append-only). The realized-vol substrate is consumed downstream by the
Phase 1 Reg T attribution wedge, the continuous monitor's greeks-refresh
``FixtureIvProvider``, and the paper-evaluation harness's
``MapVolLookup`` — see
``alphamind.distillation.realized_vol.read_realized_vol_map``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e6f8a2c4d7"
down_revision: str | Sequence[str] | None = "e7b1c3a5d2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``ticker_realized_vol`` and its as_of_date index."""
    op.create_table(
        "ticker_realized_vol",
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("as_of_date", sa.Text(), nullable=False),
        sa.Column("trailing_30d_realized_vol", sa.Float(), nullable=False),
        sa.Column("invocation_id", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("ticker", "as_of_date"),
        sa.ForeignKeyConstraint(
            ["ticker"],
            ["asset_universe.ticker"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"],
            ["invocations.invocation_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_ticker_realized_vol_as_of_date",
        "ticker_realized_vol",
        ["as_of_date"],
    )


def downgrade() -> None:
    """Drop ``ticker_realized_vol`` and its index."""
    op.drop_index(
        "ix_ticker_realized_vol_as_of_date",
        table_name="ticker_realized_vol",
    )
    op.drop_table("ticker_realized_vol")
