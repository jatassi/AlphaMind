"""Tests for the Alembic migration that adds ``theses`` + ``thesis_components``.

Story 04b / ALP-359. Pins the upgrade-then-downgrade idempotency contract
and the column-set the migration installs, mirroring the pattern from the
activity-log migration test.
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
_REVISION = "c6e3f4d5b8a9"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestThesesMigration:
    def test_upgrade_head_creates_both_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "theses" in tables
            assert "thesis_components" in tables

            theses_cols = {c["name"] for c in insp.get_columns("theses")}
            assert theses_cols == {
                "thesis_id",
                "position_id",
                "status",
                "resolution_timestamp",
                "resolution_category",
                "summary",
                "time_expectation_hours",
                "position_size_rationale",
                "generation_timestamp",
                "narrative_json",
            }

            theses_indexes = {idx["name"] for idx in insp.get_indexes("theses")}
            assert "ix_theses_status" in theses_indexes
            assert "ix_theses_position_id" in theses_indexes
            assert "ix_theses_resolution_timestamp" in theses_indexes

            component_cols = {c["name"] for c in insp.get_columns("thesis_components")}
            assert component_cols == {
                "component_id",
                "thesis_id",
                "component_type",
                "linked_bracket_leg",
                "instrument_reference",
                "narrative",
                "key_assumptions_json",
                "supporting_signals_json",
                "resolution_outcome",
                "resolution_notes",
            }

            component_indexes = {idx["name"] for idx in insp.get_indexes("thesis_components")}
            assert "ix_thesis_components_thesis_id" in component_indexes

            fks = insp.get_foreign_keys("thesis_components")
            matching = [
                fk
                for fk in fks
                if fk["referred_table"] == "theses"
                and fk["constrained_columns"] == ["thesis_id"]
                and fk["referred_columns"] == ["thesis_id"]
            ]
            assert matching, f"missing FK on thesis_components.thesis_id; got {fks}"
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
            assert "theses" not in tables
            assert "thesis_components" not in tables
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
            assert "theses" in tables
            assert "thesis_components" in tables
        finally:
            eng.dispose()
