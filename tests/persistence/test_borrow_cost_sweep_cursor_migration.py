"""Tests for the borrow_cost_sweep_cursor Alembic migration — ALP-590."""

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


class TestBorrowCostSweepCursorMigration:
    def test_upgrade_head_creates_table_with_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("borrow_cost_sweep_cursor")}
            for required in ("collector", "next_ticker", "updated_at"):
                assert required in cols, (
                    f"borrow_cost_sweep_cursor missing column {required!r}; got {sorted(cols)}"
                )
            # collector is the primary key; next_ticker is nullable (NULL after
            # a block-free full sweep), the other two are NOT NULL.
            pk_cols = insp.get_pk_constraint("borrow_cost_sweep_cursor")["constrained_columns"]
            assert pk_cols == ["collector"]
            assert cols["next_ticker"]["nullable"] is True
            assert cols["collector"]["nullable"] is False
            assert cols["updated_at"]["nullable"] is False
        finally:
            eng.dispose()

    def test_downgrade_one_drops_table(self, tmp_path: Path) -> None:
        """Targets the borrow-cost-sweep-cursor revision explicitly so future
        migrations on top don't shift the test's downgrade target."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "5a70d23bd62f")
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "borrow_cost_sweep_cursor" not in insp.get_table_names(), (
                "downgrade did not drop borrow_cost_sweep_cursor table"
            )
        finally:
            eng.dispose()
