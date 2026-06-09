"""Tests for the positions.reprotection_needed migration (ALP-938).

``f001rp0000aa`` adds ``positions.reprotection_needed`` as
``INTEGER NOT NULL DEFAULT 0`` — the durable marker the post-fill-collection
re-bracket step keys off.

Assertions:
* The migration is the single head, parented on ``e001eg0000aa``.
* After ``upgrade head`` the column exists on the ``positions`` table.
* On the prod incremental path (a legacy ``positions`` with no
  ``reprotection_needed``), the migration adds the column and back-populates
  every existing row to 0 via the ``DEFAULT 0``.
* ``downgrade`` removes the column; a re-``upgrade`` restores it.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_PARENT_REVISION = "e001eg0000aa"
_MIGRATION_REVISION = "f001rp0000aa"
_COLUMN = "reprotection_needed"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _column_names(db_path: Path) -> set[str]:
    eng = make_engine(str(db_path))
    try:
        return {c["name"] for c in inspect(eng).get_columns("positions")}
    finally:
        eng.dispose()


def _build_legacy_positions_and_run_migration(db_path: Path) -> None:
    """Build ``positions`` in its LEGACY shape (no ``reprotection_needed``), insert
    one row, stamp the DB at the parent revision, then upgrade to ``f001rp0000aa``
    so ONLY that revision executes against it.

    The upgrade targets ``f001rp0000aa`` by revision, not ``head``: this is a
    per-migration test, and the hand-built ``positions`` is a minimal stub that
    later table-touching migrations would not understand — targeting the revision
    isolates the column-add under test (ALP-933 per-migration-head fragility).

    The genesis baseline (``a000000000aa``) is ``create_all``-driven and builds
    ``positions`` *with* ``reprotection_needed`` straight from the live model,
    which masks the incremental-path omission. Stepping the real chain therefore
    can never exercise the ``add_column`` branch; this helper materializes the
    legacy on-disk shape the production DB actually carries.
    """
    eng = make_engine(str(db_path))
    try:
        with eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            # ``positions`` in its LEGACY shape: the NOT NULL columns a row needs,
            # but NO ``reprotection_needed`` column.
            conn.execute(
                text(
                    "CREATE TABLE positions ("
                    " position_id TEXT PRIMARY KEY,"
                    " status TEXT NOT NULL,"
                    " instrument_type TEXT NOT NULL,"
                    " details_json TEXT NOT NULL,"
                    " execution_history_json TEXT NOT NULL,"
                    " corporate_action_adjustment_needed INTEGER NOT NULL"
                    ")"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO positions "
                    "(position_id, status, instrument_type, details_json, "
                    " execution_history_json, corporate_action_adjustment_needed) "
                    "VALUES "
                    "('POS-MIG-1', 'OPEN', 'EQUITY', '{}', '[]', 0)"
                )
            )
            conn.execute(text("CREATE TABLE alembic_version (version_num TEXT NOT NULL)"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": _PARENT_REVISION},
            )
    finally:
        eng.dispose()

    command.upgrade(_alembic_config(db_path), _MIGRATION_REVISION)


def _reprotection_value(db_path: Path, position_id: str) -> int | None:
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT reprotection_needed FROM positions WHERE position_id = :pid"),
                {"pid": position_id},
            ).fetchone()
            return row[0] if row else None
    finally:
        eng.dispose()


class TestPositionsReprotectionNeededMigration:
    def test_single_head_parented_on_prior_head(self) -> None:
        """f001rp0000aa is the sole head, descending from e001eg0000aa."""
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        assert len(list(script.get_heads())) == 1
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        assert rev.down_revision == _PARENT_REVISION

    def test_upgrade_head_adds_column(self, tmp_path: Path) -> None:
        """After ``upgrade head`` positions carries the reprotection_needed column."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _COLUMN in _column_names(db_path)

    def test_incremental_path_adds_column_defaulting_existing_rows_to_zero(
        self, tmp_path: Path
    ) -> None:
        """On the prod incremental path (legacy ``positions`` with no
        ``reprotection_needed``), the migration adds the column and the existing
        row reads 0 via the ``DEFAULT 0`` back-population."""
        db_path = tmp_path / "incremental.db"
        _build_legacy_positions_and_run_migration(db_path)
        assert _COLUMN in _column_names(db_path)
        assert _reprotection_value(db_path, "POS-MIG-1") == 0

    def test_downgrade_removes_column(self, tmp_path: Path) -> None:
        """After downgrade the reprotection_needed column is gone."""
        db_path = tmp_path / "down.db"
        cfg = _alembic_config(db_path)
        # Target this migration's own revision, not "head" — a future sibling head
        # must not drag this per-migration test through the whole forward chain
        # (the ALP-933 per-migration-head fragility).
        command.upgrade(cfg, _MIGRATION_REVISION)
        assert _COLUMN in _column_names(db_path)
        command.downgrade(cfg, _PARENT_REVISION)
        assert _COLUMN not in _column_names(db_path)

    def test_downgrade_then_upgrade_restores_column(self, tmp_path: Path) -> None:
        """downgrade → upgrade round-trip restores the column."""
        db_path = tmp_path / "roundtrip.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _MIGRATION_REVISION)
        command.downgrade(cfg, _PARENT_REVISION)
        command.upgrade(cfg, _MIGRATION_REVISION)
        assert _COLUMN in _column_names(db_path)
