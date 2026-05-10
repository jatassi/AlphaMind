"""Tests for the SYMBOL_CHANGE handler (ALP-411 story 03a)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.execution.state_persistence.tables.activity_log import ActivityLogRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.write_paths.records import (
    CorporateActionLedgerStatus,
)
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
from tests.execution.corporate_actions._handler_substrate import (
    NOW,
    db,  # noqa: F401 — fixture re-export
    make_active_bracket,
    make_active_thesis,
    make_open_equity_position,
    make_open_options_position,
    make_open_strategy_position,
    make_pending_entry_order,
    open_handle,
    seed_cash_ledger,
    seed_drawdown_state,
    seed_invocation_substrate,
    seed_position_cluster,
)


async def test_symbol_change_renames_equity_ticker(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """Equity branch updates ``ticker`` to ``activity.new_ticker`` without scaling."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker="META",
        ratio_or_amount=1.0,
        position_id="pos-1",
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
        assert pos.details.ticker == "META"
        assert pos.details.share_count == pytest.approx(100.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(150.0)
        assert pos.corporate_action_adjustment_needed is True


async def test_symbol_change_renames_options_underlying(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """Options branch updates ``underlying_ticker`` and leaves the contract spec intact."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_options_position(
            underlying_ticker="FB",
            strike=180.0,
            contract_count=3.0,
            premium_paid_per_contract=125.0,
        ),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-opt-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker="META",
        ratio_or_amount=1.0,
        position_id="pos-1",
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
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.underlying_ticker == "META"
        assert pos.details.strike_price == pytest.approx(180.0)
        assert pos.details.contract_count == pytest.approx(3.0)
        assert pos.details.premium_paid_per_contract == pytest.approx(125.0)
        assert pos.corporate_action_adjustment_needed is True


async def test_symbol_change_renames_each_strategy_leg(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """Strategy branch renames every leg's ``underlying_ticker``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_strategy_position(underlying_ticker="FB"),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-strat-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker="META",
        ratio_or_amount=1.0,
        position_id="pos-1",
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
        assert isinstance(pos.details, StrategyPositionDetails)
        for leg in pos.details.legs:
            assert leg.options.underlying_ticker == "META"
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


async def test_symbol_change_no_cash_movement(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """No CASH_CREDITED / CASH_DEBITED entries; symbol change is cash-neutral."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-cash-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker="META",
        ratio_or_amount=1.0,
        position_id="pos-1",
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


async def test_symbol_change_cancels_bracket_and_writes_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """Bracket is dissolved with symbol-change reason; ledger row written."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-bracket-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker="META",
        ratio_or_amount=1.0,
        position_id="pos-1",
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
        assert bracket_row.corporate_action_cancellation_reason == "corporate_action_symbol_change"

        ledger_rows = (
            (
                await sess.execute(
                    select(CorporateActionIntegrationLedgerRow).where(
                        CorporateActionIntegrationLedgerRow.alpaca_activity_id
                        == "ca-symchg-bracket-1"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ledger_rows) == 1
        assert ledger_rows[0].processing_status == CorporateActionLedgerStatus.PROCESSED.value


async def test_symbol_change_emits_corporate_action_applied(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """Exactly one CORPORATE_ACTION_APPLIED event is emitted."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-applied-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker="META",
        ratio_or_amount=1.0,
        position_id="pos-1",
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


async def test_symbol_change_requires_new_ticker(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],  # noqa: F811
) -> None:
    """A SYMBOL_CHANGE activity without ``new_ticker`` raises ``ValueError``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-symchg-missing-1",
        action_type=CorporateActionType.SYMBOL_CHANGE,
        ticker="FB",
        new_ticker=None,
        ratio_or_amount=1.0,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    with pytest.raises(ValueError, match="new_ticker"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)
