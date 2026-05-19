"""Tests for the calibration_state_bootstrap_to_accumulating Alembic migration."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Literal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alphamind.persistence.session import make_engine

_REVISION = "c5d8e9a1f4b2"
_PARENT_REVISION = "b3e6f8a2c4d7"

_AFFECTED_TABLES: tuple[str, ...] = (
    "distillation_ticker_baseline",
    "distillation_pair_lag",
    "distillation_contract_history",
    "distillation_composite_state",
)


def _alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).parents[2]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _seed(db_path: Path, *, state: Literal["bootstrap", "accumulating"]) -> None:
    """Seed FK parents and one row per affected table with ``calibration_state=state``."""
    eng = make_engine(str(db_path))
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO asset_universe "
                    "(asset_id, ticker, full_name, asset_class, asset_role, exchange, "
                    "is_active, added_date, last_updated) "
                    "VALUES "
                    "('AAPL-id', 'AAPL', 'Apple Inc.', 'equity', 'core', 'NASDAQ', "
                    "1, '2026-01-01', '2026-01-01T00:00:00Z'),"
                    "('MSFT-id', 'MSFT', 'Microsoft Corp.', 'equity', 'core', 'NASDAQ', "
                    "1, '2026-01-01', '2026-01-01T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO prediction_market_contracts "
                    "(contract_id, platform, description, category, "
                    "created_at, last_seen_at) "
                    "VALUES "
                    "('CT-1', 'kalshi', 'Test contract', 'macro', "
                    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO distillation_ticker_baseline "
                    "(ticker, baseline_kind, as_of, mean, stdev, n_observations, "
                    "window_days, calibration_state, ingested_at) "
                    "VALUES "
                    "('AAPL', 'volume', '2026-04-25T00:00:00Z', 1.0, 0.1, 5, 30, "
                    f"'{state}', '2026-04-25T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO distillation_pair_lag "
                    "(lead_ticker, lag_ticker, as_of, lead_lag_days_estimate, "
                    "n_pair_events, last_overdue_flag, calibration_state, ingested_at) "
                    "VALUES "
                    "('AAPL', 'MSFT', '2026-04-25T00:00:00Z', 1.5, 3, 0, "
                    f"'{state}', '2026-04-25T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO distillation_contract_history "
                    "(contract_id, snapshot_ts, yes_probability, delta_pp_since_prior, "
                    "liquidity_usd, calibration_state, ingested_at) "
                    "VALUES "
                    "('CT-1', '2026-04-25T00:00:00Z', 0.42, 0.0, 1000.0, "
                    f"'{state}', '2026-04-25T00:00:00Z')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO distillation_composite_state "
                    "(composite_kind, as_of, composite_value, component_breakdown_json, "
                    "percentile_60d, alert_active, calibration_state, ingested_at) "
                    "VALUES "
                    "('funding_stress', '2026-04-25T00:00:00Z', 0.0, '{}', 0.5, 0, "
                    f"'{state}', '2026-04-25T00:00:00Z')"
                )
            )
    finally:
        eng.dispose()


def _read_calibration_states(db_path: Path) -> dict[str, str]:
    """Read back the calibration_state of the single row in each affected table."""
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            return {
                table: conn.execute(text(f"SELECT calibration_state FROM {table}")).scalar_one()
                for table in _AFFECTED_TABLES
            }
    finally:
        eng.dispose()


def _attempt_insert_state(db_path: Path, *, state: str) -> None:
    """Attempt to insert a composite_state row with the given calibration_state.

    Surfaces ``IntegrityError`` to the caller when the active CHECK forbids the
    value — used to prove the new/old CHECK is actually installed after a
    migration step, not merely that existing rows happen to hold the right value.
    """
    eng = make_engine(str(db_path))
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO distillation_composite_state "
                    "(composite_kind, as_of, composite_value, component_breakdown_json, "
                    "percentile_60d, alert_active, calibration_state, ingested_at) "
                    "VALUES "
                    "('market_liquidity', '2026-05-01T00:00:00Z', 0.0, '{}', 0.5, 0, "
                    f"'{state}', '2026-05-01T00:00:00Z')"
                )
            )
    finally:
        eng.dispose()


class TestCalibrationStateAccumulatingMigration:
    def test_upgrade_rewrites_bootstrap_to_accumulating_in_all_affected_tables(
        self, tmp_path: Path
    ) -> None:
        """Upgrade succeeds against a DB seeded with 'bootstrap' rows under the old CHECK.

        Regression for ALP-554: the original upgrade() emitted the UPDATE before
        dropping the old CHECK constraint, which rejected the new 'accumulating'
        value at row-write time.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _PARENT_REVISION)
        _seed(db_path, state="bootstrap")

        command.upgrade(cfg, _REVISION)

        states = _read_calibration_states(db_path)
        for table in _AFFECTED_TABLES:
            assert states[table] == "accumulating", (
                f"{table}: expected 'accumulating', got {states[table]!r}"
            )

        # New CHECK is actually installed: legacy 'bootstrap' must now be rejected.
        with pytest.raises(IntegrityError):
            _attempt_insert_state(db_path, state="bootstrap")

    def test_downgrade_rewrites_accumulating_to_bootstrap_in_all_affected_tables(
        self, tmp_path: Path
    ) -> None:
        """Downgrade succeeds against a DB seeded with 'accumulating' rows.

        Symmetric regression: the original downgrade() recreated the old CHECK
        (excluding 'accumulating') before reverting values, so the reverse
        UPDATE 'accumulating' → 'bootstrap' would fail at row-write time.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, _REVISION)
        _seed(db_path, state="accumulating")

        command.downgrade(cfg, _PARENT_REVISION)

        states = _read_calibration_states(db_path)
        for table in _AFFECTED_TABLES:
            assert states[table] == "bootstrap", (
                f"{table}: expected 'bootstrap', got {states[table]!r}"
            )

        # Old CHECK is actually reinstated: 'accumulating' must now be rejected.
        with pytest.raises(IntegrityError):
            _attempt_insert_state(db_path, state="accumulating")
