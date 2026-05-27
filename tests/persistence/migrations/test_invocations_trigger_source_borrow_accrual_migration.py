"""Tests for the migration that extends ``ck_invocations_trigger_source``
with ``"borrow_accrual"`` (ALP-715 / ALP-718).

The migration drops + recreates the CHECK constraint with the extended
vocabulary so the continuous monitor's daily borrow-accrual tick can
land its ``invocations`` row. The downgrade refuses to run if any row
already carries the new value (mirrors the prior migration's posture).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine, make_session_factory

_REVISION = "d3e7a9c1b502"
_PREVIOUS = "b8c9d2e4f7a1"


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


def _insert_invocation(
    db_path: Path,
    *,
    trigger_source: str,
    invocation_id: str = "inv-test-1",
    plt_id: str = "plt-test",
) -> None:
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
                    f"'{invocation_id}', '{plt_id}', '2026-05-27T00:00:00Z', "
                    f"'manual', '{trigger_source}', 'r', '0', 'medium', 'normal', "
                    "'normal', '{}', '0', '/p', '{}', '/c', '{}')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


class TestInvocationsTriggerSourceBorrowAccrualMigration:
    def test_borrow_accrual_value_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """The new value the migration pins must round-trip cleanly."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="borrow_accrual")

    def test_pre_existing_values_still_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """Existing values keep round-tripping under the extended constraint."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        for idx, source in enumerate(
            ("cli", "operator_console", "continuous_monitor", "replay"),
        ):
            _insert_invocation(
                db_path,
                trigger_source=source,
                invocation_id=f"inv-test-after-{idx}",
            )

    def test_borrow_accrual_rejected_at_previous_revision(self, tmp_path: Path) -> None:
        """At ``b8c9d2e4f7a1`` the value is not in the constraint; insertion fails."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _PREVIOUS)
        _seed_lifetime(db_path)
        with pytest.raises(IntegrityError):
            _insert_invocation(db_path, trigger_source="borrow_accrual")

    def test_check_constraint_present_after_upgrade(self, tmp_path: Path) -> None:
        """The recreated CHECK constraint shows up in the schema."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            checks = insp.get_check_constraints("invocations")
            by_name = {c["name"]: c for c in checks}
            assert "ck_invocations_trigger_source" in by_name
            assert "borrow_accrual" in by_name["ck_invocations_trigger_source"]["sqltext"]
        finally:
            eng.dispose()

    def test_upgrade_down_up_cycle_idempotent(self, tmp_path: Path) -> None:
        """up → down → up leaves the extended constraint in place."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        # Down to the previous revision then back up to verify both halves.
        command.downgrade(cfg, _PREVIOUS)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="borrow_accrual")

    def test_downgrade_refuses_when_borrow_accrual_rows_exist(self, tmp_path: Path) -> None:
        """Downgrade must abort rather than orphan rows under a stricter constraint."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="borrow_accrual")
        with pytest.raises(RuntimeError, match="borrow_accrual"):
            command.downgrade(cfg, _PREVIOUS)

    def test_downgrade_succeeds_when_no_borrow_accrual_rows(self, tmp_path: Path) -> None:
        """Clean downgrade restores the pre-migration vocabulary."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        # Insert a row with a pre-existing value so the downgrade has data
        # to migrate through (no offending rows).
        _insert_invocation(db_path, trigger_source="cli", invocation_id="inv-test-keep")
        command.downgrade(cfg, _PREVIOUS)
        # After downgrade, the previous constraint is back: borrow_accrual rejected.
        with pytest.raises(IntegrityError):
            _insert_invocation(
                db_path,
                trigger_source="borrow_accrual",
                invocation_id="inv-test-postdown",
            )
