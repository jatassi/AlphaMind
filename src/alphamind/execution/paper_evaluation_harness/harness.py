"""Harness composition for `compute_live_execution_estimate` (ALP-527).

Composes the fee, spread, and impact primitives (stories 01 / 02a / 02b) into a
single per-fill function that dispatches on ``InstrumentType``. Pure compute: no
DB, no HTTP, no module-level config imports — every input is threaded through
function parameters. The wedge integration (paper-mode gating, ADV / realized
volatility lookup, order-type lookup) lives at the call site (story 04a).

Sign convention for ``live_adjusted_fill_price`` (per
``paper-evaluation-harness.md`` § Spread + impact):

* Buys:  live = paper + (spread/2 + per_share_impact)  →  live > paper.
* Sells: live = paper - (spread/2 + per_share_impact)  →  live < paper.

With zero drag (e.g., zero estimated spread + zero estimated impact, which
happens when the spread floor saturates at the penny tick and the per-share
impact rounds away below it), ``live_adjusted_fill_price == fill_price``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from alphamind._kernel.money import Money, Price, money, price
from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.execution.paper_evaluation_harness.fees import compute_regulatory_fees
from alphamind.execution.paper_evaluation_harness.impact import estimate_impact
from alphamind.execution.paper_evaluation_harness.spread import estimate_spread
from alphamind.portfolio_state.records.positions import (
    InstrumentType,
    LiveExecutionEstimate,
)

__all__ = ["compute_live_execution_estimate"]

# Per-contract underlying-share equivalent for standard US options.
# Sourced from ``broker-adapter.md`` § Options contract multiplier.
_SHARES_PER_OPTIONS_CONTRACT = Decimal(100)

_HALF = Decimal("0.5")


def compute_live_execution_estimate(
    *,
    fill_price: Price,
    fill_quantity: float,
    instrument_type: InstrumentType,
    side: Literal["buy", "sell"],
    order_type: OrderType,
    adv_shares: float | None,
    realized_volatility: float | None,
    config: PaperHarness,
) -> LiveExecutionEstimate | None:
    """Estimate the live-execution drag for a single paper fill.

    Returns ``None`` when the required data inputs for the instrument class
    are missing — caller persists ``live_execution_estimate=None`` per parent
    decision H (graceful no-estimate fallback rather than synthetic values).

    Parameters
    ----------
    fill_price:
        Paper fill price per share (equities) or per contract (options).
    fill_quantity:
        Number of shares (equities) or contracts (options) filled.
    instrument_type:
        ``EQUITY`` and ``OPTIONS`` dispatch to the per-class composition.
        ``STRATEGY`` raises ``NotImplementedError`` — strategy parent fills
        never reach this layer per parent decision G.
    side:
        ``"buy"`` or ``"sell"``. Drives both the fee dispatch and the
        sign of the spread/impact applied to ``live_adjusted_fill_price``.
    order_type:
        Looks up the matching impact coefficient from
        ``config.impact_coefficients[order_type]``.
    adv_shares:
        Average daily volume — shares for equities, contracts for options.
        ``None`` means upstream data is unavailable for this contract; the
        function returns ``None`` and the caller persists no estimate.
    realized_volatility:
        Annualized realized volatility of the underlying equity (same source
        for equity and option fills). ``None`` → return ``None``.
    config:
        ``PaperHarness`` config block (spread buffer, impact coefficients,
        fee schedule).
    """
    if instrument_type is InstrumentType.STRATEGY:
        msg = (
            "compute_live_execution_estimate called with InstrumentType.STRATEGY; "
            "strategy parent fills must be decomposed into per-leg EQUITY/OPTIONS "
            "fills before reaching the paper-evaluation harness (parent decision G)."
        )
        raise NotImplementedError(msg)

    if adv_shares is None or realized_volatility is None:
        return None

    if instrument_type is InstrumentType.EQUITY:
        return _equity_estimate(
            fill_price=fill_price,
            fill_quantity=fill_quantity,
            side=side,
            order_type=order_type,
            adv_shares=adv_shares,
            realized_volatility=realized_volatility,
            config=config,
        )

    # instrument_type is InstrumentType.OPTIONS (enum is exhausted).
    return _options_estimate(
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        side=side,
        order_type=order_type,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
        config=config,
    )


def _equity_estimate(
    *,
    fill_price: Price,
    fill_quantity: float,
    side: Literal["buy", "sell"],
    order_type: OrderType,
    adv_shares: float,
    realized_volatility: float,
    config: PaperHarness,
) -> LiveExecutionEstimate:
    estimated_spread = estimate_spread(
        fill_price=fill_price,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
        buffer_pct=config.spread_buffer_pct,
    )
    estimated_impact = estimate_impact(
        fill_shares=fill_quantity,
        adv_shares=adv_shares,
        estimated_spread=estimated_spread,
        order_type=order_type,
        coefficient=config.impact_coefficients[order_type],
    )
    estimated_regulatory_fees = compute_regulatory_fees(
        instrument_type=InstrumentType.EQUITY,
        side=side,
        fill_quantity=fill_quantity,
        fill_price=fill_price,
        schedule=config.fee_schedule,
    )

    qty_decimal = Decimal(str(fill_quantity))
    # Per-fill dollar cost of crossing the spread: per-share spread * shares
    # crosses only half on average (order crosses one side of the touch).
    spread_usd = money(estimated_spread * qty_decimal * _HALF)

    live_adjusted_fill_price = _live_adjusted_price(
        fill_price=fill_price,
        per_share_spread=estimated_spread,
        per_fill_impact=estimated_impact,
        fill_quantity=qty_decimal,
        side=side,
    )

    return LiveExecutionEstimate(
        estimated_spread_usd=spread_usd,
        estimated_impact_usd=estimated_impact,
        estimated_regulatory_fees_usd=estimated_regulatory_fees,
        live_adjusted_fill_price=live_adjusted_fill_price,
    )


def _options_estimate(
    *,
    fill_price: Price,
    fill_quantity: float,
    side: Literal["buy", "sell"],
    order_type: OrderType,
    adv_shares: float,
    realized_volatility: float,
    config: PaperHarness,
) -> LiveExecutionEstimate:
    estimated_spread = estimate_spread(
        fill_price=fill_price,
        adv_shares=adv_shares,
        realized_volatility=realized_volatility,
        buffer_pct=config.spread_buffer_pct,
    )
    estimated_impact = estimate_impact(
        fill_shares=fill_quantity,
        adv_shares=adv_shares,
        estimated_spread=estimated_spread,
        order_type=order_type,
        coefficient=config.impact_coefficients[order_type],
    )
    estimated_regulatory_fees = compute_regulatory_fees(
        instrument_type=InstrumentType.OPTIONS,
        side=side,
        fill_quantity=fill_quantity,
        fill_price=fill_price,
        schedule=config.fee_schedule,
    )

    qty_decimal = Decimal(str(fill_quantity))
    spread_usd = money(estimated_spread * qty_decimal * _HALF)

    # Options: standard-contract multiplier (100 shares/contract) governs
    # the per-underlying-share attribution of the per-fill impact dollars
    # when converting them into the per-share price adjustment.
    underlying_share_equivalent = qty_decimal * _SHARES_PER_OPTIONS_CONTRACT
    live_adjusted_fill_price = _live_adjusted_price(
        fill_price=fill_price,
        per_share_spread=estimated_spread,
        per_fill_impact=estimated_impact,
        fill_quantity=underlying_share_equivalent,
        side=side,
    )

    return LiveExecutionEstimate(
        estimated_spread_usd=spread_usd,
        estimated_impact_usd=estimated_impact,
        estimated_regulatory_fees_usd=estimated_regulatory_fees,
        live_adjusted_fill_price=live_adjusted_fill_price,
    )


def _live_adjusted_price(
    *,
    fill_price: Price,
    per_share_spread: Money,
    per_fill_impact: Money,
    fill_quantity: Decimal,
    side: Literal["buy", "sell"],
) -> Price:
    """Apply spread + impact to the paper fill price with the buy/sell sign.

    Per-share components:

    * ``spread_per_share / 2`` — the order crosses one side of the touch.
    * ``per_fill_impact / fill_quantity`` — impact dollars are per-fill, so we
      attribute back to per-share for the price adjustment.

    Buys add (paid more live); sells subtract (received less live). With zero
    drag both sides reduce to ``fill_price``.
    """
    per_share_spread_component = per_share_spread * _HALF
    per_share_impact_component = (
        per_fill_impact / fill_quantity if fill_quantity > 0 else Decimal(0)
    )
    per_share_drag = per_share_spread_component + per_share_impact_component

    adjusted = fill_price + per_share_drag if side == "buy" else fill_price - per_share_drag
    return price(adjusted)
