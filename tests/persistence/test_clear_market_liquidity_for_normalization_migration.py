"""Tests for the clear_market_liquidity_for_normalization Alembic migration (ALP-575)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from alphamind.persistence.session import make_engine

_REVISION = "4f1e9b8c7a52"
_PRIOR_REVISION = "2c5e9b7f8d12"


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestClearMarketLiquidityForNormalizationMigration:
    def test_upgrade_deletes_market_liquidity_rows(self, tmp_path: Path) -> None:
        """The upgrade removes existing ``market_liquidity`` rows (raw-sum scale)
        while leaving sibling composite kinds (``funding_stress``) intact."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _PRIOR_REVISION)

        eng = make_engine(str(db_path))
        with eng.begin() as conn:
            for kind, as_of in (
                ("market_liquidity", "2026-04-25T00:00:00Z"),
                ("market_liquidity", "2026-04-26T00:00:00Z"),
                ("funding_stress", "2026-04-25T00:00:00Z"),
            ):
                conn.execute(
                    text(
                        "INSERT INTO distillation_composite_state "
                        "(composite_kind, as_of, composite_value, component_breakdown_json, "
                        "percentile_60d, alert_active, calibration_state, ingested_at) "
                        "VALUES (:kind, :as_of, 0.0, '{}', NULL, 0, 'accumulating', :as_of)"
                    ),
                    {"kind": kind, "as_of": as_of},
                )
        eng.dispose()

        command.upgrade(cfg, _REVISION)

        eng = make_engine(str(db_path))
        try:
            with eng.connect() as conn:
                kinds = sorted(
                    row.composite_kind
                    for row in conn.execute(
                        text("SELECT composite_kind FROM distillation_composite_state")
                    ).all()
                )
            assert kinds == ["funding_stress"]
        finally:
            eng.dispose()

    def test_downgrade_is_a_no_op(self, tmp_path: Path) -> None:
        """The downgrade does not error and does not modify other tables."""
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        command.downgrade(cfg, "-1")
