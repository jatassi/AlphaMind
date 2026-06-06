"""Tests for the bound-projection-rebuild-scan migration (ALP-865).

``a865wm0000bb`` is parented on the squashed genesis baseline ``a000000000aa``
and adds the durable state the bounded rebuild watermarks read/advance:

* the ``projection_rebuild_watermark`` singleton (``last_projected_event_seq``);
* ``thesis_pnl_ledger.last_derived_event_seq`` (the per-thesis cursor).

The baseline is metadata-driven (``create_all`` against the live models), so a
fresh ``upgrade head`` already stands these up and the migration is a guarded
no-op there. The add/remove behaviour is exercised by stepping the migration
down and back up: a ``downgrade`` to the baseline drops both objects, ``upgrade
head`` re-creates them — the round-trip a production DB stamped at the baseline
before these columns existed will take. (The ALP-867 reprice migration now sits
above this one, so the down-step is reached via the baseline target rather than
``downgrade -1`` from head.)
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_BASELINE_REVISION = "a000000000aa"
_MIGRATION_REVISION = "a865wm0000bb"
_WATERMARK_TABLE = "projection_rebuild_watermark"
_LEDGER_TABLE = "thesis_pnl_ledger"
_LEDGER_CURSOR_COLUMN = "last_derived_event_seq"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _watermark_present(db_path: Path) -> bool:
    eng = make_engine(str(db_path))
    try:
        return _WATERMARK_TABLE in set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _ledger_cursor_present(db_path: Path) -> bool:
    eng = make_engine(str(db_path))
    try:
        cols = {c["name"] for c in inspect(eng).get_columns(_LEDGER_TABLE)}
        return _LEDGER_CURSOR_COLUMN in cols
    finally:
        eng.dispose()


class TestBoundProjectionRebuildScanMigration:
    def test_migration_parents_on_baseline_in_a_linear_chain(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        # A single head (no branched history — ``upgrade head`` fails on a branch);
        # the ALP-867 reprice migration re-parented onto this one rather than the
        # baseline, so it — not ``a865wm0000bb`` — is now the head, but the chain
        # stays linear and this revision still parents on the genesis baseline.
        assert len(script.get_heads()) == 1
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        assert rev.down_revision == _BASELINE_REVISION

    def test_upgrade_head_has_watermark_table_and_ledger_cursor(self, tmp_path: Path) -> None:
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _watermark_present(db_path)
        assert _ledger_cursor_present(db_path)

    def test_downgrade_below_this_revision_removes_both_objects(self, tmp_path: Path) -> None:
        # The ALP-867 reprice migration now sits above this one, so this revision's
        # down-step is no longer reachable by ``downgrade -1`` from head; downgrade to
        # the baseline (reverting both) to exercise this migration's ``downgrade``.
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _BASELINE_REVISION)
        assert not _watermark_present(db_path)
        assert not _ledger_cursor_present(db_path)
        # The ledger table itself survives — only the added column is dropped.
        eng = make_engine(str(db_path))
        try:
            assert _LEDGER_TABLE in set(inspect(eng).get_table_names())
        finally:
            eng.dispose()

    def test_upgrade_after_downgrade_re_adds_both_objects(self, tmp_path: Path) -> None:
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _BASELINE_REVISION)
        command.upgrade(cfg, "head")
        assert _watermark_present(db_path)
        assert _ledger_cursor_present(db_path)

    def test_watermark_singleton_check_rejects_non_current_id(self, tmp_path: Path) -> None:
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        f"INSERT INTO {_WATERMARK_TABLE}(id, last_projected_event_seq) "
                        "VALUES ('current', 7)"
                    )
                )
                value = conn.execute(
                    text(f"SELECT last_projected_event_seq FROM {_WATERMARK_TABLE}")
                ).scalar_one()
            assert value == 7
            # The singleton CHECK pins ``id = 'current'`` — a second-id row is rejected.
            rejected = False
            try:
                with eng.begin() as conn:
                    conn.execute(
                        text(
                            f"INSERT INTO {_WATERMARK_TABLE}(id, last_projected_event_seq) "
                            "VALUES ('other', 1)"
                        )
                    )
            except Exception:
                rejected = True
            assert rejected
        finally:
            eng.dispose()
