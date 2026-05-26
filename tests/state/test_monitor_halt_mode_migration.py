"""Tests for the Alembic migration adding ``monitor_halt_mode`` (ALP-665).

The migration adds the singleton ``monitor_halt_mode`` table backing the
``halt_mode_engaged`` portfolio state field per
``docs/design/monitor-control-and-events-schema.md`` § Notes on cross-field
invariants. The continuous monitor's ``POST /control/set_halt_mode`` verb
reads / writes this row.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

# Pin the revision identifiers so the test is stable against future
# migrations chaining onto this one.
_PRIOR_REVISION = "e2f7a1c3b8d9"
_REVISION = "f1e2a3b4c5d6"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestMonitorHaltModeMigration:
    def test_upgrade_head_creates_monitor_halt_mode_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "monitor_halt_mode" in set(insp.get_table_names())
            cols = {c["name"] for c in insp.get_columns("monitor_halt_mode")}
            assert cols == {"id", "enabled", "reason", "applied_at"}
        finally:
            eng.dispose()

    def test_singleton_check_constraint_rejects_other_ids(self, tmp_path: Path) -> None:
        """The CHECK constraint pins ``id = 'current'`` — other values raise."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                # Honor the boolean CHECK so the singleton constraint is the
                # only thing under test.
                try:
                    conn.execute(
                        text(
                            "INSERT INTO monitor_halt_mode "
                            "(id, enabled, reason, applied_at) "
                            "VALUES ('not-current', 0, NULL, NULL)"
                        )
                    )
                except Exception:
                    pass
                else:
                    msg = "CHECK constraint did not reject 'not-current' as id"
                    raise AssertionError(msg)
        finally:
            eng.dispose()

    def test_upgrade_then_downgrade_then_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PRIOR_REVISION)
        command.upgrade(cfg, _REVISION)
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "monitor_halt_mode" in set(insp.get_table_names())
        finally:
            eng.dispose()
