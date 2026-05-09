"""Tests for the Alembic migration that adds ``process_lifetimes`` and
``invocations`` (story 02b).

Per the parent issue's pre-resolved decision (E), each table-creation story
owns its own migration alongside its SQLAlchemy model and round-trip tests.
This test pins the upgrade-then-downgrade idempotency contract and the
column-set the migration installs.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine

# Pin the migration revision so a future migration that lands on top doesn't
# silently shift this test's downgrade target. The revision string here must
# match the ``revision`` in the migration file.
_REVISION = "a4c1d2e3f4b5"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestProcessLifetimesAndInvocationsMigration:
    def test_upgrade_head_creates_both_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "process_lifetimes" in tables
            assert "invocations" in tables

            process_cols = {c["name"] for c in insp.get_columns("process_lifetimes")}
            assert process_cols == {
                "process_lifetime_id",
                "process_role",
                "process_start_at",
                "process_pid",
                "hostname",
                "git_sha",
                "git_branch",
                "git_dirty",
                "python_version",
                "pip_freeze_hash",
                "pip_freeze_snapshot_path",
                "anthropic_sdk_version",
                "claude_agent_sdk_version",
                "os_release",
            }

            invocation_cols = {c["name"] for c in insp.get_columns("invocations")}
            assert invocation_cols == {
                "invocation_id",
                "process_lifetime_id",
                "start_at",
                "phase1_completed_at",
                "phase2_completed_at",
                "trigger_type",
                "trigger_source",
                "trigger_reason",
                "git_sha_at_invocation",
                "active_profile",
                "active_regime",
                "active_mode",
                "active_overlays_json",
                "resolved_config_hash",
                "resolved_config_snapshot_path",
                "feature_flags_snapshot_json",
                "data_calibration_state_snapshot_path",
                "data_source_freshness_json",
                "fill_collection_summary_json",
                "command_execution_summary_json",
                "staleness_flag",
                "snapshot_metadata_json",
            }

            # The FK on process_lifetime_id with ON DELETE RESTRICT must be
            # installed by the migration (not just by the SQLAlchemy model).
            fks = insp.get_foreign_keys("invocations")
            matching = [
                fk
                for fk in fks
                if fk["referred_table"] == "process_lifetimes"
                and fk["constrained_columns"] == ["process_lifetime_id"]
                and fk["referred_columns"] == ["process_lifetime_id"]
            ]
            assert matching, f"missing FK on invocations.process_lifetime_id; got {fks}"
            assert matching[0]["options"].get("ondelete", "").upper() == "RESTRICT"
        finally:
            eng.dispose()

    def test_downgrade_drops_both_tables(self, tmp_path: Path) -> None:
        """Targets the exact revision so a future migration on top doesn't drift."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "process_lifetimes" not in tables
            assert "invocations" not in tables
        finally:
            eng.dispose()

    def test_upgrade_then_downgrade_then_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        """The migration's up/downgrade must be replayable."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "process_lifetimes" in tables
            assert "invocations" in tables
        finally:
            eng.dispose()
