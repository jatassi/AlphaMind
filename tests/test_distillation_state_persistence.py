"""
Distillation state persistence tests — story 02-distillation-layer/03.

Each test targets one acceptance criterion: round-trip insert + select for
every model, composite-key uniqueness violations, FK violations, CHECK
constraints, and alembic upgrade/downgrade cycle.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationCompositeState,
    DistillationContractHistory,
    DistillationEventHistory,
    DistillationPairLag,
    DistillationRegimeState,
    DistillationTickerBaseline,
    PredictionMarketContracts,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with all distillation tables."""
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Session bound to the in-memory engine."""
    session_factory = make_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture()
def seeded_universe(session: Session) -> AssetUniverse:
    """Insert minimal AssetUniverse rows (AAPL, MSFT) for FK satisfaction."""
    aapl = AssetUniverse(
        asset_id="asset-aapl",
        ticker=Symbol("AAPL"),
        full_name="Apple Inc.",
        asset_class="equity",
        asset_role="universe",
        exchange="NASDAQ",
        is_active=1,
        added_date="2020-01-01",
        last_updated="2026-04-26T00:00:00Z",
    )
    msft = AssetUniverse(
        asset_id="asset-msft",
        ticker=Symbol("MSFT"),
        full_name="Microsoft Corp.",
        asset_class="equity",
        asset_role="universe",
        exchange="NASDAQ",
        is_active=1,
        added_date="2020-01-01",
        last_updated="2026-04-26T00:00:00Z",
    )
    session.add_all([aapl, msft])
    session.commit()
    return aapl


@pytest.fixture()
def seeded_contract(session: Session) -> PredictionMarketContracts:
    """Insert a minimal prediction-market contract for FK satisfaction."""
    row = PredictionMarketContracts(
        contract_id="pm-001",
        platform="polymarket",
        description="Fed cuts in May?",
        category="monetary_policy",
        created_at="2026-04-01T00:00:00Z",
        last_seen_at="2026-04-26T00:00:00Z",
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# AC: Round-trip insert + select for every model
# ---------------------------------------------------------------------------


class TestRoundTrips:
    def test_distillation_ticker_baseline(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        row = DistillationTickerBaseline(
            ticker=Symbol("AAPL"),
            baseline_kind="volume",
            as_of="2026-04-25T00:00:00Z",
            mean=50_000_000.0,
            stdev=10_000_000.0,
            n_observations=20,
            window_days=20,
            calibration_state="calibrated",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(
            DistillationTickerBaseline, ("AAPL", "volume", "2026-04-25T00:00:00Z")
        )
        assert fetched is not None
        assert fetched.mean == 50_000_000.0
        assert fetched.stdev == 10_000_000.0
        assert fetched.n_observations == 20
        assert fetched.window_days == 20
        assert fetched.calibration_state == "calibrated"

    def test_distillation_pair_lag(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = DistillationPairLag(
            lead_ticker="AAPL",
            lag_ticker="MSFT",
            as_of="2026-04-25T00:00:00Z",
            lead_lag_days_estimate=2.5,
            n_pair_events=12,
            last_overdue_flag=0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(DistillationPairLag, ("AAPL", "MSFT", "2026-04-25T00:00:00Z"))
        assert fetched is not None
        assert fetched.lead_lag_days_estimate == 2.5
        assert fetched.n_pair_events == 12
        assert fetched.last_overdue_flag == 0

    def test_distillation_contract_history(
        self, session: Session, seeded_contract: PredictionMarketContracts
    ) -> None:
        row = DistillationContractHistory(
            contract_id="pm-001",
            snapshot_ts="2026-04-26T14:00:00Z",
            yes_probability=0.62,
            delta_pp_since_prior=3.5,
            liquidity_usd=125_000.0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T14:01:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(DistillationContractHistory, ("pm-001", "2026-04-26T14:00:00Z"))
        assert fetched is not None
        assert fetched.yes_probability == 0.62
        assert fetched.delta_pp_since_prior == 3.5

    def test_distillation_event_history(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        row = DistillationEventHistory(
            ticker=Symbol("AAPL"),
            event_kind="gap",
            event_ts="2026-04-25T13:30:00Z",
            direction="up",
            magnitude_atr_multiple=2.4,
            outcome="filled",
            outcome_observed_at="2026-04-25T17:00:00Z",
            ingested_at="2026-04-25T13:31:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(
            DistillationEventHistory,
            ("AAPL", "gap", "2026-04-25T13:30:00Z"),
        )
        assert fetched is not None
        assert fetched.direction == "up"
        assert fetched.magnitude_atr_multiple == 2.4
        assert fetched.outcome == "filled"
        assert fetched.outcome_observed_at == "2026-04-25T17:00:00Z"

    def test_distillation_event_history_outcome_observed_at_nullable(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        """outcome_observed_at is nullable until the outcome resolves."""
        row = DistillationEventHistory(
            ticker=Symbol("AAPL"),
            event_kind="extended_hours",
            event_ts="2026-04-25T20:00:00Z",
            direction="down",
            magnitude_atr_multiple=1.1,
            outcome="confirmed",
            outcome_observed_at=None,
            ingested_at="2026-04-25T20:01:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(
            DistillationEventHistory,
            ("AAPL", "extended_hours", "2026-04-25T20:00:00Z"),
        )
        assert fetched is not None
        assert fetched.outcome_observed_at is None

    def test_distillation_regime_state(self, session: Session) -> None:
        row = DistillationRegimeState(
            as_of="2026-04-26T13:30:00Z",
            regime_label="low_vol_compression",
            vix_level=14.5,
            term_structure_basis=-0.6,
            vvix_percentile=22.0,
            realized_vol=11.2,
            indicator_agreement_count=4,
            invocations_held=12,
            transition_state="stable",
            prior_label="low_vol_compression",
            ingested_at="2026-04-26T13:30:01Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(DistillationRegimeState, "2026-04-26T13:30:00Z")
        assert fetched is not None
        assert fetched.regime_label == "low_vol_compression"
        assert fetched.transition_state == "stable"
        assert fetched.invocations_held == 12

    def test_distillation_composite_state(self, session: Session) -> None:
        row = DistillationCompositeState(
            composite_kind="funding_stress",
            as_of="2026-04-26T13:30:00Z",
            composite_value=0.42,
            component_breakdown_json='{"sofr_iorb_bp": 5, "tga_drain_30d": 200}',
            percentile_60d=68.0,
            alert_active=0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T13:30:01Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(
            DistillationCompositeState, ("funding_stress", "2026-04-26T13:30:00Z")
        )
        assert fetched is not None
        assert fetched.composite_value == 0.42
        assert fetched.percentile_60d == 68.0
        assert fetched.alert_active == 0


# ---------------------------------------------------------------------------
# AC: Composite-key uniqueness violations raise IntegrityError
# ---------------------------------------------------------------------------


class TestCompositeKeyUniqueness:
    def test_distillation_ticker_baseline_duplicate_raises(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        def make_row() -> DistillationTickerBaseline:
            return DistillationTickerBaseline(
                ticker=Symbol("AAPL"),
                baseline_kind="volume",
                as_of="2026-04-25T00:00:00Z",
                mean=50_000_000.0,
                stdev=10_000_000.0,
                n_observations=20,
                window_days=20,
                calibration_state="calibrated",
                ingested_at="2026-04-26T00:00:00Z",
            )

        session.add(make_row())
        session.commit()
        session.add(make_row())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_distillation_pair_lag_duplicate_raises(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        def make_row() -> DistillationPairLag:
            return DistillationPairLag(
                lead_ticker="AAPL",
                lag_ticker="MSFT",
                as_of="2026-04-25T00:00:00Z",
                lead_lag_days_estimate=2.5,
                n_pair_events=12,
                last_overdue_flag=0,
                calibration_state="calibrated",
                ingested_at="2026-04-26T00:00:00Z",
            )

        session.add(make_row())
        session.commit()
        session.add(make_row())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_distillation_contract_history_duplicate_raises(
        self, session: Session, seeded_contract: PredictionMarketContracts
    ) -> None:
        def make_row() -> DistillationContractHistory:
            return DistillationContractHistory(
                contract_id="pm-001",
                snapshot_ts="2026-04-26T14:00:00Z",
                yes_probability=0.62,
                delta_pp_since_prior=3.5,
                liquidity_usd=125_000.0,
                calibration_state="calibrated",
                ingested_at="2026-04-26T14:01:00Z",
            )

        session.add(make_row())
        session.commit()
        session.add(make_row())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_distillation_event_history_duplicate_raises(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        def make_row() -> DistillationEventHistory:
            return DistillationEventHistory(
                ticker=Symbol("AAPL"),
                event_kind="gap",
                event_ts="2026-04-25T13:30:00Z",
                direction="up",
                magnitude_atr_multiple=2.4,
                outcome="filled",
                ingested_at="2026-04-25T13:31:00Z",
            )

        session.add(make_row())
        session.commit()
        session.add(make_row())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_distillation_regime_state_duplicate_raises(self, session: Session) -> None:
        def make_row() -> DistillationRegimeState:
            return DistillationRegimeState(
                as_of="2026-04-26T13:30:00Z",
                regime_label="low_vol_compression",
                vix_level=14.5,
                term_structure_basis=-0.6,
                vvix_percentile=22.0,
                realized_vol=11.2,
                indicator_agreement_count=4,
                invocations_held=12,
                transition_state="stable",
                prior_label="low_vol_compression",
                ingested_at="2026-04-26T13:30:01Z",
            )

        session.add(make_row())
        session.commit()
        session.add(make_row())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_distillation_composite_state_duplicate_raises(self, session: Session) -> None:
        def make_row() -> DistillationCompositeState:
            return DistillationCompositeState(
                composite_kind="funding_stress",
                as_of="2026-04-26T13:30:00Z",
                composite_value=0.42,
                component_breakdown_json='{"x": 1}',
                percentile_60d=68.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at="2026-04-26T13:30:01Z",
            )

        session.add(make_row())
        session.commit()
        session.add(make_row())
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# AC: Foreign-key violations raise IntegrityError
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    def test_ticker_baseline_fk_ticker(self, session: Session) -> None:
        row = DistillationTickerBaseline(
            ticker=Symbol("NONEXISTENT"),
            baseline_kind="volume",
            as_of="2026-04-25T00:00:00Z",
            mean=1.0,
            stdev=1.0,
            n_observations=1,
            window_days=20,
            calibration_state="calibrated",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_pair_lag_fk_lead_ticker(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        row = DistillationPairLag(
            lead_ticker="NONEXISTENT",
            lag_ticker="AAPL",
            as_of="2026-04-25T00:00:00Z",
            lead_lag_days_estimate=1.0,
            n_pair_events=1,
            last_overdue_flag=0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_pair_lag_fk_lag_ticker(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = DistillationPairLag(
            lead_ticker="AAPL",
            lag_ticker="NONEXISTENT",
            as_of="2026-04-25T00:00:00Z",
            lead_lag_days_estimate=1.0,
            n_pair_events=1,
            last_overdue_flag=0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_event_history_fk_ticker(self, session: Session) -> None:
        row = DistillationEventHistory(
            ticker=Symbol("NONEXISTENT"),
            event_kind="gap",
            event_ts="2026-04-25T13:30:00Z",
            direction="up",
            magnitude_atr_multiple=1.0,
            outcome="filled",
            ingested_at="2026-04-25T13:31:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_contract_history_fk_contract_id(self, session: Session) -> None:
        row = DistillationContractHistory(
            contract_id="NONEXISTENT",
            snapshot_ts="2026-04-26T14:00:00Z",
            yes_probability=0.5,
            delta_pp_since_prior=0.0,
            liquidity_usd=1.0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T14:01:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# AC: CHECK constraints
# ---------------------------------------------------------------------------


class TestCheckConstraints:
    def test_calibration_state_rejects_invalid_value_ticker_baseline(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        row = DistillationTickerBaseline(
            ticker=Symbol("AAPL"),
            baseline_kind="volume",
            as_of="2026-04-25T00:00:00Z",
            mean=1.0,
            stdev=1.0,
            n_observations=1,
            window_days=20,
            calibration_state="invalid_state",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_regime_label_rejects_invalid_value(self, session: Session) -> None:
        row = DistillationRegimeState(
            as_of="2026-04-26T13:30:00Z",
            regime_label="not_a_real_regime",
            vix_level=14.5,
            term_structure_basis=-0.6,
            vvix_percentile=22.0,
            realized_vol=11.2,
            indicator_agreement_count=4,
            invocations_held=12,
            transition_state="stable",
            prior_label="low_vol_compression",
            ingested_at="2026-04-26T13:30:01Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_transition_state_rejects_invalid_value(self, session: Session) -> None:
        row = DistillationRegimeState(
            as_of="2026-04-26T13:30:00Z",
            regime_label="low_vol_compression",
            vix_level=14.5,
            term_structure_basis=-0.6,
            vvix_percentile=22.0,
            realized_vol=11.2,
            indicator_agreement_count=4,
            invocations_held=12,
            transition_state="not_a_state",
            prior_label="low_vol_compression",
            ingested_at="2026-04-26T13:30:01Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_baseline_kind_rejects_invalid_value(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        row = DistillationTickerBaseline(
            ticker=Symbol("AAPL"),
            baseline_kind="not_a_real_kind",
            as_of="2026-04-25T00:00:00Z",
            mean=1.0,
            stdev=1.0,
            n_observations=1,
            window_days=20,
            calibration_state="calibrated",
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_event_kind_rejects_invalid_value(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        row = DistillationEventHistory(
            ticker=Symbol("AAPL"),
            event_kind="not_a_real_event_kind",
            event_ts="2026-04-25T13:30:00Z",
            direction="up",
            magnitude_atr_multiple=1.0,
            outcome="filled",
            ingested_at="2026-04-25T13:31:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_composite_kind_rejects_invalid_value(self, session: Session) -> None:
        row = DistillationCompositeState(
            composite_kind="not_a_real_composite",
            as_of="2026-04-26T13:30:00Z",
            composite_value=0.42,
            component_breakdown_json='{"x": 1}',
            percentile_60d=68.0,
            alert_active=0,
            calibration_state="calibrated",
            ingested_at="2026-04-26T13:30:01Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# AC: Alembic upgrade head + downgrade base
# ---------------------------------------------------------------------------


_DISTILLATION_TABLES = (
    "distillation_ticker_baseline",
    "distillation_pair_lag",
    "distillation_contract_history",
    "distillation_event_history",
    "distillation_regime_state",
    "distillation_composite_state",
)


def _alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` pointed at *db_path* via the ``-x db=`` arg."""
    repo_root = Path(__file__).parents[1]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


def _exec_sql(db_path: Path, sql: str) -> None:
    """Run a single SQL statement against the migrated SQLite file."""
    eng = make_engine(str(db_path))
    try:
        with eng.begin() as conn:
            conn.execute(text(sql))
    finally:
        eng.dispose()


def _scalar_sql(db_path: Path, sql: str) -> object:
    """Run a single SELECT and return the scalar result."""
    eng = make_engine(str(db_path))
    try:
        with eng.connect() as conn:
            return conn.execute(text(sql)).scalar_one()
    finally:
        eng.dispose()


def _expect_integrity_error(db_path: Path, sql: str) -> None:
    """Assert that running ``sql`` against the migrated DB raises ``IntegrityError``."""
    eng = make_engine(str(db_path))
    try:
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(text(sql))
    finally:
        eng.dispose()


def _seed_asset_universe_aapl(db_path: Path) -> None:
    _exec_sql(
        db_path,
        "INSERT INTO asset_universe (asset_id, ticker, full_name, asset_class, "
        "asset_role, exchange, is_active, added_date, last_updated) VALUES ("
        "'asset-aapl', 'AAPL', 'Apple', 'equity', 'universe', 'NASDAQ', 1, "
        "'2020-01-01', '2026-04-27T00:00:00Z')",
    )


_INSERT_VOLUME_BASELINE = (
    "INSERT INTO distillation_ticker_baseline (ticker, baseline_kind, as_of, "
    "mean, stdev, n_observations, window_days, calibration_state, ingested_at) "
    "VALUES ('AAPL', 'volume', '2026-04-27T00:00:00Z', 1.0, 0.1, 20, 20, "
    "'calibrated', '2026-04-27T00:00:00Z')"
)
_INSERT_ATM_IV_BASELINE = (
    "INSERT INTO distillation_ticker_baseline (ticker, baseline_kind, as_of, "
    "mean, stdev, n_observations, window_days, calibration_state, ingested_at) "
    "VALUES ('AAPL', 'atm_iv', '2026-04-27T00:01:00Z', 0.3, 0.05, 252, 252, "
    "'calibrated', '2026-04-27T00:01:00Z')"
)
_INSERT_CORRELATION_DIVERGENCE_EVENT = (
    "INSERT INTO distillation_event_history (ticker, event_kind, event_ts, "
    "direction, magnitude_atr_multiple, outcome, ingested_at) VALUES ("
    "'AAPL', 'correlation_divergence', '2026-04-27T00:02:00Z', 'up', 1.5, "
    "'pending', '2026-04-27T00:02:00Z')"
)


def _insert_volume_baseline(db_path: Path) -> None:
    _exec_sql(db_path, _INSERT_VOLUME_BASELINE)


def _expect_atm_iv_rejected(db_path: Path) -> None:
    _expect_integrity_error(db_path, _INSERT_ATM_IV_BASELINE)


def _assert_volume_baseline_preserved(db_path: Path) -> None:
    rebuilt = _scalar_sql(
        db_path,
        "SELECT mean FROM distillation_ticker_baseline WHERE "
        "ticker = 'AAPL' AND baseline_kind = 'volume'",
    )
    assert rebuilt == pytest.approx(1.0)


def _insert_atm_iv_baseline(db_path: Path) -> None:
    _exec_sql(db_path, _INSERT_ATM_IV_BASELINE)


def _expect_correlation_divergence_rejected(db_path: Path) -> None:
    _expect_integrity_error(db_path, _INSERT_CORRELATION_DIVERGENCE_EVENT)


def _insert_correlation_divergence_event(db_path: Path) -> None:
    _exec_sql(db_path, _INSERT_CORRELATION_DIVERGENCE_EVENT)


def _delete_correlation_divergence_rows(db_path: Path) -> None:
    _exec_sql(
        db_path,
        "DELETE FROM distillation_event_history WHERE event_kind = 'correlation_divergence'",
    )


def _delete_atm_iv_rows(db_path: Path) -> None:
    _exec_sql(
        db_path,
        "DELETE FROM distillation_ticker_baseline WHERE baseline_kind = 'atm_iv'",
    )


class TestAlembicMigration:
    def test_upgrade_head_creates_distillation_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            existing = set(insp.get_table_names())
            for tbl in _DISTILLATION_TABLES:
                assert tbl in existing, f"table {tbl!r} missing after upgrade head"
        finally:
            eng.dispose()

    def test_downgrade_base_drops_distillation_tables(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            existing = set(insp.get_table_names())
            for tbl in _DISTILLATION_TABLES:
                assert tbl not in existing, f"table {tbl!r} should be dropped"
        finally:
            eng.dispose()

    def test_round_trip_after_alembic_upgrade(self, tmp_path: Path) -> None:
        """Insert a regime row via the migrated DB to confirm columns/CHECKs match."""
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO distillation_regime_state ("
                        "as_of, regime_label, vix_level, term_structure_basis, "
                        "vvix_percentile, realized_vol, indicator_agreement_count, "
                        "invocations_held, transition_state, prior_label, ingested_at"
                        ") VALUES ("
                        ":as_of, :regime_label, :vix_level, :term_structure_basis, "
                        ":vvix_percentile, :realized_vol, :indicator_agreement_count, "
                        ":invocations_held, :transition_state, :prior_label, :ingested_at"
                        ")"
                    ),
                    {
                        "as_of": "2026-04-26T13:30:00Z",
                        "regime_label": "vol_expansion",
                        "vix_level": 22.0,
                        "term_structure_basis": 0.4,
                        "vvix_percentile": 70.0,
                        "realized_vol": 18.0,
                        "indicator_agreement_count": 3,
                        "invocations_held": 1,
                        "transition_state": "early-strong",
                        "prior_label": "low_vol_compression",
                        "ingested_at": "2026-04-26T13:30:01Z",
                    },
                )
            with eng.connect() as conn:
                result = conn.execute(
                    text(
                        "SELECT regime_label, transition_state FROM "
                        "distillation_regime_state WHERE as_of = :as_of"
                    ),
                    {"as_of": "2026-04-26T13:30:00Z"},
                ).one()
            assert result.regime_label == "vol_expansion"
            assert result.transition_state == "early-strong"
        finally:
            eng.dispose()

    def test_intermediate_revisions_round_trip(self, tmp_path: Path) -> None:
        """Each distillation revision upgrades, accepts a row, and downgrades cleanly.

        Walks the chain ``71d9125161ee → 0aa4fc8b5647 → 8a8d4e44b305``:

        - At ``71d9125161ee`` the four base baseline kinds + two base event
          kinds are accepted.
        - At ``0aa4fc8b5647`` the new ``atm_iv`` baseline kind is accepted
          (and would have been rejected under the prior CHECK).
        - At ``8a8d4e44b305`` the new ``correlation_divergence`` event
          kind is accepted (and would have been rejected under the prior
          CHECK).

        Each upgrade preserves the FK on ``ticker`` to ``asset_universe``;
        each downgrade walks back to the previous revision without raising.
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)

        # Stage 1 — base distillation tables.
        command.upgrade(cfg, "71d9125161ee")
        _seed_asset_universe_aapl(db_path)
        _insert_volume_baseline(db_path)
        _expect_atm_iv_rejected(db_path)

        # Stage 2 — atm_iv kind is admitted.
        command.upgrade(cfg, "0aa4fc8b5647")
        _assert_volume_baseline_preserved(db_path)
        _insert_atm_iv_baseline(db_path)
        _expect_correlation_divergence_rejected(db_path)

        # Stage 3 — correlation_divergence event kind is admitted.
        command.upgrade(cfg, "8a8d4e44b305")
        _insert_correlation_divergence_event(db_path)

        # Walk the chain backwards. Each pre-extension constraint would
        # invalidate rows written under its successor; the rows are
        # cleared so the round-trip completes — Fix 4 enforces this on
        # the divergence revision via the loud-refusal downgrade.
        _delete_correlation_divergence_rows(db_path)
        command.downgrade(cfg, "0aa4fc8b5647")
        _delete_atm_iv_rows(db_path)
        command.downgrade(cfg, "71d9125161ee")
        command.downgrade(cfg, "base")

    def test_downgrade_correlation_divergence_revision_errors_with_rows(
        self, tmp_path: Path
    ) -> None:
        """The 8a8d4e44b305 downgrade refuses to delete accumulated rows.

        Routine ``alembic downgrade -1`` on a populated production
        database would otherwise wipe weeks of correlation-divergence
        events; the downgrade now raises and instructs the operator to
        clear the rows explicitly first (Fix 4).
        """
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")

        eng = make_engine(str(db_path))
        try:
            with eng.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO asset_universe (asset_id, ticker, full_name, "
                        "asset_class, asset_role, exchange, is_active, added_date, "
                        "last_updated) VALUES ('asset-aapl', 'AAPL', 'Apple', "
                        "'equity', 'universe', 'NASDAQ', 1, '2020-01-01', "
                        "'2026-04-27T00:00:00Z')"
                    )
                )
                conn.execute(
                    text(
                        "INSERT INTO distillation_event_history (ticker, "
                        "event_kind, event_ts, direction, "
                        "magnitude_atr_multiple, outcome, ingested_at) VALUES ("
                        "'AAPL', 'correlation_divergence', "
                        "'2026-04-27T00:02:00Z', 'up', 1.5, 'pending', "
                        "'2026-04-27T00:02:00Z')"
                    )
                )
        finally:
            eng.dispose()

        with pytest.raises(RuntimeError, match="correlation_divergence"):
            command.downgrade(cfg, "0aa4fc8b5647")

    def test_no_redundant_indexes_on_distillation_state_tables(self, tmp_path: Path) -> None:
        """No ``ix_distillation_*`` index duplicates the composite PK.

        SQLite already builds a B-tree for the primary key, so the
        previously-shipped per-table indexes covering the same columns
        wasted write throughput on every state insert with no read
        benefit. After the cleanup, the only indexes the migration
        creates are the implicit PK ones.
        """
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            for tbl in _DISTILLATION_TABLES:
                indexes = insp.get_indexes(tbl)
                offenders = [
                    ix for ix in indexes if (ix["name"] or "").startswith("ix_distillation_")
                ]
                assert not offenders, f"unexpected redundant index(es) on {tbl}: {indexes}"
        finally:
            eng.dispose()
