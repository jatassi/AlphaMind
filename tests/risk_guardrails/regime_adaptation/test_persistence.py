"""Persistence tests for the regime-adaptation feature (story 04a).

Covers the SQLAlchemy round-trip, latest-wins read accessor, vocabulary CHECK
constraints landed by the Alembic migration, and the typed-record adapters.
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

from alphamind._kernel.regime import RegimeTransitionState
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.persistence.models import Base, RegimeAdaptationStateRow
from alphamind.persistence.session import make_engine, make_session_factory
from alphamind.risk_guardrails.regime_adaptation import (
    RegimeAdaptationState,
    insert_state,
    select_most_recent_state,
)
from alphamind.risk_guardrails.regime_adaptation.persistence import (
    row_to_state,
    state_to_row,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine with the full schema."""
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------


def _stable_state(*, as_of: str = "2026-04-28T13:30:00Z") -> RegimeAdaptationState:
    """Return a STABLE-state record with all Optional fields cleared."""
    return RegimeAdaptationState(
        as_of=as_of,
        invocation_id="INV-2026-04-28-1330",
        active_regime=Regime.normal,
        prior_regime=Regime.low_vol,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="vol_expansion",
        distillation_vix_level=18.5,
        regime_skip_emergency=False,
    )


_INGESTED_AT = "2026-04-28T13:30:01Z"


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_stable_state_round_trips(self, session: Session) -> None:
        original = _stable_state()
        insert_state(session, original, ingested_at=_INGESTED_AT)
        recovered = select_most_recent_state(session)
        assert recovered == original

    def test_empty_overlays_round_trip(self, session: Session) -> None:
        original = _stable_state()
        assert original.active_overlays == ()
        insert_state(session, original, ingested_at=_INGESTED_AT)
        recovered = select_most_recent_state(session)
        assert recovered is not None
        assert recovered.active_overlays == ()

    def test_multiple_overlays_round_trip_sorted(self, session: Session) -> None:
        # Insertion order is non-alphabetical; the adapter must sort on read.
        original = RegimeAdaptationState(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime=Regime.normal,
            prior_regime=None,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays=(Overlay.stress, Overlay.pre_event),
            distillation_regime_label="vol_expansion",
            distillation_vix_level=18.5,
            regime_skip_emergency=False,
        )
        insert_state(session, original, ingested_at=_INGESTED_AT)
        recovered = select_most_recent_state(session)
        assert recovered is not None
        assert recovered.active_overlays == (Overlay.pre_event, Overlay.stress)

    def test_loosening_state_round_trips(self, session: Session) -> None:
        original = RegimeAdaptationState(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime=Regime.normal,
            prior_regime=Regime.elevated,
            transition_state=RegimeTransitionState.LOOSENING,
            transition_invocations_remaining=2,
            transition_started_invocation_id="INV-2026-04-28-1100",
            transition_origin_regime=Regime.elevated,
            active_overlays=(Overlay.stress,),
            distillation_regime_label="vol_normalization",
            distillation_vix_level=22.0,
            regime_skip_emergency=False,
        )
        insert_state(session, original, ingested_at=_INGESTED_AT)
        recovered = select_most_recent_state(session)
        assert recovered == original

    def test_tightening_state_with_null_optionals_round_trips(self, session: Session) -> None:
        original = RegimeAdaptationState(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime=Regime.elevated,
            prior_regime=Regime.normal,
            transition_state=RegimeTransitionState.TIGHTENING,
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays=(),
            distillation_regime_label="vol_expansion",
            distillation_vix_level=24.0,
            regime_skip_emergency=False,
        )
        insert_state(session, original, ingested_at=_INGESTED_AT)
        recovered = select_most_recent_state(session)
        assert recovered == original
        assert recovered is not None
        assert recovered.transition_origin_regime is None
        assert recovered.transition_started_invocation_id is None


class TestSelectMostRecent:
    def test_bootstrap_returns_none_on_empty_table(self, session: Session) -> None:
        assert select_most_recent_state(session) is None

    def test_latest_wins_with_multiple_rows(self, session: Session) -> None:
        states = [_stable_state(as_of=f"2026-04-28T13:3{minute}:00Z") for minute in range(3)]
        for state in states:
            insert_state(session, state, ingested_at=_INGESTED_AT)
        recovered = select_most_recent_state(session)
        assert recovered is not None
        assert recovered.as_of == "2026-04-28T13:32:00Z"


class TestDuplicateAsOf:
    def test_duplicate_as_of_raises_integrity_error(self, session: Session) -> None:
        original = _stable_state()
        insert_state(session, original, ingested_at=_INGESTED_AT)
        duplicate = _stable_state()
        with pytest.raises(IntegrityError):
            insert_state(session, duplicate, ingested_at=_INGESTED_AT)
        # The first row must still be readable after the rollback.
        recovered = select_most_recent_state(session)
        assert recovered == original


# ---------------------------------------------------------------------------
# CHECK-constraint coverage
# ---------------------------------------------------------------------------


_BASE_INSERT_SQL = (
    "INSERT INTO regime_adaptation_state ("
    "as_of, invocation_id, active_regime, prior_regime, "
    "transition_state, transition_invocations_remaining, "
    "transition_started_invocation_id, transition_origin_regime, "
    "active_overlays_csv, distillation_regime_label, "
    "distillation_vix_level, regime_skip_emergency, ingested_at"
    ") VALUES ("
    ":as_of, :invocation_id, :active_regime, :prior_regime, "
    ":transition_state, :transition_invocations_remaining, "
    ":transition_started_invocation_id, :transition_origin_regime, "
    ":active_overlays_csv, :distillation_regime_label, "
    ":distillation_vix_level, :regime_skip_emergency, :ingested_at"
    ")"
)


def _row_params(**overrides: object) -> dict[str, object]:
    """Build a row-parameter dict; tests override individual fields."""
    base: dict[str, object] = {
        "as_of": "2026-04-28T13:30:00Z",
        "invocation_id": "INV-2026-04-28-1330",
        "active_regime": "normal",
        "prior_regime": None,
        "transition_state": "STABLE",
        "transition_invocations_remaining": 0,
        "transition_started_invocation_id": None,
        "transition_origin_regime": None,
        "active_overlays_csv": "",
        "distillation_regime_label": "vol_expansion",
        "distillation_vix_level": 18.5,
        "regime_skip_emergency": 0,
        "ingested_at": _INGESTED_AT,
    }
    base.update(overrides)
    return base


def _insert_via_raw_sql(engine: Engine, **overrides: object) -> None:
    with engine.begin() as conn:
        conn.execute(text(_BASE_INSERT_SQL), _row_params(**overrides))


class TestCheckConstraints:
    def test_active_regime_invalid_value_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(engine, active_regime="invalid")

    def test_transition_state_invalid_value_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(engine, transition_state="INVALID")

    def test_prior_regime_invalid_value_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(engine, prior_regime="invalid")

    def test_origin_regime_invalid_value_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(
                engine,
                transition_state="LOOSENING",
                transition_origin_regime="invalid",
                transition_started_invocation_id="INV-2026-04-28-1100",
                transition_invocations_remaining=2,
            )

    def test_skip_emergency_non_boolean_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(engine, regime_skip_emergency=2)

    def test_stable_with_nonzero_remaining_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(
                engine,
                transition_state="STABLE",
                transition_invocations_remaining=2,
            )

    def test_loosening_with_null_origin_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(
                engine,
                transition_state="LOOSENING",
                transition_invocations_remaining=2,
                transition_origin_regime=None,
                transition_started_invocation_id="INV-2026-04-28-1100",
            )

    def test_loosening_with_null_started_invocation_rejected(self, engine: Engine) -> None:
        with pytest.raises(IntegrityError):
            _insert_via_raw_sql(
                engine,
                transition_state="LOOSENING",
                transition_invocations_remaining=2,
                transition_origin_regime="elevated",
                transition_started_invocation_id=None,
            )

    def test_loosening_with_origin_and_started_accepted(self, engine: Engine) -> None:
        _insert_via_raw_sql(
            engine,
            transition_state="LOOSENING",
            transition_invocations_remaining=2,
            transition_origin_regime="elevated",
            transition_started_invocation_id="INV-2026-04-28-1100",
        )


# ---------------------------------------------------------------------------
# Read-side adapter vocabulary corruption
# ---------------------------------------------------------------------------


class TestOverlayCorruption:
    def test_invalid_overlay_csv_surfaces_value_error(
        self, engine: Engine, session: Session
    ) -> None:
        _insert_via_raw_sql(engine, active_overlays_csv="invalid_overlay")
        with pytest.raises(ValueError, match="invalid_overlay"):
            select_most_recent_state(session)


# ---------------------------------------------------------------------------
# Pure adapter tests
# ---------------------------------------------------------------------------


class TestStateRowAdapters:
    def test_state_to_row_sorts_overlays(self) -> None:
        state = RegimeAdaptationState(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime=Regime.normal,
            prior_regime=None,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays=(Overlay.stress, Overlay.pre_event),
            distillation_regime_label="vol_expansion",
            distillation_vix_level=18.5,
            regime_skip_emergency=False,
        )
        row = state_to_row(state, ingested_at=_INGESTED_AT)
        assert row.active_overlays_csv == "pre_event,stress"

    def test_state_to_row_empty_overlays_is_empty_string(self) -> None:
        row = state_to_row(_stable_state(), ingested_at=_INGESTED_AT)
        assert row.active_overlays_csv == ""

    def test_state_to_row_skip_emergency_true_persists_as_one(self) -> None:
        state = RegimeAdaptationState(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime=Regime.crisis,
            prior_regime=Regime.normal,
            transition_state=RegimeTransitionState.TIGHTENING,
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays=(),
            distillation_regime_label="crisis_spike",
            distillation_vix_level=42.0,
            regime_skip_emergency=True,
        )
        row = state_to_row(state, ingested_at=_INGESTED_AT)
        assert row.regime_skip_emergency == 1

    def test_row_to_state_invalid_active_regime_raises(self) -> None:
        row = RegimeAdaptationStateRow(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime="not_a_regime",
            prior_regime=None,
            transition_state="STABLE",
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays_csv="",
            distillation_regime_label="vol_expansion",
            distillation_vix_level=18.5,
            regime_skip_emergency=0,
            ingested_at=_INGESTED_AT,
        )
        with pytest.raises(ValueError, match="active_regime"):
            row_to_state(row)

    def test_row_to_state_invalid_transition_state_raises(self) -> None:
        row = RegimeAdaptationStateRow(
            as_of="2026-04-28T13:30:00Z",
            invocation_id="INV-2026-04-28-1330",
            active_regime="normal",
            prior_regime=None,
            transition_state="not_a_state",
            transition_invocations_remaining=0,
            transition_started_invocation_id=None,
            transition_origin_regime=None,
            active_overlays_csv="",
            distillation_regime_label="vol_expansion",
            distillation_vix_level=18.5,
            regime_skip_emergency=0,
            ingested_at=_INGESTED_AT,
        )
        with pytest.raises(ValueError, match="transition_state"):
            row_to_state(row)


# ---------------------------------------------------------------------------
# Alembic migration
# ---------------------------------------------------------------------------


def _alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` pointed at *db_path* via the ``-x db=`` arg."""
    repo_root = Path(__file__).parents[3]
    return Config(
        repo_root / "alembic.ini",
        cmd_opts=Namespace(x=[f"db={db_path}"]),
    )


class TestAlembicMigration:
    def test_upgrade_head_creates_regime_adaptation_state_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        command.upgrade(_alembic_config(db_path), "head")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "regime_adaptation_state" in set(insp.get_table_names())
            indexes = insp.get_indexes("regime_adaptation_state")
            assert any(ix["name"] == "ix_regime_adaptation_state_as_of_desc" for ix in indexes)
        finally:
            eng.dispose()

    def test_downgrade_drops_regime_adaptation_state_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        # Downgrade to the parent of this story's revision so the regime
        # adaptation table is dropped regardless of how many later
        # migrations have stacked on top.
        command.downgrade(cfg, "8a8d4e44b305")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "regime_adaptation_state" not in set(insp.get_table_names())
        finally:
            eng.dispose()

    def test_downgrade_to_base_drops_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "alembic.db"
        cfg = _alembic_config(db_path)
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")

        eng = make_engine(str(db_path))
        try:
            insp = inspect(eng)
            assert "regime_adaptation_state" not in set(insp.get_table_names())
        finally:
            eng.dispose()
