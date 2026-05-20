"""Tests for the VVIX percentile calculator (ALP-571).

Replaces the prior hardcoded ``_VVIX_PERCENTILE_PLACEHOLDER = 50.0`` that
masked the unavailable VVIX series — the calculator now returns ``None``
plus an explicit calibration tag (``UNAVAILABLE`` / ``ACCUMULATING``) so
downstream agents can distinguish a missing read from a live mid-percentile
reading.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.orchestrator import (
    _VVIX_PERCENTILE_MIN_OBSERVATIONS,
    _VVIX_SERIES_ID,
    _VX1_SERIES_ID,
    _build_regime_snapshot,
    _compute_vvix_percentile,
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


def _seed_vvix_history(session: Session, values: list[float], *, end_date: datetime) -> None:
    """Seed VVIX rows ending at ``end_date`` going backwards, one per calendar day."""
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


# ---------------------------------------------------------------------------
# _compute_vvix_percentile — three states
# ---------------------------------------------------------------------------


def test_compute_vvix_percentile_returns_unavailable_when_series_empty(session: Session) -> None:
    """Zero observations — collector failure or series-not-implemented."""
    value, state, reason = _compute_vvix_percentile(session, as_of=AS_OF)
    assert value is None
    assert state is CalibrationState.UNAVAILABLE
    assert reason == "regime: VVIX series unavailable"


def test_compute_vvix_percentile_returns_accumulating_below_min_observations(
    session: Session,
) -> None:
    """At least one observation but fewer than the minimum — time fixes it."""
    sparse_history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS - 1)]
    _seed_vvix_history(session, sparse_history, end_date=AS_OF)

    value, state, reason = _compute_vvix_percentile(session, as_of=AS_OF)
    assert value is None
    assert state is CalibrationState.ACCUMULATING
    assert reason is not None
    assert "accumulating" in reason
    # Reason carries the observation count so the operator can see the gap.
    assert str(_VVIX_PERCENTILE_MIN_OBSERVATIONS - 1) in reason


def test_compute_vvix_percentile_returns_calibrated_at_or_above_min_observations(
    session: Session,
) -> None:
    """Full history — the most-recent observation ranks against the trailing window."""
    history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS)]
    _seed_vvix_history(session, history, end_date=AS_OF)

    value, state, reason = _compute_vvix_percentile(session, as_of=AS_OF)
    assert state is CalibrationState.CALIBRATED
    assert reason is None
    # The most-recent value (highest in the monotonic series) ranks at the top.
    assert value == pytest.approx(100.0)


def test_compute_vvix_percentile_zero_variance_window_returns_accumulating(
    session: Session,
) -> None:
    """All observations identical — percentile is undefined.

    ``percentile_rank`` returns ``None`` for zero-variance distributions
    (every value identical → rank is tautological). Surface that as
    ``ACCUMULATING`` with a distinct reason rather than fabricating a
    percentile or eliding the gap.
    """
    flat_history = [80.0] * _VVIX_PERCENTILE_MIN_OBSERVATIONS
    _seed_vvix_history(session, flat_history, end_date=AS_OF)

    value, state, reason = _compute_vvix_percentile(session, as_of=AS_OF)
    assert value is None
    assert state is CalibrationState.ACCUMULATING
    assert reason is not None
    assert "zero-variance" in reason


def test_compute_vvix_percentile_never_returns_50_as_default(session: Session) -> None:
    """Regression guard for the ALP-571 anti-pattern.

    Pre-fix the placeholder constant ``_VVIX_PERCENTILE_PLACEHOLDER = 50.0``
    was stamped in unconditionally; downstream readers could not distinguish
    that default from a live median-vol percentile.
    """
    value, state, _reason = _compute_vvix_percentile(session, as_of=AS_OF)
    assert value is None  # not 50.0
    assert state is CalibrationState.UNAVAILABLE


def test_compute_vvix_percentile_ignores_observations_after_as_of(session: Session) -> None:
    """A VVIX observation timestamped after ``as_of`` must not feed the window."""
    future_value = 999.0
    session.add(
        MacroObservations(
            source="cboe",
            series_id=_VVIX_SERIES_ID,
            observation_date=(AS_OF + timedelta(days=10)).strftime("%Y-%m-%d"),
            revision_number=0,
            value=future_value,
            ingested_at=(AS_OF + timedelta(days=10)).strftime("%Y-%m-%dT00:00:00Z"),
        )
    )
    session.commit()

    value, state, _reason = _compute_vvix_percentile(session, as_of=AS_OF)
    assert value is None
    assert state is CalibrationState.UNAVAILABLE


# ---------------------------------------------------------------------------
# _build_regime_snapshot — combines VIX state and VVIX state
# ---------------------------------------------------------------------------


def _seed_vix(session: Session) -> None:
    session.add(
        MacroObservations(
            source="fred",
            series_id="VIXCLS",
            observation_date="2026-05-15",
            revision_number=0,
            value=17.26,
            ingested_at="2026-05-15T20:00:00Z",
        )
    )


def _seed_vx1(session: Session) -> None:
    """Seed a single VX1 observation so the term-structure basis is CALIBRATED.

    VVIX-focused tests isolate the VVIX failure mode by neutralizing the
    VX1 dimension — without this seed every test where VX1 is otherwise
    unrelated would surface a VX1-unavailable degradation that masks the
    VVIX assertion.
    """
    session.add(
        MacroObservations(
            source="cboe",
            series_id=_VX1_SERIES_ID,
            observation_date="2026-05-15",
            revision_number=0,
            value=18.0,
            ingested_at="2026-05-15T20:00:00Z",
        )
    )


def test_build_regime_snapshot_vix_present_vvix_absent_tags_unavailable(
    session: Session,
) -> None:
    """VIX present + VX1 present + VVIX series absent — block degraded, reason names VVIX."""
    _seed_vix(session)
    _seed_vx1(session)
    session.commit()

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vvix_percentile is None
    assert calibration_state is CalibrationState.UNAVAILABLE
    assert bootstrap_reason == "regime: VVIX series unavailable"


def test_build_regime_snapshot_vix_present_vvix_accumulating_tags_accumulating(
    session: Session,
) -> None:
    """VIX + VX1 present + VVIX accumulating — block tagged ACCUMULATING."""
    _seed_vix(session)
    _seed_vx1(session)
    sparse_history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS - 1)]
    _seed_vvix_history(session, sparse_history, end_date=AS_OF)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vvix_percentile is None
    assert calibration_state is CalibrationState.ACCUMULATING
    assert bootstrap_reason is not None
    assert "VVIX" in bootstrap_reason


def test_build_regime_snapshot_vix_and_vvix_present_is_calibrated(session: Session) -> None:
    """VIX + VX1 + VVIX all present and fully calibrated — block is CALIBRATED."""
    _seed_vix(session)
    _seed_vx1(session)
    history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS)]
    _seed_vvix_history(session, history, end_date=AS_OF)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.vvix_percentile is not None
    assert calibration_state is CalibrationState.CALIBRATED
    assert bootstrap_reason is None


def test_build_regime_snapshot_vix_missing_takes_precedence_over_vvix(session: Session) -> None:
    """When VIX is missing the block is UNAVAILABLE with the VIX reason — VIX wins."""
    history = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS)]
    _seed_vvix_history(session, history, end_date=AS_OF)

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert calibration_state is CalibrationState.UNAVAILABLE
    assert bootstrap_reason == "regime: VIXCLS observation missing"
    # VVIX still flows through the snapshot so the value isn't dropped on the
    # floor — the calibration tag carries the priority degradation signal.
    assert snapshot.vvix_percentile is not None
