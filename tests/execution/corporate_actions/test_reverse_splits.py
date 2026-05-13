"""Tests for the REVERSE_SPLIT handler (ALP-411 story 03a).

The handler is invoked through the public ``integrate_ca_activity`` entry
point so the test exercises both the dispatch wiring and the handler itself.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

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
    CashCreditedDetail,
    CashCreditReason,
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


async def test_reverse_split_scales_equity_quantity_and_basis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A 1-for-10 reverse split divides shares by 10 and multiplies basis by 10."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=2.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
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
        assert pos.details.share_count == pytest.approx(10.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(20.0)
        assert pos.corporate_action_adjustment_needed is True


async def test_reverse_split_credits_fractional_cash_out_when_cash_impact_positive(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When ``signed_cash_impact_usd > 0`` the handler emits CASH_CREDITED w/ fractional reason."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=101.0, average_cost_basis_per_share=2.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory, current_cash_usd=50_000.0)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-cash-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
        position_id="pos-1",
        signed_cash_impact_usd=2.50,
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
        cash_entries = [r for r in log_rows if r.event_type == EventType.CASH_CREDITED.value]
        assert len(cash_entries) == 1
        from alphamind.portfolio_state.events.codec import decode_detail

        detail = decode_detail(cash_entries[0].detail_json, CashCreditedDetail)
        assert isinstance(detail, CashCreditedDetail)
        assert detail.reason == CashCreditReason.FRACTIONAL_SHARE_CASH_OUT
        assert detail.amount_usd == Decimal("2.50")


async def test_reverse_split_no_cash_credit_when_impact_zero(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """No CASH_CREDITED entry when ``signed_cash_impact_usd == 0``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=2.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-zero-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
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
        cash_entries = [r for r in log_rows if r.event_type == EventType.CASH_CREDITED.value]
        assert cash_entries == []


async def test_reverse_split_options_projects_alpaca_state_and_clears_greeks(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Options branch reads Alpaca's post-adjustment state and zeroes greeks to None."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_options_position(
            strike=150.0,
            contract_count=5.0,
            premium_paid_per_contract=250.0,
        ),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    lookup = FakeAlpacaPositionLookup()
    # Post-adjustment Alpaca position: 1 contract, $4.50 per-share avg cost,
    # multiplier still 100 (Alpaca doesn't change multiplier for whole-ratio
    # reverse splits but the projection is from the snapshot, not derived).
    lookup.register("AAPL", make_options_position_snapshot(qty=1.0, avg_entry_price=4.50))

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-opt-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=5.0,
        position_id="pos-1",
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
        assert pos.details.contract_count == pytest.approx(1.0)
        # 4.50 per-share * 100 multiplier = 450.0 per-contract premium
        assert pos.details.premium_paid_per_contract == pytest.approx(450.0)
        # Greeks should be cleared (sentinel zero values) — the underlying
        # protocol on the OptionGreeks model requires non-None floats, so the
        # handler signals "stale" via the freshness flag instead.
        assert pos.details.greeks.refresh_failed is True
        assert pos.corporate_action_adjustment_needed is True


async def test_reverse_split_strategy_applies_per_leg_projection(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Strategy branch projects per-leg state from Alpaca, with one log entry per CA."""
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
    lookup.register("AAPL", make_options_position_snapshot(qty=1.0, avg_entry_price=4.50))

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-strat-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=5.0,
        position_id="pos-1",
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
            assert leg.options.contract_count == pytest.approx(1.0)
            assert leg.options.premium_paid_per_contract == pytest.approx(450.0)
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


async def test_reverse_split_emits_corporate_action_applied(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Exactly one CORPORATE_ACTION_APPLIED event is emitted."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=2.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-applied-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
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


async def test_reverse_split_cancels_bracket(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Bracket is dissolved with reason ``corporate_action_reverse_split``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=2.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-bracket-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
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
        assert bracket_row.corporate_action_cancellation_reason == "corporate_action_reverse_split"

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
        types = [r.event_type for r in log_rows]
        assert types.count(EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value) == 1


async def test_reverse_split_writes_dedup_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Exactly one ledger row is written for the activity ID."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=100.0, average_cost_basis_per_share=2.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-ledger-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        ledger_rows = (
            (
                await sess.execute(
                    select(CorporateActionIntegrationLedgerRow).where(
                        CorporateActionIntegrationLedgerRow.alpaca_activity_id
                        == "ca-rsplit-ledger-1"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ledger_rows) == 1
        assert ledger_rows[0].processing_status == CorporateActionLedgerStatus.PROCESSED.value


async def test_reverse_split_raises_on_missing_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A reverse split for a missing position id raises ``ValueError``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-rsplit-missing-1",
        action_type=CorporateActionType.REVERSE_SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=10.0,
        position_id="pos-missing",
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    with pytest.raises(ValueError, match="missing position"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)
