"""Pure trigger evaluators for options bracket-stop firing (story 04c / ALP-440).

Two evaluators, both pure and synchronous:

* :func:`evaluate_price_based_trigger` reads the underlying-equity price from
  the cache and compares it against the leg's threshold per the leg's
  ``GTE`` / ``LTE`` direction. Per
  ``docs/design/05-execution-layer/orders-and-brackets.md`` § Options
  price-based stops, the trigger evaluates against the *underlying* — no
  derived pricing in the trigger decision.

* :func:`evaluate_pl_target_trigger` derives the option's current price via
  the guardrail-evaluation Black-Scholes core (the same primitive the
  validation / breach loop uses) and compares against the P/L threshold
  recalibrated to the actual fill price per
  ``docs/design/05-execution-layer/orders-and-brackets.md`` § P/L-based
  bracket legs. A configurable derivation-uncertainty buffer (default
  applied via 03a's freshness flag at the call site) raises the effective
  threshold to suppress false firings under noisy derived pricing.
"""

from __future__ import annotations

from datetime import datetime

from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegType,
    PLAnchorSpec,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionsPositionDetails,
    PositionRecord,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

_DAYS_PER_YEAR = 365


def evaluate_price_based_trigger(
    *,
    position: PositionRecord,
    leg: BracketLeg,
    spot: float,
) -> bool:
    """Return True when ``spot`` has crossed the leg's underlying-price stop.

    The leg's :class:`PriceTrigger` carries ``direction`` (``GTE`` or
    ``LTE``) so the long / short asymmetry is captured at the leg level:

    * Long-position stop: ``direction="LTE"`` — fires when ``spot <= threshold``.
    * Short-position stop: ``direction="GTE"`` — fires when ``spot >= threshold``.

    ``position`` is accepted for caller symmetry with
    :func:`evaluate_pl_target_trigger`; the body does not currently read
    its fields. The direction on the leg's trigger is the authoritative
    side-selector (the OPEN command builder sets it consistent with the
    position's direction).
    """
    del position  # accepted for caller symmetry; direction is on the leg's trigger
    if not isinstance(leg.trigger, PriceTrigger):
        msg = (
            f"evaluate_price_based_trigger requires a PriceTrigger leg; "
            f"got trigger_type={leg.trigger.trigger_type!r}"
        )
        raise TypeError(msg)
    threshold = leg.trigger.threshold_usd
    if leg.trigger.direction == "LTE":
        return spot <= threshold
    return spot >= threshold


def evaluate_pl_target_trigger(
    *,
    position: PositionRecord,
    leg: BracketLeg,
    spot: float,
    risk_free_rate: float,
    as_of: datetime,
    buffer_pct: float = 0.0,
) -> bool:
    """Return True when the derived option price has crossed the P/L threshold.

    Computes the option's current market price via the guardrail-evaluation
    Black-Scholes closed-form (same primitive used at OPEN/ADD validation
    and by the breach loop). Compares against the absolute price threshold
    derived from the leg's :class:`PLAnchorSpec` and ``actual_entry_price``:

    * ``spec_type="target"``: take-profit. For a long position, the threshold
      is ``actual_entry_price * (1 + pct)`` — premium must rise to lock in.
      For a short position, the threshold is ``actual_entry_price * (1 - pct)``
      — premium must fall.
    * ``spec_type="stop"``: take-loss. Sign convention mirrors target.

    The ``buffer_pct`` widens the effective firing threshold by that
    multiplicative fraction in the *direction that suppresses firing*
    (raising the bar for take-profit; lowering the bar for take-loss
    from the perspective of "how bad does it have to get"). Production
    wires ``buffer_pct`` from configuration; the default of 0.0 is the
    pure-derivation behaviour suitable for tests.
    """
    if not isinstance(leg.trigger, PriceTrigger):
        msg = (
            f"evaluate_pl_target_trigger requires a PriceTrigger leg; "
            f"got trigger_type={leg.trigger.trigger_type!r}"
        )
        raise TypeError(msg)
    anchor = leg.pl_anchor
    if anchor is None:
        msg = (
            f"evaluate_pl_target_trigger requires a leg with pl_anchor set; "
            f"got leg_id={leg.leg_id!r}"
        )
        raise ValueError(msg)
    entry_price = anchor.actual_entry_price
    if entry_price is None:
        # Per orders-and-brackets.md § P/L-based: until fill anchoring runs,
        # the P/L leg cannot be evaluated against fill-anchored absolute
        # prices. Treat as not-fired.
        return False
    option_details = _options_details_for_pl(position)
    derived_price = _bs_option_price(
        spot=spot,
        strike=option_details.strike_price,
        time_to_expiration_years=_time_to_expiration_years(option_details, as_of),
        risk_free_rate=risk_free_rate,
        iv=_iv_for_pl(option_details),
        contract_type=option_details.contract_type,
    )
    return _crosses_threshold(
        derived_price=derived_price,
        anchor=anchor,
        position_direction=position.direction,
        leg_type=leg.leg_type,
        entry_price=entry_price,
        buffer_pct=buffer_pct,
    )


def evaluate_strategy_pl_target_trigger(
    *,
    position: PositionRecord,
    leg: BracketLeg,
    spot: float,
    risk_free_rate: float,
    as_of: datetime,
    buffer_pct: float = 0.0,
) -> bool:
    """Return True when a strategy's net P/L has crossed the leg's take-profit.

    A multi-leg strategy take-profit references the strategy's *net P/L*, not
    a single-sided underlying-price threshold (parent ALP-588 decision F).
    The net P/L is the strategy's current market value minus its cost basis:

    * Current market value -- for each leg, the leg's current option price is
      derived via the Black-Scholes closed form (the same primitive the
      single-option evaluator and the breach loop use) from ``spot`` and the
      leg's strike / contract type / expiration / IV; per-leg values are
      summed signed (LONG ``+``, SHORT ``-``, scaled by
      ``contract_count * contract_multiplier``).
    * Cost basis -- the record's :attr:`StrategyPositionDetails.net_premium_usd`
      (debit-positive / credit-negative). The strategy's cost basis is carried
      straight on the record, so -- unlike the single-option path -- this
      evaluator needs no :attr:`PLAnchorSpec.actual_entry_price` anchor.

    The leg's :class:`PLAnchorSpec` ``pct`` is the fraction of the strategy's
    max profit to capture; the dollar take-profit target is
    ``pct * max_profit_usd``. The leg fires when net P/L reaches that target.
    ``buffer_pct`` widens the target multiplicatively in the firing-suppression
    direction (a higher bar), matching :func:`evaluate_pl_target_trigger`.
    """
    details = position.details
    if not isinstance(details, StrategyPositionDetails):
        msg = (
            f"evaluate_strategy_pl_target_trigger requires a strategy position; "
            f"got instrument_type={details.instrument_type!r}"
        )
        raise TypeError(msg)
    anchor = leg.pl_anchor
    if anchor is None:
        msg = (
            f"evaluate_strategy_pl_target_trigger requires a leg with pl_anchor set; "
            f"got leg_id={leg.leg_id!r}"
        )
        raise ValueError(msg)
    market_value = sum(
        _strategy_leg_market_value(
            leg=strategy_leg, spot=spot, risk_free_rate=risk_free_rate, as_of=as_of
        )
        for strategy_leg in details.legs
    )
    net_pl = market_value - details.net_premium_usd
    target_pl = anchor.pct * details.max_profit_usd
    # Buffer raises the bar (harder to fire) — same suppression direction as
    # the single-option long take-profit.
    effective_target = target_pl * (1.0 + buffer_pct)
    return net_pl >= effective_target


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strategy_leg_market_value(
    *,
    leg: StrategyLeg,
    spot: float,
    risk_free_rate: float,
    as_of: datetime,
) -> float:
    """Signed current USD market value of a single strategy leg.

    The leg's current option price is the Black-Scholes closed form at
    ``spot``; the value is ``contract_count * contract_multiplier * price``
    with the sign ``-1`` for a SHORT (written) leg and ``+1`` otherwise -- the
    same signed convention as :func:`compute_strategy_market_value_usd`.
    """
    opts = leg.options
    derived_price = _bs_option_price(
        spot=spot,
        strike=opts.strike_price,
        time_to_expiration_years=_time_to_expiration_years(opts, as_of),
        risk_free_rate=risk_free_rate,
        iv=_iv_for_pl(opts),
        contract_type=opts.contract_type,
    )
    leg_value = opts.contract_count * opts.contract_multiplier * derived_price
    sign = -1.0 if leg.direction is Direction.SHORT else 1.0
    return sign * leg_value


def _options_details_for_pl(position: PositionRecord) -> OptionsPositionDetails:
    """Return the OptionsPositionDetails the single-option P/L evaluator scores against.

    For single-leg options positions, the position's own details. Strategy
    positions are evaluated by :func:`evaluate_strategy_pl_target_trigger`,
    which the watcher routes to directly -- so a strategy reaching this helper
    is a routing bug, surfaced as a clear ``TypeError``.
    """
    details = position.details
    if isinstance(details, OptionsPositionDetails):
        return details
    if isinstance(details, StrategyPositionDetails):
        msg = (
            "evaluate_pl_target_trigger does not score strategy positions; "
            "route strategy P/L firing through evaluate_strategy_pl_target_trigger"
        )
        raise TypeError(msg)
    msg = (
        f"evaluate_pl_target_trigger requires an options or strategy position; "
        f"got instrument_type={details.instrument_type!r}"
    )
    raise ValueError(msg)


def _iv_for_pl(details: OptionsPositionDetails) -> float:
    iv = details.greeks.iv_used
    if iv is None or iv <= 0.0:
        msg = (
            f"OptionsPositionDetails.greeks.iv_used must be a positive float for "
            f"P/L derivation; got {iv!r}"
        )
        raise ValueError(msg)
    return iv


def _time_to_expiration_years(details: OptionsPositionDetails, as_of: datetime) -> float:
    return (details.expiration_date - as_of.date()).days / _DAYS_PER_YEAR


def _bs_option_price(
    *,
    spot: float,
    strike: float,
    time_to_expiration_years: float,
    risk_free_rate: float,
    iv: float,
    contract_type: OptionContractType,
) -> float:
    library_ct = ContractType.CALL if contract_type is OptionContractType.CALL else ContractType.PUT
    # ``bs_price`` requires positive spot/strike/iv; the validators on
    # OptionsPositionDetails / OptionGreeks already enforce strike + iv > 0.
    if spot <= 0.0:
        return 0.0
    return bs_price(
        spot=spot,
        strike=strike,
        time_to_expiration_years=time_to_expiration_years,
        risk_free_rate=risk_free_rate,
        implied_volatility=iv,
        contract_type=library_ct,
    )


def _crosses_threshold(
    *,
    derived_price: float,
    anchor: PLAnchorSpec,
    position_direction: Direction,
    leg_type: BracketLegType,
    entry_price: float,
    buffer_pct: float,
) -> bool:
    """Compute whether ``derived_price`` has crossed the P/L threshold.

    Threshold math: ``threshold = entry_price * (1 +/- pct * sign)`` where
    sign depends on (a) position direction, (b) leg_type. The buffer widens
    the threshold in the firing-suppression direction.
    """
    is_target = anchor.spec_type == "target"
    long_position = position_direction is Direction.LONG

    if is_target:
        # Take-profit: long wants price up, short wants price down.
        if long_position:
            base_threshold = entry_price * (1.0 + anchor.pct)
            # Buffer raises the threshold (harder to fire).
            effective = base_threshold * (1.0 + buffer_pct)
            return derived_price >= effective
        base_threshold = entry_price * (1.0 - anchor.pct)
        # Short take-profit: buffer pushes threshold lower (harder to fire).
        effective = base_threshold * (1.0 - buffer_pct)
        return derived_price <= effective

    # Take-loss / stop: long wants to fire on price down, short on price up.
    if leg_type is BracketLegType.PRICE_STOP:
        if long_position:
            base_threshold = entry_price * (1.0 - anchor.pct)
            # Buffer pushes threshold lower (harder to fire on noise).
            effective = base_threshold * (1.0 - buffer_pct)
            return derived_price <= effective
        base_threshold = entry_price * (1.0 + anchor.pct)
        # Short stop: buffer raises threshold (harder to fire).
        effective = base_threshold * (1.0 + buffer_pct)
        return derived_price >= effective
    # PLAnchorSpec.spec_type=="stop" on a non-PRICE_STOP leg is rejected by
    # BracketLeg's validator, so this branch is unreachable. Defensive only.
    msg = f"pl_anchor.spec_type='stop' requires leg_type=PRICE_STOP; got {leg_type!r}"
    raise ValueError(msg)
