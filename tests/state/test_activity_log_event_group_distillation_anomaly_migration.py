"""Tests for the ``DISTILLATION_ANOMALY`` event_group CHECK-widen migration (ALP-933).

``e001eg0000aa`` widens the ``activity_log.event_group`` CHECK
(``ck_activity_log_event_group``) to admit the ``EventGroup.DISTILLATION_ANOMALY``
member. The distillation orchestrator emits ``DISTILLATION_ANOMALY_FLAG`` rows
whose ``event_group`` is exactly ``DISTILLATION_ANOMALY``; the companion ALP-879
migration widened only the *event_type* CHECK (and ALP-899 / ALP-918 the *source*
CHECK), silently leaving the *group* CHECK narrow on alembic-managed DBs created
before the model gained the member. Parented onto the current head so the chain
stays linear (a single head).

The genesis baseline is metadata-driven, so a fresh ``upgrade head`` already
builds the wide CHECK and a forward-only upgrade cannot *demonstrate* the widen.
The widen is therefore exercised via **downgrade → reject → upgrade → accept**
(the CHECK-vocab trap): step the migration down one revision (narrowing the
CHECK), confirm a ``DISTILLATION_ANOMALY`` insert is rejected, step back up
(widening), confirm it is now accepted.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from alphamind.persistence.session import make_engine

_PARENT_REVISION = "d001th0000aa"
_MIGRATION_REVISION = "e001eg0000aa"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _try_insert_distillation_anomaly_group(db_path: Path) -> bool:
    """Attempt to insert a ``DISTILLATION_ANOMALY``-group row; True iff rejected.

    FK enforcement is disabled for this insert (``PRAGMA foreign_keys=OFF``) so
    the NOT-NULL ``invocation_id`` FK does not mask the CHECK — only the
    ``event_group`` CHECK can reject the row. ``event_type`` and ``source`` use
    already-whitelisted members (``DISTILLATION_ANOMALY_FLAG`` /
    ``DISTILLATION_ORCHESTRATOR``) so only the group CHECK is under test. CHECK
    constraints are enforced regardless of the FK pragma.
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
                        "VALUES ('alp-mig-test', 'inv-x', '2026-06-08T00:00:00+00:00', "
                        "'DISTILLATION_ANOMALY_FLAG', 'DISTILLATION_ANOMALY', "
                        "'DISTILLATION_ORCHESTRATOR', '{}')"
                    )
                )
        except Exception:
            return True
        return False
    finally:
        eng.dispose()


class TestDistillationAnomalyEventGroupMigration:
    def test_migration_is_single_head_parented_on_current_head(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        # Exactly one head — two Alembic heads break ``upgrade head``.
        assert len(list(script.get_heads())) == 1
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        assert rev.down_revision == _PARENT_REVISION

    def test_upgrade_head_check_admits_distillation_anomaly(self, tmp_path: Path) -> None:
        """A fresh ``upgrade head`` (metadata-driven baseline + the guarded no-op
        upgrade) yields a CHECK that already admits ``DISTILLATION_ANOMALY``."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _try_insert_distillation_anomaly_group(db_path) is False  # accepted

    def test_downgrade_rejects_then_upgrade_accepts(self, tmp_path: Path) -> None:
        """The CHECK-vocab round-trip: downgrade narrows the CHECK (reject),
        upgrade widens it (accept) — the behaviour a forward-only upgrade can't
        show."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        command.downgrade(cfg, _PARENT_REVISION)
        assert _try_insert_distillation_anomaly_group(db_path) is True

        command.upgrade(cfg, "head")
        assert _try_insert_distillation_anomaly_group(db_path) is False
