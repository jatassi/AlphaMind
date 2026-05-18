"""Tests for read-time live-adjusted P/L drag helpers (story 04b / ALP-529).

These helpers sum the per-fill ``LiveExecutionEstimate`` cost components persisted
on ``FillRecord`` rows. The existing raw ``PortfolioPnL`` typed record and
``PositionView`` shape are untouched per parent decision (A) — live-adjusted P/L
is computed at read time by downstream consumers (feedback loop, future
evaluation reports).
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.money import money, price, signed_money
from alphamind.portfolio_state.records.orders import OrderStatus
from alphamind.portfolio_state.records.positions import LiveExecutionEstimate
from alphamind.state.records import FillProcessingStatus, FillRecord

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_FILL_AT = datetime(2026, 5, 18, 14, 30, tzinfo=UTC)
_PERSISTED_AT = datetime(2026, 5, 18, 14, 30, 1, tzinfo=UTC)

_ESTIMATE_2_1_050 = LiveExecutionEstimate(
    estimated_spread_usd=money("2"),
    estimated_impact_usd=money("1"),
    estimated_regulatory_fees_usd=money("0.50"),
    live_adjusted_fill_price=price("101"),
)


def _fill_record(
    *,
    fill_id: str = "fill-1",
    live_execution_estimate: LiveExecutionEstimate | None = None,
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        order_id="order-1",
        fill_timestamp=_FILL_AT,
        fill_price=price("100"),
        fill_quantity=10.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=signed_money("0"),
        fees_usd=money("0"),
        execution_venue=None,
        gateway_reference=None,
        persistence_timestamp=_PERSISTED_AT,
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=live_execution_estimate,
    )


def test_compute_position_live_drag_empty_returns_zero() -> None:
    from alphamind.portfolio_state.computations import compute_position_live_drag

    assert compute_position_live_drag(()) == money("0")


def test_compute_position_live_drag_sums_three_identical_estimates() -> None:
    from alphamind.portfolio_state.computations import compute_position_live_drag

    fills = tuple(
        _fill_record(fill_id=f"fill-{i}", live_execution_estimate=_ESTIMATE_2_1_050)
        for i in range(3)
    )
    # Per-fill drag = 2 + 1 + 0.50 = 3.50; 3 fills → 10.50.
    assert compute_position_live_drag(fills) == money("10.50")


def test_compute_position_live_drag_skips_none_estimates() -> None:
    from alphamind.portfolio_state.computations import compute_position_live_drag

    fills = (
        _fill_record(fill_id="fill-0", live_execution_estimate=_ESTIMATE_2_1_050),
        _fill_record(fill_id="fill-1", live_execution_estimate=None),
        _fill_record(fill_id="fill-2", live_execution_estimate=_ESTIMATE_2_1_050),
    )
    # None fill contributes 0; two non-None fills × 3.50 = 7.00.
    assert compute_position_live_drag(fills) == money("7.00")


def test_compute_position_live_drag_all_none_returns_zero() -> None:
    """Fills from live mode or pre-harness paper-mode carry None estimates."""
    from alphamind.portfolio_state.computations import compute_position_live_drag

    fills = tuple(
        _fill_record(fill_id=f"fill-{i}", live_execution_estimate=None) for i in range(3)
    )
    assert compute_position_live_drag(fills) == money("0")


def test_compute_portfolio_live_drag_empty_mapping_returns_zero() -> None:
    from alphamind.portfolio_state.computations import compute_portfolio_live_drag

    assert compute_portfolio_live_drag({}) == money("0")
