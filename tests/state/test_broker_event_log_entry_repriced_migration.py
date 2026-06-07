"""Tests for the ``ENTRY_REPRICED`` CHECK-widen migration (ALP-867).

``a867er0000cc`` widens the ``broker_event_log.event_type`` CHECK to admit the new
``ENTRY_REPRICED`` member, re-parented onto ALP-865's ``a865wm0000bb`` so the chain
stays linear (a single head).

The genesis baseline is metadata-driven, so a fresh ``upgrade head`` already builds
the wide CHECK and a forward-only upgrade cannot *demonstrate* the widen. The widen
is therefore exercised via **downgrade → reject → upgrade → accept** (the CHECK-vocab
trap): step the migration down one revision (narrowing the CHECK), confirm an
``ENTRY_REPRICED`` insert is rejected, step back up (widening), confirm it is now
accepted.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from alphamind.persistence.session import make_engine

_BASELINE_REVISION = "a000000000aa"
_PARENT_REVISION = "a865wm0000bb"
_MIGRATION_REVISION = "a867er0000cc"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _try_insert_entry_repriced(db_path: Path) -> bool:
    """Attempt to insert an ``ENTRY_REPRICED`` row; return True iff it was rejected.

    All link columns are NULL (a reprice carries no broker-carried attribution), so
    only the ``event_type`` CHECK can reject the row.
    """
    eng = make_engine(str(db_path))
    try:
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO broker_event_log "
                        "(event_key, event_type, raw_payload_json, captured_at) "
                        "VALUES ('revt-mig-test', 'ENTRY_REPRICED', '{}', "
                        "'2026-06-06T00:00:00+00:00')"
                    )
                )
        except Exception:
            return True
        return False
    finally:
        eng.dispose()


class TestEntryRepricedMigration:
    def test_migration_is_single_head_parented_on_a865(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        # A single head (no branched history — two Alembic heads break ``upgrade head``).
        # The head advances as later migrations land; the invariant is exactly 1 head.
        assert len(list(script.get_heads())) == 1
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        # Re-parented onto ALP-865's migration (a linear chain), not the baseline.
        assert rev.down_revision == _PARENT_REVISION

    def test_upgrade_head_check_admits_entry_repriced(self, tmp_path: Path) -> None:
        """A fresh ``upgrade head`` (metadata-driven baseline + the guarded no-op
        upgrade) yields a CHECK that already admits ``ENTRY_REPRICED``."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _try_insert_entry_repriced(db_path) is False  # accepted

    def test_downgrade_rejects_then_upgrade_accepts_entry_repriced(self, tmp_path: Path) -> None:
        """The CHECK-vocab round-trip: downgrade narrows the CHECK (reject), upgrade
        widens it (accept) — the behaviour a forward-only upgrade can't show."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        # Step down to the parent of this migration (a865wm0000bb), which pre-dates
        # ENTRY_REPRICED. Using the explicit revision (not ``-1``) keeps the test
        # stable as later migrations extend the chain above a867er0000cc.
        command.downgrade(cfg, _PARENT_REVISION)
        assert _try_insert_entry_repriced(db_path) is True

        # Step back up: the CHECK is widened, so the member is now accepted.
        command.upgrade(cfg, "head")
        assert _try_insert_entry_repriced(db_path) is False

    def test_downgrade_one_step_lands_on_the_parent_revision(self, tmp_path: Path) -> None:
        """``downgrade -1`` from head lands on ALP-865's migration — the watermark
        table + ledger cursor survive (only the CHECK widen is reverted)."""
        from sqlalchemy import inspect

        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
            ledger_cols = {c["name"] for c in inspect(eng).get_columns("thesis_pnl_ledger")}
        finally:
            eng.dispose()
        # ALP-865's objects are untouched by this migration's down-step.
        assert "projection_rebuild_watermark" in tables
        assert "last_derived_event_seq" in ledger_cols
        assert "broker_event_log" in tables
