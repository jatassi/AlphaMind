"""Tests for ``LastRefreshState`` and seeding from existing ``OptionGreeks`` (story 03a)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.continuous_monitor.greeks_refresh import LastRefreshState
from alphamind.execution.continuous_monitor.greeks_refresh.state import (
    seed_last_refresh_states,
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


def _options_position(
    *,
    position_id: str,
    as_of_timestamp: datetime | None,
    underlying_ticker: str = "AAPL",
) -> PositionRecord:
    greeks = OptionGreeks(
        delta=0.4,
        gamma=0.02,
        theta=-0.01,
        vega=0.1,
        as_of_timestamp=as_of_timestamp,
        iv_used=0.25 if as_of_timestamp is not None else None,
        refresh_failed=False,
    )
    fill = PositionFill(
        fill_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        fill_price=price(2.5),
        fill_quantity=1.0,
        slippage=signed_money(0.0),
        fees=money(0.0),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        details=OptionsPositionDetails(
            underlying_ticker=Symbol(underlying_ticker),
            strike_price=200.0,
            expiration_date=datetime(2026, 6, 19, tzinfo=UTC).date(),
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=2.5,
            greeks=greeks,
        ),
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _strategy_position(*, position_id: str, as_of_timestamp: datetime | None) -> PositionRecord:
    leg_details = OptionsPositionDetails(
        underlying_ticker=Symbol("SPY"),
        strike_price=500.0,
        expiration_date=datetime(2026, 6, 19, tzinfo=UTC).date(),
        contract_type=OptionContractType.CALL,
        contract_count=1.0,
        contract_multiplier=100.0,
        premium_paid_per_contract=5.0,
        greeks=OptionGreeks(
            delta=0.5,
            gamma=0.01,
            theta=-0.02,
            vega=0.15,
            as_of_timestamp=as_of_timestamp,
            iv_used=0.20 if as_of_timestamp is not None else None,
        ),
    )
    strategy_greeks = OptionGreeks(
        delta=0.25,
        gamma=0.005,
        theta=-0.01,
        vega=0.08,
        as_of_timestamp=as_of_timestamp,
        iv_used=0.20 if as_of_timestamp is not None else None,
    )
    fill = PositionFill(
        fill_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        fill_price=price(5.0),
        fill_quantity=1.0,
        slippage=signed_money(0.0),
        fees=money(0.0),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
        details=StrategyPositionDetails(
            strategy_type_label="vertical_call_spread",
            legs=(StrategyLeg(leg_id="leg-1", direction=Direction.LONG, options=leg_details),),
            net_premium_usd=500.0,
            max_profit_usd=1000.0,
            max_loss_usd=500.0,
            breakeven_levels=(505.0,),
            strategy_greeks=strategy_greeks,
        ),
        execution_history=(fill,),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


class TestLastRefreshStateShape:
    def test_is_frozen(self) -> None:
        state = LastRefreshState(
            position_id=PositionId("pos-1"),
            last_refreshed_at=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            underlying_price_at_last_refresh=200.0,
        )
        with pytest.raises(FrozenInstanceError):
            state.underlying_price_at_last_refresh = 201.0  # type: ignore[misc]

    def test_carries_position_id_and_anchor_fields(self) -> None:
        ts = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        state = LastRefreshState(
            position_id=PositionId("pos-42"),
            last_refreshed_at=ts,
            underlying_price_at_last_refresh=199.75,
        )
        assert state.position_id == "pos-42"
        assert state.last_refreshed_at == ts
        assert state.underlying_price_at_last_refresh == 199.75


class TestSeedLastRefreshStates:
    """The task seeds in-memory state from existing ``OptionGreeks.as_of_timestamp``
    on startup. Positions with ``as_of_timestamp is None`` are treated as "due now"
    — by returning a sentinel anchor at the epoch so the scheduled-trigger delta
    is unbounded; the move-trigger delta is anchored at the current underlying
    price so it does not fire spuriously off a None price.
    """

    def test_seed_from_existing_as_of_timestamp(self) -> None:
        ts = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        position = _options_position(position_id=PositionId("pos-1"), as_of_timestamp=ts)
        states = seed_last_refresh_states(
            (position,),
            underlying_prices={"AAPL": 198.0},
            now=datetime(2026, 5, 11, 14, 35, tzinfo=UTC),
        )
        state = states["pos-1"]
        assert state.position_id == "pos-1"
        assert state.last_refreshed_at == ts
        assert state.underlying_price_at_last_refresh == 198.0

    def test_position_with_no_as_of_is_due_now(self) -> None:
        """Positions never refreshed are seeded with the epoch so the scheduled
        delta exceeds any positive interval — the task picks them up on the
        first inspection tick.
        """
        position = _options_position(position_id=PositionId("pos-2"), as_of_timestamp=None)
        now = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        states = seed_last_refresh_states(
            (position,),
            underlying_prices={"AAPL": 198.0},
            now=now,
        )
        state = states["pos-2"]
        assert state.position_id == "pos-2"
        # Anchored at the epoch — guaranteed older than any greeks_refresh_interval
        # the config validator permits.
        assert state.last_refreshed_at < now
        # Move-trigger anchor uses the current spot so a None as_of doesn't
        # trip a spurious move trigger.
        assert state.underlying_price_at_last_refresh == 198.0

    def test_seed_skips_unknown_underlying(self) -> None:
        """When the cache hasn't seen a quote yet, anchor price defaults to 0.0
        which is interpreted as "no anchor" by the move-trigger check (the task
        falls back to the scheduled path until a quote arrives).
        """
        position = _options_position(
            position_id=PositionId("pos-3"),
            as_of_timestamp=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
            underlying_ticker=Symbol("UNKNOWN"),
        )
        states = seed_last_refresh_states(
            (position,),
            underlying_prices={},
            now=datetime(2026, 5, 11, 14, 30, tzinfo=UTC),
        )
        assert states["pos-3"].underlying_price_at_last_refresh == 0.0

    def test_seed_handles_strategy_position(self) -> None:
        ts = datetime(2026, 5, 11, 14, 30, tzinfo=UTC)
        position = _strategy_position(position_id=PositionId("strat-1"), as_of_timestamp=ts)
        states = seed_last_refresh_states(
            (position,),
            underlying_prices={"SPY": 500.0},
            now=datetime(2026, 5, 11, 14, 35, tzinfo=UTC),
        )
        state = states["strat-1"]
        assert state.position_id == "strat-1"
        assert state.last_refreshed_at == ts
        assert state.underlying_price_at_last_refresh == 500.0
