"""Integration tests for the option-lifecycle handlers against a real DB.

These exercise the shell: load the open option position by OCC symbol, append
the lifecycle activity to ``broker_event_log``, book the realized PnL into
``thesis_pnl_ledger``, and persist the position transitions — all in the open
``InvocationHandle`` transaction. The DB is real (a sanctioned boundary); no
internal collaborator is mocked.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.money import price, signed_money
from alphamind.execution.account_activities.dispatch import integrate_lifecycle_event
from alphamind.execution.account_activities.records import (
    LifecycleActivityType,
    LifecycleEvent,
    TradeLeg,
)
from alphamind.portfolio_state.records.positions import EquityPositionDetails
from alphamind.state.tables.broker_event_log import BrokerEventLogRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import row_to_record
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from tests.execution.corporate_actions._handler_substrate import (
    make_active_bracket,
    make_active_thesis,
    make_open_options_position,
    make_pending_entry_order,
    open_handle,
    seed_invocation_substrate,
    seed_position_cluster,
)

# The corporate-actions substrate seeds AAPL CALL, strike 150, exp 2026-09-18.
_TXN = dt.datetime(2026, 9, 18, 20, 0, 0, tzinfo=dt.UTC)
_OCC = "AAPL260918C00150000"


async def _seed_open_option(
    factory: async_sessionmaker[AsyncSession],
    *,
    contract_count: float = 5.0,
    premium_paid_per_contract: float = 250.0,
) -> None:
    await seed_invocation_substrate(factory)
    position = make_open_options_position(
        position_id="pos-1",
        thesis_id="thesis-1",
        bracket_id="brk-1",
        contract_count=contract_count,
        premium_paid_per_contract=premium_paid_per_contract,
    )
    await seed_position_cluster(
        factory,
        position=position,
        order=make_pending_entry_order(),
        thesis=make_active_thesis(thesis_id="thesis-1", position_id="pos-1"),
        bracket=make_active_bracket(bracket_id="brk-1", position_id="pos-1"),
    )


def _expiry_event() -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-exp-1",
        activity_type=LifecycleActivityType.OPEXP,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )


async def test_expiry_closes_option_and_books_negative_premium(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An OTM expiry closes the option (no OPEN/0 husk) and books -premium."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(handle, _expiry_event())
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos = await sess.get(PositionRow, "pos-1")
        assert pos is not None
        record = row_to_record(pos)
        assert record.status.value == "CLOSED"
        assert record.realized_pnl_to_date_usd == pytest.approx(-1250.0)

        ledger = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert ledger is not None
        assert ledger.realized_pnl_usd == Decimal(-1250)

        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1
        assert events[0].event_type == "OPEXP"
        assert events[0].position_id == "pos-1"


async def test_re_polling_same_activity_does_not_double_book(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Idempotent on event_key: re-integrating the same activity is a no-op."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(handle, _expiry_event())
        # Re-poll: same activity_id arrives again in the same transaction.
        await integrate_lifecycle_event(handle, _expiry_event())
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        assert len(events) == 1  # one row, not two
        ledger = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert ledger is not None
        # PnL booked once, not -2500.
        assert ledger.realized_pnl_usd == Decimal(-1250)


def _assignment_event(activity_type: LifecycleActivityType) -> LifecycleEvent:
    return LifecycleEvent(
        activity_id="act-asn-1",
        activity_type=activity_type,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=TradeLeg(
            activity_id="act-trd-1",
            equity_symbol="AAPL",
            qty=500.0,
            strike_price=price(150.0),
            side="buy",
            net_amount=signed_money(-75_000.0),
        ),
    )


async def test_assignment_opens_equity_at_strike_with_thesis_link(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An assignment opens the equity at the strike with the option's thesis link."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(handle, _assignment_event(LifecycleActivityType.OPASN))
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Option closed (no husk); equity opened at the strike with the thesis link.
        option = await sess.get(PositionRow, "pos-1")
        assert option is not None
        assert option.status == "CLOSED"

        stmt = select(PositionRow).where(PositionRow.status == "OPEN")
        equities = (await sess.execute(stmt)).scalars().all()
        assert len(equities) == 1
        equity = row_to_record(equities[0])
        assert equity.thesis_id == "thesis-1"
        assert equity.parent_position_id == "pos-1"
        details = equity.details
        assert isinstance(details, EquityPositionDetails)
        assert details.ticker == "AAPL"
        assert details.share_count == 500.0
        assert details.average_cost_basis_per_share == pytest.approx(150.0)

        # Both the OPASN and its paired OPTRD landed in the event log, and BOTH
        # carry the resolved thesis/position attribution (the OPTRD row must not
        # be left NULL — 03c's per-thesis PnL join reads it).
        events = (await sess.execute(select(BrokerEventLogRow))).scalars().all()
        by_type = {e.event_type: e for e in events}
        assert set(by_type) == {"OPASN", "OPTRD"}
        assert by_type["OPASN"].thesis_id == "thesis-1"
        assert by_type["OPASN"].position_id == "pos-1"
        assert by_type["OPTRD"].thesis_id == "thesis-1"
        assert by_type["OPTRD"].position_id == "pos-1"


async def test_exercise_books_strike_pnl_and_opens_equity_leg(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An exercise books -premium and opens the resulting equity leg with the thesis link."""
    _engine, factory = db
    await _seed_open_option(factory, contract_count=5.0, premium_paid_per_contract=250.0)

    ctx, handle = await open_handle(factory)
    try:
        await integrate_lifecycle_event(handle, _assignment_event(LifecycleActivityType.OPEXC))
    finally:
        await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        option = await sess.get(PositionRow, "pos-1")
        assert option is not None
        assert option.status == "CLOSED"

        ledger = await sess.get(ThesisPnlLedgerRow, "thesis-1")
        assert ledger is not None
        assert ledger.realized_pnl_usd == Decimal(-1250)

        stmt = select(PositionRow).where(PositionRow.status == "OPEN")
        equities = (await sess.execute(stmt)).scalars().all()
        assert len(equities) == 1
        equity = row_to_record(equities[0])
        assert equity.thesis_id == "thesis-1"
        details = equity.details
        assert isinstance(details, EquityPositionDetails)
        assert details.average_cost_basis_per_share == pytest.approx(150.0)


async def test_assignment_without_paired_optrd_surfaces(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A missing paired OPTRD surfaces (raises) — the equity leg is underspecified."""
    _engine, factory = db
    await _seed_open_option(factory)

    unpaired = LifecycleEvent(
        activity_id="act-asn-2",
        activity_type=LifecycleActivityType.OPASN,
        occ_symbol=_OCC,
        qty=5.0,
        transaction_time=_TXN,
        paired_trade=None,
    )

    ctx, handle = await open_handle(factory)
    try:
        with pytest.raises(ValueError, match="no paired OPTRD"):
            await integrate_lifecycle_event(handle, unpaired)
    finally:
        await ctx.__aexit__(None, None, None)
