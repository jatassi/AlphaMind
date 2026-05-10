"""Tests for the Phase 1 fill-integration write path (story 07 / ALP-365).

The Phase 1 write path drains every ``processing_status='unprocessed'`` fill
record, integrates each into state (orders / positions / brackets / theses /
cash_ledger / drawdown_state), emits activity-log entries, and marks the
fills processed — all in one transaction.

Tests exercise the public entry point ``process_unprocessed_fills(handle, *,
config)`` against an on-disk SQLite DB, with the ``InvocationContext`` open
around the call. Each test seeds the prerequisite Tier-1 entities (orders,
positions, brackets, theses, cash, drawdown) via the same per-table codecs
shipped in stories 04a-04e.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.execution.state_persistence.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.execution.state_persistence.tables.activity_log import ActivityLogRow
from alphamind.execution.state_persistence.tables.brackets import BracketRow
from alphamind.execution.state_persistence.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.execution.state_persistence.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.execution.state_persistence.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.execution.state_persistence.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.execution.state_persistence.tables.fill_records import FillRecordRow
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.tables.orders import OrderRow
from alphamind.execution.state_persistence.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.tables.positions_codec import (
    row_to_record as position_row_to_record,
)
from alphamind.execution.state_persistence.tables.theses import ThesisRow
from alphamind.execution.state_persistence.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)
from alphamind.execution.state_persistence.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.execution.state_persistence.write_paths.records import (
    CorporateActionLedgerStatus,
    FillProcessingStatus,
    FillRecord,
)
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
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"
_PROCESS_ID = "proc-1"


# ---------------------------------------------------------------------------
# Engine + session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.execution.state_persistence.tables  # noqa: F401

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


def _make_state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/pip-freeze",
            "invocation_provenance_root": "/tmp/provenance",
        }
    )


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
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-1.txt",
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


def _make_pending_entry_order(
    order_id: str = "ord-entry-1",
    *,
    bracket_id: str = "brk-1",
    quantity: float = 10.0,
    direction: OrderDirection = OrderDirection.BUY,
    role: OrderRole = OrderRole.ENTRY,
    status: OrderStatus = OrderStatus.PENDING,
    filled_quantity: float = 0.0,
    avg_fill_price: float | None = None,
    position_id: str | None = None,
) -> OrderRecord:
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": bracket_id,
            "role": role,
            "instrument_spec": EquityInstrumentSpec(ticker="AAPL"),
            "direction": direction,
            "order_type": OrderType.MARKET,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(),
            "quantity": quantity,
            "duration": OrderDuration.DAY,
            "status": status,
            "alpaca_order_id": f"alp-{order_id}",
            "alpaca_order_id_chain": (f"alp-{order_id}",),
            "submission_timestamp": _NOW - timedelta(minutes=15),
            "last_update_timestamp": _NOW - timedelta(minutes=15),
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
            "remaining_quantity": quantity - filled_quantity,
            "modification_count": 0,
            "originating_thesis_id": "thesis-1",
            "originating_pm_command_id": None,
            "age_hours": 0.25,
        }
    )


def _make_pending_position(
    position_id: str = "pos-1",
    *,
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    direction: Direction = Direction.LONG,
    ticker: str = "AAPL",
    share_count: float = 0.0,
    average_cost_basis_per_share: float = 0.0,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=ticker,
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
    )
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.PENDING,
            "direction": direction,
            "entry_timestamp": None,
            "details": details,
            "execution_history": (),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_open_position(
    position_id: str = "pos-1",
    *,
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    direction: Direction = Direction.LONG,
    ticker: str = "AAPL",
    share_count: float = 10.0,
    average_cost_basis_per_share: float = 150.0,
    fill_price: float = 150.0,
) -> PositionRecord:
    """Build an OPEN position whose execution_history reflects an entry fill."""
    from alphamind.portfolio_state.records.positions import PositionFill

    details = EquityPositionDetails(
        ticker=ticker,
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=fill_price,
            fill_quantity=share_count,
            slippage=0.0,
            fees=0.0,
        ),
    )
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.OPEN,
            "direction": direction,
            "entry_timestamp": _NOW - timedelta(hours=2),
            "details": details,
            "execution_history": history,
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_pending_bracket(bracket_id: str = "brk-1", position_id: str = "pos-1") -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=f"{bracket_id}-ord-stop",
        trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id="ord-entry-1",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_bracket(bracket_id: str = "brk-1", position_id: str = "pos-1") -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=f"{bracket_id}-ord-stop",
        trigger=PriceTrigger(underlying_ticker="AAPL", threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-entry-1",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_thesis(
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
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
    time_expectation_hours = 24.0
    return ThesisRecord(
        thesis_id=thesis_id,
        position_id=position_id,
        summary="AAPL momentum",
        key_catalyst="Q3 earnings beat",
        position_size_rationale="Sized at 5%",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=generation_at,
        time_expectation_hours=time_expectation_hours,
        age_hours=4.0,
        expected_resolution_at=generation_at + timedelta(hours=time_expectation_hours),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _make_thesis_with_resolved_components(
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
) -> ThesisRecord:
    """ACTIVE thesis whose components already carry a resolution_outcome.

    Story 07 transitions ``status`` to RESOLVED on position closure but
    leaves the components' resolution_outcome alone (analyst owns that).
    For the exit-fill happy-path test we pre-populate the outcomes so the
    resulting RESOLVED ThesisRecord remains valid.
    """
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative=f"{ct.value} narrative",
            key_assumptions=(),
            generation_timestamp=_NOW - timedelta(hours=4),
            resolution_outcome=ThesisComponentOutcome.VALIDATED,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    generation_at = _NOW - timedelta(hours=4)
    time_expectation_hours = 24.0
    return ThesisRecord(
        thesis_id=thesis_id,
        position_id=position_id,
        summary="AAPL momentum",
        key_catalyst="Q3 earnings beat",
        position_size_rationale="Sized at 5%",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=generation_at,
        time_expectation_hours=time_expectation_hours,
        age_hours=4.0,
        expected_resolution_at=generation_at + timedelta(hours=time_expectation_hours),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _make_cash_ledger(current_cash_usd: float = 100_000.0) -> CashLedger:
    return CashLedger.model_validate(
        {
            "current_cash_usd": current_cash_usd,
            "settled_cash_usd": current_cash_usd,
            "reserved_capital_usd": 0.0,
            "available_buying_power_usd": current_cash_usd,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )


def _make_drawdown_state(
    equity_high_water_mark_usd: float = 100_000.0,
) -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": 0.0,
            "equity_high_water_mark_usd": equity_high_water_mark_usd,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": 0.0,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


def _make_unprocessed_fill(
    fill_id: str,
    *,
    order_id: str = "ord-entry-1",
    fill_quantity: float = 10.0,
    fill_price: float = 150.0,
    fill_timestamp: datetime | None = None,
    remaining_quantity_after: float = 0.0,
    order_status_after: OrderStatus = OrderStatus.FILLED,
    fees_usd: float = 0.0,
    slippage_usd: float | None = 0.0,
) -> FillRecord:
    ts = fill_timestamp if fill_timestamp is not None else _NOW - timedelta(minutes=10)
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        fill_timestamp=ts,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
        remaining_quantity_after=remaining_quantity_after,
        order_status_after=order_status_after,
        slippage_usd=slippage_usd,
        fees_usd=fees_usd,
        execution_venue="NASDAQ",
        gateway_reference=f"alp-{fill_id}",
        persistence_timestamp=ts + timedelta(seconds=1),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_invocation_substrate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Persist process_lifetime + invocation row pair so FKs satisfy."""
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_make_invocation_record()))
        await sess.commit()


async def _seed_position_order_thesis_bracket(
    factory: async_sessionmaker[AsyncSession],
    position: PositionRecord,
    order: OrderRecord,
    thesis: ThesisRecord,
    bracket: BracketRecord,
) -> None:
    """Seed a full position cluster in a single deferred-FK transaction.

    All four entities reference each other cyclically, so they must commit
    together.  Bracket legs are flushed after the parent bracket row so the
    non-deferred bracket_legs.bracket_id FK is satisfied at flush time.

    Protective-leg order_ids (deferred FK to orders) are also seeded as stub
    orders in the same transaction so the COMMIT does not raise IntegrityError.
    """
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_row, leg_rows = bracket_record_to_rows(bracket)

    # Collect every order_id that appears in the bracket or its legs but is not
    # the main order being seeded: brackets.entry_order_id and each leg.order_id.
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


async def _seed_position(
    factory: async_sessionmaker[AsyncSession],
    record: PositionRecord,
) -> None:
    async with factory() as sess:
        sess.add(position_record_to_row(record))
        await sess.commit()


async def _seed_order(
    factory: async_sessionmaker[AsyncSession],
    record: OrderRecord,
) -> None:
    async with factory() as sess:
        sess.add(order_record_to_row(record))
        await sess.commit()


async def _seed_bracket(
    factory: async_sessionmaker[AsyncSession],
    record: BracketRecord,
) -> None:
    parent_row, leg_rows = bracket_record_to_rows(record)
    async with factory() as sess:
        sess.add(parent_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _seed_thesis(
    factory: async_sessionmaker[AsyncSession],
    record: ThesisRecord,
) -> None:
    parent_row, child_rows = thesis_record_to_rows(record)
    async with factory() as sess:
        sess.add(parent_row)
        await sess.flush()
        for crow in child_rows:
            sess.add(crow)
        await sess.commit()


async def _seed_cash_ledger(
    factory: async_sessionmaker[AsyncSession],
    record: CashLedger | None = None,
) -> None:
    record = record if record is not None else _make_cash_ledger()
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(record, last_updated_at=_NOW))
        await sess.commit()


async def _seed_drawdown_state(
    factory: async_sessionmaker[AsyncSession],
    record: DrawdownState | None = None,
) -> None:
    record = record if record is not None else _make_drawdown_state()
    async with factory() as sess:
        sess.add(drawdown_state_record_to_row(record, last_updated_at=_NOW))
        await sess.commit()


async def _append_fill(
    factory: async_sessionmaker[AsyncSession],
    fill: FillRecord,
) -> None:
    async with factory() as sess:
        await append_fill_record(sess, fill)
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[InvocationContext, InvocationHandle]:
    """Open an InvocationContext and return (ctx, handle).

    Caller is responsible for ``await ctx.__aexit__(None, None, None)`` after
    Phase 1 completes (or passing an exc to trigger rollback).
    """
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=_INV_ID + "-phase1"),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_entry_fill_transitions_pending_position_to_open(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Happy-path entry fill: position PENDING → OPEN, fill marked processed,
    cash debited, activity log carries the lifecycle entries."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 0

    # Fill row transitions to processed with stamps populated.
    async with factory() as sess:
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-1"))
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.PROCESSED.value
        assert fill_row.processing_invocation_id == handle.invocation_id
        assert fill_row.processing_timestamp is not None

        # Position transitions PENDING → OPEN with the fill in execution_history.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert pos.entry_timestamp is not None
        assert len(pos.execution_history) == 1
        assert pos.execution_history[0].fill_quantity == 10.0
        assert pos.execution_history[0].fill_price == 150.0
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == 10.0
        assert pos.details.average_cost_basis_per_share == 150.0

        # Order transitions PENDING → FILLED.
        order_row = (
            await sess.execute(select(OrderRow).where(OrderRow.order_id == "ord-entry-1"))
        ).scalar_one()
        assert order_row.status == OrderStatus.FILLED.value
        assert order_row.filled_quantity == 10.0
        assert order_row.average_fill_price == 150.0
        assert order_row.remaining_quantity == 0.0

        # Cash debited: 10 shares * $150 = $1500.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 - 1500.0)

        # Activity log entries: order_filled + position_opened + bracket_activated +
        # capital_released + cash_debited.
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
    assert EventType.ORDER_FILLED.value in types
    assert EventType.POSITION_OPENED.value in types
    assert EventType.BRACKET_ACTIVATED.value in types
    assert EventType.CAPITAL_RELEASED.value in types
    assert EventType.CASH_DEBITED.value in types


async def test_exit_fill_closes_position_and_resolves_thesis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Happy-path exit fill: position OPEN → CLOSED, realized P/L computed,
    bracket DISSOLVED, thesis RESOLVED."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    close_order = _make_pending_entry_order(
        order_id="ord-close-1",
        role=OrderRole.CLOSE,
        direction=OrderDirection.SELL,
        position_id="pos-1",
    )
    # All four entities reference each other cyclically — seed in one transaction.
    # _make_active_bracket uses entry_order_id="ord-entry-1" and a protective leg
    # with order_id="brk-1-ord-stop", so we need stubs for all referenced orders.
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_thesis_with_resolved_components())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids = {entry_order.order_id, close_order.order_id}
    async with factory() as sess:
        sess.add(position_record_to_row(_make_open_position()))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(entry_order))
        sess.add(order_record_to_row(close_order))
        for oid in leg_order_ids:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, bracket_row.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=98_500.0))
    await _seed_drawdown_state(factory)
    # Sell 10 shares at $160 -> realized P/L = (160 - 150) * 10 = $100.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-close-1",
            order_id="ord-close-1",
            fill_price=160.0,
            fill_quantity=10.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.CLOSED
        assert pos.realized_pnl_to_date_usd == pytest.approx(100.0)

        # Bracket transitions ACTIVE → DISSOLVED with all legs CANCELLED.
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value

        # Thesis transitions ACTIVE → RESOLVED with resolution_timestamp set.
        thesis_row = (
            await sess.execute(select(ThesisRow).where(ThesisRow.thesis_id == "thesis-1"))
        ).scalar_one()
        assert thesis_row.status == ThesisRecordStatus.RESOLVED.value
        assert thesis_row.resolution_timestamp is not None
        # resolution_category remains None — analyst pipeline owns that.
        assert thesis_row.resolution_category is None

        # Activity log carries position_closed + bracket_dissolved + thesis_resolved.
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
    assert EventType.POSITION_CLOSED.value in types
    assert EventType.BRACKET_DISSOLVED.value in types
    assert EventType.THESIS_RESOLVED.value in types
    assert EventType.CASH_CREDITED.value in types
    assert EventType.ORDER_FILLED.value in types


async def test_multi_fill_ordering_produces_cumulative_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Two unprocessed fills on the same order, processed in fill-timestamp
    order, produce the right cumulative state."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(quantity=10.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Fill 1: 4 shares at $150 (earlier timestamp); Fill 2: 6 shares at $151.
    earlier = _NOW - timedelta(minutes=20)
    later = _NOW - timedelta(minutes=10)
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-2-later",
            fill_quantity=6.0,
            fill_price=151.0,
            fill_timestamp=later,
            remaining_quantity_after=0.0,
            order_status_after=OrderStatus.FILLED,
        ),
    )
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-1-earlier",
            fill_quantity=4.0,
            fill_price=150.0,
            fill_timestamp=earlier,
            remaining_quantity_after=6.0,
            order_status_after=OrderStatus.PARTIALLY_FILLED,
        ),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 2

    async with factory() as sess:
        order_row = (
            await sess.execute(select(OrderRow).where(OrderRow.order_id == "ord-entry-1"))
        ).scalar_one()
        # filled = 4 + 6 = 10. weighted avg fill = (4*150 + 6*151) / 10 = 150.6.
        assert order_row.filled_quantity == pytest.approx(10.0)
        assert order_row.status == OrderStatus.FILLED.value
        assert order_row.average_fill_price == pytest.approx(150.6)

        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(10.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(150.6)
        # Both fills land in the execution_history in chronological order.
        timestamps = [f.fill_timestamp for f in pos.execution_history]
        assert timestamps == sorted(timestamps)

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        # Net cash debit = 4*150 + 6*151 = 1506.
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 - 1506.0)


async def test_corporate_action_split_emits_events_and_ledger_anchor(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A stock split's corporate_action_applied entry fires, position quantity
    and cost basis adjust per the ratio, and the CA integration ledger records
    the dedupe anchor."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        CorporateActionActivity,
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_open_position(share_count=10.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-1",
        action_type=CorporateActionType.SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=4.0,  # 4-for-1 split.
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle, config=_make_state_persistence_config(), ca_activities=(ca,)
    )
    await ctx.__aexit__(None, None, None)

    assert summary.ca_activities_processed == 1

    async with factory() as sess:
        # Position quantity * 4, cost basis / 4.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(40.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(37.5)

        # Activity log carries corporate_action_applied + bracket_cancelled_corporate_action.
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

        # CA integration ledger records the dedupe anchor.
        ledger_row = (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id == "ca-split-1"
                )
            )
        ).scalar_one()
        assert ledger_row.processing_status == CorporateActionLedgerStatus.PROCESSED.value
        assert ledger_row.processing_invocation_id == handle.invocation_id


async def test_atomicity_exception_rolls_back_fills_and_log(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An exception mid-integration leaves fills unprocessed and the activity
    log carries no entries from this invocation.

    The trigger is a SELL_TO_OPEN direction on a PENDING position — Phase 1
    raises NotImplementedError for short-entry fills (FK enforcement makes the
    original "missing position row" scenario impossible at the seeding layer).
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    short_entry_order = _make_pending_entry_order(
        order_id="ord-short-1",
        direction=OrderDirection.SELL_TO_OPEN,
        position_id="pos-1",
    )
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        short_entry_order,
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1", order_id="ord-short-1"))

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    try:
        with pytest.raises(NotImplementedError, match="SHORT entry"):
            await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    finally:
        # Funnel the (caught) exception through the context manager so the
        # surrounding transaction rolls back.
        await ctx.__aexit__(NotImplementedError, NotImplementedError("forced"), None)

    async with factory() as sess:
        # Fill row remains unprocessed.
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-1"))
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.UNPROCESSED.value
        assert fill_row.processing_invocation_id is None

        # No activity log entries persist for this invocation.
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )
        assert log_rows == []


async def test_quarantined_fill_excluded_without_aborting_batch(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill with negative quantity is marked quarantined and excluded from
    integration; other fills in the batch still process normally."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # The malformed fill — Pydantic FillRecord allows negative quantities at
    # the type level (no validator); the SQL CHECK doesn't either. We bypass
    # by writing the row directly with a negative quantity.
    bad_fill_row = FillRecordRow(
        fill_id="fill-bad",
        order_id="ord-entry-1",
        fill_timestamp=(_NOW - timedelta(minutes=20)).isoformat(),
        fill_price=150.0,
        fill_quantity=-1.0,  # <— invalid
        remaining_quantity_after=11.0,
        order_status_after=OrderStatus.PARTIALLY_FILLED.value,
        slippage_usd=0.0,
        fees_usd=0.0,
        execution_venue="NASDAQ",
        gateway_reference="alp-bad",
        persistence_timestamp=_NOW.isoformat(),
        processing_status=FillProcessingStatus.UNPROCESSED.value,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution_json=None,
        live_execution_estimate_json=None,
    )
    async with factory() as sess:
        sess.add(bad_fill_row)
        await sess.commit()
    # And a good fill that should still process to completion.
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-good"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 1

    async with factory() as sess:
        bad = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-bad"))
        ).scalar_one()
        good = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-good"))
        ).scalar_one()
        assert bad.processing_status == FillProcessingStatus.QUARANTINED.value
        assert bad.processing_invocation_id == handle.invocation_id
        assert good.processing_status == FillProcessingStatus.PROCESSED.value

        # The good fill's position update is in place.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN


async def test_buy_fill_decrements_reserved_capital_to_zero(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A buy fill consuming the full reservation must decrement
    reserved_capital_usd by the fill consideration. Without this, Phase 2's
    OPEN reserve and Phase 1's fill double-count: current_cash drops AND
    reserved_capital stays — overstating committed capital.
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(quantity=10.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # Seed cash with a $1000 reservation already in place (mirroring Phase 2's OPEN).
    seeded = CashLedger.model_validate(
        {
            "current_cash_usd": 100_000.0,
            "settled_cash_usd": 100_000.0,
            "reserved_capital_usd": 1_000.0,
            "available_buying_power_usd": 99_000.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )
    await _seed_cash_ledger(factory, seeded)
    await _seed_drawdown_state(factory)
    # Buy fill: 10 shares * $100 = $1000 consideration matches the reservation.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-1",
            fill_price=100.0,
            fill_quantity=10.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        # Reservation drained by the fill.
        assert cash.reserved_capital_usd == pytest.approx(0.0)
        # Cash debit applied as before.
        assert cash.current_cash_usd == pytest.approx(99_000.0)


async def test_buy_fill_clamps_reserved_capital_decrement_at_zero(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When the buy-fill consideration exceeds the seeded reservation
    (partial reservations, rounding, mid-flight adjustments), the
    decrement must clamp at zero rather than going negative.
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(quantity=10.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # Seed only $500 reserved while the fill consumes $1000.
    seeded = CashLedger.model_validate(
        {
            "current_cash_usd": 100_000.0,
            "settled_cash_usd": 100_000.0,
            "reserved_capital_usd": 500.0,
            "available_buying_power_usd": 99_500.0,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
    )
    await _seed_cash_ledger(factory, seeded)
    await _seed_drawdown_state(factory)
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-1",
            fill_price=100.0,
            fill_quantity=10.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(0.0)


async def test_pending_position_with_missing_bracket_row_rejected_at_commit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """FK enforcement prevents committing a position that references a non-existent
    bracket row — the deferred FK on positions.bracket_id raises IntegrityError at
    COMMIT, which is the database-level equivalent of the application-level
    StateInconsistencyError that Phase 1 used to guard against.

    This test verifies that the invariant is enforced at the schema layer.
    """
    from sqlalchemy.exc import IntegrityError

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Attempt to commit a position row pointing at a bracket that does not exist.
    position_row = position_record_to_row(_make_pending_position())  # bracket_id="brk-1"
    with pytest.raises(IntegrityError):
        async with factory() as sess:
            sess.add(position_row)
            await sess.commit()


async def test_open_position_with_missing_bracket_row_rejected_at_commit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """FK enforcement prevents committing an OPEN position that references a
    non-existent bracket row — deferred FK on positions.bracket_id raises
    IntegrityError at COMMIT.  This is the schema-level guard for the
    invariant that Phase 1's _dissolve_bracket path previously enforced at
    the application layer.
    """
    from sqlalchemy.exc import IntegrityError

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Attempt to commit an OPEN position pointing at a bracket that does not exist.
    position_row = position_record_to_row(_make_open_position())  # bracket_id="brk-1"
    with pytest.raises(IntegrityError):
        async with factory() as sess:
            sess.add(position_row)
            await sess.commit()


async def test_open_position_with_missing_thesis_row_rejected_at_commit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """FK enforcement prevents committing a position that references a non-existent
    thesis row — deferred FK on positions.thesis_id raises IntegrityError at COMMIT.
    This is the schema-level guard for the invariant that Phase 1's
    _maybe_resolve_thesis path previously enforced at the application layer.
    """
    from sqlalchemy.exc import IntegrityError

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Attempt to commit an OPEN position pointing at a thesis that does not exist.
    position_row = position_record_to_row(_make_open_position())  # thesis_id="thesis-1"
    with pytest.raises(IntegrityError):
        async with factory() as sess:
            sess.add(position_row)
            await sess.commit()


async def test_corporate_action_position_with_missing_bracket_row_rejected_at_commit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """FK enforcement prevents committing a position that references a non-existent
    bracket row — deferred FK on positions.bracket_id raises IntegrityError at COMMIT.
    This is the schema-level guard for the invariant that Phase 1's
    _cancel_bracket_for_corporate_action path previously enforced at the application layer.
    """
    from sqlalchemy.exc import IntegrityError

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Attempt to commit an OPEN position pointing at a bracket that does not exist.
    # _make_open_position uses bracket_id="brk-1" by default.
    position_row = position_record_to_row(_make_open_position(share_count=10.0))
    with pytest.raises(IntegrityError):
        async with factory() as sess:
            sess.add(position_row)
            await sess.commit()


async def test_short_entry_fill_raises_explicit_not_implemented(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A SELL-side fill against a PENDING position represents a short-open
    entry — narrowed out of Phase 1 v1. The dispatcher must surface a clear
    NotImplementedError naming the missing capability, not the cryptic
    "exit fill quantity exceeds open share count" leak from ``_apply_exit_fill``.
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(
            order_id="ord-short-entry",
            direction=OrderDirection.SELL_TO_OPEN,
            position_id="pos-1",
        ),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(
        factory,
        _make_unprocessed_fill(fill_id="fill-short-1", order_id="ord-short-entry"),
    )

    ctx, handle = await _open_handle(factory)
    try:
        with pytest.raises(NotImplementedError, match="SHORT entry"):
            await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    finally:
        await ctx.__aexit__(NotImplementedError, NotImplementedError("forced"), None)


async def test_phase1_stamps_completion_timestamp_on_invocation_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """After process_unprocessed_fills commits, the bound invocation row's
    phase1_completed_at must be a valid ISO-8601 UTC timestamp — the SQL
    repository's snapshot-isolation guard reads this column and raises
    RepositoryConsistencyError when it is NULL.
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    await process_unprocessed_fills(handle, config=_make_state_persistence_config())
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        row = await sess.get(InvocationRow, invocation_id)
        assert row is not None
        assert row.phase1_completed_at is not None
        # Must round-trip through fromisoformat (covers both Z-suffix and +00:00 forms).
        parsed = datetime.fromisoformat(row.phase1_completed_at)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# ALP-415: chronological fill + CA merge + reconciliation
# ---------------------------------------------------------------------------


def _alpaca_account_snapshot(*, cash: float = 100_000.0) -> TradeAccountSnapshot:
    """Build a typed ``TradeAccountSnapshot`` for the new entry-point signature."""
    return TradeAccountSnapshot(
        account_id="alp-account-1",
        cash=cash,
        equity=cash,
        buying_power=cash * 2.0,
        regt_buying_power=cash * 2.0,
        daytrading_buying_power=cash * 4.0,
        maintenance_margin=0.0,
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _alpaca_equity_snapshot(*, symbol: str = "AAPL", qty: float = 10.0) -> PositionSnapshot:
    """Build a typed ``PositionSnapshot`` for the new entry-point signature."""
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=qty,
        avg_entry_price=150.0,
        market_value=qty * 150.0,
        cost_basis=qty * 150.0,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
        current_price=150.0,
        side="long",
    )


async def test_summary_carries_reconciliation_alert_count(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``Phase1Summary`` exposes ``reconciliation_alerts`` and the count reflects
    one ``RECONCILIATION_ALERT`` per unexplained delta."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_open_position(share_count=10.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        ca_activities=(),
        # 10 shares local vs 9 shares Alpaca produces one position-delta alert;
        # cash mismatch produces a second.
        alpaca_positions=(_alpaca_equity_snapshot(qty=9.0),),
        alpaca_account=_alpaca_account_snapshot(cash=50_000.0),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.reconciliation_alerts == 2


async def test_fill_before_ca_reflects_pre_action_quantity_at_fill(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill timestamped before a CA's transaction_time integrates first.

    Setup: OPEN position with 10 shares. Add fill (BUY +5 shares) at T-30min.
    A SPLIT 2-for-1 CA at T-10min applies after. Post-state:
      * pre-CA share_count = 10 + 5 = 15 (entry+add fill applied first)
      * post-CA share_count = 15 * 2 = 30 (split applied second)
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        CorporateActionActivity,
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Open position with 10 shares + an entry order already filled.
    add_order = _make_pending_entry_order(
        order_id="ord-add-1",
        role=OrderRole.ADD_ENTRY,
        position_id="pos-1",
        quantity=5.0,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids = {entry_order.order_id, add_order.order_id}
    async with factory() as sess:
        sess.add(position_record_to_row(_make_open_position(share_count=10.0)))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(entry_order))
        sess.add(order_record_to_row(add_order))
        for oid in leg_order_ids:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, bracket_row.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Fill at T-30min (before CA at T-10min).
    fill_ts = _NOW - timedelta(minutes=30)
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-add-1",
            order_id="ord-add-1",
            fill_quantity=5.0,
            fill_price=150.0,
            fill_timestamp=fill_ts,
            remaining_quantity_after=0.0,
            order_status_after=OrderStatus.FILLED,
        ),
    )

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-merge-1",
        action_type=CorporateActionType.SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=2.0,  # 2-for-1 split.
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=10),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        ca_activities=(ca,),
        alpaca_positions=(),
        alpaca_account=None,
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # Fill applied first: 10 + 5 = 15. Then SPLIT 2x: 15 * 2 = 30.
        assert pos.details.share_count == pytest.approx(30.0)


async def test_fill_after_ca_reflects_post_action_quantity_at_fill(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill timestamped after a CA's transaction_time integrates after.

    Setup: OPEN position with 10 shares. SPLIT 2-for-1 CA at T-30min, then
    an ADD fill (+5 shares) at T-10min. Post-state:
      * post-CA share_count = 10 * 2 = 20
      * post-fill share_count = 20 + 5 = 25
    """
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        CorporateActionActivity,
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    add_order = _make_pending_entry_order(
        order_id="ord-add-2",
        role=OrderRole.ADD_ENTRY,
        position_id="pos-1",
        quantity=5.0,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids = {entry_order.order_id, add_order.order_id}
    async with factory() as sess:
        sess.add(position_record_to_row(_make_open_position(share_count=10.0)))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(entry_order))
        sess.add(order_record_to_row(add_order))
        for oid in leg_order_ids:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, bracket_row.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Fill at T-10min (after the CA at T-30min).
    fill_ts = _NOW - timedelta(minutes=10)
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-add-2",
            order_id="ord-add-2",
            fill_quantity=5.0,
            fill_price=75.0,  # post-split price.
            fill_timestamp=fill_ts,
            remaining_quantity_after=0.0,
            order_status_after=OrderStatus.FILLED,
        ),
    )

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-split-merge-2",
        action_type=CorporateActionType.SPLIT,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=2.0,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=30),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        ca_activities=(ca,),
        alpaca_positions=(),
        alpaca_account=None,
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        # Split applied first: 10 * 2 = 20. Then fill: 20 + 5 = 25.
        assert pos.details.share_count == pytest.approx(25.0)
