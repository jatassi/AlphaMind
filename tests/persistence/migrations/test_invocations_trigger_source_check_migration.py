"""Tests for the F15 migration that adds the CHECK constraint on
``invocations.trigger_source``.

The migration pins the ``operator_console`` value used by the
command-center activity-log explorer's "Operator actions" filter view,
plus every other ``trigger_source`` value the codebase emits today
(scheduler RunType members, monitor emergency, CLI, replay, test
fixtures). The migration is purely additive — no existing row is
invalidated — so the upgrade succeeds against a populated DB.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine, make_session_factory

_REVISION = "b8c9d2e4f7a1"
_PREVIOUS = "cc1f2e3d4a5b"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _seed_lifetime(db_path: Path, plt_id: str = "plt-test") -> None:
    """Insert a process_lifetimes row so invocation FK resolves."""
    eng = make_engine(str(db_path))
    try:
        factory = make_session_factory(eng)
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO process_lifetimes ("
                    "process_lifetime_id, process_role, process_start_at, "
                    "process_pid, hostname, git_sha, git_branch, git_dirty, "
                    "python_version, pip_freeze_hash, pip_freeze_snapshot_path, "
                    "anthropic_sdk_version, claude_agent_sdk_version, os_release"
                    ") VALUES ("
                    f"'{plt_id}', 'pipeline', '2026-05-27T00:00:00Z', "
                    "1, 'h', '0', 'b', 0, '3.13', '0', '/p', 'na', 'na', 'os')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


def _insert_invocation(db_path: Path, *, trigger_source: str, plt_id: str = "plt-test") -> None:
    eng = make_engine(str(db_path))
    try:
        factory = make_session_factory(eng)
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO invocations ("
                    "invocation_id, process_lifetime_id, start_at, trigger_type, "
                    "trigger_source, trigger_reason, git_sha_at_invocation, "
                    "active_profile, active_regime, active_mode, active_overlays_json, "
                    "resolved_config_hash, resolved_config_snapshot_path, "
                    "feature_flags_snapshot_json, data_calibration_state_snapshot_path, "
                    "data_source_freshness_json"
                    ") VALUES ("
                    f"'inv-test-{trigger_source}-1', '{plt_id}', '2026-05-27T00:00:00Z', "
                    f"'manual', '{trigger_source}', 'r', '0', 'medium', 'normal', "
                    "'normal', '{}', '0', '/p', '{}', '/c', '{}')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


class TestInvocationsTriggerSourceCheckMigration:
    def test_operator_console_value_accepted(self, tmp_path: Path) -> None:
        """The new value the migration pins must round-trip cleanly."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        # If the CHECK is well-formed, this insert succeeds.
        _insert_invocation(db_path, trigger_source="operator_console")

    def test_pre_existing_values_still_accepted(self, tmp_path: Path) -> None:
        """The migration is additive — existing values still write."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        for source in ("cli", "debug_e2e_cli", "continuous_monitor", "replay"):
            _insert_invocation(db_path, trigger_source=source)

    def test_unknown_trigger_source_rejected_after_upgrade(self, tmp_path: Path) -> None:
        """A typo'd value should fail at the SQLite layer."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        with pytest.raises(IntegrityError):
            _insert_invocation(db_path, trigger_source="operator-console")  # typo: hyphen

    def test_check_constraint_present_in_schema(self, tmp_path: Path) -> None:
        """The CHECK constraint shows up in the SQLite ``sqlite_master`` table."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            checks = insp.get_check_constraints("invocations")
            names = {c["name"] for c in checks}
            assert "ck_invocations_trigger_source" in names
        finally:
            eng.dispose()

    def test_downgrade_removes_the_check(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            checks = insp.get_check_constraints("invocations")
            names = {c["name"] for c in checks}
            assert "ck_invocations_trigger_source" not in names
        finally:
            eng.dispose()
