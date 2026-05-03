"""Tests for the event_calendar.sectors Alembic migration — story 06 part E."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestEventCalendarSectorsMigration:
    def test_upgrade_head_adds_sectors_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("event_calendar")}
            assert "sectors" in cols, f"missing 'sectors' column; got {sorted(cols)}"
            # Nullable TEXT.
            assert cols["sectors"]["nullable"] is True
        finally:
            eng.dispose()

    def test_downgrade_one_drops_sectors_column(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            col_names = {c["name"] for c in insp.get_columns("event_calendar")}
            assert "sectors" not in col_names
            # The rest of the table is intact.
            assert "event_id" in col_names
            assert "scheduled_at" in col_names
        finally:
            eng.dispose()
