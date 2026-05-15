"""Tests for the Alembic migration that adds ``activity_log`` (story 03 / ALP-357).

Per the parent issue's pre-resolved decision (E), each table-creation story
owns its own migration alongside its SQLAlchemy model. This test pins the
upgrade-then-downgrade idempotency contract and the column-set the migration
installs.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine

# Pin the migration revision so a future migration that lands on top doesn't
# silently shift this test's downgrade target.
_REVISION = "b5d2e3f4c6a7"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestActivityLogMigration:
    def test_upgrade_head_creates_activity_log(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "activity_log" in tables

            cols = {c["name"] for c in insp.get_columns("activity_log")}
            assert cols == {
                "entry_id",
                "invocation_id",
                "entry_at",
                "event_type",
                "event_group",
                "position_id",
                "order_id",
                "thesis_id",
                "source",
                "detail_json",
            }

            indexes = {idx["name"] for idx in insp.get_indexes("activity_log")}
            assert "ix_activity_log_invocation_id" in indexes
            assert "ix_activity_log_event_type_invocation_id" in indexes
            assert "ix_activity_log_position_id_entry_at" in indexes

            fks = insp.get_foreign_keys("activity_log")
            matching = [
                fk
                for fk in fks
                if fk["referred_table"] == "invocations"
                and fk["constrained_columns"] == ["invocation_id"]
                and fk["referred_columns"] == ["invocation_id"]
            ]
            assert matching, f"missing FK on activity_log.invocation_id; got {fks}"
            assert matching[0]["options"].get("ondelete", "").upper() == "RESTRICT"
        finally:
            eng.dispose()

    def test_downgrade_drops_activity_log(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "activity_log" not in tables
        finally:
            eng.dispose()

    def test_upgrade_then_downgrade_then_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "activity_log" in tables
        finally:
            eng.dispose()
