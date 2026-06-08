"""Tests for the theses.invocation_id migration (ALP-919 / story 02i).

``d001th0000aa`` adds a nullable ``theses.invocation_id`` column (FK to
``invocations.invocation_id``) and back-populates from ``activity_log``
``THESIS_CREATED`` entries.

Assertions:
* The migration is the single head, parented on ``c918aa0000ff``.
* After ``upgrade head`` the column and index exist on the ``theses`` table.
* A thesis that has a ``THESIS_CREATED`` activity_log entry is back-populated.
* A thesis with no ``THESIS_CREATED`` entry remains NULL.
* After ``downgrade`` the column is gone; ``upgrade`` again restores it.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_PARENT_REVISION = "c918aa0000ff"
_MIGRATION_REVISION = "d001th0000aa"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _column_names(db_path: Path) -> set[str]:
    eng = make_engine(str(db_path))
    try:
        return {c["name"] for c in inspect(eng).get_columns("theses")}
    finally:
        eng.dispose()


def _index_names(db_path: Path) -> set[str]:
    eng = make_engine(str(db_path))
    try:
        return {str(idx["name"]) for idx in inspect(eng).get_indexes("theses")}
    finally:
        eng.dispose()


def _theses_fk_targets(db_path: Path) -> set[tuple[str, str]]:
    """Return ``(referenced_table, on_delete)`` pairs for every FK on ``theses``.

    Reads ``PRAGMA foreign_key_list(theses)`` — the persisted on-disk DDL — so it
    sees the FK only if it was actually attached to the table, not merely promised
    by the model metadata.
    """
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            rows = conn.execute(text("PRAGMA foreign_key_list(theses)")).fetchall()
            # PRAGMA columns: id, seq, table, from, to, on_update, on_delete, match
            return {(str(r[2]), str(r[6])) for r in rows}
    finally:
        eng.dispose()


def _build_legacy_theses_and_run_migration(db_path: Path) -> None:
    """Build ``theses`` in its LEGACY shape (no ``invocation_id`` column), plus the
    minimal FK-target / back-population tables, stamp the DB at the parent revision,
    then run ``upgrade head`` so ONLY ``d001th0000aa`` executes against it.

    The genesis baseline (``a000000000aa``) is ``create_all``-driven and builds
    ``theses`` *with* the ``invocation_id`` column + FK straight from the live model,
    which masks the incremental-path omission. Stepping the real chain therefore can
    never exercise the ``add_column`` branch. This helper sidesteps the baseline and
    materializes the legacy on-disk shape the production DB actually carries, so the
    migration's own column-add path is the thing under test.
    """
    db_path_str = str(db_path)
    eng = make_engine(db_path_str)
    try:
        with eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            # FK target for the new column (minimal — only what the FK references).
            conn.execute(text("CREATE TABLE invocations (invocation_id TEXT PRIMARY KEY)"))
            # Back-population source.
            conn.execute(
                text(
                    "CREATE TABLE activity_log ("
                    " entry_id TEXT PRIMARY KEY,"
                    " invocation_id TEXT,"
                    " event_type TEXT,"
                    " thesis_id TEXT"
                    ")"
                )
            )
            # ``theses`` in its LEGACY shape: every column the migration's UPDATE and
            # the existing seed touch, but NO ``invocation_id`` column and NO
            # ``fk_theses_invocation_id`` constraint.
            conn.execute(
                text(
                    "CREATE TABLE theses ("
                    " thesis_id TEXT PRIMARY KEY,"
                    " position_id TEXT NOT NULL,"
                    " status TEXT NOT NULL,"
                    " nature TEXT NOT NULL,"
                    " summary TEXT NOT NULL,"
                    " time_expectation_hours REAL,"
                    " generation_timestamp TEXT NOT NULL,"
                    " narrative_json TEXT NOT NULL"
                    ")"
                )
            )
            # Alembic version table stamped at the parent revision, so ``upgrade head``
            # runs only ``d001th0000aa`` against this legacy-shape DB.
            conn.execute(text("CREATE TABLE alembic_version (version_num TEXT NOT NULL)"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": _PARENT_REVISION},
            )
    finally:
        eng.dispose()

    command.upgrade(_alembic_config(db_path), "head")


def _seed_and_back_populate(db_path: Path, *, with_thesis_created_entry: bool) -> str | None:
    """Seed a thesis row (+ optional THESIS_CREATED activity_log entry) then run
    ``upgrade head``.  Returns the ``invocation_id`` value from the theses row
    after migration — ``None`` if absent.

    FK enforcement is disabled so we can insert rows without satisfying FK parents
    (invocations, positions) — the migration env also runs with ``foreign_keys=OFF``.
    """
    db_path_str = str(db_path)
    cfg = _alembic_config(db_path)

    # Step to the parent revision so the theses table is already present but
    # the invocation_id column is absent (we're testing the migration adds it).
    command.upgrade(cfg, _PARENT_REVISION)

    eng = make_engine(db_path_str)
    try:
        with eng.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=OFF"))
            # Minimal thesis row (only the NOT NULL columns).
            conn.execute(
                text(
                    "INSERT INTO theses "
                    "(thesis_id, position_id, status, nature, summary, "
                    " time_expectation_hours, generation_timestamp, narrative_json) "
                    "VALUES "
                    "('thes-mig-1', 'pos-mig-1', 'ACTIVE', 'DIRECTIONAL', 'stub', "
                    " 24.0, '2026-06-07T10:00:00Z', '{}')"
                )
            )
            if with_thesis_created_entry:
                # Minimal activity_log row carrying THESIS_CREATED for the thesis.
                conn.execute(
                    text(
                        "INSERT INTO activity_log "
                        "(entry_id, invocation_id, entry_at, event_type, event_group, "
                        " thesis_id, source, detail_json) "
                        "VALUES "
                        "('entry-mig-1', 'inv-mig-gen-1', '2026-06-07T10:00:00Z', "
                        " 'THESIS_CREATED', 'THESIS', "
                        " 'thes-mig-1', 'COMMAND_EXECUTOR', '{}')"
                    )
                )
    finally:
        eng.dispose()

    # Apply the migration.
    command.upgrade(cfg, "head")

    eng = make_engine(db_path_str)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT invocation_id FROM theses WHERE thesis_id = 'thes-mig-1'")
            ).fetchone()
            return row[0] if row else None
    finally:
        eng.dispose()


class TestThesesInvocationIdMigration:
    def test_single_head_parented_on_prior_head(self) -> None:
        """d001th0000aa is the sole head, descending from c918aa0000ff."""
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        assert len(list(script.get_heads())) == 1
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        assert rev.down_revision == _PARENT_REVISION

    def test_upgrade_head_adds_column_and_index(self, tmp_path: Path) -> None:
        """After ``upgrade head`` theses has invocation_id column + ix_theses_invocation_id."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert "invocation_id" in _column_names(db_path)
        assert "ix_theses_invocation_id" in _index_names(db_path)

    def test_incremental_path_attaches_invocation_fk(self, tmp_path: Path) -> None:
        """On the prod incremental path (legacy ``theses`` with no ``invocation_id``),
        ``d001th0000aa`` rebuilds the table carrying ``fk_theses_invocation_id``
        (FK → ``invocations.invocation_id``, ``ON DELETE RESTRICT``).

        This is the faithful test: it bypasses the ``create_all`` genesis baseline
        (which would build the FK from the model and mask the omission) and runs only
        ``d001th0000aa`` against a hand-built legacy-shape table.
        """
        db_path = tmp_path / "incremental.db"
        _build_legacy_theses_and_run_migration(db_path)

        assert "invocation_id" in _column_names(db_path)
        assert ("invocations", "RESTRICT") in _theses_fk_targets(db_path)

    def test_incremental_path_downgrade_drops_fk_and_round_trips(self, tmp_path: Path) -> None:
        """On the incremental path, ``downgrade`` removes the column and its FK, and a
        re-``upgrade`` re-attaches ``fk_theses_invocation_id`` (the rebuild round-trips).
        """
        db_path = tmp_path / "incremental_roundtrip.db"
        _build_legacy_theses_and_run_migration(db_path)
        cfg = _alembic_config(db_path)

        command.downgrade(cfg, _PARENT_REVISION)
        assert "invocation_id" not in _column_names(db_path)
        assert _theses_fk_targets(db_path) == set()

        command.upgrade(cfg, "head")
        assert "invocation_id" in _column_names(db_path)
        assert ("invocations", "RESTRICT") in _theses_fk_targets(db_path)

    def test_back_populate_from_thesis_created_entry(self, tmp_path: Path) -> None:
        """A thesis with a THESIS_CREATED activity_log entry is back-populated."""
        db_path = tmp_path / "backpop_yes.db"
        result = _seed_and_back_populate(db_path, with_thesis_created_entry=True)
        assert result == "inv-mig-gen-1"

    def test_no_thesis_created_entry_stays_null(self, tmp_path: Path) -> None:
        """A thesis with no THESIS_CREATED entry remains NULL after migration."""
        db_path = tmp_path / "backpop_no.db"
        result = _seed_and_back_populate(db_path, with_thesis_created_entry=False)
        assert result is None

    def test_downgrade_removes_column_and_index(self, tmp_path: Path) -> None:
        """After downgrade the invocation_id column and index are gone."""
        db_path = tmp_path / "down.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        assert "invocation_id" in _column_names(db_path)
        command.downgrade(cfg, _PARENT_REVISION)
        assert "invocation_id" not in _column_names(db_path)
        assert "ix_theses_invocation_id" not in _index_names(db_path)

    def test_downgrade_then_upgrade_restores_column(self, tmp_path: Path) -> None:
        """downgrade → upgrade round-trip restores the column and index."""
        db_path = tmp_path / "roundtrip.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _PARENT_REVISION)
        command.upgrade(cfg, "head")
        assert "invocation_id" in _column_names(db_path)
        assert "ix_theses_invocation_id" in _index_names(db_path)
