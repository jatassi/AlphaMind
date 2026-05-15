"""Tests for the Alembic migration that adds ``cash_ledger`` and
``drawdown_state`` (story 04e / ALP-362).

Per the parent issue's pre-resolved decision (E), each table-creation
story owns its own migration. This story bundles both singleton tables in
a single migration; the test pins the upgrade-then-downgrade idempotency
contract and the column set the migration installs.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine

# Pin the migration revision so a future migration that lands on top
# doesn't silently shift this test's downgrade target.
_REVISION = "c8e1a4b6d3f2"

_CASH_COLUMNS = {
    "id",
    "current_cash_usd",
    "settled_cash_usd",
    "reserved_capital_usd",
    "available_buying_power_usd",
    "margin_held_usd",
    "unsettled_proceeds_json",
    "last_updated_at",
}
_DRAWDOWN_COLUMNS = {
    "id",
    "equity_high_water_mark_usd",
    "current_drawdown_pct",
    "drawdown_duration_hours",
    "lifetime_max_drawdown_pct",
    "drawdown_by_source_json",
    "last_updated_at",
}


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestCashLedgerAndDrawdownStateMigration:
    def test_upgrade_head_creates_both_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "cash_ledger" in tables
            assert "drawdown_state" in tables

            cash_cols = {c["name"] for c in insp.get_columns("cash_ledger")}
            assert cash_cols == _CASH_COLUMNS

            drawdown_cols = {c["name"] for c in insp.get_columns("drawdown_state")}
            assert drawdown_cols == _DRAWDOWN_COLUMNS

            cash_pk = insp.get_pk_constraint("cash_ledger")
            assert cash_pk["constrained_columns"] == ["id"]

            drawdown_pk = insp.get_pk_constraint("drawdown_state")
            assert drawdown_pk["constrained_columns"] == ["id"]
        finally:
            eng.dispose()

    def test_downgrade_drops_both_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "cash_ledger" not in tables
            assert "drawdown_state" not in tables
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
            assert "cash_ledger" in tables
            assert "drawdown_state" in tables
        finally:
            eng.dispose()
