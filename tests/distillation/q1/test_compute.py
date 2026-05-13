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
        sector_resolved=33,
        sector_filled=30,
    )
    result = compute_gap_fill_probability(history=history, min_events=30)
    assert result.state is CalibrationState.BOOTSTRAP
    assert result.value == pytest.approx(30.0 / 33.0)


def test_gap_fill_probability_unavailable_when_pool_empty() -> None:
    """Empty sector pool collapses to UNAVAILABLE per the calibration framework."""
    history = GapFillEventHistory(
        ticker_resolved=0,
        ticker_filled=0,
        sector_resolved=0,
        sector_filled=0,
    )
    result = compute_gap_fill_probability(history=history, min_events=30)
    assert result.state is CalibrationState.UNAVAILABLE
    assert result.value is None
