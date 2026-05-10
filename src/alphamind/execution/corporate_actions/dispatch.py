"""Corporate-action dispatch table and public entry point (ALP-409).

``integrate_ca_activity`` is the single public entry point for integrating one
Alpaca CA activity into AlphaMind's state.  Internally it consults ``_HANDLERS``,
a dict keyed on ``CorporateActionType`` with exactly one entry per enum member.

The SPLIT handler is fully implemented; every other member raises
``NotImplementedError`` with the standard message.  Stories 03a-03d replace
those stubs with real implementations without touching this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.portfolio_state.events.activity_log import CorporateActionType

from .handlers.splits import (
    handle_cash_dividend_long,
    handle_cash_dividend_short,
    handle_cash_merger,
    handle_reverse_split,
    handle_spin_off,
    handle_split,
    handle_stock_dividend,
    handle_stock_merger,
    handle_symbol_change,
)
from .types import AlpacaPositionLookup, CorporateActionActivity

# Type alias for all CA handler callables.
CAHandler = Callable[
    [InvocationHandle, CorporateActionActivity, "AlpacaPositionLookup | None"],
    Awaitable[None],
]

_HANDLERS: dict[CorporateActionType, CAHandler] = {
    CorporateActionType.SPLIT: handle_split,
    CorporateActionType.REVERSE_SPLIT: handle_reverse_split,
    CorporateActionType.STOCK_DIVIDEND: handle_stock_dividend,
    CorporateActionType.CASH_DIVIDEND_LONG: handle_cash_dividend_long,
    CorporateActionType.CASH_DIVIDEND_SHORT: handle_cash_dividend_short,
    CorporateActionType.CASH_MERGER: handle_cash_merger,
    CorporateActionType.STOCK_MERGER: handle_stock_merger,
    CorporateActionType.SPIN_OFF: handle_spin_off,
    CorporateActionType.SYMBOL_CHANGE: handle_symbol_change,
}


async def integrate_ca_activity(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    alpaca_position_lookup: AlpacaPositionLookup | None,
) -> None:
    """Integrate one CA activity into AlphaMind state via the per-type dispatch table.

    Routes *activity* to the appropriate handler in ``_HANDLERS`` based on
    ``activity.action_type``.  Raises ``NotImplementedError`` for any
    ``CorporateActionType`` whose handler is not yet implemented (all except
    ``SPLIT`` until stories 03a-03d land).

    Args:
        handle: Open ``InvocationHandle`` from the surrounding ``InvocationContext``.
        activity: The CA activity to integrate.
        alpaca_position_lookup: Optional callable for reading live Alpaca positions.
            Equity-only handlers ignore this argument; pass ``None`` until story 04
            wires the real lookup.
    """
    handler = _HANDLERS[activity.action_type]
    await handler(handle, activity, alpaca_position_lookup)


__all__ = [
    "_HANDLERS",
    "CAHandler",
    "integrate_ca_activity",
]
