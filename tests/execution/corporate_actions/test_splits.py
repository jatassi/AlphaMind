"""Tests for the SPLIT handler via the public ``integrate_ca_activity`` entry point.

Exercises the same state transitions as
``tests/execution/state_persistence/test_phase1_write_path.py::test_corporate_action_split_emits_events_and_ledger_anchor``
but calls ``integrate_ca_activity`` directly (bypassing the
``process_unprocessed_fills`` shim) to verify the moved internals compose
correctly with the new public entry point.
"""

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

# ---------------------------------------------------------------------------
# Equity — combined position + bracket + ledger test
# ---------------------------------------------------------------------------


async def test_split_equity_projects_position_and_clears_bracket_and_writes_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A 4-for-1 split multiplies share count by 4, divides cost basis by 4,
    dissolves the bracket with reason ``corporate_action_split``, writes a
    PROCESSED ledger anchor, and emits both CORPORATE_ACTION_APPLIED and
    BRACKET_CANCELLED_CORPORATE_ACTION events."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_equity_position(share_count=10.0, average_cost_basis_per_share=150.0),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-equity-combined-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Position: quantity x4, basis /4, flagged for adjustment.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(40.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(37.5)
        assert pos.corporate_action_adjustment_needed is True

        # Bracket: dissolved with split reason.
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value
        assert bracket_row.corporate_action_cancellation_reason == "corporate_action_split"

        # Ledger: PROCESSED anchor written.
        ledger_row = (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id
                    == "ca-split-equity-combined-1"
                )
            )
        ).scalar_one()
        assert ledger_row.processing_status == CorporateActionLedgerStatus.PROCESSED.value
        assert ledger_row.processing_invocation_id == handle.invocation_id

        # Activity log: both expected event types emitted.
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
        assert EventType.CORPORATE_ACTION_APPLIED.value in types
        assert EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value in types


# ---------------------------------------------------------------------------
# Options + strategy positions (ALP-639)
# ---------------------------------------------------------------------------


async def test_split_options_projects_alpaca_state_and_clears_greeks(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Forward split on an options position projects post-adjustment state from
    Alpaca, flags the position for adjustment, cancels the bracket, writes the
    dedup row, and marks greeks stale via ``refresh_failed=True``."""
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
    # Post-adjustment Alpaca snapshot for a 4-for-1 forward split on a contract
    # whose pre-adjustment per-share basis was 2.50 (premium 250 / multiplier 100):
    # contract count fans to 20, per-share basis drops to 0.625, multiplier
    # unchanged at 100 → post per-contract premium = 0.625 * 100 = 62.50.
    lookup.register("AAPL", make_options_position_snapshot(qty=20.0, avg_entry_price=0.625))

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-opt-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
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
        assert pos.details.contract_count == pytest.approx(20.0)
        assert pos.details.premium_paid_per_contract == pytest.approx(62.50)
        assert pos.details.greeks.refresh_failed is True
        assert pos.corporate_action_adjustment_needed is True

        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value

        ledger_row = (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id == "ca-split-opt-1"
                )
            )
        ).scalar_one()
        assert ledger_row.processing_status == CorporateActionLedgerStatus.PROCESSED.value


async def test_split_strategy_applies_per_leg_projection(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Forward split on a multi-leg strategy position projects per-leg state from
    Alpaca and emits exactly one CORPORATE_ACTION_APPLIED entry for the parent."""
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
    lookup.register("AAPL", make_options_position_snapshot(qty=20.0, avg_entry_price=0.625))

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-strat-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    # _project_strategy_from_snapshot consults the lookup once per leg; the
    # two-leg strategy fixture produces two calls keyed on the same underlying.
    assert lookup.calls == ["AAPL", "AAPL"]
    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, StrategyPositionDetails)
        for leg in pos.details.legs:
            assert leg.options.contract_count == pytest.approx(20.0)
            assert leg.options.premium_paid_per_contract == pytest.approx(62.50)
            assert leg.options.greeks.refresh_failed is True
        assert pos.details.strategy_greeks.refresh_failed is True
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


async def test_split_on_options_without_lookup_raises_value_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A SPLIT on an options position with ``alpaca_position_lookup=None`` raises
    ``ValueError`` from ``apply_options_position_mutation`` — the post-ALP-639
    replacement for the old ``NotImplementedError`` from ``_require_equity_details``.
    """
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await seed_invocation_substrate(factory)
    await seed_position_cluster(
        factory,
        make_open_options_position(),
        make_pending_entry_order(),
        make_active_thesis(),
        make_active_bracket(),
    )
    await seed_cash_ledger(factory)
    await seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-opt-no-lookup",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=NOW - timedelta(minutes=5),
    )

    ctx, handle = await open_handle(factory)
    with pytest.raises(ValueError, match="AlpacaPositionLookup is required"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)
