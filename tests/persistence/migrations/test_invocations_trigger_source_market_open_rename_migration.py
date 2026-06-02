"""Tests for the migration that renames ``invocations.trigger_source``
``'pre_open'`` -> ``'market_open'`` (ALP-822).

The migration rewrites historical ``pre_open`` rows to ``market_open`` and
drops + recreates ``ck_invocations_trigger_source`` with the renamed value.
Unlike the additive ``borrow_accrual`` migration this one is a *rename*:
the post-upgrade constraint forbids the old value and the downgrade remaps
the new value back rather than refusing to run.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine, make_session_factory

_REVISION = "a7e3f1c9d2b4"
_PREVIOUS = "c1b2a3d4e5f6"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _seed_lifetime(db_path: Path, plt_id: str = "plt-test") -> None:
    """Insert a process_lifetimes row so the invocation FK resolves."""
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
                    f"'{plt_id}', 'pipeline', '2026-06-02T00:00:00Z', "
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
                    f"'{invocation_id}', '{plt_id}', '2026-06-02T00:00:00Z', "
                    f"'scheduled', '{trigger_source}', 'r', '0', 'medium', 'normal', "
                    "'normal', '{}', '0', '/p', '{}', '/c', '{}')"
                )
            )
            session.commit()
    finally:
        eng.dispose()


def _trigger_source_of(db_path: Path, invocation_id: str) -> str:
    eng = make_engine(str(db_path))
    try:
        factory = make_session_factory(eng)
        with factory() as session:
            value = session.execute(
                text("SELECT trigger_source FROM invocations WHERE invocation_id = :i"),
                {"i": invocation_id},
            ).scalar_one()
            return str(value)
    finally:
        eng.dispose()


class TestInvocationsTriggerSourceMarketOpenRename:
    def test_pre_open_rows_rewritten_to_market_open_after_upgrade(self, tmp_path: Path) -> None:
        """A historical ``pre_open`` row is rewritten to ``market_open``."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        # At the previous revision ``pre_open`` is still a valid value.
        command.upgrade(cfg, _PREVIOUS)
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="pre_open", invocation_id="inv-old")
        # Upgrade applies the rename.
        command.upgrade(cfg, _REVISION)
        assert _trigger_source_of(db_path, "inv-old") == "market_open"

    def test_market_open_value_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """The renamed value round-trips cleanly under the new constraint."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="market_open")
        assert _trigger_source_of(db_path, "inv-test-1") == "market_open"

    def test_pre_open_rejected_after_upgrade(self, tmp_path: Path) -> None:
        """After the rename the old value is no longer in the constraint."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        with pytest.raises(IntegrityError):
            _insert_invocation(db_path, trigger_source="pre_open")

    def test_pre_existing_values_still_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """Untouched values keep round-tripping under the renamed constraint."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _seed_lifetime(db_path)
        for idx, source in enumerate(
            ("cli", "operator_console", "continuous_monitor", "borrow_accrual", "replay"),
        ):
            _insert_invocation(
                db_path,
                trigger_source=source,
                invocation_id=f"inv-test-after-{idx}",
            )

    def test_check_constraints_after_upgrade(self, tmp_path: Path) -> None:
        """The recreated CHECK carries the new value, drops the old, and the
        sibling CHECK constraints survive the batch table-recreate."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            checks = insp.get_check_constraints("invocations")
            by_name = {c["name"]: c for c in checks}
            trigger_source_sql = by_name["ck_invocations_trigger_source"]["sqltext"]
            assert "'market_open'" in trigger_source_sql
            assert "'pre_open'" not in trigger_source_sql
            # The batch recreate must not drop the other named CHECKs.
            assert "ck_invocations_trigger_type" in by_name
            assert "ck_invocations_active_mode" in by_name
        finally:
            eng.dispose()

    def test_downgrade_restores_pre_open_value_and_vocab(self, tmp_path: Path) -> None:
        """Downgrade remaps ``market_open`` rows back and restores the old CHECK."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="market_open", invocation_id="inv-new")
        command.downgrade(cfg, _PREVIOUS)
        # Row remapped back, old vocabulary restored.
        assert _trigger_source_of(db_path, "inv-new") == "pre_open"
        with pytest.raises(IntegrityError):
            _insert_invocation(
                db_path,
                trigger_source="market_open",
                invocation_id="inv-postdown",
            )

    def test_upgrade_down_up_cycle_idempotent(self, tmp_path: Path) -> None:
        """up -> down -> up leaves the renamed constraint in place."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        command.upgrade(cfg, _REVISION)
        _seed_lifetime(db_path)
        _insert_invocation(db_path, trigger_source="market_open")
