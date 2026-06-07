"""Tests for the ``ANALYSIS_PIPELINE`` source CHECK-widen migration (ALP-899).

``b899tr0000ff`` widens the ``activity_log.source`` CHECK to admit the new
``EventSource.ANALYSIS_PIPELINE`` member (the deliberative pipeline's
thesis-resolution step emits ``THESIS_RESOLVED`` under it), parented onto the
current head so the chain stays linear (a single head).

The genesis baseline is metadata-driven, so a fresh ``upgrade head`` already
builds the wide CHECK and a forward-only upgrade cannot *demonstrate* the
widen. The widen is therefore exercised via **downgrade → reject → upgrade →
accept** (the CHECK-vocab trap): step the migration down one revision
(narrowing the CHECK), confirm an ``ANALYSIS_PIPELINE`` insert is rejected,
step back up (widening), confirm it is now accepted.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from alphamind.persistence.session import make_engine

_PARENT_REVISION = "b001fc0000ee"
_MIGRATION_REVISION = "b899tr0000ff"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _try_insert_analysis_pipeline_source(db_path: Path) -> bool:
    """Attempt to insert an ``ANALYSIS_PIPELINE``-source row; True iff rejected.

    FK enforcement is disabled for this insert (``PRAGMA foreign_keys=OFF``) so
    the NOT-NULL ``invocation_id`` FK does not mask the CHECK — only the
    ``source`` CHECK can reject the row. CHECK constraints are enforced
    regardless of the FK pragma.
    """
    eng = make_engine(str(db_path))
    try:
        try:
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(
                    text(
                        "INSERT INTO activity_log "
                        "(entry_id, invocation_id, entry_at, event_type, event_group, "
                        "source, detail_json) "
                        "VALUES ('alp-mig-test', 'inv-x', '2026-06-07T00:00:00+00:00', "
                        "'THESIS_RESOLVED', 'THESIS', 'ANALYSIS_PIPELINE', '{}')"
                    )
                )
        except Exception:
            return True
        return False
    finally:
        eng.dispose()


class TestAnalysisPipelineSourceMigration:
    def test_migration_is_single_head_parented_on_current_head(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        # Exactly one head — two Alembic heads break ``upgrade head``.
        assert len(list(script.get_heads())) == 1
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        assert rev.down_revision == _PARENT_REVISION

    def test_upgrade_head_check_admits_analysis_pipeline(self, tmp_path: Path) -> None:
        """A fresh ``upgrade head`` (metadata-driven baseline + the guarded no-op
        upgrade) yields a CHECK that already admits ``ANALYSIS_PIPELINE``."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _try_insert_analysis_pipeline_source(db_path) is False  # accepted

    def test_downgrade_rejects_then_upgrade_accepts(self, tmp_path: Path) -> None:
        """The CHECK-vocab round-trip: downgrade narrows the CHECK (reject),
        upgrade widens it (accept) — the behaviour a forward-only upgrade can't
        show."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        command.downgrade(cfg, _PARENT_REVISION)
        assert _try_insert_analysis_pipeline_source(db_path) is True

        command.upgrade(cfg, "head")
        assert _try_insert_analysis_pipeline_source(db_path) is False
