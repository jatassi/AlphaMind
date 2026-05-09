"""Tests for the Alembic migration that adds ``brackets`` and ``bracket_legs``
(story 04d / ALP-361).

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
_REVISION = "d7e3f4a5b6c8"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


_BRACKETS_COLS = {
    "bracket_id",
    "position_id",
    "status",
    "entry_order_id",
    "entry_window_deadline",
    "corporate_action_cancellation_reason",
    "modification_history_json",
}

_BRACKET_LEGS_COLS = {
    "bracket_leg_id",
    "bracket_id",
    "leg_index",
    "leg_type",
    "order_id",
    "trigger_kind",
    "trigger_payload_json",
    "pl_anchor_json",
    "enforcement",
    "leg_status",
}


class TestBracketsAndLegsMigration:
    def test_upgrade_head_creates_both_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "brackets" in tables
            assert "bracket_legs" in tables

            assert {c["name"] for c in insp.get_columns("brackets")} == _BRACKETS_COLS
            assert {c["name"] for c in insp.get_columns("bracket_legs")} == _BRACKET_LEGS_COLS

            # ix_brackets_position_id must be UNIQUE (1:1 with positions).
            br_indexes = {idx["name"]: idx for idx in insp.get_indexes("brackets")}
            assert "ix_brackets_position_id" in br_indexes
            assert bool(br_indexes["ix_brackets_position_id"]["unique"]) is True
            assert "ix_brackets_status" in br_indexes

            # uq_bracket_legs_bracket_id_leg_index must be UNIQUE.
            leg_indexes = {idx["name"]: idx for idx in insp.get_indexes("bracket_legs")}
            assert "ix_bracket_legs_bracket_id" in leg_indexes
            assert "uq_bracket_legs_bracket_id_leg_index" in leg_indexes
            assert bool(leg_indexes["uq_bracket_legs_bracket_id_leg_index"]["unique"]) is True

            # FK on bracket_legs.bracket_id with ON DELETE RESTRICT.
            fks = insp.get_foreign_keys("bracket_legs")
            matching = [
                fk
                for fk in fks
                if fk["referred_table"] == "brackets"
                and fk["constrained_columns"] == ["bracket_id"]
                and fk["referred_columns"] == ["bracket_id"]
            ]
            assert matching, f"missing FK on bracket_legs.bracket_id; got {fks}"
            assert matching[0]["options"].get("ondelete", "").upper() == "RESTRICT"
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
            assert "brackets" not in tables
            assert "bracket_legs" not in tables
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
            assert "brackets" in tables
            assert "bracket_legs" in tables
        finally:
            eng.dispose()
