"""Tests for the pure trigger evaluators (story 04c / ALP-440)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alphamind._kernel.ids import (
    BracketId,
    PositionId,
    Symbol,
)
from alphamind.execution.continuous_monitor.bracket_stops.triggers import (
    evaluate_pl_target_trigger,
    evaluate_price_based_trigger,
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
                fill_price=premium_paid,
                fill_quantity=1.0,
                slippage=0.0,
                fees=0.0,
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
