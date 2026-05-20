"""Pure-function utilities for at-expiration payoff metrics of a multi-leg
options strategy (story 01c).

Each function evaluates the strategy's piecewise-linear payoff function at
each strike kink (plus a U=0 endpoint for stocks bounded below) and reports
the metric named by the design's `StrategyPositionDetails` schema. Strategies
with unbounded upside (e.g., long call, long straddle) report
`float('inf')` for max profit; strategies with unbounded downside (e.g.,
naked short call) report `float('-inf')` for max loss.
"""

from __future__ import annotations

from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    StrategyLeg,
)


def _validate_legs(legs: tuple[StrategyLeg, ...]) -> None:
    """Run shared input validation. Raises ValueError on empty legs, missing
    direction, non-positive contract count, mixed expirations, or mixed
    underlying tickers."""
    if not legs:
        msg = "legs must be non-empty"
        raise ValueError(msg)
    expirations = {leg.options.expiration_date for leg in legs}
    if len(expirations) > 1:
        msg = (
            f"all legs must share a single expiration_date; got "
            f"{sorted(d.isoformat() for d in expirations)}"
        )
        raise ValueError(msg)
    underlyings = {leg.options.underlying_ticker for leg in legs}
    if len(underlyings) > 1:
        msg = f"all legs must share a single underlying_ticker; got {sorted(underlyings)}"
        raise ValueError(msg)
    for leg in legs:
        if leg.direction is None:
            msg = f"leg {leg.leg_id!r} is missing direction"
            raise ValueError(msg)
        if leg.options.contract_count <= 0:
            msg = f"leg {leg.leg_id!r} has non-positive contract_count {leg.options.contract_count}"
            raise ValueError(msg)


def _leg_sign(leg: StrategyLeg) -> float:
    """+1 for LONG, -1 for SHORT. Caller must have validated direction."""
    return 1.0 if leg.direction == Direction.LONG else -1.0


def _leg_payoff(leg: StrategyLeg, underlying_price: float) -> float:
    """Per-leg payoff at the given underlying price."""
    opt = leg.options
    if opt.contract_type == OptionContractType.CALL:
        intrinsic = max(underlying_price - opt.strike_price, 0.0)
    else:
        intrinsic = max(opt.strike_price - underlying_price, 0.0)
    return _leg_sign(leg) * intrinsic * opt.contract_count * opt.contract_multiplier


def _strategy_payoff(
    legs: tuple[StrategyLeg, ...],
    underlying_price: float,
    net_premium_usd: float,
) -> float:
    """Strategy payoff at U = sum(leg payoffs) - net_premium."""
    return sum(_leg_payoff(leg, underlying_price) for leg in legs) - net_premium_usd


def _slope_at_high_u(legs: tuple[StrategyLeg, ...]) -> float:
    """Slope of the payoff function as underlying U → +∞.

    Only CALL legs contribute (puts are out-of-the-money for large U). Each
    long-call adds +C·M; each short-call adds -C·M.
    """
    slope = 0.0
    for leg in legs:
        if leg.options.contract_type == OptionContractType.CALL:
            slope += _leg_sign(leg) * leg.options.contract_count * leg.options.contract_multiplier
    return slope


def _sample_points(legs: tuple[StrategyLeg, ...]) -> list[float]:
    """Finite sample points for max/min/breakeven evaluation: U=0 plus each
    unique strike, sorted ascending."""
    strikes = {leg.options.strike_price for leg in legs}
    return sorted({0.0, *strikes})


def compute_strategy_net_premium_usd(legs: tuple[StrategyLeg, ...]) -> float:
    """Signed net premium (in USD) of a multi-leg options strategy.

    Computed as the sum over legs of
    ``leg_sign * contract_count * contract_multiplier * premium_paid_per_contract``,
    where ``leg_sign`` is ``+1`` for a LONG leg (premium paid) and ``-1`` for a
    SHORT leg (premium received).

    Sign convention: a positive result is a net debit (the strategy costs
    capital to open); a negative result is a net credit (the strategy is
    opened for a cash inflow). This is exactly the ``net_premium_usd``
    argument the :func:`compute_strategy_max_profit_usd` /
    :func:`compute_strategy_max_loss_usd` / :func:`compute_strategy_breakeven_levels`
    functions expect.

    Raises ``ValueError`` if ``legs`` is empty, any leg is missing direction,
    any leg has non-positive ``contract_count``, or legs span multiple
    expiration dates or underlying tickers.
    """
    _validate_legs(legs)
    return sum(
        _leg_sign(leg)
        * leg.options.contract_count
        * leg.options.contract_multiplier
        * leg.options.premium_paid_per_contract
        for leg in legs
    )


def compute_strategy_max_profit_usd(
    legs: tuple[StrategyLeg, ...],
    net_premium_usd: float,
) -> float:
    """Maximum P/L (in USD) the strategy can realize at expiration.

    Computed by evaluating the piecewise-linear at-expiration payoff function
    at each strike kink (plus the U=0 endpoint) and taking the maximum.
    Returns ``float('inf')`` when the slope as U → +∞ is positive (the call
    side of the strategy is net-long).

    Sign convention: positive = profit. ``net_premium_usd`` is the net debit
    (positive) or net credit (negative) at entry; it shifts the payoff
    vertically.

    Raises ``ValueError`` if ``legs`` is empty, any leg is missing direction,
    any leg has non-positive ``contract_count``, or legs span multiple
    expiration dates.
    """
    _validate_legs(legs)
    if _slope_at_high_u(legs) > 0.0:
        return float("inf")
    samples = [_strategy_payoff(legs, u, net_premium_usd) for u in _sample_points(legs)]
    return max(samples)


def compute_strategy_max_loss_usd(
    legs: tuple[StrategyLeg, ...],
    net_premium_usd: float,
) -> float:
    """Minimum P/L (in USD; negative = loss) the strategy can realize at
    expiration.

    Same evaluation procedure as :func:`compute_strategy_max_profit_usd`,
    taking the minimum across sample points. Returns ``float('-inf')`` when
    the slope as U → +∞ is negative (the call side is net-short — naked
    short-call risk).

    Raises ``ValueError`` per :func:`compute_strategy_max_profit_usd`.
    """
    _validate_legs(legs)
    if _slope_at_high_u(legs) < 0.0:
        return float("-inf")
    samples = [_strategy_payoff(legs, u, net_premium_usd) for u in _sample_points(legs)]
    return min(samples)


def compute_strategy_breakeven_levels(
    legs: tuple[StrategyLeg, ...],
    net_premium_usd: float,
) -> tuple[float, ...]:
    """Underlying-price levels at which the strategy's at-expiration P/L is zero.

    Computed by enumerating zero-crossings of the piecewise-linear payoff
    function. Walks adjacent finite sample pairs (``U=0`` plus each unique
    strike), and additionally extrapolates over ``(max_strike, +∞)`` using
    the high-U slope when that slope is non-zero. Exact-zero kink points
    are themselves breakevens. The result is sorted ascending and deduped
    within float tolerance.

    Returns the empty tuple when the payoff has no zero crossing (the
    strategy is always profitable or always lossy across all underlying
    prices).

    Raises ``ValueError`` per :func:`compute_strategy_max_profit_usd`.
    """
    _validate_legs(legs)

    samples = _sample_points(legs)
    payoffs = [_strategy_payoff(legs, u, net_premium_usd) for u in samples]

    crossings: list[float] = []
    for i in range(len(samples) - 1):
        u_lo, u_hi = samples[i], samples[i + 1]
        p_lo, p_hi = payoffs[i], payoffs[i + 1]
        if p_lo == 0.0:
            crossings.append(u_lo)
        if p_lo * p_hi < 0.0:
            # Linear interpolation between opposite-sign endpoints.
            crossings.append(u_lo + abs(p_lo) / (abs(p_lo) + abs(p_hi)) * (u_hi - u_lo))
    # Trailing kink: include if exactly zero.
    if payoffs and payoffs[-1] == 0.0:
        crossings.append(samples[-1])

    # Extrapolate over (max_strike, +∞) using the high-U slope, if non-zero
    # and the payoff at max_strike has opposite sign from the slope's
    # asymptotic direction.
    slope = _slope_at_high_u(legs)
    if slope != 0.0 and samples:
        p_last = payoffs[-1]
        # As U increases beyond the last sample, payoff(U) = p_last + slope*(U - u_last).
        # A zero crossing exists when p_last and slope have opposite signs.
        if p_last * slope < 0.0:
            crossings.append(samples[-1] + (-p_last) / slope)

    # Sort and dedupe within float tolerance.
    crossings.sort()
    deduped: list[float] = []
    for x in crossings:
        if not deduped or abs(x - deduped[-1]) > 1e-9:
            deduped.append(x)
    return tuple(deduped)
