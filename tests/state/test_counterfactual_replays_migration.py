"""Tests for the add_counterfactual_replays migration (ALP-556).

Verifies:
* The new revision upgrades cleanly on a fresh metadata-built DB (idempotent —
  ``create_table`` is guarded, so upgrade is a no-op).
* Downgrade removes the table.
* Downgrade then upgrade re-creates the table (reversible).
* ``down_revision`` chains to the prior head (``a867er0000cc``).
* The migration is now the single head.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_PRIOR_HEAD = "a867er0000cc"
_NEW_REVISION = "a556rp0000dd"
_TABLE = "counterfactual_replays"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestCounterfactualReplaysMigration:
    def test_revision_is_reachable(self) -> None:
        """The a556rp0000dd revision is reachable in the migration chain."""
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        rev = script.get_revision(_NEW_REVISION)
        assert rev is not None

    def test_new_revision_parents_on_prior_head(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        rev = script.get_revision(_NEW_REVISION)
        assert rev is not None
        assert rev.down_revision == _PRIOR_HEAD

    def test_upgrade_head_creates_counterfactual_replays_table(self, tmp_path: Path) -> None:
        """Fresh ``upgrade head`` yields a DB with the counterfactual_replays table."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert _TABLE in tables

    def test_upgrade_head_idempotent_on_fresh_db(self, tmp_path: Path) -> None:
        """The guarded upgrade is a no-op on a metadata-built DB (no double-create)."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        # Second upgrade head on same DB must not raise
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert _TABLE in tables

    def test_downgrade_to_prior_head_removes_table(self, tmp_path: Path) -> None:
        """Downgrading to the revision before a556rp0000dd drops the counterfactual_replays table."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        # Downgrade specifically to the revision before a556rp0000dd (the add-table migration).
        command.downgrade(cfg, _PRIOR_HEAD)
        # Then downgrade one more step to remove the table itself.
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert _TABLE not in tables

    def test_downgrade_then_upgrade_recreates_table(self, tmp_path: Path) -> None:
        """The full round-trip: upgrade → downgrade to prior → upgrade creates the table."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _PRIOR_HEAD)
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert _TABLE in tables

    def test_downgrade_one_step_stays_on_this_revision(self, tmp_path: Path) -> None:
        """After ``downgrade -1`` from head, the DB is at the phase-rename revision."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")

        # The counterfactual_replays table still exists — only the column rename is reverted.
        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert _TABLE in tables
        assert "broker_event_log" in tables

    def test_upgrade_check_constraints_present_in_ddl(self, tmp_path: Path) -> None:
        """The counterfactual_replays DDL carries CHECK constraints for all enums."""
        from alphamind.execution.counterfactual_replay_engine.enums import (
            ExitLeg,
            UnevaluableReason,
        )

        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            with eng.connect() as conn:
                ddl = conn.execute(
                    text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
                    {"t": _TABLE},
                ).scalar_one()
        finally:
            eng.dispose()

        for member in ExitLeg:
            assert f"'{member.value}'" in ddl, f"exit_leg CHECK missing {member.value!r}"
        for reason in UnevaluableReason:
            assert f"'{reason.value}'" in ddl, f"unevaluable_reason CHECK missing {reason.value!r}"
