"""Tests for SPY-derived realized-vol in the distillation orchestrator.

Replaces the prior hardcoded ``realized_vol_5d = realized_vol_20d = 0.0``
placeholders that triggered the ALP-492 Gap 1 WARN on every invocation
even when SPY data was present.
"""

from __future__ import annotations

import math
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
    _compute_realized_vols,
    _realized_vols_from_log_returns,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    MacroObservations,
    OhlcvBars,
)
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


def _add_spy_universe_row(session: Session) -> None:
    session.add(
        AssetUniverse(
            asset_id="asset-spy",
            ticker="SPY",
            full_name="S&P 500 ETF",
            asset_class="equity",
            asset_role="benchmark",
            exchange="NYSE",
            is_active=1,
            added_date="2026-01-01",
            last_updated="2026-01-01T00:00:00Z",
        )
    )
    session.flush()


def _add_spy_bar(session: Session, *, period_start: datetime, close: float) -> None:
    period_start_iso = period_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        OhlcvBars(
            ticker="SPY",
            timeframe="1d",
            period_start=period_start_iso,
            period_end=period_start_iso,
            session="regular",
            adj_open=close,
            adj_high=close,
            adj_low=close,
            adj_close=close,
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at=period_start_iso,
        )
    )


def _seed_spy_closes(session: Session, closes: list[float], *, end_date: datetime) -> None:
    """Seed SPY daily bars ending on ``end_date`` going backwards, one per business day."""
    _add_spy_universe_row(session)
    # Walk backwards from end_date and skip weekends so the seeded bars align
    # with what production OHLCV looks like.
    placed = 0
    cursor = end_date
    rev = list(reversed(closes))
    while placed < len(rev):
        if cursor.weekday() < 5:  # weekdays only (Mon=0..Fri=4)
            _add_spy_bar(session, period_start=cursor, close=rev[placed])
            placed += 1
        cursor -= timedelta(days=1)
    session.flush()


def _seed_calibrated_vvix(session: Session, *, end_date: datetime) -> None:
    """Seed enough VVIX + VX1 observations to put the regime block in CALIBRATED state.

    Tests that exercise rule-firing logic want a known regime calibration tag
    so an unrelated VVIX- or VX1-missing degradation doesn't taint the
    assertion. Seeds both supporting series in one call.
    """
    values = [80.0 + i for i in range(_VVIX_PERCENTILE_MIN_OBSERVATIONS)]
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
    # Seed a single VX1 observation so the term-structure basis calculator
    # returns CALIBRATED — the latest-value query at ``end_date`` picks
    # this up regardless of as_of date.
    session.add(
        MacroObservations(
            source="cboe",
            series_id=_VX1_SERIES_ID,
            observation_date=end_date.strftime("%Y-%m-%d"),
            revision_number=0,
            value=18.0,
            ingested_at=end_date.strftime("%Y-%m-%dT00:00:00Z"),
        )
    )
    session.flush()


# ---------------------------------------------------------------------------
# Pure helper — _realized_vols_from_log_returns
# ---------------------------------------------------------------------------


def test_realized_vols_short_window_only() -> None:
    # 6 returns is enough for the 5d window but not the 20d window.
    returns = [0.01, -0.01, 0.005, -0.005, 0.002, -0.002]
    rv5, rv20 = _realized_vols_from_log_returns(returns)
    assert rv5 > 0.0
    assert rv20 == 0.0


def test_realized_vols_both_windows_filled() -> None:
    # 25 alternating returns produces non-zero stdev for both windows.
    returns = [(0.01 if i % 2 == 0 else -0.01) for i in range(25)]
    rv5, rv20 = _realized_vols_from_log_returns(returns)
    assert rv5 > 0.0
    assert rv20 > 0.0


def test_realized_vols_annualization_factor() -> None:
    # 20 alternating ±0.01 returns: sample stdev ≈ 0.01026... ; annualized
    # by sqrt(252) ≈ 15.874... gives roughly 0.163.
    returns = [(0.01 if i % 2 == 0 else -0.01) for i in range(20)]
    _rv5, rv20 = _realized_vols_from_log_returns(returns)
    # Annualization checks: the value should be sample_stdev * sqrt(252).
    import statistics

    expected = statistics.stdev(returns) * math.sqrt(252)
    assert rv20 == pytest.approx(expected, rel=1e-9)


def test_realized_vols_flat_series_is_zero() -> None:
    # Identical closes -> log returns are all zero -> stdev is zero.
    returns = [0.0] * 25
    rv5, rv20 = _realized_vols_from_log_returns(returns)
    assert rv5 == 0.0
    assert rv20 == 0.0


def test_realized_vols_empty_returns_zero() -> None:
    rv5, rv20 = _realized_vols_from_log_returns([])
    assert (rv5, rv20) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# DB-backed — _compute_realized_vols
# ---------------------------------------------------------------------------


def test_compute_realized_vols_empty_db_returns_zero(session: Session) -> None:
    rv5, rv20 = _compute_realized_vols(session, as_of=AS_OF)
    assert (rv5, rv20) == (0.0, 0.0)


def test_compute_realized_vols_uses_only_spy_within_lookback(session: Session) -> None:
    # Seed 25 bars; both windows should produce non-zero readings.
    closes = [700.0 + 5.0 * (i % 2) for i in range(25)]  # alternating
    _seed_spy_closes(session, closes, end_date=AS_OF)
    session.commit()

    rv5, rv20 = _compute_realized_vols(session, as_of=AS_OF)
    assert rv5 > 0.0
    assert rv20 > 0.0


def test_compute_realized_vols_excludes_bars_after_as_of(session: Session) -> None:
    # A bar in the future (after as_of) must not be used.
    _add_spy_universe_row(session)
    _add_spy_bar(session, period_start=AS_OF + timedelta(days=2), close=999.0)
    session.commit()

    rv5, rv20 = _compute_realized_vols(session, as_of=AS_OF)
    assert (rv5, rv20) == (0.0, 0.0)


def test_compute_realized_vols_ignores_non_daily_timeframes(session: Session) -> None:
    _add_spy_universe_row(session)
    period_start_iso = AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        OhlcvBars(
            ticker="SPY",
            timeframe="1m",  # intraday — must be skipped
            period_start=period_start_iso,
            period_end=period_start_iso,
            session="regular",
            adj_open=700.0,
            adj_high=700.0,
            adj_low=700.0,
            adj_close=700.0,
            adj_volume=1,
            adj_vwap=700.0,
            unadj_open=700.0,
            unadj_high=700.0,
            unadj_low=700.0,
            unadj_close=700.0,
            unadj_volume=1,
            unadj_vwap=700.0,
            trade_count=None,
            source="test",
            ingested_at=period_start_iso,
        )
    )
    session.commit()

    rv5, rv20 = _compute_realized_vols(session, as_of=AS_OF)
    assert (rv5, rv20) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Integration — _build_regime_snapshot now populates realized vol
# ---------------------------------------------------------------------------


def test_build_regime_snapshot_uses_real_realized_vols_when_spy_available(
    session: Session,
) -> None:
    # Seed VIX + VVIX + SPY closes.
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
    _seed_calibrated_vvix(session, end_date=AS_OF)
    closes = [700.0 + 5.0 * (i % 2) for i in range(25)]
    _seed_spy_closes(session, closes, end_date=AS_OF)
    session.commit()

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert calibration_state is CalibrationState.CALIBRATED
    assert bootstrap_reason is None
    assert snapshot.vix_level == pytest.approx(17.26)
    assert snapshot.realized_vol_5d > 0.0
    assert snapshot.realized_vol_20d > 0.0


def test_build_regime_snapshot_falls_back_when_spy_absent(session: Session) -> None:
    # VIX is present (and VVIX seeded so the block stays CALIBRATED on the
    # VVIX side) but no SPY bars exist — realized vols fall back to 0.0/0.0
    # and the ALP-492 Gap 1 WARN trips on the bundle, surfacing the
    # data-pipeline gap to operators.
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
    _seed_calibrated_vvix(session, end_date=AS_OF)
    session.commit()

    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert calibration_state is CalibrationState.CALIBRATED  # VIX + VVIX present
    assert bootstrap_reason is None
    assert snapshot.realized_vol_5d == 0.0
    assert snapshot.realized_vol_20d == 0.0


def test_build_regime_snapshot_vix_missing_emits_unavailable(session: Session) -> None:
    snapshot, calibration_state, bootstrap_reason = _build_regime_snapshot(session, as_of=AS_OF)
    # Per ALP-540: zero VIX observations is a collector failure, not a
    # warm-up state — the regime block should surface as UNAVAILABLE.
    # Per ALP-572 the regime block now combines every contributing
    # source's reason via the worst-wins fold, so the bootstrap_reason
    # surfaces VIX, VVIX, and VX1 outages in one string (every source
    # is empty in this test). Assert the VIX outage is named — that's
    # the load-bearing signal — without pinning the exact ordering.
    assert calibration_state is CalibrationState.UNAVAILABLE
    assert bootstrap_reason is not None
    assert "regime: VIXCLS observation missing" in bootstrap_reason
    assert snapshot.vix_level == 0.0
    assert snapshot.realized_vol_5d == 0.0
    assert snapshot.realized_vol_20d == 0.0


def test_vol_expansion_label_fires_when_rv_rising_and_vix_elevated(
    session: Session,
) -> None:
    """Locks in the rule-firing change PR #63 enables.

    Pre-fix the rv_5d/rv_20d hardcode at 0.0 meant ``classify_regime`` never
    found ``rv_5d > rv_20d`` true and the ``vol_expansion`` rule never fired
    — every invocation fell through to the VIX-band fallback. Post-fix the
    rule is live: seed elevated-band VIX + a SPY series whose 5d realized
    vol exceeds its 20d realized vol and verify the label.
    """
    from alphamind.distillation.regime import (
        RegimeClassificationThresholds,
        classify_regime,
    )

    # Front-load 20 calm bars, then 5 bars with much larger day-over-day moves
    # so the 5d realized vol is materially higher than the 20d window.
    calm_segment = [700.0 + 0.1 * (i % 2) for i in range(20)]
    volatile_segment = [700.0 + 8.0 * (i % 2) for i in range(5)]
    closes = calm_segment + volatile_segment
    _seed_spy_closes(session, closes, end_date=AS_OF)
    session.add(
        MacroObservations(
            source="fred",
            series_id="VIXCLS",
            observation_date="2026-05-15",
            revision_number=0,
            # VIX in the elevated band (24 ∈ [normal_vix_min, elevated_vix_max]
            # under typical thresholds).
            value=24.0,
            ingested_at="2026-05-15T20:00:00Z",
        )
    )
    session.commit()

    snapshot, _state, _reason = _build_regime_snapshot(session, as_of=AS_OF)
    assert snapshot.realized_vol_5d > snapshot.realized_vol_20d > 0.0

    thresholds = RegimeClassificationThresholds(
        low_vol_vix_max=15.0,
        normal_vix_min=15.0,
        normal_vix_max=20.0,
        elevated_vix_min=20.0,
        elevated_vix_max=28.0,
        crisis_vix_min=28.0,
        term_structure_backwardation_threshold=0.0,
        vvix_high_percentile=80.0,
        vvix_low_percentile=20.0,
    )
    assert classify_regime(snapshot=snapshot, thresholds=thresholds).value == "vol_expansion"
