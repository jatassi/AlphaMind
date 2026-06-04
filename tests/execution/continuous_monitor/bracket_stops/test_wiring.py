"""Tests for ``alphamind.execution.continuous_monitor.bracket_stops.wiring``.

Covers :class:`AlpacaBracketCloseSubmitter` — specifically that a strategy
CLOSE through the continuous-monitor bracket-stop path produces close-side
``OptionLegRequest`` legs at the broker boundary (ALP-597). The open→close
inversion happens once, at the shared
:func:`strategy_legs_to_close_acks` seam.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, PositionIntent

from alphamind._kernel.ids import BracketId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.config.models.execution import (
    ExecutionConfig,
    FeeSchedule,
    GreeksRefresh,
    PaperHarness,
)
from alphamind.config.models.execution import OrderType as ExecOrderType
from alphamind.execution.continuous_monitor.bracket_stops.wiring import (
    AlpacaBracketCloseSubmitter,
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

_NOW = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)


def _execution_config() -> ExecutionConfig:
    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=15, move_trigger_pct=0.02),
        conservative_delta_buffer_pct=0.05,
        submission_retry_window_seconds=5,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.001,
            impact_coefficients={
                ExecOrderType.market: 0.001,
                ExecOrderType.limit: 0.0005,
                ExecOrderType.stop: 0.0015,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.0,
    )


class _CapturingClient:
    """Stub alpaca-py TradingClient that records the request it receives."""

    def __init__(self) -> None:
        self.captured_request: Any = None

    def submit_order(self, request: Any) -> Any:
        self.captured_request = request
        order = _FakeOrder()
        if hasattr(request, "client_order_id"):
            order.client_order_id = request.client_order_id
        return order


class _FakeOrder:
    """Duck-typed Alpaca Order surrogate sufficient for the mleg translator."""

    def __init__(self) -> None:
        self.id = "alpaca-mleg-bracket-1"
        self.client_order_id = "MON.session.1.0~the-THE-SPY-0123456789abcdef0123456789abcdef~inv-X"
        self.status = _FakeEnum("accepted")
        self.order_class = _FakeEnum("mleg")
        self.legs: Any = None


class _FakeEnum:
    def __init__(self, value: str) -> None:
        self.value = value


class _ClientFactory:
    """Minimal client factory whose ``build_trading_client`` returns *client*."""

    def __init__(self, client: _CapturingClient) -> None:
        self._client = client

    def build_trading_client(self) -> Any:
        return self._client


def _strategy_position(
    *,
    leg_one_direction: Direction | None = Direction.LONG,
    leg_two_direction: Direction | None = Direction.SHORT,
) -> PositionRecord:
    """A 2-leg vertical-spread strategy position: LONG call + SHORT call."""
    leg_one = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=500.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(
            delta=0.5, gamma=0.01, theta=-0.02, vega=0.15, as_of_timestamp=_NOW, iv_used=0.25
        ),
    )
    leg_two = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=510.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.0,
        greeks=OptionGreeks(
            delta=0.3, gamma=0.01, theta=-0.015, vega=0.12, as_of_timestamp=_NOW, iv_used=0.25
        ),
    )
    return PositionRecord(
        position_id=PositionId("pos-st"),
        thesis_id=ThesisId("THESIS-ST"),
        bracket_id=BracketId("brk-st"),
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW,
        details=StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(
                StrategyLeg(leg_id="leg-1", direction=leg_one_direction, options=leg_one),
                StrategyLeg(leg_id="leg-2", direction=leg_two_direction, options=leg_two),
            ),
            net_premium_usd=300.0,
            max_profit_usd=700.0,
            max_loss_usd=300.0,
            breakeven_levels=(503.0,),
            strategy_greeks=OptionGreeks(
                delta=0.2, gamma=0.0, theta=-0.005, vega=0.03, as_of_timestamp=_NOW, iv_used=0.25
            ),
        ),
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW,
                fill_price=price(3.0),
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


@pytest.mark.asyncio
async def test_bracket_strategy_close_submits_close_side_legs() -> None:
    """A bracket-stop strategy CLOSE submits close-side ``OptionLegRequest`` legs.

    The LONG-opened leg closes sell / sell_to_close; the SHORT-opened leg
    closes buy / buy_to_close. No leg carries a ``*_to_open`` intent.
    """
    client = _CapturingClient()
    submitter = AlpacaBracketCloseSubmitter(
        client_factory=_ClientFactory(client),
        execution_config=_execution_config(),
    )
    position = _strategy_position()
    assert isinstance(position.details, StrategyPositionDetails)

    result = await submitter.submit_strategy_close(
        position=position,
        details=position.details,
        client_order_id_base="MON.session.1.0~the-THE-SPY-0123456789abcdef0123456789abcdef~inv-X",
    )

    assert result.mode == "strategy_combined"
    request = client.captured_request
    assert request.order_class == OrderClass.MLEG
    assert [leg.position_intent for leg in request.legs] == [
        PositionIntent.SELL_TO_CLOSE,
        PositionIntent.BUY_TO_CLOSE,
    ]
    assert [leg.side for leg in request.legs] == [OrderSide.SELL, OrderSide.BUY]
    assert all(not leg.position_intent.value.endswith("_to_open") for leg in request.legs)


@pytest.mark.asyncio
async def test_bracket_strategy_close_leg_without_direction_raises() -> None:
    """A strategy CLOSE leg without ``direction`` raises ValueError via the seam."""
    client = _CapturingClient()
    submitter = AlpacaBracketCloseSubmitter(
        client_factory=_ClientFactory(client),
        execution_config=_execution_config(),
    )
    position = _strategy_position(leg_one_direction=None)
    assert isinstance(position.details, StrategyPositionDetails)

    with pytest.raises(ValueError, match="direction"):
        await submitter.submit_strategy_close(
            position=position,
            details=position.details,
            client_order_id_base="MON.session.1.0~the-THE-SPY-0123456789abcdef0123456789abcdef~inv-X",
        )


@pytest.mark.asyncio
async def test_per_leg_fallback_requires_explicit_leg_direction() -> None:
    """The per-leg fallback requires an explicit per-leg direction (ALP-608).

    A strategy leg carries its own direction; the per-leg fallback must not
    fall back to a position-level direction when a leg's ``direction`` is
    ``None`` — a strategy is neither long nor short at the position level.
    """
    client = _CapturingClient()
    submitter = AlpacaBracketCloseSubmitter(
        client_factory=_ClientFactory(client),
        execution_config=_execution_config(),
    )
    position = _strategy_position(leg_one_direction=None)
    assert isinstance(position.details, StrategyPositionDetails)

    with pytest.raises(ValueError, match="direction"):
        await submitter._per_leg_fallback(
            position=position,
            details=position.details,
            client_order_id_base="MON.session.1.0~the-THE-SPY-0123456789abcdef0123456789abcdef~inv-X",
        )
