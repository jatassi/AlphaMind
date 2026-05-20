"""Tests for the pure trigger evaluators (story 04c / ALP-440)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import (
    BracketId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.continuous_monitor.bracket_stops.triggers import (
    evaluate_pl_target_trigger,
    evaluate_price_based_trigger,
    evaluate_strategy_pl_target_trigger,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    PLAnchorSpec,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _options_position(
    *,
    direction: Direction = Direction.LONG,
    strike: float = 850.0,
    expiration: date = date(2026, 6, 19),
    contract_type: OptionContractType = OptionContractType.CALL,
    premium_paid: float = 12.0,
    iv_used: float = 0.30,
) -> PositionRecord:
    greeks = OptionGreeks(
        delta=0.5,
        gamma=0.02,
        theta=-0.04,
        vega=0.20,
        as_of_timestamp=_NOW,
        iv_used=iv_used,
        refresh_failed=False,
    )
    return PositionRecord(
        position_id=PositionId("pos-1"),
        thesis_id=None,
        bracket_id=BracketId("brk-1"),
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW,
        details=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=strike,
            expiration_date=expiration,
            contract_type=contract_type,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=premium_paid,
            greeks=greeks,
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(premium_paid),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _price_stop_leg(
    *,
    underlying_ticker: str = "NVDA",
    threshold_usd: float = 865.0,
    direction: str = "LTE",
) -> BracketLeg:
    return BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol(underlying_ticker),
            threshold_usd=threshold_usd,
            direction=direction,  # type: ignore[arg-type]
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )


def _pl_target_leg(
    *,
    target_pct: float = 0.80,
    actual_entry_price: float = 12.0,
) -> BracketLeg:
    return BracketLeg(
        leg_id="leg-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=actual_entry_price * (1.0 + target_pct),
            direction="GTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        pl_anchor=PLAnchorSpec(
            spec_type="target",
            pct=target_pct,
            planned_entry_price=actual_entry_price,
            actual_entry_price=actual_entry_price,
            recalculated_at_fill=True,
        ),
    )


def _pl_stop_leg(
    *,
    stop_pct: float = 0.30,
    actual_entry_price: float = 12.0,
) -> BracketLeg:
    return BracketLeg(
        leg_id="leg-pl-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=actual_entry_price * (1.0 - stop_pct),
            direction="LTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        pl_anchor=PLAnchorSpec(
            spec_type="stop",
            pct=stop_pct,
            planned_entry_price=actual_entry_price,
            actual_entry_price=actual_entry_price,
            recalculated_at_fill=True,
        ),
    )


# ---------------------------------------------------------------------------
# evaluate_price_based_trigger — long position
# ---------------------------------------------------------------------------


class TestPriceBasedTriggerLong:
    def test_spot_below_stop_fires_long(self) -> None:
        position = _options_position(direction=Direction.LONG)
        leg = _price_stop_leg(threshold_usd=865.0, direction="LTE")
        assert evaluate_price_based_trigger(position=position, leg=leg, spot=864.0) is True

    def test_spot_at_stop_fires_long(self) -> None:
        position = _options_position(direction=Direction.LONG)
        leg = _price_stop_leg(threshold_usd=865.0, direction="LTE")
        assert evaluate_price_based_trigger(position=position, leg=leg, spot=865.0) is True

    def test_spot_above_stop_does_not_fire_long(self) -> None:
        position = _options_position(direction=Direction.LONG)
        leg = _price_stop_leg(threshold_usd=865.0, direction="LTE")
        assert evaluate_price_based_trigger(position=position, leg=leg, spot=865.01) is False


# ---------------------------------------------------------------------------
# evaluate_price_based_trigger — short position
# ---------------------------------------------------------------------------


class TestPriceBasedTriggerShort:
    def test_spot_above_stop_fires_short(self) -> None:
        position = _options_position(direction=Direction.SHORT)
        leg = _price_stop_leg(threshold_usd=865.0, direction="GTE")
        assert evaluate_price_based_trigger(position=position, leg=leg, spot=866.0) is True

    def test_spot_at_stop_fires_short(self) -> None:
        position = _options_position(direction=Direction.SHORT)
        leg = _price_stop_leg(threshold_usd=865.0, direction="GTE")
        assert evaluate_price_based_trigger(position=position, leg=leg, spot=865.0) is True

    def test_spot_below_stop_does_not_fire_short(self) -> None:
        position = _options_position(direction=Direction.SHORT)
        leg = _price_stop_leg(threshold_usd=865.0, direction="GTE")
        assert evaluate_price_based_trigger(position=position, leg=leg, spot=864.99) is False


# ---------------------------------------------------------------------------
# evaluate_pl_target_trigger
# ---------------------------------------------------------------------------


class TestPlTargetTrigger:
    # Entry premium chosen to match ATM BS-derived value at strike=850, IV=0.30,
    # 39 DTE, r=0.045 → ~$35.24. With this anchor:
    #   target (80% profit) → $63.43; fires at spot ≥ ~$895
    #   stop   (30% loss)  → $24.67; fires at spot ≤ ~$823

    def test_target_fires_when_derived_price_at_target(self) -> None:
        """Long call, entry=$35.24 (ATM-consistent), target 80% profit = $63.43.
        Spot=900 with IV=0.30 derives ~$67.77 > $63.43 → fire."""
        position = _options_position(
            direction=Direction.LONG,
            strike=850.0,
            premium_paid=35.24,
            iv_used=0.30,
        )
        leg = _pl_target_leg(target_pct=0.80, actual_entry_price=35.24)
        fired = evaluate_pl_target_trigger(
            position=position,
            leg=leg,
            spot=900.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        assert fired is True

    def test_target_does_not_fire_when_derived_price_below_target(self) -> None:
        position = _options_position(
            direction=Direction.LONG,
            strike=850.0,
            premium_paid=35.24,
            iv_used=0.30,
        )
        leg = _pl_target_leg(target_pct=0.80, actual_entry_price=35.24)
        fired = evaluate_pl_target_trigger(
            position=position,
            leg=leg,
            spot=850.0,  # ATM → derived ~$35.24, well below $63.43 target
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        assert fired is False

    def test_stop_fires_when_derived_price_at_stop(self) -> None:
        """P/L stop fires when derived price <= stop threshold.

        Long call entry $35.24, 30% stop = $24.67. Spot=820 with IV=0.30
        derives ~$21.24 < $24.67 → fire."""
        position = _options_position(
            direction=Direction.LONG,
            strike=850.0,
            premium_paid=35.24,
            iv_used=0.30,
        )
        leg = _pl_stop_leg(stop_pct=0.30, actual_entry_price=35.24)
        fired = evaluate_pl_target_trigger(
            position=position,
            leg=leg,
            spot=820.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        assert fired is True

    def test_buffer_widens_threshold_for_target(self) -> None:
        """The derivation-uncertainty buffer raises the effective fire threshold
        on a take-profit target — false-firing protection. A spot that fires
        with no buffer must not fire when the buffer is applied generously."""
        position = _options_position(
            direction=Direction.LONG,
            strike=850.0,
            premium_paid=35.24,
            iv_used=0.30,
        )
        leg = _pl_target_leg(target_pct=0.80, actual_entry_price=35.24)
        # Spot=895 → derived ≈ \$65 — just above the \$63.43 no-buffer threshold.
        fired_no_buffer = evaluate_pl_target_trigger(
            position=position,
            leg=leg,
            spot=895.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        fired_with_buffer = evaluate_pl_target_trigger(
            position=position,
            leg=leg,
            spot=895.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.50,  # 50% buffer → threshold raises to ~\$95
        )
        # The buffer must suppress the marginal fire.
        assert fired_no_buffer is True
        assert fired_with_buffer is False

    def test_short_target_fires_when_derived_price_dropped(self) -> None:
        """Short take-profit: premium drop is profit. Entry $35.24, 80% target
        → absolute target $7.05. Spot=780 with IV=0.30 derives ~$9.12 — still
        above target — so push lower: spot=750."""
        position = _options_position(
            direction=Direction.SHORT,
            strike=850.0,
            premium_paid=35.24,
            iv_used=0.30,
        )
        leg = BracketLeg(
            leg_id="leg-short-target",
            leg_type=BracketLegType.TAKE_PROFIT,
            order_id=None,
            trigger=PriceTrigger(
                underlying_ticker=Symbol("NVDA"),
                threshold_usd=7.05,
                direction="LTE",
            ),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
            pl_anchor=PLAnchorSpec(
                spec_type="target",
                pct=0.80,
                planned_entry_price=35.24,
                actual_entry_price=35.24,
                recalculated_at_fill=True,
            ),
        )
        # Spot far OTM → call premium decays to a few dollars
        fired = evaluate_pl_target_trigger(
            position=position,
            leg=leg,
            spot=750.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        assert fired is True


# ---------------------------------------------------------------------------
# evaluate_strategy_pl_target_trigger — net-credit strategy take-profit
# ---------------------------------------------------------------------------


def _strategy_leg(
    *,
    leg_id: str,
    strike: float,
    direction: Direction,
    contract_type: OptionContractType = OptionContractType.PUT,
    contract_count: float = 1.0,
    iv_used: float = 0.30,
) -> StrategyLeg:
    return StrategyLeg(
        leg_id=leg_id,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=strike,
            expiration_date=date(2026, 6, 19),
            contract_type=contract_type,
            contract_count=contract_count,
            contract_multiplier=100.0,
            premium_paid_per_contract=0.0,
            greeks=OptionGreeks(
                delta=-0.3,
                gamma=0.02,
                theta=-0.04,
                vega=0.2,
                as_of_timestamp=_NOW,
                iv_used=iv_used,
            ),
        ),
        direction=direction,
    )


def _credit_put_spread(*, net_premium_usd: float) -> PositionRecord:
    """A bull put credit spread on NVDA: short the 850 put, long the 840 put.

    ``net_premium_usd`` is the strategy cost basis (debit-positive /
    credit-negative); a credit spread carries it negative.
    """
    return PositionRecord(
        position_id=PositionId("pos-strat-1"),
        thesis_id=None,
        bracket_id=BracketId("brk-strat-1"),
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_spread",
            legs=(
                _strategy_leg(leg_id="leg-short", strike=850.0, direction=Direction.SHORT),
                _strategy_leg(leg_id="leg-long", strike=840.0, direction=Direction.LONG),
            ),
            net_premium_usd=net_premium_usd,
            max_profit_usd=abs(net_premium_usd),
            max_loss_usd=-(1000.0 - abs(net_premium_usd)),
            breakeven_levels=(),
            strategy_greeks=OptionGreeks(delta=0.1, gamma=0.0, theta=0.01, vega=-0.05),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(1.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_pl_target_leg(*, target_pct: float = 0.50) -> BracketLeg:
    """A strategy TAKE_PROFIT leg carrying a P/L-percentage target.

    The strategy evaluator reads the strategy's ``net_premium_usd`` straight
    off the record as the cost basis, so the anchor needs no
    ``actual_entry_price`` — ``planned_entry_price`` is a positive sentinel
    only (the credit magnitude).
    """
    return BracketLeg(
        leg_id="leg-strat-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"),
            threshold_usd=850.0,
            direction="GTE",
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        pl_anchor=PLAnchorSpec(
            spec_type="target",
            pct=target_pct,
            planned_entry_price=3.0,
        ),
    )


def _credit_spread_net_premium_at(entry_spot: float) -> float:
    """Net premium (credit-negative) for the bull put spread at *entry_spot*.

    Mirrors the strategy evaluator's per-leg pricing: short 850 put minus
    long 840 put, scaled by 100, signed (SHORT subtracts). A bull put credit
    spread has the short leg richer, so the signed sum is negative — the
    credit received, expressed credit-negative.
    """
    from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
    from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

    ttm = (date(2026, 6, 19) - _NOW.date()).days / 365
    short_put = bs_price(
        spot=entry_spot,
        strike=850.0,
        time_to_expiration_years=ttm,
        risk_free_rate=0.045,
        implied_volatility=0.30,
        contract_type=ContractType.PUT,
    )
    long_put = bs_price(
        spot=entry_spot,
        strike=840.0,
        time_to_expiration_years=ttm,
        risk_free_rate=0.045,
        implied_volatility=0.30,
        contract_type=ContractType.PUT,
    )
    return (-short_put + long_put) * 100.0


class TestStrategyPlTargetTrigger:
    """Strategy P/L-target firing — net P/L from per-leg BS prices and the
    record's ``net_premium_usd`` (parent ALP-588 decision F; ALP-601)."""

    def test_credit_spread_fires_when_net_pl_reaches_target(self) -> None:
        """A bull put credit spread, 50%-of-credit take-profit. As NVDA rallies
        the spread decays toward worthless — the strategy's net P/L climbs from
        ~0 at entry toward the full credit and crosses the target."""
        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_put_spread(net_premium_usd=net_premium)
        leg = _strategy_pl_target_leg(target_pct=0.50)
        # Spot well above both strikes → both puts near-worthless → the
        # strategy has captured ~the full credit; net P/L ≥ 50%-of-credit.
        fired = evaluate_strategy_pl_target_trigger(
            position=position,
            leg=leg,
            spot=1000.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        assert fired is True

    def test_credit_spread_does_not_fire_below_target(self) -> None:
        """At the entry spot the strategy's net P/L is ~0 — well below the
        take-profit target — so the leg does not fire."""
        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_put_spread(net_premium_usd=net_premium)
        leg = _strategy_pl_target_leg(target_pct=0.50)
        fired = evaluate_strategy_pl_target_trigger(
            position=position,
            leg=leg,
            spot=850.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        assert fired is False

    def test_single_option_pl_helper_no_longer_raises_not_implemented(self) -> None:
        """The single-option ``_options_details_for_pl`` no longer raises
        ``NotImplementedError`` for a strategy position — a strategy reaching
        it is a routing bug, surfaced as a plain ``TypeError`` (ALP-601)."""
        from alphamind.execution.continuous_monitor.bracket_stops.triggers import (
            _options_details_for_pl,
        )

        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_put_spread(net_premium_usd=net_premium)
        with pytest.raises(TypeError, match="evaluate_strategy_pl_target_trigger"):
            _options_details_for_pl(position)
        # The replaced behaviour was a NotImplementedError — assert it is gone.
        try:
            _options_details_for_pl(position)
        except NotImplementedError:  # pragma: no cover - regression guard
            raise AssertionError("strategy must no longer raise NotImplementedError") from None
        except TypeError:
            pass

    def test_buffer_suppresses_marginal_fire(self) -> None:
        """The derivation-uncertainty buffer raises the effective target — a
        net P/L just above the no-buffer target must not fire with a generous
        buffer applied."""
        net_premium = _credit_spread_net_premium_at(entry_spot=850.0)
        position = _credit_put_spread(net_premium_usd=net_premium)
        leg = _strategy_pl_target_leg(target_pct=0.50)
        # Spot=920 decays the spread enough to clear the 50% target with no
        # buffer, but not the doubled (100%-buffer) target.
        fired_no_buffer = evaluate_strategy_pl_target_trigger(
            position=position,
            leg=leg,
            spot=920.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=0.0,
        )
        fired_with_buffer = evaluate_strategy_pl_target_trigger(
            position=position,
            leg=leg,
            spot=920.0,
            risk_free_rate=0.045,
            as_of=_NOW,
            buffer_pct=1.0,  # doubles the target → suppresses the marginal fire
        )
        assert fired_no_buffer is True
        assert fired_with_buffer is False


class TestPriceBasedTriggerValidation:
    def test_non_price_trigger_raises(self) -> None:
        """A non-PriceTrigger leg (Time/Event) is a programming error."""
        from alphamind.portfolio_state.records.orders import TimeTrigger

        position = _options_position()
        time_leg = BracketLeg(
            leg_id="leg-time",
            leg_type=BracketLegType.TIME_EXPIRATION,
            order_id=None,
            trigger=TimeTrigger(deadline=_NOW),
            enforcement=BracketLegEnforcement.MECHANICAL,
            status=BracketLegStatus.ACTIVE,
        )
        with pytest.raises(TypeError):
            evaluate_price_based_trigger(position=position, leg=time_leg, spot=850.0)
