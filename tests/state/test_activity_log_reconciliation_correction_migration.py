"""Tests for the Alembic migration extending ``activity_log.event_type`` (ALP-619).

The migration extends the CHECK constraint with the
``RECONCILIATION_CORRECTION`` event type emitted by the post-Phase-1
auto-correction path when local state is rewritten from Alpaca's snapshot.
The type reuses the existing ``RECONCILIATION`` event group — no
``event_group`` extension.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from alphamind.persistence.session import make_engine
from alphamind.portfolio_state.events.activity_log import (
    EventGroup,
    EventSource,
    EventType,
)

# Pin both the ALP-619 revision and its predecessor so this test is stable
# against future migrations chaining onto it.
_PRIOR_REVISION = "d3f6a1c7e9b2"
_REVISION = "d8a3f2c7b9e4"

_NEW_EVENT_TYPE = EventType.RECONCILIATION_CORRECTION


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestActivityLogReconciliationCorrectionMigration:
    """Upgrade idempotency + downgrade safety contract."""

    def test_upgrade_head_keeps_activity_log_table(self, tmp_path: Path) -> None:
        """After upgrade, the table is intact and inspectable."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "activity_log" in set(insp.get_table_names())
            cols = {c["name"] for c in insp.get_columns("activity_log")}
            assert "event_type" in cols
        finally:
            eng.dispose()

    def test_new_event_type_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """A row with ``RECONCILIATION_CORRECTION`` inserts cleanly past the CHECK."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                _insert_process_lifetime(conn)
                _insert_invocation(conn)
                conn.execute(
                    text(
                        "INSERT INTO activity_log "
                        "(entry_id, invocation_id, entry_at, event_type, "
                        " event_group, position_id, order_id, thesis_id, "
                        " source, detail_json) "
                        "VALUES "
                        "(:eid, :iid, :ts, :et, :eg, NULL, NULL, NULL, "
                        " :src, :body)"
                    ),
                    {
                        "eid": f"alp619-{_NEW_EVENT_TYPE.value}",
                        "iid": "inv-1",
                        "ts": "2026-05-25T12:00:00Z",
                        "et": _NEW_EVENT_TYPE.value,
                        "eg": EventGroup.RECONCILIATION.value,
                        "src": EventSource.CORPORATE_ACTION_PROCESSOR.value,
                        "body": "{}",
                    },
                )
        finally:
            eng.dispose()

    def test_downgrade_refuses_when_new_event_type_rows_exist(self, tmp_path: Path) -> None:
        """Downgrade refuses to invalidate accumulated rows of the new event type."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                _insert_process_lifetime(conn)
                _insert_invocation(conn)
                conn.execute(
                    text(
                        "INSERT INTO activity_log "
                        "(entry_id, invocation_id, entry_at, event_type, "
                        " event_group, position_id, order_id, thesis_id, "
                        " source, detail_json) "
                        "VALUES "
                        "(:eid, 'inv-1', '2026-05-25T12:00:00Z', "
                        " :et, :eg, NULL, NULL, NULL, :src, '{}')"
                    ),
                    {
                        "eid": f"alp619-down-{_NEW_EVENT_TYPE.value}",
                        "et": _NEW_EVENT_TYPE.value,
                        "eg": EventGroup.RECONCILIATION.value,
                        "src": EventSource.CORPORATE_ACTION_PROCESSOR.value,
                    },
                )
        finally:
            eng.dispose()

        try:
            command.downgrade(cfg, _PRIOR_REVISION)
        except RuntimeError as exc:
            assert _NEW_EVENT_TYPE.value in str(exc)
        else:
            raise AssertionError("downgrade should have refused with surviving row")

    def test_upgrade_then_downgrade_then_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        """Up → down → up sequence leaves the schema in a valid state."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PRIOR_REVISION)
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "activity_log" in set(insp.get_table_names())
        finally:
            eng.dispose()


def _insert_process_lifetime(conn: Connection) -> None:
    """Minimal row honoring all NOT NULL columns of ``process_lifetimes``."""
    conn.execute(
        text(
            "INSERT INTO process_lifetimes "
            "(process_lifetime_id, process_role, process_start_at, process_pid, "
            " hostname, git_sha, git_branch, git_dirty, python_version, "
            " pip_freeze_hash, pip_freeze_snapshot_path, anthropic_sdk_version, "
            " claude_agent_sdk_version, os_release) "
            "VALUES "
            "('proc-1', 'pipeline', '2026-05-25T12:00:00Z', 12345, 'host', "
            " :sha, 'main', 0, '3.13.1', :hash, '/tmp/pip.txt', '0.40.0', "
            " '0.1.69', 'Linux-6.5.0')"
        ),
        {"sha": "a" * 40, "hash": "0" * 64},
    )


def _insert_invocation(conn: Connection) -> None:
    """Minimal row honoring all NOT NULL columns of ``invocations``."""
    conn.execute(
        text(
            "INSERT INTO invocations "
            "(invocation_id, process_lifetime_id, start_at, trigger_type, "
            " trigger_source, trigger_reason, git_sha_at_invocation, "
            " active_profile, active_regime, active_mode, active_overlays_json, "
            " resolved_config_hash, resolved_config_snapshot_path, "
            " feature_flags_snapshot_json, data_calibration_state_snapshot_path, "
            " data_source_freshness_json) "
            "VALUES "
            "('inv-1', 'proc-1', '2026-05-25T12:00:00Z', 'scheduled', 'cron', "
            " '0 9 * * 1-5', :sha, 'medium', 'normal', 'normal', '[]', "
            " :hash, '/tmp/resolved.json', '{}', '/tmp/calibration.json', '{}')"
        ),
        {"sha": "a" * 40, "hash": "0" * 64},
    )
