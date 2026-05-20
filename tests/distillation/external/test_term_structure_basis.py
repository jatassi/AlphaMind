"""Tests for the VIX term-structure basis calculator (ALP-572).

Replaces the prior hardcoded ``vx1_minus_vix=0.0`` that masked the
unavailable VX1 series — the calculator now returns ``None`` plus an
explicit calibration tag (``UNAVAILABLE`` / ``CALIBRATED``) so downstream
agents can distinguish a missing read from a live "flat term structure"
reading.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.orchestrator import (
    _VVIX_PERCENTILE_MIN_OBSERVATIONS,
    _VVIX_SERIES_ID,
    _VX1_SERIES_ID,
    _build_regime_snapshot,
    _combine_calibration_states,
    _compute_term_structure_basis,
)
from alphamind.persistence.models import Base, MacroObservations
from alphamind.persistence.session import make_engine, make_session_factory

AS_OF = datetime(2026, 5, 16, 17, 0, tzinfo=UTC)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


def _seed_vx1(session: Session, value: float) -> None:
    """Seed a single VX1 observation at ``AS_OF``."""
    session.add(
        MacroObservations(
            source="cboe",
            series_id=_VX1_SERIES_ID,
            observation_date=AS_OF.strftime("%Y-%m-%d"),
            revision_number=0,
            value=value,
            ingested_at=AS_OF.strftime("%Y-%m-%dT00:00:00Z"),
        )
    )
    session.commit()


def _seed_vix(session: Session, value: float = 17.26) -> None:
    session.add(
        MacroObservations(
            source="fred",
            series_id="VIXCLS",
            observation_date="2026-05-15",
            revision_number=0,
            value=value,
            ingested_at="2026-05-15T20:00:00Z",
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# _compute_term_structure_basis — three states
# ---------------------------------------------------------------------------


def test_compute_term_structure_basis_returns_unavailable_when_vx1_missing(
    session: Session,
) -> None:
    """Zero observations — collector failure or series-not-implemented."""
    basis, state, reason = _compute_term_structure_basis(session, vix=17.26)
    assert basis is None
    assert state is CalibrationState.UNAVAILABLE
    assert reason == "regime: VX1 series unavailable"


def test_compute_term_structure_basis_returns_calibrated_with_both_inputs(
    session: Session,
) -> None:
    """VX1 present + VIX anchor present — basis = VX1 - VIX."""
    _seed_vx1(session, 18.0)
    basis, state, reason = _compute_term_structure_basis(session, vix=17.0)
    assert basis == pytest.approx(1.0)
    assert state is CalibrationState.CALIBRATED
    assert reason is None


def test_compute_term_structure_basis_signed_basis_preserved_in_backwardation(
    session: Session,
) -> None:
    """A backwardation basis (VX1 < VIX) renders as a negative real, not 0."""
    _seed_vx1(session, 16.5)
    basis, _state, _reason = _compute_term_structure_basis(session, vix=17.0)
    assert basis is not None
    assert basis < 0.0
    assert basis == pytest.approx(-0.5)


def test_compute_term_structure_basis_returns_unavailable_when_vix_anchor_missing(
    session: Session,
) -> None:
    """VX1 alone without VIX cannot compute the basis.

    The VIX outage is surfaced separately by the caller; this function
    emits ``UNAVAILABLE`` without a duplicate reason so the combined
    message stays scannable.
    """
    _seed_vx1(session, 18.0)
    basis, state, reason = _compute_term_structure_basis(session, vix=None)
    assert basis is None
    assert state is CalibrationState.UNAVAILABLE
    assert reason is None


def test_compute_term_structure_basis_never_returns_zero_as_default(
    session: Session,
) -> None:
    """Regression guard for the ALP-572 anti-pattern.

    Pre-fix the orchestrator stamped ``vx1_minus_vix=0.0`` unconditionally;
    downstream readers could not distinguish that default from a live
    "flat term structure" reading.
    """
    basis, state, _reason = _compute_term_structure_basis(session, vix=17.0)
    assert basis is None  # not 0.0
    assert state is CalibrationState.UNAVAILABLE


# ---------------------------------------------------------------------------
# _combine_calibration_states — worst-wins fold across multiple sources
# ---------------------------------------------------------------------------


def test_combine_calibration_states_all_calibrated_is_calibrated() -> None:
    state, reason = _combine_calibration_states(
        (CalibrationState.CALIBRATED, None),
        (CalibrationState.CALIBRATED, None),
    )
    assert state is CalibrationState.CALIBRATED
    assert reason is None


def test_combine_calibration_states_one_unavailable_wins() -> None:
    """UNAVAILABLE beats CALIBRATED; the unavailable reason carries through."""
    state, reason = _combine_calibration_states(
        (CalibrationState.UNAVAILABLE, "regime: VX1 series unavailable"),
        (CalibrationState.CALIBRATED, None),
    )
    assert state is CalibrationState.UNAVAILABLE
    assert reason == "regime: VX1 series unavailable"


def test_combine_calibration_states_two_unavailable_concatenates_reasons() -> None:
    """When two sources are both UNAVAILABLE, both reasons surface."""
    state, reason = _combine_calibration_states(
        (CalibrationState.UNAVAILABLE, "regime: VVIX series unavailable"),
        (CalibrationState.UNAVAILABLE, "regime: VX1 series unavailable"),
    )
    assert state is CalibrationState.UNAVAILABLE
    assert reason is not None
    assert "VVIX series unavailable" in reason
    assert "VX1 series unavailable" in reason


def test_combine_calibration_states_unavailable_beats_accumulating() -> None:
    """UNAVAILABLE > ACCUMULATING in the severity ladder."""
    accumulating_reason = "regime: VVIX percentile accumulating (40 obs < 60 required)"
    state, reason = _combine_calibration_states(
        (CalibrationState.ACCUMULATING, accumulating_reason),
        (CalibrationState.UNAVAILABLE, "regime: VX1 series unavailable"),
    )
    assert state is CalibrationState.UNAVAILABLE
    # Only the most-severe reason is returned — the accumulating reason
    # is dropped because the operator's first concern is the harder
    # failure.
    assert reason == "regime: VX1 series unavailable"


# ---------------------------------------------------------------------------
# _build_regime_snapshot — combined VIX / VVIX / VX1 state
# ---------------------------------------------------------------------------


def _seed_vvix_history(session: Session, values: list[float], *, end_date: datetime) -> None:
    """Seed VVIX rows ending at ``end_date``, one per calendar day backwards."""
    from datetime import timedelta

    for offset, value in enumerate(reversed(values)):
        observation_day = end_date - timedelta(days=offset)
        session.add(
            MacroObservations(
                source="cboe",
                series_id=_VVIX_SERIES_ID,
                observation_date=observation_day.strftime("%Y-%m-%d"),
                revision_number=0,
                value=value,
                ingested_at=observation_day.strftime("%Y-%m-%dT00:00:00Z"),
            )
        )
    session.commit()


def test_build_regime_snapshot_vx1_absent_tags_unavailable(session: Session) -> None:
    """VIX present + VVIX present + VX1 absent — block degraded, reason names VX1."""
    _seed_vix(session)
    history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS)]
    _seed_vvix_history(session, history, end_date=AS_OF)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vx1_minus_vix is None
    assert calibration_state is CalibrationState.UNAVAILABLE
    assert bootstrap_reason is not None
    assert "VX1 series unavailable" in bootstrap_reason


def test_build_regime_snapshot_vx1_and_vvix_absent_tags_both_in_reason(
    session: Session,
) -> None:
    """Both supporting series missing — the combined reason names both."""
    _seed_vix(session)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vx1_minus_vix is None
    assert snapshot.vvix_percentile is None
    assert calibration_state is CalibrationState.UNAVAILABLE
    assert bootstrap_reason is not None
    assert "VVIX series unavailable" in bootstrap_reason
    assert "VX1 series unavailable" in bootstrap_reason


def test_build_regime_snapshot_all_three_present_is_calibrated(session: Session) -> None:
    """VIX + VVIX + VX1 all present — block is CALIBRATED."""
    _seed_vix(session, value=17.0)
    _seed_vx1(session, 18.0)
    history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS)]
    _seed_vvix_history(session, history, end_date=AS_OF)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vx1_minus_vix == pytest.approx(1.0)
    assert snapshot.vvix_percentile is not None
    assert calibration_state is CalibrationState.CALIBRATED
    assert bootstrap_reason is None


def test_build_regime_snapshot_vix_missing_still_carries_vx1_through_to_snapshot(
    session: Session,
) -> None:
    """VIX missing dominates the reason, but VX1 still flows through.

    The basis itself is ``None`` because the subtraction has no anchor —
    not because the field was dropped on the floor. The calibration tag
    carries the priority degradation signal.
    """
    _seed_vx1(session, 18.0)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vx1_minus_vix is None
    assert calibration_state is CalibrationState.UNAVAILABLE
    assert bootstrap_reason == "regime: VIXCLS observation missing"
