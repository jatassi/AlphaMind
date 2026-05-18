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

from collections.abc import Mapping

from alphamind._kernel.ids import PositionId
from alphamind._kernel.money import DECIMAL_ZERO, Money, signed_money
from alphamind.state.records import FillRecord


def compute_position_live_drag(fills: tuple[FillRecord, ...]) -> Money:
    """Sum live-execution drag across a position's fills."""
    return signed_money(
        sum(
            (
                est.estimated_spread_usd
                + est.estimated_impact_usd
                + est.estimated_regulatory_fees_usd
                for fill in fills
                if (est := fill.live_execution_estimate) is not None
            ),
            DECIMAL_ZERO,
        )
    )


def compute_portfolio_live_drag(
    fills_by_position: Mapping[PositionId, tuple[FillRecord, ...]],
) -> Money:
    """Sum ``compute_position_live_drag`` across every position in the mapping.

    The returned :class:`Money` is suitable as the "live-adjusted P/L delta"
    that downstream consumers subtract from raw realized P/L. Returns
    ``signed_money("0")`` for an empty mapping.
    """
    total = DECIMAL_ZERO
    for fills in fills_by_position.values():
        total += compute_position_live_drag(fills)
    return signed_money(total)
