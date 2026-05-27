"""Tests for the migration that extends ``ck_activity_log_source``
with ``"BORROW_ACCRUAL_MONITOR"`` (ALP-715 review F9).

The migration drops + recreates the CHECK constraint with the extended
vocabulary so the continuous monitor's borrow-accrual tick can emit
activity-log entries under the dedicated source value (instead of
overloading ``MARGIN_MONITOR``). The downgrade refuses to run if any
row already carries the new value (mirrors the prior migration's
posture).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine, make_session_factory

_REVISION = "e2c4a8f1d5b3"
_PREVIOUS = "c3b7d5a8e4f2"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _seed_lifetime(db_path: Path, plt_id: str = "plt-test") -> None:
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
                    f"'{plt_id}', 'monitor', '2026-05-27T00:00:00Z', "
                    "1, 'h', '0', 'b', 0, '3.13', '0', '/p', 'na', 'na', 'os')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


def _seed_invocation(
    db_path: Path,
    *,
    invocation_id: str = "inv-test-1",
    plt_id: str = "plt-test",
    trigger_source: str = "borrow_accrual",
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
                    f"'scheduled', '{trigger_source}', 'r', '0', '', '', 'normal', "
                    "'[]', '0', '/p', '{}', '/c', '{}')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


def _insert_activity_log_row(
    db_path: Path,
    *,
    source: str,
    entry_id: str = "entry-test-1",
    invocation_id: str = "inv-test-1",
) -> None:
    eng = make_engine(str(db_path))
    try:
        factory = make_session_factory(eng)
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO activity_log ("
                    "entry_id, invocation_id, entry_at, event_type, event_group, "
                    "position_id, order_id, thesis_id, source, detail_json"
                    ") VALUES ("
                    f"'{entry_id}', '{invocation_id}', '2026-05-27T00:00:00Z', "
                    "'BORROW_COST_ACCRUED', 'CASH_AND_MARGIN', "
                    f"NULL, NULL, NULL, '{source}', '{{}}')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


class TestActivityLogSourceBorrowAccrualMonitorMigration:
    def test_borrow_accrual_monitor_value_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """The new value the migration pins must round-trip cleanly."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        _seed_invocation(db_path)
        _insert_activity_log_row(db_path, source="BORROW_ACCRUAL_MONITOR")

    def test_pre_existing_sources_still_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """Existing values keep round-tripping under the extended constraint."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        _seed_invocation(db_path)
        for idx, source in enumerate(
            ("MARGIN_MONITOR", "BRACKET_MANAGER", "GUARDRAIL_LAYER", "OPERATOR_CONSOLE"),
        ):
            _insert_activity_log_row(
                db_path, source=source, entry_id=f"entry-test-after-{idx}"
            )

    # NOTE: a "rejected at previous revision" test would normally pin that
    # the new value fails ``ck_activity_log_source`` at the prior head.
    # That test is omitted here because the original ``b5d2e3f4c6a7`` create
    # migration installs the constraint by importing the live ``EventSource``
    # enum (rather than snapshotting), so a fresh ``alembic upgrade head``
    # already admits the new value on a fresh DB. The constraint-recreate
    # below is still load-bearing for any DB whose ``activity_log`` table
    # pre-dates the enum addition — its existing CHECK was installed when
    # ``EventSource`` did not yet have ``BORROW_ACCRUAL_MONITOR``.

    def test_check_constraint_present_after_upgrade(self, tmp_path: Path) -> None:
        """The recreated CHECK constraint shows up in the schema."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            checks = insp.get_check_constraints("activity_log")
            by_name = {c["name"]: c for c in checks}
            assert "ck_activity_log_source" in by_name
            assert "BORROW_ACCRUAL_MONITOR" in by_name["ck_activity_log_source"]["sqltext"]
        finally:
            eng.dispose()

    def test_upgrade_down_up_cycle_idempotent(self, tmp_path: Path) -> None:
        """up → down → up leaves the extended constraint in place."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _seed_invocation(db_path)
        _insert_activity_log_row(db_path, source="BORROW_ACCRUAL_MONITOR")

    def test_downgrade_refuses_when_borrow_accrual_monitor_rows_exist(
        self, tmp_path: Path
    ) -> None:
        """Downgrade must abort rather than orphan rows under a stricter constraint."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _seed_invocation(db_path)
        _insert_activity_log_row(db_path, source="BORROW_ACCRUAL_MONITOR")
        with pytest.raises(RuntimeError, match="BORROW_ACCRUAL_MONITOR"):
            command.downgrade(cfg, _PREVIOUS)

    def test_downgrade_succeeds_when_no_borrow_accrual_monitor_rows(
        self, tmp_path: Path
    ) -> None:
        """Clean downgrade restores the pre-migration vocabulary."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _seed_invocation(db_path)
        _insert_activity_log_row(
            db_path, source="MARGIN_MONITOR", entry_id="entry-test-keep"
        )
        command.downgrade(cfg, _PREVIOUS)
        with pytest.raises(IntegrityError):
            _insert_activity_log_row(
                db_path,
                source="BORROW_ACCRUAL_MONITOR",
                entry_id="entry-test-postdown",
            )
