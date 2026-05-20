"""Per-leg Reg T initial-margin formula (ALP-424 / story 03a).

Pure module: no I/O, no clock reads, no global mutation.

Per-leg percentages are named module-level constants sourced from
``venue-configuration.md § Margin tiers``. They are not operator-tunable;
they mirror the FINRA Reg T rule.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
    position_direction,
)

# ---------------------------------------------------------------------------
# Per-leg Reg T margin constants (venue-configuration.md § Margin tiers)
# ---------------------------------------------------------------------------

_LONG_EQUITY_INITIAL_MARGIN_PCT = 0.50
_SHORT_EQUITY_INITIAL_MARGIN_PCT = 1.50
_LONG_OPTION_INITIAL_MARGIN_PCT = 1.00
_SHORT_OPTION_OTM_BASE_PCT = 0.20  # 20% of underlying minus OTM amount
_SHORT_OPTION_FLOOR_PCT = 0.10  # 10% of underlying floor
_OPTION_CONTRACT_MULTIPLIER = 100


def _equity_margin(
    details: EquityPositionDetails,
    direction: Direction,
    underlying_prices: Mapping[str, float],
) -> float:
    """Reg T margin for one equity position."""
    price = underlying_prices[details.ticker]
    qty = abs(details.share_count)
    if direction == Direction.LONG:
        return _LONG_EQUITY_INITIAL_MARGIN_PCT * qty * price
    return _SHORT_EQUITY_INITIAL_MARGIN_PCT * qty * price


def _option_leg_margin(
    options: OptionsPositionDetails,
    leg_direction: Direction,
    underlying_prices: Mapping[str, float],
) -> float:
    """Reg T margin for one option leg (single or strategy)."""
    underlying_price = underlying_prices[options.underlying_ticker]
    contracts = abs(options.contract_count)
    premium = options.premium_paid_per_contract

    if leg_direction == Direction.LONG:
        return _LONG_OPTION_INITIAL_MARGIN_PCT * contracts * premium * _OPTION_CONTRACT_MULTIPLIER

    # Short option: FINRA Reg T formula (parent decision (A))
    if options.contract_type == OptionContractType.CALL:
        otm_amount = max(options.strike_price - underlying_price, 0.0)
    else:  # PUT
        otm_amount = max(underlying_price - options.strike_price, 0.0)

    per_contract = max(
        underlying_price * _SHORT_OPTION_OTM_BASE_PCT - otm_amount,
        underlying_price * _SHORT_OPTION_FLOOR_PCT,
        premium,
    )
    return per_contract * contracts * _OPTION_CONTRACT_MULTIPLIER


def _strategy_naked_leg_sum(
    details: StrategyPositionDetails,
    underlying_prices: Mapping[str, float],
) -> float:
    """Sum the per-leg naked Reg T margin across a strategy's legs.

    The conservative treatment for an undefined-risk strategy: every leg
    contributes its standalone naked-option margin with no spread offset.
    """
    total = 0.0
    for leg in details.legs:
        leg_direction = leg.direction if leg.direction is not None else Direction.LONG
        total += _option_leg_margin(leg.options, leg_direction, underlying_prices)
    return total


def _strategy_position_margin(
    details: StrategyPositionDetails,
    underlying_prices: Mapping[str, float],
) -> float:
    """Reg T margin for a strategy position.

    A defined-risk strategy (a finite ``max_loss_usd``: vertical spreads,
    iron condors) requires margin equal to its capped loss — the most the
    position can lose — which is the collateral a broker holds for a
    defined-risk structure. ``max_loss_usd`` is the signed worst-case P/L
    (a loss → negative); margin is a positive USD requirement, hence
    ``abs(...)``.

    An undefined-risk strategy (``max_loss_usd == float('-inf')``, a naked
    short component) has no capped loss; it falls back to the per-leg
    naked-margin sum, the conservative treatment.

    A ``max_loss_usd`` of ``0.0`` is a malformed record — ``compute_regt_margin``
    only sums OPEN positions and an OPEN strategy carries story-02-recomputed
    payoff metrics — so it also falls back to the per-leg naked sum rather
    than reporting zero margin.
    """
    max_loss = details.max_loss_usd
    if max_loss == 0.0 or not math.isfinite(max_loss):
        return _strategy_naked_leg_sum(details, underlying_prices)
    return abs(max_loss)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_regt_margin(
    positions: tuple[PositionRecord, ...],
    underlying_prices: Mapping[str, float],
) -> float:
    """Sum Reg T initial-margin requirements across the position set.

    Pure function. Returns the portfolio-aggregate Reg T requirement in
    USD. ``positions`` is the open-position set (Status.OPEN); pending /
    closed positions contribute zero by exclusion. ``underlying_prices``
    is a Mapping[symbol, current price] used for equity market-value and
    short-options-formula inputs.

    Per-leg formulas (``venue-configuration.md § Margin tiers``):

    * Long equity:  ``0.50 * |quantity| * underlying_price``
    * Short equity: ``1.50 * |quantity| * underlying_price``
    * Long options: ``1.00 * |contracts| * premium * 100``
      (premium is the option's mark/cost-basis price per contract)
    * Short options (FINRA Reg T, parent decision (A))::

          max(
              underlying_price * 0.20 - OTM_amount,
              underlying_price * 0.10,
              option_premium,
          ) * |contracts| * 100

      where ``OTM_amount = max(strike - underlying, 0)`` for calls and
      ``max(underlying - strike, 0)`` for puts.

    Strategy positions iterate over their legs; each leg contributes per
    the long/short option formulas (legs are option instruments). The
    per-leg sum gives the strategy's Reg T contribution.

    Raises ``KeyError`` if a position's underlying symbol is missing from
    ``underlying_prices`` — that is an upstream contract violation
    (Phase 1 driver is responsible for populating prices for every
    open-position underlying).
    """
    total = 0.0
    for pos in positions:
        if pos.status != PositionStatus.OPEN:
            continue
        details = pos.details
        if isinstance(details, EquityPositionDetails):
            direction = position_direction(pos)
            assert direction is not None  # equity position
            total += _equity_margin(details, direction, underlying_prices)
        elif isinstance(details, OptionsPositionDetails):
            direction = position_direction(pos)
            assert direction is not None  # single-leg options position
            total += _option_leg_margin(details, direction, underlying_prices)
        elif isinstance(details, StrategyPositionDetails):
            total += _strategy_position_margin(details, underlying_prices)
    return total
