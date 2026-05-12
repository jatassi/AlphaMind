"""Tests for ``compute_target_underlyings`` (story 02b / ALP-434).

The subscription manager folds the repository's open-position snapshot down to
the unique set of underlying tickers the price stream should subscribe to:

* every equity position contributes ``details.ticker``;
* every options position contributes ``details.underlying_ticker``;
* every strategy position contributes each leg's
  ``options.underlying_ticker``.

Duplicate tickers collapse to a single entry. Closed positions are filtered by
the repository's ``get_open_positions`` call — the function reads through that
seam rather than re-implementing position-status filtering.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from alphamind.execution.continuous_monitor.underlying_stream import (
    compute_target_underlyings,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
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
# In-memory PositionRecord helpers; cover the three instrument types.
# ---------------------------------------------------------------------------


def _fill(ts: datetime, price: float = 100.0) -> PositionFill:
    return PositionFill(
        fill_timestamp=ts,
        fill_price=price,
        fill_quantity=10.0,
        slippage=0.0,
        fees=0.0,
    )


def _equity(
    *,
    position_id: str,
    ticker: str,
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRecord:
    ts = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    history: tuple[PositionFill, ...] = (_fill(ts),) if status == PositionStatus.OPEN else ()
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=Direction.LONG,
        entry_timestamp=ts if status == PositionStatus.OPEN else None,
        details=EquityPositionDetails(
            ticker=ticker,
            share_count=10.0,
            average_cost_basis_per_share=100.0,
        ),
        execution_history=history,
        realized_pnl_to_date_usd=0.0 if status == PositionStatus.CLOSED else None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _greeks() -> OptionGreeks:
    return OptionGreeks(delta=0.5, gamma=0.01, theta=-0.05, vega=0.2)


def _options_details(underlying: str) -> OptionsPositionDetails:
    return OptionsPositionDetails(
        underlying_ticker=underlying,
        strike_price=100.0,
        expiration_date=date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=2.0,
        greeks=_greeks(),
    )


def _options(*, position_id: str, underlying: str) -> PositionRecord:
    ts = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=ts,
        details=_options_details(underlying),
        execution_history=(_fill(ts),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy(*, position_id: str, leg_underlyings: tuple[str, ...]) -> PositionRecord:
    ts = datetime(2026, 5, 11, 14, 30, 0, tzinfo=UTC)
    legs = tuple(
        StrategyLeg(leg_id=f"leg-{i}", direction=Direction.LONG, options=_options_details(u))
        for i, u in enumerate(leg_underlyings)
    )
    return PositionRecord(
        position_id=position_id,
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=ts,
        details=StrategyPositionDetails(
            strategy_type_label="iron_condor",
            legs=legs,
            net_premium_usd=100.0,
            max_profit_usd=200.0,
            max_loss_usd=-300.0,
            breakeven_levels=(95.0, 105.0),
            strategy_greeks=_greeks(),
        ),
        execution_history=(_fill(ts),),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


class _FakeReader:
    """Minimal in-memory OpenPositionsReader for unit tests."""

    def __init__(self, positions: tuple[PositionRecord, ...]) -> None:
        self._positions = positions

    async def get_open_positions(self) -> tuple[PositionRecord, ...]:
        return self._positions


# ---------------------------------------------------------------------------
# compute_target_underlyings
# ---------------------------------------------------------------------------


class TestComputeTargetUnderlyings:
    async def test_empty_when_no_open_positions(self) -> None:
        reader = _FakeReader(())
        assert await compute_target_underlyings(reader) == frozenset()

    async def test_equity_position_contributes_ticker(self) -> None:
        reader = _FakeReader((_equity(position_id="p1", ticker="SPY"),))
        assert await compute_target_underlyings(reader) == frozenset({"SPY"})

    async def test_options_position_contributes_underlying_ticker(self) -> None:
        reader = _FakeReader((_options(position_id="p1", underlying="AAPL"),))
        assert await compute_target_underlyings(reader) == frozenset({"AAPL"})

    async def test_strategy_position_contributes_every_leg_underlying(self) -> None:
        strategy = _strategy(position_id="p1", leg_underlyings=("AAPL", "AAPL", "MSFT"))
        reader = _FakeReader((strategy,))
        assert await compute_target_underlyings(reader) == frozenset({"AAPL", "MSFT"})

    async def test_multiple_positions_on_same_underlying_collapse_to_single_entry(self) -> None:
        reader = _FakeReader(
            (
                _equity(position_id="p1", ticker="SPY"),
                _equity(position_id="p2", ticker="SPY"),
                _options(position_id="p3", underlying="SPY"),
            )
        )
        assert await compute_target_underlyings(reader) == frozenset({"SPY"})

    async def test_union_across_instrument_types(self) -> None:
        reader = _FakeReader(
            (
                _equity(position_id="p1", ticker="SPY"),
                _options(position_id="p2", underlying="AAPL"),
                _strategy(position_id="p3", leg_underlyings=("MSFT", "NVDA")),
            )
        )
        targets = await compute_target_underlyings(reader)
        assert targets == frozenset({"SPY", "AAPL", "MSFT", "NVDA"})


class TestOpenPositionsReaderProtocol:
    """The function only consumes ``get_open_positions`` — any reader satisfies it."""

    async def test_protocol_check(self) -> None:
        reader = _FakeReader(())
        assert isinstance(reader, OpenPositionsReader)
