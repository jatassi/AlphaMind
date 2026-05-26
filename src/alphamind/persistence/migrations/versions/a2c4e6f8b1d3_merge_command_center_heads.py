"""merge ALP-128 command-center forked heads

Revision ID: a2c4e6f8b1d3
Revises: a1b2c3d4e5f6, f1e2a3b4c5d6
Create Date: 2026-05-26 00:00:02.000000

ALP-128 wave-1 introduced two parallel migrations that both descend from the
previous singleton head (``e2f7a1c3b8d9``):

* ``a1b2c3d4e5f6`` — extend ``activity_log.event_type`` CHECK with
  ``PROFILE_SWITCHED`` (story 01b / ALP-663).
* ``f1e2a3b4c5d6`` — add the ``monitor_halt_mode`` singleton table
  (story 01c / ALP-665).

The two stories landed independently and both legally chained off the same
prior head. The resulting forked graph leaves Alembic with multiple heads,
which breaks ``alembic upgrade head`` (raises ``CommandError: Multiple head
revisions``) and therefore every test that drives a schema build through
``command.upgrade(cfg, "head")``.

This migration is a no-op merge: it reunites the two branches into a single
head so downstream migrations can chain off one revision again. There is no
schema change to apply — both branch tips already produced their tables and
constraints.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a2c4e6f8b1d3"
down_revision: str | Sequence[str] | None = ("a1b2c3d4e5f6", "f1e2a3b4c5d6")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op — merging two branch heads requires no schema change."""


def downgrade() -> None:
    """No-op — splitting back into two heads is purely a graph operation."""
