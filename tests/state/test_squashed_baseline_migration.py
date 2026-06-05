"""Tests for the squashed full-schema baseline migration (ALP-843 / W0a).

The redesign cuts over on a fresh DB, so the 46-migration chain is retired and
collapsed into one baseline with ``down_revision = None``. Asserts that
``alembic upgrade head`` on an empty database yields the full new-design schema:
the new tables exist, the carried-over collector/reference tables exist, the
``broker_event_log.event_type`` CHECK carries the full vocabulary, and the
``bracket_legs.enforcement_binding`` CHECK covers both members.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine
from alphamind.portfolio_state.records.orders import EnforcementBinding
from alphamind.state.records_broker_event_log import BrokerEventType

_BASELINE_REVISION = "a000000000aa"

_NEW_DESIGN_TABLES = {
    "broker_event_log",
    "thesis_pnl_ledger",
    "capital_reservations",
    "position_greeks",
}

# Carried-over tables the redesign does not change (collector / reference /
# pipeline + the core state-persistence tables). A representative subset — the
# baseline must materialize the whole schema, not just the new tables.
_CARRIED_OVER_TABLES = {
    "positions",
    "orders",
    "theses",
    "brackets",
    "bracket_legs",
    "cash_ledger",
    "fill_records",
    "asset_universe",
    "ohlcv_bars",
    "invocations",
    "alerts",  # command-center base
}


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _table_constraint_sql(engine_path: str, table: str) -> str:
    eng = make_engine(engine_path)
    try:
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            ).scalar_one()
        return str(row)
    finally:
        eng.dispose()


class TestSquashedBaseline:
    def test_baseline_is_the_sole_head_with_no_down_revision(self) -> None:
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        heads = script.get_heads()
        assert list(heads) == [_BASELINE_REVISION]
        rev = script.get_revision(_BASELINE_REVISION)
        assert rev is not None
        assert rev.down_revision is None

    def test_upgrade_head_creates_new_design_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "baseline.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert tables >= _NEW_DESIGN_TABLES

    def test_upgrade_head_creates_carried_over_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "baseline.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert tables >= _CARRIED_OVER_TABLES

    def test_event_type_check_carries_full_vocabulary(self, tmp_path: Path) -> None:
        db_path = tmp_path / "baseline.db"
        command.upgrade(_alembic_config(db_path), "head")

        ddl = _table_constraint_sql(str(db_path), "broker_event_log")
        for member in BrokerEventType:
            assert f"'{member.value}'" in ddl, f"missing {member.value} in event_type CHECK"

    def test_enforcement_binding_check_covers_both_members(self, tmp_path: Path) -> None:
        db_path = tmp_path / "baseline.db"
        command.upgrade(_alembic_config(db_path), "head")

        ddl = _table_constraint_sql(str(db_path), "bracket_legs")
        assert "ck_bracket_legs_enforcement_binding" in ddl
        for member in EnforcementBinding:
            assert f"'{member.value}'" in ddl

    def test_upgrade_then_downgrade_then_upgrade_is_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "baseline.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert tables >= _NEW_DESIGN_TABLES
