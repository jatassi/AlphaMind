"""Pure-function position-level computations for the snapshot assembler (story 05a).

All functions are stateless and deterministic: no I/O, no clocks, no shared state.
The assembler (story 06) calls these in a fixed order and supplies all external inputs
(prices, timestamps) as arguments.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.portfolio_state.pricing import PriceQuote
from alphamind.portfolio_state.records.orders import BracketLegType, BracketRecord
from alphamind.portfolio_state.records.positions import (
    Direction,
    InstrumentType,
    PositionRecord,
)


class MissingLegPriceError(KeyError):
    """Raised by strategy-variant functions when a leg_id is absent from the supplied prices dict."""


# ---------------------------------------------------------------------------
# 1. Market value
# ---------------------------------------------------------------------------


def compute_market_value_usd(position: PositionRecord, price: PriceQuote) -> float:
    """Return signed market value in USD for EQUITY or OPTIONS positions.

    For EQUITY:
    - LONG: share_count * price_usd (positive)
    - SHORT: -share_count * price_usd (negative)

    For OPTIONS: contract_count * contract_multiplier * price_usd
    (price must be the option's own price, not the underlying's).

    Raises ValueError for STRATEGY positions — use compute_strategy_market_value_usd.
    """
    if position.instrument_type == InstrumentType.STRATEGY:
        msg = "use compute_strategy_market_value_usd"
        raise ValueError(msg)
    if position.instrument_type == InstrumentType.EQUITY:
        assert position.equity_details is not None
        mv = position.equity_details.share_count * price.price_usd
        return mv if position.direction == Direction.LONG else -mv
    # OPTIONS
    assert position.options_details is not None
    return (
        position.options_details.contract_count
        * position.options_details.contract_multiplier
        * price.price_usd
    )


def compute_strategy_market_value_usd(
    position: PositionRecord,
    leg_prices: dict[str, PriceQuote],
) -> float:
    """Return market value in USD for a STRATEGY position, summing across legs.

    Each leg contributes: contract_count * contract_multiplier * leg_prices[leg_id].price_usd

    Raises MissingLegPriceError if any leg_id is absent from leg_prices.
    """
    assert position.strategy_details is not None
    total = 0.0
    for leg in position.strategy_details.legs:
        if leg.leg_id not in leg_prices:
            raise MissingLegPriceError(leg.leg_id)
        opts = leg.options
        total += opts.contract_count * opts.contract_multiplier * leg_prices[leg.leg_id].price_usd
    return total


# ---------------------------------------------------------------------------
# 2. Position weight
# ---------------------------------------------------------------------------


def compute_position_weight_pct(
    position_market_value_usd: float,
    total_portfolio_value_usd: float,
) -> float:
    """Return position weight as a percentage of total portfolio value.

    Weight is based on the absolute market value so short positions contribute
    positively to overall portfolio weight.

    Returns 0.0 when total_portfolio_value_usd == 0 (empty portfolio).
    Raises ValueError when total_portfolio_value_usd < 0.
    """
    if total_portfolio_value_usd < 0:
        msg = f"total_portfolio_value_usd must be >= 0; got {total_portfolio_value_usd}"
        raise ValueError(msg)
    if total_portfolio_value_usd == 0:
        return 0.0
    return (abs(position_market_value_usd) / total_portfolio_value_usd) * 100.0


# ---------------------------------------------------------------------------
# 3. Position age
# ---------------------------------------------------------------------------


def compute_position_age_hours(entry_timestamp: datetime, now: datetime) -> float:
    """Return the age of the position in fractional hours.

    Both arguments must be tz-aware; mixing tz-aware and tz-naive raises ValueError.
    Returns 0.0 when entry_timestamp == now; negative when now < entry_timestamp
    (no clamping — the caller is responsible for asserting monotonicity).
    """
    entry_aware = entry_timestamp.tzinfo is not None
    now_aware = now.tzinfo is not None
    if entry_aware != now_aware:
        msg = "entry_timestamp and now must both be tz-aware or both tz-naive"
        raise ValueError(msg)
    return (now - entry_timestamp).total_seconds() / 3600.0


# ---------------------------------------------------------------------------
# 4. Unrealized P/L
# ---------------------------------------------------------------------------


def compute_unrealized_pnl_usd(
    market_value_usd: float,
    cost_basis_usd: float,
    direction: Direction,
) -> float:
    """Return unrealized P/L in USD.

    LONG:  market_value_usd - cost_basis_usd
    SHORT: cost_basis_usd - abs(market_value_usd)
    """
    if direction == Direction.LONG:
        return market_value_usd - cost_basis_usd
    return cost_basis_usd - abs(market_value_usd)


def compute_unrealized_pnl_pct(unrealized_pnl_usd: float, cost_basis_usd: float) -> float:
    """Return unrealized P/L as a percentage of cost basis.

    Returns 0.0 when cost_basis_usd == 0 (zero-cost position; no ZeroDivisionError).
    """
    if cost_basis_usd == 0:
        return 0.0
    return (unrealized_pnl_usd / cost_basis_usd) * 100.0


# ---------------------------------------------------------------------------
# 5. Distance to target / stop
# ---------------------------------------------------------------------------


def _parse_price_trigger(trigger_condition: str) -> float | None:
    """Parse a trigger_condition string as a price level.

    Returns the float value when the string is numeric, None otherwise.
    """
    try:
        return float(trigger_condition)
    except ValueError:
        return None


def compute_distance_to_target_usd(
    current_price_usd: float,
    bracket: BracketRecord,
    direction: Direction,
) -> float | None:
    """Return distance to the TAKE_PROFIT target in USD.

    Sign convention:
    - LONG:  target_price - current_price_usd  (positive = upside remaining)
    - SHORT: current_price_usd - target_price  (positive = downside remaining)

    Returns None when:
    - No TAKE_PROFIT leg is present in the bracket.
    - The leg's trigger_condition is non-numeric (event- or time-based).
    """
    for leg in bracket.protective_legs:
        if leg.leg_type == BracketLegType.TAKE_PROFIT:
            target_price = _parse_price_trigger(leg.trigger_condition)
            if target_price is None:
                return None
            if direction == Direction.LONG:
                return target_price - current_price_usd
            return current_price_usd - target_price
    return None


def compute_distance_to_stop_usd(
    current_price_usd: float,
    bracket: BracketRecord,
    direction: Direction,
) -> float | None:
    """Return distance to the PRICE_STOP level in USD.

    Sign convention:
    - LONG:  current_price_usd - stop_price  (positive = cushion above the stop)
    - SHORT: stop_price - current_price_usd  (positive = cushion below the stop)

    Returns None when:
    - No PRICE_STOP leg is present.
    - The leg's trigger_condition is non-numeric.
    """
    for leg in bracket.protective_legs:
        if leg.leg_type == BracketLegType.PRICE_STOP:
            stop_price = _parse_price_trigger(leg.trigger_condition)
            if stop_price is None:
                return None
            if direction == Direction.LONG:
                return current_price_usd - stop_price
            return stop_price - current_price_usd
    return None


# ---------------------------------------------------------------------------
# 6. Risk/reward at current price
# ---------------------------------------------------------------------------


def compute_risk_reward_at_current(
    distance_to_target_usd: float | None,
    distance_to_stop_usd: float | None,
) -> float | None:
    """Return the risk/reward ratio at the current price.

    Returns distance_to_target_usd / abs(distance_to_stop_usd).

    Returns None when:
    - Either argument is None.
    - distance_to_stop_usd == 0 (position is at the stop; ratio undefined).

    A positive ratio indicates favorable R:R remaining; the ratio can be negative
    when the price has moved past the target or past the stop.
    """
    if distance_to_target_usd is None or distance_to_stop_usd is None:
        return None
    if distance_to_stop_usd == 0:
        return None
    return distance_to_target_usd / abs(distance_to_stop_usd)


# ---------------------------------------------------------------------------
# 7. Notional exposure
# ---------------------------------------------------------------------------


def compute_notional_exposure_usd(position: PositionRecord, price: PriceQuote) -> float:
    """Return notional exposure in USD (always non-negative) for EQUITY or OPTIONS.

    For EQUITY: share_count * price_usd (underlying price; direction is captured separately).
    For OPTIONS: contract_count * contract_multiplier * underlying_price
                 (price must be the underlying's price, not the option premium).

    Raises ValueError for STRATEGY positions — use compute_strategy_notional_exposure_usd.
    """
    if position.instrument_type == InstrumentType.STRATEGY:
        msg = "use compute_strategy_notional_exposure_usd"
        raise ValueError(msg)
    if position.instrument_type == InstrumentType.EQUITY:
        assert position.equity_details is not None
        return position.equity_details.share_count * price.price_usd
    # OPTIONS
    assert position.options_details is not None
    return (
        position.options_details.contract_count
        * position.options_details.contract_multiplier
        * price.price_usd
    )


def compute_strategy_notional_exposure_usd(
    position: PositionRecord,
    leg_underlying_prices: dict[str, PriceQuote],
) -> float:
    """Return notional exposure in USD for a STRATEGY position, summing leg notionals.

    Each leg contributes: contract_count * contract_multiplier * underlying_price_usd

    Raises MissingLegPriceError if any leg_id is absent from leg_underlying_prices.
    """
    assert position.strategy_details is not None
    total = 0.0
    for leg in position.strategy_details.legs:
        if leg.leg_id not in leg_underlying_prices:
            raise MissingLegPriceError(leg.leg_id)
        opts = leg.options
        total += (
            opts.contract_count
            * opts.contract_multiplier
            * leg_underlying_prices[leg.leg_id].price_usd
        )
    return total


# ---------------------------------------------------------------------------
# 8. Delta-adjusted exposure
# ---------------------------------------------------------------------------


def compute_delta_adjusted_exposure_usd(position: PositionRecord, price: PriceQuote) -> float:
    """Return delta-adjusted exposure in USD for EQUITY or OPTIONS.

    EQUITY LONG:  +share_count * price_usd  (delta = +1)
    EQUITY SHORT: -share_count * price_usd  (delta = -1)
    OPTIONS:      contract_count * contract_multiplier * greeks.delta * underlying_price_usd
                  (price must be the underlying's price; the assembler resolves option vs underlying)

    Raises ValueError for STRATEGY positions — use compute_strategy_delta_adjusted_exposure_usd.
    """
    if position.instrument_type == InstrumentType.STRATEGY:
        msg = "use compute_strategy_delta_adjusted_exposure_usd"
        raise ValueError(msg)
    if position.instrument_type == InstrumentType.EQUITY:
        assert position.equity_details is not None
        notional = position.equity_details.share_count * price.price_usd
        return notional if position.direction == Direction.LONG else -notional
    # OPTIONS
    assert position.options_details is not None
    opts = position.options_details
    return opts.contract_count * opts.contract_multiplier * opts.greeks.delta * price.price_usd


def compute_strategy_delta_adjusted_exposure_usd(
    position: PositionRecord,
    leg_underlying_prices: dict[str, PriceQuote],
) -> float:
    """Return delta-adjusted exposure in USD for a STRATEGY position.

    Returns: strategy_greeks.delta * sum(leg notional exposures)

    The strategy's net delta (from strategy_details.strategy_greeks) is applied to the
    summed underlying notional. Raises MissingLegPriceError if any leg_id is absent.
    """
    assert position.strategy_details is not None
    summed_notional = compute_strategy_notional_exposure_usd(position, leg_underlying_prices)
    return position.strategy_details.strategy_greeks.delta * summed_notional


__all__ = [
    "MissingLegPriceError",
    "compute_delta_adjusted_exposure_usd",
    "compute_distance_to_stop_usd",
    "compute_distance_to_target_usd",
    "compute_market_value_usd",
    "compute_notional_exposure_usd",
    "compute_position_age_hours",
    "compute_position_weight_pct",
    "compute_risk_reward_at_current",
    "compute_strategy_delta_adjusted_exposure_usd",
    "compute_strategy_market_value_usd",
    "compute_strategy_notional_exposure_usd",
    "compute_unrealized_pnl_pct",
    "compute_unrealized_pnl_usd",
]
