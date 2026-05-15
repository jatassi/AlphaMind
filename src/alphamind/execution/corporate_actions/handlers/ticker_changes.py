"""SYMBOL_CHANGE handler (ALP-411).

Renames the ticker reference in equity, options, and strategy positions
without changing quantity, basis, or cash.  Greeks remain valid (no
structural change).
"""

from __future__ import annotations

from alphamind.state.invocation_context.context import (
    InvocationHandle,
)

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._handler_base import (
    apply_ticker_only_mutation,
    finalize_ca_handler,
    load_position_for_ca,
)


async def handle_symbol_change(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    _: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a symbol/ticker change.

    Updates the ticker on the underlying details payload (equity ticker,
    options underlying, or each strategy leg's underlying) and finalizes
    with the standard CA log + ledger triplet.
    """
    pos_row, position = await load_position_for_ca(handle, activity)
    result = apply_ticker_only_mutation(position, activity)
    await finalize_ca_handler(handle, pos_row=pos_row, activity=activity, result=result)


__all__ = ["handle_symbol_change"]
