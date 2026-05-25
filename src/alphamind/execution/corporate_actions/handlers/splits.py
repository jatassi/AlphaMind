"""Equity / options / strategy SPLIT handler (ALP-409, options + strategy via ALP-639).

The forward-split handler mirrors :mod:`reverse_splits` / :mod:`stock_dividends`:
equity positions take the multiplicative ``share_count *= ratio`` /
``basis /= ratio`` mutation, while options and strategy positions project
post-adjustment state from Alpaca's snapshot via
:func:`apply_options_position_mutation` and have their greeks flagged stale.
"""

from __future__ import annotations

from alphamind.state.invocation_context.context import (
    InvocationHandle,
)

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._handler_base import (
    apply_options_position_mutation,
    finalize_ca_handler,
    load_position_for_ca,
)


async def handle_split(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    alpaca_position_lookup: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a forward stock split.

    * Equity: ``share_count *= ratio_or_amount``;
      ``average_cost_basis_per_share /= ratio_or_amount``.
    * Options / strategy: read post-adjustment state from
      ``alpaca_position_lookup`` and project onto the local record; greeks are
      flagged stale (``refresh_failed = True``).
    """
    pos_row, position = await load_position_for_ca(handle, activity)
    result = apply_options_position_mutation(
        position,
        activity,
        alpaca_position_lookup,
        equity_quantity_factor=activity.ratio_or_amount,
        equity_basis_factor=1.0 / activity.ratio_or_amount,
    )
    await finalize_ca_handler(handle, pos_row=pos_row, activity=activity, result=result)


__all__ = ["handle_split"]
