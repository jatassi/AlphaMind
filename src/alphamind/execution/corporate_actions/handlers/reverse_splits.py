"""REVERSE_SPLIT handler (ALP-411).

Mirrors the forward-SPLIT handler in :mod:`splits` but inverts the equity
mutation (``share_count /= ratio``, ``basis *= ratio``) and credits the
fractional-share cash-out when Alpaca reports a residual.  Options and strategy positions
project from Alpaca's post-adjustment snapshot via ``AlpacaPositionLookup``;
greeks are flagged stale via ``OptionGreeks.refresh_failed`` per
``architecture.md § 4d`` because OCC strike/contract-count changes invalidate
the prior IV-derived greeks (a fresh IV fetch + Black-Scholes recompute
happens at the next 4d refresh).
"""

from __future__ import annotations

from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.portfolio_state.events.activity_log import CashCreditReason

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._handler_base import (
    apply_options_position_mutation,
    finalize_ca_handler,
    load_position_for_ca,
)
from ._shared import _apply_signed_cash_movement


async def handle_reverse_split(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
    alpaca_position_lookup: AlpacaPositionLookup | None = None,
) -> None:
    """Apply a reverse stock split.

    * Equity: ``share_count /= ratio_or_amount``;
      ``average_cost_basis_per_share *= ratio_or_amount``.
      Positive ``signed_cash_impact_usd`` is credited via
      :class:`CashCreditReason.FRACTIONAL_SHARE_CASH_OUT`.
    * Options / strategy: read post-adjustment state from
      ``alpaca_position_lookup`` and project onto the local record; greeks are
      flagged stale (``refresh_failed = True``).
    """
    pos_row, position = await load_position_for_ca(handle, activity)
    result = apply_options_position_mutation(
        position,
        activity,
        alpaca_position_lookup,
        equity_quantity_factor=1.0 / activity.ratio_or_amount,
        equity_basis_factor=activity.ratio_or_amount,
    )

    if activity.signed_cash_impact_usd > 0:
        await _apply_signed_cash_movement(
            handle,
            activity.signed_cash_impact_usd,
            reason=CashCreditReason.FRACTIONAL_SHARE_CASH_OUT.value,
            timestamp=activity.transaction_time,
            position_id=activity.position_id,
        )

    await finalize_ca_handler(handle, pos_row=pos_row, activity=activity, result=result)


__all__ = ["handle_reverse_split"]
