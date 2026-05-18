"""Tests for read-time live-adjusted P/L drag helpers (story 04b / ALP-529).

These helpers sum the per-fill ``LiveExecutionEstimate`` cost components persisted
on ``FillRecord`` rows. The existing raw ``PortfolioPnL`` typed record and
``PositionView`` shape are untouched per parent decision (A) — live-adjusted P/L
is computed at read time by downstream consumers (feedback loop, future
evaluation reports).
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.money import money


def test_compute_position_live_drag_empty_returns_zero() -> None:
    from alphamind.portfolio_state.computations import compute_position_live_drag

    assert compute_position_live_drag(()) == money("0")
