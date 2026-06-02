"""Tests for the Alembic migration that adds ``escalated`` to ``unattributed_fills`` (ALP-771).

Pins the upgrade-then-downgrade idempotency contract and the column-set at head.
The revision is pinned so a future migration does not silently shift the
downgrade target. The downgrade removes the ``escalated`` column but keeps the
table (``c1b2a3d4e5f6`` revises ``a2f8c1d4e6b9`` which created the table).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine

_REVISION = "c1b2a3d4e5f6"

_EXPECTED_COLS = {
    "broker_fill_key",
    "alpaca_order_id",
    "client_order_id",
    "event_type",
    "fill_timestamp",
    "fill_price",
    "fill_quantity",
    "raw_report_json",
    "first_seen_at",
    "last_retry_at",
    "retry_count",
    "alerted",
    "escalated",
}


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestUnattributedFillsMigration:
    def test_upgrade_head_creates_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "unattributed_fills" in set(insp.get_table_names())
            assert {c["name"] for c in insp.get_columns("unattributed_fills")} == _EXPECTED_COLS

            index_names = {idx["name"] for idx in insp.get_indexes("unattributed_fills")}
            assert "ix_unattributed_fills_alpaca_order_id" in index_names

            # The whole point of the table: no FK to orders.
            assert insp.get_foreign_keys("unattributed_fills") == []
        finally:
            eng.dispose()

    def test_downgrade_removes_escalated_column(self, tmp_path: Path) -> None:
        """Downgrading c1b2a3d4e5f6 → a2f8c1d4e6b9 drops ``escalated``; table stays."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "unattributed_fills" in set(insp.get_table_names())
            col_names = {c["name"] for c in insp.get_columns("unattributed_fills")}
            assert "escalated" not in col_names
            assert "alerted" in col_names
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
            assert "unattributed_fills" in set(insp.get_table_names())
        finally:
            eng.dispose()
