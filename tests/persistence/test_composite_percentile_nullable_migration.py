"""Tests for the composite_percentile_nullable Alembic migration."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from alphamind.persistence.session import make_engine

_REVISION = "05a4f2c8d691"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestCompositePercentileNullableMigration:
    def test_upgrade_head_relaxes_percentile_60d_to_nullable(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("distillation_composite_state")}
            assert "percentile_60d" in cols
            assert cols["percentile_60d"]["nullable"] is True
        finally:
            eng.dispose()

    def test_downgrade_backfills_nulls_to_zero_and_reinstates_not_null(
        self, tmp_path: Path
    ) -> None:
        """The downgrade must convert NULL → 0.0 *before* re-tightening the constraint.

        Pin the order — a regression that swaps these steps would only surface
        in production against a row written under the new publish semantics.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO distillation_composite_state "
                    "(composite_kind, as_of, composite_value, component_breakdown_json, "
                    "percentile_60d, alert_active, calibration_state, ingested_at) "
                    "VALUES "
                    "('funding_stress', '2026-04-25T00:00:00Z', 0.0, '{}', NULL, 0, "
                    "'accumulating', '2026-04-25T00:00:00Z')"
                )
            )
        eng.dispose()

        command.downgrade(cfg, "-1")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            cols = {c["name"]: c for c in insp.get_columns("distillation_composite_state")}
            assert cols["percentile_60d"]["nullable"] is False

            with eng.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT percentile_60d FROM distillation_composite_state "
                        "WHERE as_of = '2026-04-25T00:00:00Z'"
                    )
                ).one()
            assert row.percentile_60d == 0.0
        finally:
            eng.dispose()
