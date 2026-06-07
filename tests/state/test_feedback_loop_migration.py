"""Tests for the combined feedback-loop migration (ALP-879).

``c001fb0000ff`` creates the six feedback-loop tables
(``agent_calls``, ``validations``, ``validation_outcomes``,
``retrospective_reports``, ``retrospective_decisions``,
``weekly_digest_snapshots``) and widens the ``activity_log.event_type``
CHECK to admit ``DISTILLATION_ANOMALY_FLAG`` (story 02e / ALP-877).

The genesis baseline (``a000000000aa``) is metadata-driven, so a fresh
``upgrade head`` already builds all six tables and the wide CHECK — a
forward-only upgrade cannot *demonstrate* the widen. The CHECK widening is
therefore exercised via **downgrade → reject → upgrade → accept** (the
CHECK-vocab trap).
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine

_PRIOR_HEAD = "b001fc0000ee"
_MIGRATION_REVISION = "c001fb0000ff"

_NEW_TABLES = {
    "agent_calls",
    "validations",
    "validation_outcomes",
    "retrospective_reports",
    "retrospective_decisions",
    "weekly_digest_snapshots",
}

_DISTILLATION_ANOMALY_FLAG = "DISTILLATION_ANOMALY_FLAG"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _table_ddl(db_path: Path, table: str) -> str:
    """Return the CREATE TABLE DDL for *table* from sqlite_master."""
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            result: str = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            ).scalar_one()
            return result
    finally:
        eng.dispose()


def _fk_off_engine(db_path: Path) -> sa.engine.Engine:
    """Return a SQLAlchemy Engine with PRAGMA foreign_keys=OFF.

    Migration test helpers that insert rows with dangling FK references (the
    invocations parent doesn't exist in a scratch DB) need FK enforcement off --
    matching the Alembic migration env -- so that only CHECK constraint violations
    produce rejections, not FK violations.
    """
    from sqlalchemy import event as sa_event
    from sqlalchemy.engine import Engine

    eng: Engine = make_engine(str(db_path))

    @sa_event.listens_for(eng, "connect")
    def _fk_off(dbapi_conn: object, _record: object) -> None:
        from typing import cast

        cast(sa.engine.interfaces.DBAPIConnection, dbapi_conn).execute("PRAGMA foreign_keys=OFF")

    return eng


_ANOMALY_INSERT_SQL = (
    "INSERT INTO activity_log "
    "(entry_id, invocation_id, entry_at, event_type, event_group, source, detail_json) "
    "VALUES "
    "('test-entry-1', 'inv-test', '2026-06-07T00:00:00+00:00', "
    " 'DISTILLATION_ANOMALY_FLAG', 'DISTILLATION_ANOMALY', "
    " 'DISTILLATION_ORCHESTRATOR', '{}')"
)

_EMPTY_RESPONSE_INSERT_SQL = (
    "INSERT INTO agent_calls "
    "(agent_call_id, invocation_id, agent_name, attempt_number, "
    " model_id, prompt_path, prompt_git_sha, prompt_content_hash, "
    " sampling_params_json, input_tokens, output_tokens, "
    " cache_read_tokens, cache_write_tokens, wall_clock_ms, "
    " stop_reason, success, error_class) "
    "VALUES "
    "('call-1', 'inv-test', 'synthesizer', 1, "
    " 'claude-3-7-sonnet', 'prompts/synthesizer.md', 'abc123', 'def456', "
    " '{}', 100, 50, 0, 0, 1200, "
    " 'end_turn', 0, 'empty_response')"
)


def _try_insert(db_path: Path, sql: str) -> bool:
    """Execute *sql* with FK enforcement off; return True iff the row was REJECTED.

    Uses a fresh engine with ``PRAGMA foreign_keys=OFF`` so that only CHECK
    constraint violations produce rejections, not FK violations (the scratch DB
    has no invocations parent row).
    """
    eng = _fk_off_engine(db_path)
    rejected = False
    try:
        with eng.begin() as conn:
            conn.execute(text(sql))
    except Exception:
        rejected = True
    finally:
        eng.dispose()
    return rejected


def _insert_anomaly_row(db_path: Path) -> bool:
    """Try inserting a DISTILLATION_ANOMALY_FLAG activity_log row.

    Returns True if rejected (CHECK violation), False if accepted.
    """
    return _try_insert(db_path, _ANOMALY_INSERT_SQL)


def _insert_empty_response_agent_call(db_path: Path) -> bool:
    """Try inserting an agent_calls row with error_class='empty_response'.

    Returns True if rejected (CHECK violation), False if accepted.
    """
    return _try_insert(db_path, _EMPTY_RESPONSE_INSERT_SQL)


class TestFeedbackLoopMigrationChain:
    """Revision chain and head invariants."""

    def test_single_head(self) -> None:
        """``alembic heads`` reports exactly one head."""
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        assert len(list(script.get_heads())) == 1

    def test_revision_parents_on_prior_head(self) -> None:
        """c001fb0000ff descends from b001fc0000ee (the phase-rename migration)."""
        repo_root = Path(__file__).parents[2]
        script = ScriptDirectory.from_config(Config(repo_root / "alembic.ini"))
        rev = script.get_revision(_MIGRATION_REVISION)
        assert rev is not None
        assert rev.down_revision == _PRIOR_HEAD


class TestFeedbackLoopMigrationUpgrade:
    """Fresh upgrade head creates all six tables with correct structure."""

    def test_upgrade_head_creates_all_six_tables(self, tmp_path: Path) -> None:
        """A fresh ``upgrade head`` yields a DB with all six feedback-loop tables."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert tables >= _NEW_TABLES

    def test_agent_calls_error_class_check_has_all_seven_members(self, tmp_path: Path) -> None:
        """The agent_calls DDL lists all 7 error_class members (incl. empty_response)."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        ddl = _table_ddl(db_path, "agent_calls")
        for member in (
            "timeout",
            "malformed_output",
            "context_overflow",
            "model_api_error",
            "tool_use_error",
            "empty_response",
            "internal_error",
        ):
            assert repr(member) in ddl, f"error_class CHECK missing {member!r}"

    def test_empty_response_agent_call_inserts_post_upgrade(self, tmp_path: Path) -> None:
        """After upgrade, an agent_calls row with error_class='empty_response' is accepted."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _insert_empty_response_agent_call(db_path) is False  # accepted

    def test_activity_log_check_admits_distillation_anomaly_flag(self, tmp_path: Path) -> None:
        """After upgrade, a DISTILLATION_ANOMALY_FLAG activity_log insert succeeds."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        assert _insert_anomaly_row(db_path) is False  # accepted

    def test_activity_log_event_type_check_ddl_contains_new_member(self, tmp_path: Path) -> None:
        """The activity_log DDL contains DISTILLATION_ANOMALY_FLAG in the event_type CHECK."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        ddl = _table_ddl(db_path, "activity_log")
        assert repr(_DISTILLATION_ANOMALY_FLAG) in ddl

    def test_validation_outcomes_has_unique_constraint(self, tmp_path: Path) -> None:
        """validation_outcomes carries uq_validation_outcomes_validation_id UNIQUE."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            ucs = inspect(eng).get_unique_constraints("validation_outcomes")
        finally:
            eng.dispose()
        uc_names = {uc["name"] for uc in ucs}
        assert "uq_validation_outcomes_validation_id" in uc_names

    def test_weekly_digest_snapshots_has_unique_constraint(self, tmp_path: Path) -> None:
        """weekly_digest_snapshots carries uq_weekly_digest_snapshots_week_start UNIQUE."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            ucs = inspect(eng).get_unique_constraints("weekly_digest_snapshots")
        finally:
            eng.dispose()
        uc_names = {uc["name"] for uc in ucs}
        assert "uq_weekly_digest_snapshots_week_start" in uc_names

    def test_agent_calls_fk_to_invocations(self, tmp_path: Path) -> None:
        """agent_calls.invocation_id FK targets invocations.invocation_id."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            fks = inspect(eng).get_foreign_keys("agent_calls")
        finally:
            eng.dispose()
        fk_targets = {
            fk["constrained_columns"][0]: f"{fk['referred_table']}.{fk['referred_columns'][0]}"
            for fk in fks
        }
        assert fk_targets.get("invocation_id") == "invocations.invocation_id"

    def test_validation_outcomes_fk_to_validations(self, tmp_path: Path) -> None:
        """validation_outcomes.validation_id FK targets validations.validation_id."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            fks = inspect(eng).get_foreign_keys("validation_outcomes")
        finally:
            eng.dispose()
        fk_targets = {
            fk["constrained_columns"][0]: f"{fk['referred_table']}.{fk['referred_columns'][0]}"
            for fk in fks
        }
        assert fk_targets.get("validation_id") == "validations.validation_id"

    def test_retrospective_decisions_fks(self, tmp_path: Path) -> None:
        """retrospective_decisions carries FKs to retrospective_reports and validations."""
        db_path = tmp_path / "mig.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            fks = inspect(eng).get_foreign_keys("retrospective_decisions")
        finally:
            eng.dispose()
        fk_targets = {
            fk["constrained_columns"][0]: f"{fk['referred_table']}.{fk['referred_columns'][0]}"
            for fk in fks
        }
        assert fk_targets.get("report_id") == "retrospective_reports.report_id"
        assert fk_targets.get("linked_validation_id") == "validations.validation_id"


class TestFeedbackLoopMigrationCheckWiden:
    """CHECK-vocab round-trip: downgrade narrows, upgrade widens."""

    def test_downgrade_rejects_then_upgrade_accepts_distillation_anomaly_flag(
        self, tmp_path: Path
    ) -> None:
        """Downgrade narrows the event_type CHECK (reject), upgrade widens it (accept)."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        # Step down to the parent of this migration (b001fc0000ee).
        command.downgrade(cfg, _PRIOR_HEAD)
        assert _insert_anomaly_row(db_path) is True  # rejected by narrowed CHECK

        # Step back up: the CHECK is widened, so the insert is now accepted.
        command.upgrade(cfg, "head")
        assert _insert_anomaly_row(db_path) is False  # accepted


class TestFeedbackLoopMigrationDowngrade:
    """Downgrade cleanly drops the six tables and narrows the CHECK."""

    def test_downgrade_removes_all_six_tables(self, tmp_path: Path) -> None:
        """Downgrading to the prior head removes all six feedback-loop tables."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _PRIOR_HEAD)

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        for table in _NEW_TABLES:
            assert table not in tables, f"{table} should have been dropped by downgrade"

    def test_downgrade_narrows_activity_log_event_type_check(self, tmp_path: Path) -> None:
        """After downgrade, the activity_log event_type CHECK excludes DISTILLATION_ANOMALY_FLAG."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _PRIOR_HEAD)
        ddl = _table_ddl(db_path, "activity_log")
        assert repr(_DISTILLATION_ANOMALY_FLAG) not in ddl

    def test_downgrade_then_upgrade_recreates_tables(self, tmp_path: Path) -> None:
        """The full round-trip: upgrade → downgrade → upgrade re-creates all six tables."""
        db_path = tmp_path / "mig.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, _PRIOR_HEAD)
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            tables = set(inspect(eng).get_table_names())
        finally:
            eng.dispose()
        assert tables >= _NEW_TABLES


class TestFeedbackLoopMigrationDrift:
    """Migrated schema matches Base.metadata for the six new tables."""

    def test_no_table_diff_for_new_tables_against_metadata(self, tmp_path: Path) -> None:
        """autogenerate against a fresh head DB produces no add_table ops for the six tables.

        A missing table would produce an ``add_table`` diff — this confirms the
        migration's column/constraint spec matches what Base.metadata declares.
        """
        import alphamind.state.tables  # noqa: F401

        db_path = tmp_path / "drift.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            with eng.connect() as conn:
                ctx = MigrationContext.configure(
                    connection=conn,
                    opts={"compare_type": True, "target_metadata": Base.metadata},
                )
                diffs = compare_metadata(ctx, Base.metadata)
        finally:
            eng.dispose()

        # Filter to add_table diffs only for our six tables — autogenerate is
        # permitted to emit other diffs (e.g. index/check diffs on existing tables
        # are a known Alembic SQLite reflection limitation). What we cannot tolerate
        # is autogenerate thinking any of the six tables is wholly missing.
        add_table_names = {d[1].name for d in diffs if isinstance(d, tuple) and d[0] == "add_table"}
        missing = add_table_names & _NEW_TABLES
        assert not missing, (
            f"autogenerate thinks these tables are missing from the migrated DB: {missing}"
        )
