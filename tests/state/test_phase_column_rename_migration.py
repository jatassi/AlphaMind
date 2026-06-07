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

import pytest
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

    def test_upgrade_preserves_existing_rows(self, tmp_path: Path) -> None:
        """Rows seeded at the prior head are readable under the new column names."""
        db_path = tmp_path / "seed.db"
        cfg = _alembic_config(db_path)
        # Upgrade to the prior head first, seed a row, then upgrade to new head.
        command.upgrade(cfg, _PRIOR_HEAD)

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                # Disable FK enforcement for the seed insert so we don't need a
                # matching process_lifetimes row.
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                # Insert a minimal invocations row with the old column names present.
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
                        "  'inv-seed-01', 'plt-seed-01', '2026-01-01T00:00:00Z',"
                        "  '2026-01-01T00:01:00Z', '2026-01-01T00:02:00Z',"
                        "  'scheduled', 'cron', 'test seed',"
                        "  'abc123', 'default', 'normal',"
                        "  'normal', '{}', 'hash123',"
                        "  '/tmp/snapshot', '{}', '/tmp/cal', '{}'"
                        ")"
                    )
                )
        finally:
            eng.dispose()

        # Now upgrade to new head (renames columns).
        command.upgrade(cfg, _NEW_REVISION)

        eng = make_engine(str(db_path))
        try:
            with eng.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT fill_collection_completed_at, command_execution_completed_at "
                        "FROM invocations WHERE invocation_id = 'inv-seed-01'"
                    )
                ).one()
        finally:
            eng.dispose()

        assert row[0] == "2026-01-01T00:01:00Z"
        assert row[1] == "2026-01-01T00:02:00Z"

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
