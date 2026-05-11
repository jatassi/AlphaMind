"""Per-class-group PM stress revaluation (ALP-425, story 03b).

Per ``regt-margin-attribution.md`` § Portfolio-margin reference model:
``stress_class_group`` returns the absolute value of the worst (most-negative)
P/L across a 10-point equidistant shock grid applied to one class group's
instruments. The grid spans ``[-shock_pct, +shock_pct]`` of the current
underlying price, with both endpoints inclusive.

At ``_SHOCK_GRID_POINTS = 10``, the grid positions are
``[-1.0, -7/9, -5/9, -3/9, -1/9, +1/9, +3/9, +5/9, +7/9, +1.0]`` (units of
``shock_pct``). The reader can reproduce by hand:
``grid_position_i = -1.0 + (i / (N - 1)) * 2.0`` for ``i in range(N)``.

The IV multiplier varies linearly from ``worst_down_multiplier`` at grid
position ``-1.0`` to ``worst_up_multiplier`` at grid position ``+1.0``. The
multiplier at any grid position is computed by linear interpolation between
those two endpoints; at ``grid_position = 0.0`` the multiplier is the midpoint
of the two endpoints.

Asset-class taxonomy is not v1 — ``high_cap_equity`` and ``small_cap_equity``
are exposed on ``ShockParameters`` for future revisions, but v1 looks up only
``per_symbol_overrides``, falling through directly to ``unmapped_default``
when no per-symbol entry exists.

Pure module: no I/O, no clock reads, no global mutation. The only external
calls are to the ``IvProvider`` supplied via ``MarketInputs`` (once per option
leg) and ``bs_price`` (once per option leg per grid point).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from alphamind.execution.regt_margin_attribution.class_groups import ClassGroup
from alphamind.execution.regt_margin_attribution.config import (
    IvShockMultipliers,
    RegTMarginAttributionConfig,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    OptionContractType,
    OptionsPositionDetails,
    PositionRecord,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
from alphamind.risk_guardrails.guardrail_evaluation.types import (
    ContractType,
    MarketInputs,
)

_SHOCK_GRID_POINTS = 10  # per design § Portfolio-margin reference model
_GRID_POSITIONS: tuple[float, ...] = tuple(
    -1.0 + i * (2.0 / (_SHOCK_GRID_POINTS - 1)) for i in range(_SHOCK_GRID_POINTS)
)

_OPTION_CONTRACT_TYPE_MAP: dict[OptionContractType, ContractType] = {
    OptionContractType.CALL: ContractType.CALL,
    OptionContractType.PUT: ContractType.PUT,
}


@dataclass(frozen=True, slots=True)
class _LegBaseline:
    """Per-leg baseline state pre-computed once before the grid loop.

    The IV lookup is done exactly once (at the unshocked strike/expiration);
    the multiplier is applied per grid point. ``signed_contract_units`` folds
    the long/short direction, contract count, and contract multiplier into a
    single number so the P/L line stays a single multiplication.
    """

    strike: float
    time_to_expiration_years: float
    contract_type: ContractType
    signed_contract_units: float
    baseline_iv: float
    baseline_price: float


def stress_class_group(
    *,
    class_group: ClassGroup,
    market_inputs: MarketInputs,
    config: RegTMarginAttributionConfig,
) -> float:
    """Compute the OCC TIMS / FINRA 4210 baseline v1 per-class-group margin.

    Pure function. Returns the absolute value of the worst-case P/L magnitude
    across a 10-point equidistant shock grid applied to all instruments in
    ``class_group``. Per ``regt-margin-attribution.md § Portfolio-margin
    reference model``.

    Steps:
      1. Look up the per-symbol shock percentage from ``config.shock_parameters``.
      2. Construct the 10-point grid spanning ``[-shock_pct, +shock_pct]`` of
         the current underlying price.
      3. At each grid point, revalue every instrument in the class group:
         - Equity: ``shocked_underlying * signed_quantity``.
         - Options (positions or strategy legs): ``bs_price(...)`` with
           ``spot = shocked_underlying``, ``implied_volatility = baseline_iv *
           iv_multiplier_at_this_grid_point``, all other inputs from
           ``market_inputs``. Net P/L per leg = ``(shocked_price -
           baseline_price) * signed_contracts * contract_multiplier``.
      4. Sum the P/Ls across instruments to get total P/L at this grid point.
      5. Return ``abs(min(total_pl_at_each_grid_point))``.

    Baseline revaluation (at grid position 0.0, the unstressed midpoint)
    reuses ``bs_price`` with ``spot = current_underlying`` and
    ``iv = baseline_iv``; net P/L at that point is 0.0 by construction.

    Raises ``KeyError`` if the class group's underlying symbol is missing
    from ``market_inputs.underlying_prices``.
    Raises ``IvLookupError`` (propagated from ``market_inputs.iv_provider``)
    if no IV is available for an option leg's strike/expiration.
    """
    symbol = class_group.underlying_symbol
    spot = market_inputs.underlying_prices[symbol]
    shock_pct = _shock_pct_for_underlying(symbol, config)
    leg_baselines = _build_leg_baselines(class_group.positions, market_inputs)
    equity_signed_qty = _equity_signed_quantity_sum(class_group.positions)

    total_pl_per_grid: list[float] = []
    for grid_pos in _GRID_POSITIONS:
        shocked_spot = spot * (1.0 + grid_pos * shock_pct)
        iv_multiplier = _iv_multiplier_at_grid_point(grid_pos, config.iv_shock)

        equity_pl = (shocked_spot - spot) * equity_signed_qty

        option_pl = 0.0
        for leg in leg_baselines:
            shocked_price = bs_price(
                spot=shocked_spot,
                strike=leg.strike,
                time_to_expiration_years=leg.time_to_expiration_years,
                risk_free_rate=market_inputs.risk_free_rate,
                implied_volatility=leg.baseline_iv * iv_multiplier,
                contract_type=leg.contract_type,
            )
            option_pl += (shocked_price - leg.baseline_price) * leg.signed_contract_units

        total_pl_per_grid.append(equity_pl + option_pl)

    return abs(min(total_pl_per_grid))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _shock_pct_for_underlying(
    symbol: str,
    config: RegTMarginAttributionConfig,
) -> float:
    """Return the per-symbol shock percentage from ``config.shock_parameters``.

    Looks up ``per_symbol_overrides[symbol.upper()]`` and returns it if present;
    otherwise returns ``unmapped_default``. Asset-class taxonomy is not v1 —
    ``high_cap_equity`` and ``small_cap_equity`` are reserved for future
    revisions and not consulted here.
    """
    overrides = config.shock_parameters.per_symbol_overrides
    return overrides.get(symbol.upper(), config.shock_parameters.unmapped_default)


def _iv_multiplier_at_grid_point(
    grid_position: float,
    iv_shock_config: IvShockMultipliers,
) -> float:
    """Linearly interpolate the IV multiplier paired to a grid position.

    At ``grid_position = -1.0`` returns ``worst_down_multiplier`` (typically
    >= 1.0; IV rises on down-shocks). At ``+1.0`` returns
    ``worst_up_multiplier`` (typically <= 1.0; IV falls on up-shocks). At
    ``0.0`` returns the midpoint of the two.
    """
    weight = (grid_position + 1.0) / 2.0
    return iv_shock_config.worst_down_multiplier + weight * (
        iv_shock_config.worst_up_multiplier - iv_shock_config.worst_down_multiplier
    )


def _build_leg_baselines(
    positions: tuple[PositionRecord, ...],
    market_inputs: MarketInputs,
) -> list[_LegBaseline]:
    """Build per-leg baseline state for every option/strategy-leg in the group.

    One IV lookup and one ``bs_price`` baseline call per leg; the grid loop
    then reuses the baselines across all 10 shock points.
    """
    baselines: list[_LegBaseline] = []
    for position in positions:
        if isinstance(position.details, OptionsPositionDetails):
            position_sign = _direction_sign(position.direction)
            baselines.append(
                _baseline_for_leg(
                    underlying_ticker=position.details.underlying_ticker,
                    strike=position.details.strike_price,
                    expiration=position.details.expiration_date,
                    contract_type=position.details.contract_type,
                    contract_multiplier=position.details.contract_multiplier,
                    signed_contracts=position_sign * position.details.contract_count,
                    market_inputs=market_inputs,
                )
            )
        elif isinstance(position.details, StrategyPositionDetails):
            position_sign = _direction_sign(position.direction)
            for leg in position.details.legs:
                leg_sign = _strategy_leg_sign(leg)
                baselines.append(
                    _baseline_for_leg(
                        underlying_ticker=leg.options.underlying_ticker,
                        strike=leg.options.strike_price,
                        expiration=leg.options.expiration_date,
                        contract_type=leg.options.contract_type,
                        contract_multiplier=leg.options.contract_multiplier,
                        signed_contracts=position_sign * leg_sign * leg.options.contract_count,
                        market_inputs=market_inputs,
                    )
                )
    return baselines


def _baseline_for_leg(
    *,
    underlying_ticker: str,
    strike: float,
    expiration: date,
    contract_type: OptionContractType,
    contract_multiplier: float,
    signed_contracts: float,
    market_inputs: MarketInputs,
) -> _LegBaseline:
    # Normalise the ticker once and reuse for both lookups. ``OptionsPositionDetails``
    # / ``StrategyLeg.options`` carry no normalisation, so an option whose
    # ``underlying_ticker`` drifts in casing from its parent class group's symbol
    # would otherwise resolve in ``underlying_prices`` (upper-cased) but
    # ``IvLookupError`` against the IV provider (raw).
    normalised_ticker = underlying_ticker.upper()
    spot = market_inputs.underlying_prices[normalised_ticker]
    library_contract_type = _OPTION_CONTRACT_TYPE_MAP[contract_type]
    iv_result = market_inputs.iv_provider.lookup_iv(
        underlying=normalised_ticker,
        strike=strike,
        expiration=expiration,
        contract_type=library_contract_type,
        as_of=market_inputs.as_of,
    )
    time_to_expiration_years = max(
        (expiration - market_inputs.as_of.date()).days / 365.0,
        0.0,
    )
    baseline_price = bs_price(
        spot=spot,
        strike=strike,
        time_to_expiration_years=time_to_expiration_years,
        risk_free_rate=market_inputs.risk_free_rate,
        implied_volatility=iv_result.implied_volatility,
        contract_type=library_contract_type,
    )
    return _LegBaseline(
        strike=strike,
        time_to_expiration_years=time_to_expiration_years,
        contract_type=library_contract_type,
        signed_contract_units=signed_contracts * contract_multiplier,
        baseline_iv=iv_result.implied_volatility,
        baseline_price=baseline_price,
    )


def _direction_sign(direction: Direction) -> float:
    return 1.0 if direction == Direction.LONG else -1.0


def _strategy_leg_sign(leg: StrategyLeg) -> float:
    """Strategy-leg direction; ``None`` defaults to LONG (per StrategyLeg shape)."""
    if leg.direction == Direction.SHORT:
        return -1.0
    return 1.0


def _equity_signed_quantity_sum(
    positions: tuple[PositionRecord, ...],
) -> float:
    """Sum of signed share counts across equity positions in the group."""
    total = 0.0
    for position in positions:
        if isinstance(position.details, EquityPositionDetails):
            total += _direction_sign(position.direction) * position.details.share_count
    return total
