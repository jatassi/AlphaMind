"""Tests for the migration that extends ``ck_positions_status`` with
``"CANCELLED"`` and backfills the two stranded PENDING rows (ALP-744).

The migration drops + recreates the CHECK constraint with the extended
vocabulary so the cancel/dissolve write path can persist a never-filled
position's terminal CANCELLED state, then backfills the two known stranded
rows. The downgrade refuses to run if any row already carries the new value.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine, make_session_factory

_REVISION = "b6e1f9a4c3d2"
_PREVIOUS = "e2c4a8f1d5b3"

# The two rows the backfill targets (ALP-744 prod evidence, 2026-05-29).
_SCHW = "POS-SCHW-3241748bbea254ca86e646eb1022adb7"
_ZS = "POS-ZS-ecd62928cd9e5e6e9e7c14e9971a7d08"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _insert_position(
    db_path: Path,
    *,
    position_id: str,
    status: str,
    direction: str = "SHORT",
) -> None:
    """Insert a never-filled equity position row (empty history, no entry).

    thesis_id / bracket_id are left NULL so no FK into theses / brackets needs
    satisfying — the migration's backfill keys on status + position_id only.
    """
    eng = make_engine(str(db_path))
    try:
        factory = make_session_factory(eng)
        with factory() as session:
            session.execute(
                text(
                    "INSERT INTO positions ("
                    "position_id, thesis_id, bracket_id, status, direction, "
                    "entry_timestamp, instrument_type, details_json, "
                    "execution_history_json, realized_pnl_to_date_usd, "
                    "corporate_action_adjustment_needed, parent_position_id, origin"
                    ") VALUES ("
                    ":pid, NULL, NULL, :status, :direction, NULL, 'EQUITY', '{}', "
                    "'[]', NULL, 0, NULL, NULL)"
                ),
                {"pid": position_id, "status": status, "direction": direction},
            )
            session.commit()
    finally:
        eng.dispose()


def _read_status(db_path: Path, position_id: str) -> str | None:
    eng = make_engine(str(db_path))
    try:
        factory = make_session_factory(eng)
        with factory() as session:
            return session.execute(
                text("SELECT status FROM positions WHERE position_id = :pid"),
                {"pid": position_id},
            ).scalar_one_or_none()
    finally:
        eng.dispose()


class TestPositionsStatusCancelledMigration:
    def test_cancelled_value_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """The new terminal value the migration pins must round-trip cleanly."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        _insert_position(db_path, position_id="pos-cancelled", status="CANCELLED")
        assert _read_status(db_path, "pos-cancelled") == "CANCELLED"

    def test_pre_existing_values_still_accepted_after_upgrade(self, tmp_path: Path) -> None:
        """Existing statuses keep round-tripping under the extended constraint."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        for idx, status in enumerate(("PENDING", "OPEN", "CLOSED")):
            _insert_position(db_path, position_id=f"pos-{idx}", status=status)
            assert _read_status(db_path, f"pos-{idx}") == status

    def test_downgrade_narrows_then_upgrade_rewidens(self, tmp_path: Path) -> None:
        """The genuine widen proof: downgrade narrows the constraint back to the
        3-value vocabulary (CANCELLED rejected), and re-upgrading admits it again.

        Note: we cannot assert "CANCELLED rejected at the previous revision" by
        upgrading forward to ``_PREVIOUS`` — the historical ``c8e3f4a2b1d6``
        add_positions migration builds ``ck_positions_status`` by iterating the
        *live* ``PositionStatus`` enum, so once CANCELLED is an enum member that
        migration already bakes it in on a freshly-built DB. The existing
        production table (created when the enum had only three values) carries
        the narrow constraint this migration actually widens; our own downgrade
        reconstructs that narrow baseline to test against.
        """
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        with pytest.raises(IntegrityError):
            _insert_position(db_path, position_id="pos-cancelled-narrow", status="CANCELLED")

        command.upgrade(cfg, _REVISION)
        _insert_position(db_path, position_id="pos-cancelled-wide", status="CANCELLED")
        assert _read_status(db_path, "pos-cancelled-wide") == "CANCELLED"

    def test_backfill_transitions_stranded_pending_rows(self, tmp_path: Path) -> None:
        """The two named stranded rows move PENDING → CANCELLED; an unrelated
        PENDING row is left untouched."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _PREVIOUS)
        _insert_position(db_path, position_id=_SCHW, status="PENDING")
        _insert_position(db_path, position_id=_ZS, status="PENDING")
        _insert_position(db_path, position_id="pos-healthy-pending", status="PENDING")

        command.upgrade(cfg, _REVISION)

        assert _read_status(db_path, _SCHW) == "CANCELLED"
        assert _read_status(db_path, _ZS) == "CANCELLED"
        # An ordinary still-resting PENDING position must not be swept up.
        assert _read_status(db_path, "pos-healthy-pending") == "PENDING"

    def test_check_constraint_present_after_upgrade(self, tmp_path: Path) -> None:
        """The recreated CHECK constraint shows up in the schema with CANCELLED."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")
        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            checks = insp.get_check_constraints("positions")
            by_name = {c["name"]: c for c in checks}
            assert "ck_positions_status" in by_name
            assert "CANCELLED" in by_name["ck_positions_status"]["sqltext"]
        finally:
            eng.dispose()

    def test_upgrade_down_up_cycle_idempotent(self, tmp_path: Path) -> None:
        """up → down → up leaves the extended constraint in place."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, _PREVIOUS)
        command.upgrade(cfg, _REVISION)
        _insert_position(db_path, position_id="pos-cancelled", status="CANCELLED")
        assert _read_status(db_path, "pos-cancelled") == "CANCELLED"

    def test_downgrade_refuses_when_cancelled_rows_exist(self, tmp_path: Path) -> None:
        """Downgrade must abort rather than orphan rows under a stricter constraint."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _insert_position(db_path, position_id="pos-cancelled", status="CANCELLED")
        with pytest.raises(RuntimeError, match="CANCELLED"):
            command.downgrade(cfg, _PREVIOUS)

    def test_downgrade_succeeds_when_no_cancelled_rows(self, tmp_path: Path) -> None:
        """Clean downgrade restores the pre-migration vocabulary."""
        from sqlalchemy.exc import IntegrityError

        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _insert_position(db_path, position_id="pos-keep", status="OPEN")
        command.downgrade(cfg, _PREVIOUS)
        # After downgrade the previous constraint is back: CANCELLED rejected.
        with pytest.raises(IntegrityError):
            _insert_position(db_path, position_id="pos-postdown", status="CANCELLED")
