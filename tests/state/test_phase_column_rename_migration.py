"""Tests for the phase_column_rename migration (ALP-901).

Verifies:
* ``down_revision`` chains to the prior head (``a556rp0000dd``).
* The new revision is the single head.
* Upgrade renames phase1_completed_at → fill_collection_completed_at and
  phase2_completed_at → command_execution_completed_at, preserving existing rows.
* Downgrade restores the original column names.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_PRIOR_HEAD = "a556rp0000dd"
_NEW_REVISION = "b001fc0000ee"
_TABLE = "invocations"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestPhaseColumnRenameMigration:
    def test_new_revision_is_single_head(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        heads = list(script.get_heads())
        assert heads == [_NEW_REVISION]

    def test_new_revision_parents_on_prior_head(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        rev = script.get_revision(_NEW_REVISION)
        assert rev is not None
        assert rev.down_revision == _PRIOR_HEAD

    def test_upgrade_renames_columns(self, tmp_path: Path) -> None:
        """After upgrade, the invocations table has the new column names."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            cols = {c["name"] for c in inspect(eng).get_columns(_TABLE)}
        finally:
            eng.dispose()

        assert "fill_collection_completed_at" in cols
        assert "command_execution_completed_at" in cols
        assert "phase1_completed_at" not in cols
        assert "phase2_completed_at" not in cols

    def test_upgrade_renames_legacy_columns_and_preserves_rows(self, tmp_path: Path) -> None:
        """Upgrade on a DB that genuinely has the old column names exercises the rename branch.

        The genesis baseline (``a000000000aa``) drives ``Base.metadata.create_all`` against
        the current ORM model, which already uses the new names.  A plain
        ``upgrade head`` therefore never enters the ``if "phase1_completed_at" in cols``
        branch.  This test replicates the real production case — a DB stamped at the
        prior head that still carries the old column names — by:

        1. Upgrading to the prior head (genesis creates the new names).
        2. Renaming the columns *back* to the old names via raw SQLite DDL, simulating a
           legacy database that was stamped at ``a556rp0000dd`` before this migration ran.
           The Alembic version table still records ``a556rp0000dd``, so the next
           ``upgrade`` correctly targets ``b001fc0000ee``.
        3. Seeding a row with non-null values in the old columns.
        4. Running ``upgrade`` to ``b001fc0000ee`` — the guard is now ``True``, so the
           rename branch is actually taken.
        5. Asserting the new column names are present and the seeded values survive.
        """
        db_path = tmp_path / "legacy.db"
        cfg = _alembic_config(db_path)
        # Step 1: Bring the DB to the prior head (genesis creates new column names).
        command.upgrade(cfg, _PRIOR_HEAD)

        # Step 2: Rename columns back to old names to simulate a legacy DB.
        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                conn.execute(
                    text(
                        "ALTER TABLE invocations RENAME COLUMN "
                        "fill_collection_completed_at TO phase1_completed_at"
                    )
                )
                conn.execute(
                    text(
                        "ALTER TABLE invocations RENAME COLUMN "
                        "command_execution_completed_at TO phase2_completed_at"
                    )
                )

            # Step 3: Seed a row with non-null values in the old columns.
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                conn.execute(
                    text(
                        "INSERT INTO invocations ("
                        "  invocation_id, process_lifetime_id, start_at,"
                        "  phase1_completed_at, phase2_completed_at,"
                        "  trigger_type, trigger_source, trigger_reason,"
                        "  git_sha_at_invocation, active_profile, active_regime,"
                        "  active_mode, active_overlays_json, resolved_config_hash,"
                        "  resolved_config_snapshot_path, feature_flags_snapshot_json,"
                        "  data_calibration_state_snapshot_path, data_source_freshness_json"
                        ") VALUES ("
                        "  'inv-legacy-01', 'plt-legacy-01', '2026-01-01T00:00:00Z',"
                        "  '2026-01-01T00:01:00Z', '2026-01-01T00:02:00Z',"
                        "  'scheduled', 'cron', 'test legacy seed',"
                        "  'abc123', 'default', 'normal',"
                        "  'normal', '{}', 'hash123',"
                        "  '/tmp/snapshot', '{}', '/tmp/cal', '{}'"
                        ")"
                    )
                )
        finally:
            eng.dispose()

        # Step 4: Upgrade — the old column names are present so the rename branch fires.
        command.upgrade(cfg, _NEW_REVISION)

        # Step 5: Assert new column names are present and seeded values survived.
        eng = make_engine(str(db_path))
        try:
            cols = {c["name"] for c in inspect(eng).get_columns(_TABLE)}
            assert "fill_collection_completed_at" in cols
            assert "command_execution_completed_at" in cols
            assert "phase1_completed_at" not in cols
            assert "phase2_completed_at" not in cols

            with eng.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT fill_collection_completed_at, command_execution_completed_at "
                        "FROM invocations WHERE invocation_id = 'inv-legacy-01'"
                    )
                ).one()
        finally:
            eng.dispose()

        assert row[0] == "2026-01-01T00:01:00Z"
        assert row[1] == "2026-01-01T00:02:00Z"

    def test_downgrade_restores_old_names_and_preserves_rows(self, tmp_path: Path) -> None:
        """Downgrade on a legacy DB (old column names) restores old names with data intact.

        Mirrors the upgrade test: starts from a DB with old column names (pre-rename),
        upgrades via the rename branch, then downgrades back — asserting the old names
        return and the seeded row's values survive the round-trip.
        """
        db_path = tmp_path / "legacy-down.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _PRIOR_HEAD)

        # Rename columns back to old names to simulate a legacy DB.
        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                conn.execute(
                    text(
                        "ALTER TABLE invocations RENAME COLUMN "
                        "fill_collection_completed_at TO phase1_completed_at"
                    )
                )
                conn.execute(
                    text(
                        "ALTER TABLE invocations RENAME COLUMN "
                        "command_execution_completed_at TO phase2_completed_at"
                    )
                )
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                conn.execute(
                    text(
                        "INSERT INTO invocations ("
                        "  invocation_id, process_lifetime_id, start_at,"
                        "  phase1_completed_at, phase2_completed_at,"
                        "  trigger_type, trigger_source, trigger_reason,"
                        "  git_sha_at_invocation, active_profile, active_regime,"
                        "  active_mode, active_overlays_json, resolved_config_hash,"
                        "  resolved_config_snapshot_path, feature_flags_snapshot_json,"
                        "  data_calibration_state_snapshot_path, data_source_freshness_json"
                        ") VALUES ("
                        "  'inv-rtrip-01', 'plt-rtrip-01', '2026-01-01T00:00:00Z',"
                        "  '2026-01-01T00:03:00Z', '2026-01-01T00:04:00Z',"
                        "  'scheduled', 'cron', 'test round-trip',"
                        "  'abc123', 'default', 'normal',"
                        "  'normal', '{}', 'hash456',"
                        "  '/tmp/snapshot2', '{}', '/tmp/cal2', '{}'"
                        ")"
                    )
                )
        finally:
            eng.dispose()

        # Upgrade (rename fires), then downgrade back to prior head.
        command.upgrade(cfg, _NEW_REVISION)
        command.downgrade(cfg, _PRIOR_HEAD)

        eng = make_engine(str(db_path))
        try:
            cols = {c["name"] for c in inspect(eng).get_columns(_TABLE)}
            assert "phase1_completed_at" in cols
            assert "phase2_completed_at" in cols
            assert "fill_collection_completed_at" not in cols
            assert "command_execution_completed_at" not in cols

            with eng.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT phase1_completed_at, phase2_completed_at "
                        "FROM invocations WHERE invocation_id = 'inv-rtrip-01'"
                    )
                ).one()
        finally:
            eng.dispose()

        assert row[0] == "2026-01-01T00:03:00Z"
        assert row[1] == "2026-01-01T00:04:00Z"

    def test_downgrade_restores_old_column_names(self, tmp_path: Path) -> None:
        """After downgrade -1, the invocations table has the original column names."""
        db_path = tmp_path / "down.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            cols = {c["name"] for c in inspect(eng).get_columns(_TABLE)}
        finally:
            eng.dispose()

        assert "phase1_completed_at" in cols
        assert "phase2_completed_at" in cols
        assert "fill_collection_completed_at" not in cols
        assert "command_execution_completed_at" not in cols
