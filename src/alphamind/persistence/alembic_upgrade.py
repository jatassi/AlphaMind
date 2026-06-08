"""Shared ``alembic upgrade head`` helper (ALP-930 (D)).

The single shared body the feedback-loop verify script and the replay-harness
engine both call to land the full schema on a fresh scratch SQLite DB. Both
callers previously inlined the same ``Config(...) + command.upgrade(...)`` body;
collapsing them here keeps the upgrade invocation in one place.

The ``repo_root`` is a parameter rather than computed inside the util because the
two callers sit at different depths in the package tree and each already resolves
its own repo root — the util stays location-agnostic.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_to_head(db_path: str | Path, *, repo_root: Path) -> None:
    """Run ``alembic upgrade head`` against *db_path* using the packaged config.

    Builds an Alembic :class:`~alembic.config.Config` from ``repo_root /
    "alembic.ini"`` and the ``db=`` x-arg the migration ``env.py`` reads, then
    upgrades to head — landing the complete schema on a fresh on-disk DB.
    """
    cfg = Config(repo_root / "alembic.ini", cmd_opts=Namespace(x=[f"db={db_path}"]))
    command.upgrade(cfg, "head")


__all__ = ["upgrade_to_head"]
