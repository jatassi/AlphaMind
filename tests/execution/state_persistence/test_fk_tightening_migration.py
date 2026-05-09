"""Tests for the FK-tightening Alembic migration (ALP-119 follow-up).

The integration migration adds the FK constraints documented as deferred in
each per-table mapping module — the substrate's parent tables now all ship,
so the per-table comments' "FKs land in a follow-up integration migration
once those tables ship" promises can be honored.

Per the design doc, deletes are RESTRICTed at every link so the audit trail
cannot be broken. The migration is fully reversible.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, inspect, text
from sqlalchemy.exc import IntegrityError

from alphamind.persistence.session import make_engine

# Pin both the new revision id and its predecessor so the test is stable
# against future migrations chaining onto it.
_PRIOR_REVISION = "d4a8e9f2c1b3"
_REVISION = "e9d2c4f7b3a1"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestFkTighteningMigration:
    def test_upgrade_keeps_all_state_persistence_tables(self, tmp_path: Path) -> None:
        """After upgrade, every state-persistence table is still inspectable."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            for required in (
                "positions",
                "theses",
                "thesis_components",
                "brackets",
                "bracket_legs",
                "orders",
                "activity_log",
            ):
                assert required in tables, f"missing table {required!r} after upgrade"
        finally:
            eng.dispose()

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
            assert "positions" in set(insp.get_table_names())
        finally:
            eng.dispose()

    def test_positions_thesis_id_fk_rejects_orphan(self, tmp_path: Path) -> None:
        """After upgrade, committing a position with an orphan thesis_id is rejected.

        The FK is DEFERRABLE INITIALLY DEFERRED — the per-INSERT check passes,
        but COMMIT fails the constraint check. The semantic invariant (no orphan
        rows reach the durable schema) holds regardless of when the check fires.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            with pytest.raises(IntegrityError), eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO positions "
                        "(position_id, thesis_id, bracket_id, status, direction, "
                        " entry_timestamp, instrument_type, details_json, "
                        " execution_history_json, realized_pnl_to_date_usd, "
                        " corporate_action_adjustment_needed, parent_position_id, "
                        " origin) "
                        "VALUES "
                        "('pos-orphan', 'no-such-thesis', NULL, 'OPEN', 'LONG', "
                        " '2026-05-08T12:00:00Z', 'EQUITY', '{}', '[]', NULL, "
                        " 0, NULL, NULL)"
                    )
                )
        finally:
            eng.dispose()

    def test_orders_bracket_id_fk_rejects_orphan(self, tmp_path: Path) -> None:
        """After upgrade, committing an order with an orphan bracket_id is rejected.

        Constraint is DEFERRABLE INITIALLY DEFERRED — the violation surfaces at
        COMMIT, not at INSERT. The semantic invariant is unchanged: no orphan
        rows reach the durable schema.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            with pytest.raises(IntegrityError), eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO orders "
                        "(order_id, position_id, bracket_id, order_role, "
                        " order_class, instrument_spec_json, direction, order_type, "
                        " quantity, price_parameters_json, duration, status, "
                        " alpaca_order_id, alpaca_order_id_chain_json, "
                        " submission_timestamp, last_update_timestamp, "
                        " filled_quantity, average_fill_price, remaining_quantity, "
                        " modification_count, metadata_json) "
                        "VALUES "
                        "('ord-orphan', NULL, 'no-such-bracket', 'ENTRY', "
                        " 'SIMPLE', '{}', 'BUY', 'MARKET', 1.0, '{}', 'DAY', "
                        " 'PENDING', 'alp-1', '[]', '2026-05-08T12:00:00Z', "
                        " '2026-05-08T12:00:00Z', 0.0, NULL, 1.0, 0, '{}')"
                    )
                )
        finally:
            eng.dispose()

    def test_upgrade_preserves_existing_rows(self, tmp_path: Path) -> None:
        """Seed a positions/orders pair before the upgrade; assert rows
        survive the table-rebuild that adds the FK."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _PRIOR_REVISION)

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO positions "
                        "(position_id, thesis_id, bracket_id, status, direction, "
                        " entry_timestamp, instrument_type, details_json, "
                        " execution_history_json, realized_pnl_to_date_usd, "
                        " corporate_action_adjustment_needed, parent_position_id, "
                        " origin) "
                        "VALUES "
                        "('pos-keep', NULL, NULL, 'OPEN', 'LONG', "
                        " '2026-05-08T12:00:00Z', 'EQUITY', '{}', '[]', NULL, "
                        " 0, NULL, NULL)"
                    )
                )
            command.upgrade(cfg, _REVISION)
            with eng.begin() as conn:
                count = conn.execute(
                    text("SELECT COUNT(*) FROM positions WHERE position_id = 'pos-keep'")
                ).scalar_one()
                assert count == 1
        finally:
            eng.dispose()

    def test_activity_log_position_id_fk_rejects_orphan(self, tmp_path: Path) -> None:
        """After upgrade, an activity_log entry with a non-NULL position_id
        pointing at a missing positions row is rejected at COMMIT. NULL still allowed.

        Constraint is DEFERRABLE INITIALLY DEFERRED — INSERT succeeds, COMMIT
        fails. The semantic invariant (no orphan rows reach the durable schema)
        is preserved.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                _insert_process_lifetime(conn)
                _insert_invocation(conn)
            with pytest.raises(IntegrityError), eng.begin() as conn:
                conn.execute(text("PRAGMA foreign_keys = ON"))
                conn.execute(
                    text(
                        "INSERT INTO activity_log "
                        "(entry_id, invocation_id, entry_at, event_type, "
                        " event_group, position_id, order_id, thesis_id, "
                        " source, detail_json) "
                        "VALUES "
                        "('al-orphan', 'inv-1', '2026-05-08T12:00:00Z', "
                        " 'POSITION_OPENED', 'POSITION_LIFECYCLE', "
                        " 'no-such-pos', NULL, NULL, 'COMMAND_EXECUTOR', '{}')"
                    )
                )
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
            "('proc-1', 'pipeline', '2026-05-08T12:00:00Z', 12345, 'host', "
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
            "('inv-1', 'proc-1', '2026-05-08T12:00:00Z', 'scheduled', 'cron', "
            " '0 9 * * 1-5', :sha, 'medium', 'normal', 'normal', '[]', "
            " :hash, '/tmp/resolved.json', '{}', '/tmp/calibration.json', '{}')"
        ),
        {"sha": "a" * 40, "hash": "0" * 64},
    )
