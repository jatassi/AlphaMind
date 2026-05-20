"""clear_market_liquidity_for_normalization

Revision ID: 4f1e9b8c7a52
Revises: 2c5e9b7f8d12
Create Date: 2026-05-19 22:00:00.000000

ALP-575 changes the ``market_liquidity`` composite from a raw FRED sum to a
sum of per-component percentile ranks (0..300 scale). The pre-existing
``DistillationCompositeState`` rows for ``composite_kind = 'market_liquidity'``
were written on the old raw-FRED scale (typically ~10-50, dominated by VIX);
mixing them with new normalized-scale rows would render
``percentile_60d`` against the composite history meaningless until 60 new
rows accumulate to displace the legacy ones.

Pre-ALP-575 the composite was bootstrap-window data only (calibration state
``accumulating`` < 60 observations), so clearing the rows is safe — no
decision was ever taken against a calibrated raw-sum reading. The
downgrade is intentionally a no-op: there is no way to reconstruct the
deleted rows, and the prior code path will simply re-bootstrap.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f1e9b8c7a52"
down_revision: str | Sequence[str] | None = "2c5e9b7f8d12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Delete raw-sum ``market_liquidity`` composite rows so the new normalized
    sums rank against same-scale history only."""
    op.execute(
        sa.text(
            "DELETE FROM distillation_composite_state WHERE composite_kind = 'market_liquidity'"
        )
    )


def downgrade() -> None:
    """No-op — deleted rows cannot be reconstructed; the prior code path
    will re-bootstrap from the next invocation onward."""
