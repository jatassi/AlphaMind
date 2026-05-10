"""STOCK_DIVIDEND handler (ALP-411).

Stock dividends issue additional shares pro-rata; cost basis dilutes by the
same factor.  No cash impact.  Options and strategy positions project from
Alpaca's post-adjustment snapshot per the same skeleton as
:mod:`reverse_splits`.
"""

from __future__ import annotations

from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._handler_base import (
    apply_options_position_mutation,
    finalize_ca_handler,
    load_position_for_ca,
)


async def handle_stock_dividend(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    alpaca_position_lookup: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a stock dividend.

    * Equity: ``share_count *= (1 + ratio_or_amount)``;
      ``average_cost_basis_per_share /= (1 + ratio_or_amount)``.  No cash
      impact.
    * Options / strategy: project from ``alpaca_position_lookup`` like the
      reverse-split handler; greeks are flagged stale.
    """
    pos_row, position = await load_position_for_ca(handle, activity)
    factor = 1.0 + activity.ratio_or_amount
    result = apply_options_position_mutation(
        position,
        activity,
        alpaca_position_lookup,
        equity_quantity_factor=factor,
        equity_basis_factor=1.0 / factor,
    )
    await finalize_ca_handler(handle, pos_row=pos_row, activity=activity, result=result)


__all__ = ["handle_stock_dividend"]
