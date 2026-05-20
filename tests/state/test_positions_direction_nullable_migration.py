"""Tests for the positions_direction_nullable Alembic migration (ALP-610)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_REVISION = "d3f6a1c7e9b2"
_PRIOR_REVISION = "5a70d23bd62f"

# A minimal positions-row INSERT template — only the NOT NULL columns plus the
# discriminator + direction. The strategy / equity rows differ only in
# ``instrument_type`` and ``direction``.
_INSERT = (
    "INSERT INTO positions (position_id, status, direction, instrument_type, "
    "details_json, execution_history_json, corporate_action_adjustment_needed) "
    "VALUES ('{pid}', 'OPEN', {direction}, '{itype}', '{{}}', '[]', 0)"
)


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestPositionsDirectionNullableMigration:
    def test_upgrade_head_relaxes_direction_to_nullable(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("positions")}
            assert "direction" in cols
            assert cols["direction"]["nullable"] is True
        finally:
            eng.dispose()

    def test_upgrade_backfills_strategy_rows_to_null(self, tmp_path: Path) -> None:
        """The upgrade clears the inert 'LONG' placeholder on legacy strategy rows
        while leaving equity / options rows untouched."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        # Upgrade only to the prior revision so the column is still NOT NULL
        # and the legacy strategy row carries the 'LONG' placeholder.
        command.upgrade(cfg, _PRIOR_REVISION)

        eng = make_engine(str(db_path))
        with eng.begin() as conn:
            conn.execute(text(_INSERT.format(pid="strat-1", direction="'LONG'", itype="STRATEGY")))
            conn.execute(text(_INSERT.format(pid="eq-1", direction="'LONG'", itype="EQUITY")))
            conn.execute(text(_INSERT.format(pid="opt-1", direction="'SHORT'", itype="OPTIONS")))
        eng.dispose()

        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            with eng.connect() as conn:
                rows = {
                    r.position_id: r.direction
                    for r in conn.execute(text("SELECT position_id, direction FROM positions"))
                }
            assert rows["strat-1"] is None
            assert rows["eq-1"] == "LONG"
            assert rows["opt-1"] == "SHORT"
        finally:
            eng.dispose()

    def test_strategy_row_inserts_with_null_direction_after_upgrade(self, tmp_path: Path) -> None:
        """Once the column is nullable a fresh strategy row inserts with direction NULL."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(_INSERT.format(pid="strat-new", direction="NULL", itype="STRATEGY"))
                )
            with eng.connect() as conn:
                direction = conn.execute(
                    text("SELECT direction FROM positions WHERE position_id = 'strat-new'")
                ).scalar_one()
            assert direction is None
        finally:
            eng.dispose()

    def test_downgrade_refills_strategy_rows_and_reinstates_not_null(self, tmp_path: Path) -> None:
        """The downgrade must re-fill strategy NULLs to 'LONG' *before* re-tightening
        the constraint — a regression that swaps the order would fail the migration."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        with eng.begin() as conn:
            conn.execute(text(_INSERT.format(pid="strat-1", direction="NULL", itype="STRATEGY")))
            conn.execute(text(_INSERT.format(pid="eq-1", direction="'SHORT'", itype="EQUITY")))
        eng.dispose()

        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("positions")}
            assert cols["direction"]["nullable"] is False

            with eng.connect() as conn:
                rows = {
                    r.position_id: r.direction
                    for r in conn.execute(text("SELECT position_id, direction FROM positions"))
                }
            assert rows["strat-1"] == "LONG"
            assert rows["eq-1"] == "SHORT"
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
            cols = {c["name"]: c for c in insp.get_columns("positions")}
            assert cols["direction"]["nullable"] is True
        finally:
            eng.dispose()
