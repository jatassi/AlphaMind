"""Tests for the Alembic migration that adds the three command-center
tables (story 02 / ALP-666).

Per the parent issue's pre-resolved decision (E) (each table-creation
story owns its own migration alongside its SQLAlchemy model), this test
pins the upgrade-then-downgrade idempotency contract and the column set
the migration installs for ``alerts`` / ``webauthn_credentials`` /
``operator_sessions``.

The migration's ``down_revision`` is ``a2c4e6f8b1d3`` — the Wave-1 merge
migration that resolved the forked head. Verifying linear chaining is
the orchestrator's responsibility (run ``ScriptDirectory.get_heads()``);
this test exercises the schema effect.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from alphamind.persistence.session import make_engine

# Pin the migration revision so a future migration that lands on top
# doesn't silently shift this test's downgrade target. The revision id
# is structured around the issue: ``cc`` for command-center, then a
# 12-hex suffix matching the existing migration revid convention.
_REVISION = "cc1f2e3d4a5b"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestCommandCenterTablesMigration:
    def test_upgrade_head_creates_all_three_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "alerts" in tables
            assert "webauthn_credentials" in tables
            assert "operator_sessions" in tables
        finally:
            eng.dispose()

    def test_alerts_has_expected_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"] for c in insp.get_columns("alerts")}
            assert cols == {
                "alert_id",
                "rule_name",
                "severity",
                "status",
                "fired_at",
                "acknowledged_at",
                "snoozed_until",
                "context_json",
            }
        finally:
            eng.dispose()

    def test_webauthn_credentials_has_expected_columns(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"] for c in insp.get_columns("webauthn_credentials")}
            assert cols == {
                "credential_id",
                "public_key",
                "sign_count",
                "transports",
                "created_at",
            }
        finally:
            eng.dispose()

    def test_operator_sessions_has_expected_columns_and_fk(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"] for c in insp.get_columns("operator_sessions")}
            assert cols == {
                "session_id",
                "credential_id",
                "expires_at",
                "csrf_token_hash",
                "created_at",
            }

            fks = insp.get_foreign_keys("operator_sessions")
            matching = [
                fk
                for fk in fks
                if fk["referred_table"] == "webauthn_credentials"
                and fk["constrained_columns"] == ["credential_id"]
                and fk["referred_columns"] == ["credential_id"]
            ]
            assert matching, f"missing FK on operator_sessions.credential_id; got {fks}"
            assert matching[0]["options"].get("ondelete", "").upper() == "RESTRICT"
        finally:
            eng.dispose()

    def test_downgrade_drops_all_three_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            tables = set(insp.get_table_names())
            assert "alerts" not in tables
            assert "webauthn_credentials" not in tables
            assert "operator_sessions" not in tables
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
            assert "alerts" in tables
            assert "webauthn_credentials" in tables
            assert "operator_sessions" in tables
        finally:
            eng.dispose()


class TestMigrationGraphStaysLinear:
    """Single-head invariant — the new migration chains off the Wave 1
    merge head, so ``ScriptDirectory.get_heads()`` remains a one-element
    list."""

    def test_single_head_after_new_migration(self) -> None:
        from alembic.script import ScriptDirectory

        repo_root = Path(__file__).parents[3]
        cfg = Config(repo_root / "alembic.ini")
        heads = ScriptDirectory.from_config(cfg).get_heads()
        assert list(heads) == [_REVISION]
