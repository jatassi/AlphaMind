"""Tests for the STOCK_DIVIDEND handler (ALP-411 story 03a)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId, Symbol
from alphamind.portfolio_state.events.activity_log import (
    CorporateActionType,
    EventType,
)
from alphamind.portfolio_state.records.orders import BracketStatus
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.state.records import (
    CorporateActionLedgerStatus,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from tests.execution.corporate_actions._handler_substrate import (
    NOW,
    FakeAlpacaPositionLookup,
    make_active_bracket,
    make_active_thesis,
    make_open_equity_position,
    make_open_options_position,
    make_open_strategy_position,
    make_options_position_snapshot,
    make_pending_entry_order,
    open_handle,
    seed_cash_ledger,
    seed_drawdown_state,
    seed_invocation_substrate,
    seed_position_cluster,
)


async def test_stock_dividend_scales_equity_quantity_and_basis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A 10% stock dividend multiplies shares by 1.1 and divides basis by 1.1."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=110.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stockdiv-1",
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(110.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(100.0)
        assert pos.corporate_action_adjustment_needed is True


async def test_stock_dividend_no_cash_movement(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """No CASH_CREDITED / CASH_DEBITED entries; no cash impact."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=110.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stockdiv-cash-1",
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.invocation_id == handle.invocation_id
                    )
                )
            )
            .scalars()
            .all()
        )
        types = {r.event_type for r in log_rows}
        assert EventType.CASH_CREDITED.value not in types
        assert EventType.CASH_DEBITED.value not in types


async def test_stock_dividend_options_projects_alpaca_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Options branch reads Alpaca's post-adjustment state and flags greeks stale."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_options_position(
            strike=150.0,
            contract_count=10.0,
            premium_paid_per_contract=250.0,
        ),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    lookup = FakeAlpacaPositionLookup()
    # 10% stock dividend → 11 contracts post-adjustment, $2.27 per share
    lookup.register("AAPL", make_options_position_snapshot(qty=11.0, avg_entry_price=2.27))

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stockdiv-opt-1",
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    assert lookup.calls == ["AAPL"]
    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(11.0)
        assert pos.details.premium_paid_per_contract == pytest.approx(227.0)
        assert pos.details.greeks.refresh_failed is True
        assert pos.corporate_action_adjustment_needed is True


async def test_stock_dividend_strategy_applies_per_leg_projection(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Strategy branch projects each leg from Alpaca."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_strategy_position(),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    lookup = FakeAlpacaPositionLookup()
    lookup.register("AAPL", make_options_position_snapshot(qty=11.0, avg_entry_price=2.27))

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stockdiv-strat-1",
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, StrategyPositionDetails)
        for leg in pos.details.legs:
            assert leg.options.contract_count == pytest.approx(11.0)
            assert leg.options.premium_paid_per_contract == pytest.approx(227.0)
            assert leg.options.greeks.refresh_failed is True
        assert pos.corporate_action_adjustment_needed is True

        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.invocation_id == handle.invocation_id
                    )
                )
            )
            .scalars()
            .all()
        )
        applied = [r for r in log_rows if r.event_type == EventType.CORPORATE_ACTION_APPLIED.value]
        assert len(applied) == 1


async def test_stock_dividend_emits_corporate_action_applied(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Exactly one CORPORATE_ACTION_APPLIED event is emitted."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=110.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stockdiv-applied-1",
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.05,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.invocation_id == handle.invocation_id
                    )
                )
            )
            .scalars()
            .all()
        )
        applied = [r for r in log_rows if r.event_type == EventType.CORPORATE_ACTION_APPLIED.value]
        assert len(applied) == 1


async def test_stock_dividend_cancels_bracket_and_writes_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Bracket is dissolved with stock-dividend reason; ledger row written."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=110.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stockdiv-bracket-1",
        action_type=CorporateActionType.STOCK_DIVIDEND,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value
        assert bracket_row.corporate_action_cancellation_reason == "corporate_action_stock_dividend"

        ledger_rows = (
            (
                await sess.execute(
                    select(CorporateActionIntegrationLedgerRow).where(
                        CorporateActionIntegrationLedgerRow.alpaca_activity_id
                        == "ca-stockdiv-bracket-1"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ledger_rows) == 1
        assert ledger_rows[0].processing_status == CorporateActionLedgerStatus.PROCESSED.value
