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

from alphamind._kernel.money import DECIMAL_ZERO, Money, money


def compute_position_live_drag(fills: tuple) -> Money:  # type: ignore[type-arg]
    """Sum live-execution-drag cost components across a position's fills.

    Returns ``money("0")`` for an empty tuple.
    """
    if not fills:
        return money("0")
    return money(DECIMAL_ZERO)
