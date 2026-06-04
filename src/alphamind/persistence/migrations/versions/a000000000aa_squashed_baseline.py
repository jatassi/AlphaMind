"""squashed_baseline — the single full-schema genesis baseline (ALP-843 / W0a)

Revision ID: a000000000aa
Revises:
Create Date: 2026-06-04 00:00:00.000000

The broker-boundary redesign cuts over on a **fresh account + fresh DB**
(genesis-cutover runbook), so the 46-migration incremental chain that grew the
legacy schema is retired and collapsed into this one baseline. ``down_revision``
is ``None``: ``alembic upgrade head`` on an empty database stands up the *entire*
new-design schema in a single step.

The schema is built directly from the SQLAlchemy metadata — the single source of
truth the Alembic env already targets for autogenerate — rather than hand-
transcribed ``op.create_table`` calls. Driving creation from ``Base.metadata``
(state-persistence + collector/reference/pipeline tables) and
``CommandCenterBase.metadata`` (the separate command-center session factory)
guarantees the baseline can never drift from the models: the new-design tables
(``broker_event_log``, ``thesis_pnl_ledger``, ``capital_reservations``,
``position_greeks``) and the ``bracket_legs.enforcement_binding`` column are
created with the full CHECK-constraint vocabularies their models declare, which
is exactly what avoids the migration CHECK-vocab trap (a constraint built from a
partial enum would retroactively differ on a fresh DB when a member is added).

Metadata-driven creation also folds in the five collector/reference tables that
the legacy chain never materialized but the models and collectors have always
expected (``borrow_cost_daily`` / ``borrow_cost_intraday`` /
``earnings_estimate_revisions`` / ``short_interest_snapshots`` /
``short_volume_daily``) — on a genesis DB the right state is the schema the code
expects, so the baseline resolves that latent drift rather than perpetuating it.
"""

import warnings
from collections.abc import Sequence

from alembic import op
from sqlalchemy.exc import SAWarning

# Import the metadata-bearing packages for their registration side effects.
# ``alphamind.state.tables`` attaches every state-persistence + new-design table
# to ``Base.metadata``; ``persistence.models`` carries the collector / reference
# / pipeline tables on the same ``Base``; the command-center package owns a
# SEPARATE ``CommandCenterBase.metadata`` — importing ``CommandCenterBase`` from
# that module triggers the same registration side effects.
import alphamind.state.tables  # noqa: F401
from alphamind.command_center.persistence.tables import CommandCenterBase
from alphamind.persistence.models import Base

# revision identifiers, used by Alembic.
revision: str = "a000000000aa"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the entire new-design schema on an empty database."""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    CommandCenterBase.metadata.create_all(bind=bind)


def downgrade() -> None:
    """Drop the entire schema (genesis baseline — nothing precedes it).

    The state-persistence tables form a cyclic FK graph
    (positions ↔ theses ↔ brackets ↔ orders), which ``drop_all`` cannot
    topologically sort. Drop order is irrelevant here: the Alembic env runs
    migrations with ``PRAGMA foreign_keys=OFF``, so the unsortable-cycle warning
    is benign and suppressed.
    """
    bind = op.get_bind()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SAWarning)
        CommandCenterBase.metadata.drop_all(bind=bind)
        Base.metadata.drop_all(bind=bind)
