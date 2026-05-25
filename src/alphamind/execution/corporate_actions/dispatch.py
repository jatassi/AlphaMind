"""Corporate-action dispatch table and public entry point (ALP-409).

``integrate_ca_activity`` is the single public entry point for integrating one
Alpaca CA activity into AlphaMind's state.  Internally it consults ``_HANDLERS``,
a dict keyed on ``CorporateActionType`` with exactly one entry per enum member.

The SPLIT handler is fully implemented; remaining handlers are wired into
``_HANDLERS`` here but their bodies vary in completeness (see each handler
module for current status).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from alphamind.portfolio_state.events.activity_log import CorporateActionType
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)

from .handlers.cash_dividends import (
    handle_cash_dividend_long,
    handle_cash_dividend_short,
)
from .handlers.mergers import handle_cash_merger, handle_stock_merger
from .handlers.reverse_splits import handle_reverse_split
from .handlers.spin_offs import handle_spin_off
from .handlers.splits import handle_split
from .handlers.stock_dividends import handle_stock_dividend
from .handlers.ticker_changes import handle_symbol_change
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
    ``activity.action_type``. Raises ``NotImplementedError`` for any
    ``CorporateActionType`` whose handler body has not landed yet — see each
    handler module for current status (cash-dividend and split handlers are
    tracked as ALP-638 / ALP-639).

    Args:
        handle: Open ``InvocationHandle`` from the surrounding ``InvocationContext``.
        activity: The CA activity to integrate.
        alpaca_position_lookup: Optional callable for reading live Alpaca positions.
            Equity-only handlers ignore this argument; callers that haven't
            wired a lookup pass ``None``.
    """
    handler = _HANDLERS[activity.action_type]
    await handler(handle, activity, alpaca_position_lookup)


__all__ = [
    "_HANDLERS",
    "CAHandler",
    "integrate_ca_activity",
]
