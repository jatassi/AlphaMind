"""Read-time live-adjusted P/L drag helpers (story 04b / ALP-529).

Pure functions that sum the per-fill ``LiveExecutionEstimate`` cost components
persisted on ``FillRecord`` rows. The existing raw ``PortfolioPnL`` typed
record and ``PositionView`` shape are untouched per parent decision (A);
live-adjusted P/L is computed at read time by downstream consumers (feedback
loop, future evaluation reports) that care.

The three cost components — spread, impact, regulatory fees — are the additive
primitive for portfolio-level drag aggregation. ``live_adjusted_fill_price`` is
NOT summed here: it is the per-fill price-level signal, not a drag accumulator.
"""

from __future__ import annotations

from alphamind._kernel.money import DECIMAL_ZERO, Money, signed_money
from alphamind.state.records import FillRecord


def compute_position_live_drag(fills: tuple[FillRecord, ...]) -> Money:
    """Sum live-execution-drag cost components across a position's fills.

    Per-fill drag = ``estimated_spread_usd + estimated_impact_usd +
    estimated_regulatory_fees_usd``. Fills with ``live_execution_estimate is
    None`` (live mode, or paper mode pre-harness) contribute zero. Returns
    ``signed_money("0")`` for an empty tuple.
    """
    total = DECIMAL_ZERO
    for fill in fills:
        estimate = fill.live_execution_estimate
        if estimate is None:
            continue
        total += (
            estimate.estimated_spread_usd
            + estimate.estimated_impact_usd
            + estimate.estimated_regulatory_fees_usd
        )
    return signed_money(total)
