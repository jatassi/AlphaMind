"""Shared low-level record and view builders for portfolio_state/consumers/ tests.

Hoisted from the five consumer test files to centralize record-shape definitions
(ALP-788). All builders here produce data identical to the prior per-file copies.

These are pure data builders (no test logic). Import explicitly in the test modules
that need them (mirrors the pattern in tests/portfolio_state/_fixtures.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EquityInstrumentSpec,
    OrderDirection,
    OrderDuration,
    OrderRecord,
    OrderRole,
    OrderStatus,
    OrderType,
    PriceParameters,
    PriceTrigger,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Shared timestamps (values match the per-file _T0 across all consumer tests)
# ---------------------------------------------------------------------------

_T0 = datetime(2025, 1, 1, 10, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Low-level record builders (hoisted; identical semantics to prior copies)
# ---------------------------------------------------------------------------


def _make_fill(at_price: float = 150.0) -> PositionFill:
    return PositionFill(
        fill_timestamp=_T0,
        fill_price=price(at_price),
        fill_quantity=100.0,
        slippage=signed_money(0.01),
        fees=money(1.0),
    )


def _make_open_position(pos_id: str = "POS-001", ticker: str = "AAPL") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=100.0,
        average_cost_basis_per_share=150.0,
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_T0,
        details=equity,
        execution_history=(_make_fill(),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(15500.0),
        unrealized_pnl_usd=signed_money(500.0),
        unrealized_pnl_pct=3.33,
        position_weight_pct=10.0,
        position_age_hours=4.0,
        notional_exposure_usd=money(15000.0),
        delta_adjusted_exposure_usd=signed_money(15000.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_pending_position(pos_id: str = "POS-PEND") -> PositionView:
    equity = EquityPositionDetails(
        ticker=Symbol("GOOG"),
        share_count=10.0,
        average_cost_basis_per_share=2800.0,
    )
    record = PositionRecord(
        position_id=PositionId(pos_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=equity,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(0.0),
        unrealized_pnl_usd=signed_money(0.0),
        unrealized_pnl_pct=0.0,
        position_weight_pct=0.0,
        position_age_hours=0.0,
        notional_exposure_usd=money(0.0),
        delta_adjusted_exposure_usd=signed_money(0.0),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


def _make_bracket(bracket_id: str = "BRK-001", position_id: str = "POS-001") -> BracketRecord:
    leg = BracketLeg(
        leg_id="leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId("ord-stop-1"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
    )


def _make_pending_order(
    order_id: str = "ORD-001",
    position_id: str = "POS-001",
) -> OrderRecord:
    spec = EquityInstrumentSpec(ticker=Symbol("AAPL"))
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId("BRK-001"),
        role=OrderRole.ENTRY,
        instrument_spec=spec,
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        price_parameters=PriceParameters(limit_price=None, stop_trigger_price=None),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-001"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-001"),),
        submission_timestamp=_T0,
        last_update_timestamp=_T0,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=1.0,
    )


def _make_cash_ledger() -> CashLedger:
    return CashLedger(
        current_cash_usd=50000.0,
        settled_cash_usd=48000.0,
        reserved_capital_usd=2000.0,
        available_buying_power_usd=46000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=50.0,
        true_deployable_capital_usd=44000.0,
        regt_excess_trailing_30d_usd=1000.0,
        regt_excess_trailing_90d_usd=3000.0,
        regt_excess_lifetime_usd=10000.0,
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=2.0,
        equity_high_water_mark_usd=110000.0,
        drawdown_duration_hours=8.0,
        lifetime_max_drawdown_pct=5.0,
        intraday_drawdown_pct=0.5,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_risk_budget() -> RiskBudgetConsumption:
    return RiskBudgetConsumption(entries=())


def _make_active_risk_params() -> ActiveRiskParameterSet:
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(),
        active_overlays=(),
    )
