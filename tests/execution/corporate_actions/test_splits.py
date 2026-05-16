"""Tests for the SPLIT handler via the public ``integrate_ca_activity`` entry point.

Exercises the same state transitions as
``tests/execution/state_persistence/test_phase1_write_path.py::test_corporate_action_split_emits_events_and_ledger_anchor``
but calls ``integrate_ca_activity`` directly (bypassing the
``process_unprocessed_fills`` shim) to verify the moved internals compose
correctly with the new public entry point.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.regime import RiskZone
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.events.activity_log import (
    CorporateActionType,
    EventType,
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
    OrderClass,
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
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.state.invocation_context.context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    CorporateActionLedgerStatus,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-ca-2026-05-08T12:00:00Z"
_PROCESS_ID = "proc-ca-1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind_ca.db"

    import alphamind.state.tables  # noqa: F401 — side-effect import

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-ca-1.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0",
    )


def _make_invocation_record(invocation_id: str = _INV_ID) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/provenance/inv/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/provenance/calibration.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _make_open_position(
    position_id: str = "pos-1",
    *,
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    share_count: float = 10.0,
    average_cost_basis_per_share: float = 150.0,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=Symbol("AAPL"),
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=average_cost_basis_per_share,
            fill_quantity=share_count,
            slippage=0.0,
            fees=0.0,
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_pending_entry_order(
    order_id: str = "ord-entry-1",
    bracket_id: str = "brk-1",
) -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId("pos-1"),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("thesis-1"),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _make_active_bracket(bracket_id: str = "brk-1", position_id: str = "pos-1") -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_thesis(thesis_id: str = "thesis-1", position_id: str = "pos-1") -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=ThesisId(thesis_id),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative=f"{ct.value} narrative",
            key_assumptions=(KeyAssumption(text="Earnings beat", outcome=None),),
            generation_timestamp=_NOW - timedelta(hours=4),
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    generation_at = _NOW - timedelta(hours=4)
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="AAPL momentum",
        key_catalyst="Q3 earnings beat",
        position_size_rationale="Sized at 5%",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=generation_at,
        time_expectation_hours=24.0,
        age_hours=4.0,
        expected_resolution_at=generation_at + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _make_cash_ledger(current_cash_usd: float = 100_000.0) -> CashLedger:
    return CashLedger(
        current_cash_usd=current_cash_usd,
        settled_cash_usd=current_cash_usd,
        reserved_capital_usd=0.0,
        available_buying_power_usd=current_cash_usd,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState(
        current_drawdown_pct=0.0,
        equity_high_water_mark_usd=100_000.0,
        drawdown_duration_hours=0.0,
        lifetime_max_drawdown_pct=0.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_invocation_substrate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_make_invocation_record()))
        await sess.commit()


async def _seed_position_cluster(
    factory: async_sessionmaker[AsyncSession],
    position: PositionRecord,
    order: OrderRecord,
    thesis: ThesisRecord,
    bracket: BracketRecord,
) -> None:
    from tests.state._fk_substrate import stub_order_row

    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_row, leg_rows = bracket_record_to_rows(bracket)

    seeded_order_ids: set[str] = {order.order_id}
    extra_order_ids: list[str] = [bracket_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(order))
        for oid in extra_order_ids:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, bracket.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _seed_cash_ledger(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(_make_cash_ledger(), last_updated_at=_NOW))
        await sess.commit()


async def _seed_drawdown_state(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(drawdown_state_record_to_row(_make_drawdown_state(), last_updated_at=_NOW))
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=_INV_ID + "-direct"),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_split_adjusts_position_quantity_and_basis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A 4-for-1 split multiplies share count by 4 and divides cost basis by 4."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(share_count=10.0, average_cost_basis_per_share=150.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-direct-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(40.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(37.5)
        assert pos.corporate_action_adjustment_needed is True


async def test_split_emits_corporate_action_applied_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The SPLIT handler emits a CORPORATE_ACTION_APPLIED activity-log entry."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(share_count=10.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-event-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=2.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
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
        assert EventType.CORPORATE_ACTION_APPLIED.value in types


async def test_split_cancels_bracket_and_emits_bracket_cancelled_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The SPLIT handler cancels the bracket and emits BRACKET_CANCELLED_CORPORATE_ACTION."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(share_count=10.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-bracket-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=3.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Bracket is DISSOLVED.
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value
        assert bracket_row.corporate_action_cancellation_reason == "corporate_action_split"

        # Activity log contains the bracket-cancelled entry.
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
        assert EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value in types


async def test_split_writes_ledger_dedup_anchor(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The SPLIT handler writes a dedup anchor to the CA integration ledger."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(share_count=10.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-ledger-1",
        action_type=CorporateActionType.SPLIT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        ledger_row = (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id == "ca-split-ledger-1"
                )
            )
        ).scalar_one()
        assert ledger_row.processing_status == CorporateActionLedgerStatus.PROCESSED.value
        assert ledger_row.processing_invocation_id == handle.invocation_id
