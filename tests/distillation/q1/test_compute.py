"""Pure-compute tests for q1 — no SQLite, no Session.

Story ALP-467: the compute/load boundary split's defining property is that
each ``q1/*_compute.py`` (and equivalently the pure modules retained from
the pre-split layout) is testable from hand-built frozen inputs with no
ORM or in-memory database. Tests in this module construct inputs directly
and assert on the pure return shape.
"""

from __future__ import annotations

import pytest

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.q1.gap_compute import (
    GapFillEventHistory,
    compute_gap_fill_probability,
)


def test_gap_fill_probability_per_ticker_when_calibrated() -> None:
    """Per-ticker resolved count above the threshold yields a calibrated rate."""
    # 30 resolved events, 15 filled => rate 0.5, threshold met.
    history = GapFillEventHistory(
        ticker_resolved=30,
        ticker_filled=15,
        ticker_pending=0,
        sector_resolved=0,
        sector_filled=0,
    )
    result = compute_gap_fill_probability(history=history, min_events=30)
    assert result.state is CalibrationState.CALIBRATED
    assert result.value == pytest.approx(0.5)


def test_gap_fill_probability_sector_pool_when_bootstrap() -> None:
    """Below threshold per-ticker but sector pool present yields a bootstrap rate."""
    # 3 ticker events (below 30 threshold) but a healthy sector pool.
    history = GapFillEventHistory(
        ticker_resolved=3,
        ticker_filled=0,
        ticker_pending=0,
        sector_resolved=33,
        sector_filled=30,
    )
    result = compute_gap_fill_probability(history=history, min_events=30)
    assert result.state is CalibrationState.ACCUMULATING
    assert result.value == pytest.approx(30.0 / 33.0)


def test_gap_fill_probability_unavailable_when_pool_empty() -> None:
    """Empty sector pool with zero detected events collapses to UNAVAILABLE."""
    history = GapFillEventHistory(
        ticker_resolved=0,
        ticker_filled=0,
        ticker_pending=0,
        sector_resolved=0,
        sector_filled=0,
    )
    result = compute_gap_fill_probability(history=history, min_events=30)
    assert result.state is CalibrationState.UNAVAILABLE
    assert result.value is None


def test_gap_fill_probability_accumulating_when_pending_but_unresolved() -> None:
    """0 resolved + N pending is bootstrap-accumulating, not collector-silent.

    ALP-573: ``gap_fill_baseline_days`` is 252 trading days, so a fresh
    install with a 30-day bootstrap window will have 0 resolved gap events
    even when gap events are being detected and persisted correctly. The
    correct calibration state is ACCUMULATING (give it time), not
    UNAVAILABLE (collector silent / operator action required). The reason
    text should make the distinction visible.
    """
    history = GapFillEventHistory(
        ticker_resolved=0,
        ticker_filled=0,
        ticker_pending=4,
        sector_resolved=0,
        sector_filled=0,
    )
    result = compute_gap_fill_probability(history=history, min_events=30)
    assert result.state is CalibrationState.ACCUMULATING
    assert result.value is None
    assert result.bootstrap_reason is not None
    assert "pending" in result.bootstrap_reason
    assert "0 observations" not in result.bootstrap_reason


def test_gap_fill_probability_reason_distinguishes_no_detected_from_pending() -> None:
    """The 0-detected reason names the absence; the pending reason names the count."""
    no_events = GapFillEventHistory(
        ticker_resolved=0,
        ticker_filled=0,
        ticker_pending=0,
        sector_resolved=0,
        sector_filled=0,
    )
    pending_events = GapFillEventHistory(
        ticker_resolved=0,
        ticker_filled=0,
        ticker_pending=7,
        sector_resolved=0,
        sector_filled=0,
    )
    no_result = compute_gap_fill_probability(history=no_events, min_events=30)
    pending_result = compute_gap_fill_probability(history=pending_events, min_events=30)

    assert no_result.state is CalibrationState.UNAVAILABLE
    assert pending_result.state is CalibrationState.ACCUMULATING
    assert no_result.bootstrap_reason != pending_result.bootstrap_reason
    assert pending_result.bootstrap_reason is not None
    assert "7" in pending_result.bootstrap_reason
