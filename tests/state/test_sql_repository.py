"""Tests for ``SqlPortfolioStateRepository`` (story 06 / ALP-364).

The SQL repository implements the 17-method ``PortfolioStateRepository``
Protocol against the SQLAlchemy tables shipped in stories 02b and 04a-04e.
Every test exercises the public Protocol surface — no internal state is
inspected — so the tests survive an internal refactor of the codecs or
query strategy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.execution.regt_margin_attribution import RegTExcessAggregates
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.activity_log import (
    ActivityLogEntry,
    EventGroup,
    EventSource,
    EventType,
    PMDecisionDetail,
    PMVerdict,
    PositionOpenedDetail,
    PositionOpenMechanism,
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
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
    ThesisResolutionCategory,
)
from alphamind.portfolio_state.repository import (
    PortfolioStateRepository,
    RepositoryConsistencyError,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.activity_log import (
    activity_log_entry_to_row,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    ProcessLifetimeRecord,
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    FillProcessingStatus,
    FillRecord,
    RegTMarginAttribution,
)
from alphamind.state.repository import (
    build_sql_portfolio_state_repository,
)
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.state.tables.fill_records_codec import (
    record_to_row as fill_record_to_row,
)
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

# ---------------------------------------------------------------------------
# Shared timestamps and identifiers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_PHASE1_AT = _NOW - timedelta(seconds=30)
_PRIOR_START = _NOW - timedelta(hours=1)
_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"
_PRIOR_INV_ID = "inv-2026-05-08T11:00:00Z-bbbb"
_PROCESS_ID = "proc-1"


# ---------------------------------------------------------------------------
# Engine + session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB.

    Tables materialize via ``Base.metadata.create_all`` against a sync engine
    (the codebase's standard workflow); reads/writes go through the async
    engine — same DB file, same pragmas via the same shared listener.
    """
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Record builders
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


def _make_active_risk_parameters(
    rule_value: float = 1000.0,
    parameter_change_flag: bool = False,
) -> ActiveRiskParameterSet:
    entry = ActiveRiskParameterEntry(
        rule_id="max_position_size_usd",
        rule_label="Max position size (USD)",
        value=rule_value,
        unit="USD",
        regime_multiplier_applied=1.0,
        base_value=rule_value,
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=parameter_change_flag,
        entries=(entry,),
        active_overlays=(),
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


def _make_invocation_record(
    invocation_id: str = _INV_ID,
    *,
    start_at: datetime = _NOW,
    phase1_completed_at: datetime | None = _PHASE1_AT,
    resolved_config_snapshot_path: str = "/tmp/provenance/inv/resolved.json",
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=start_at.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=(
            None
            if phase1_completed_at is None
            else phase1_completed_at.isoformat().replace("+00:00", "Z")
        ),
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
        resolved_config_snapshot_path=resolved_config_snapshot_path,
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
    ticker: str = "AAPL",
    realized_pnl_to_date_usd: float | None = None,
    status: PositionStatus = PositionStatus.OPEN,
    closed_at: datetime | None = None,
) -> PositionRecord:
    """Build a position record. closed_at is recorded on the latest fill if status=CLOSED."""
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=150.0,
    )
    fills: tuple[PositionFill, ...]
    if status == PositionStatus.PENDING:
        fills = ()
    else:
        fill_at = closed_at if closed_at is not None else _NOW - timedelta(hours=2)
        fills = (
            PositionFill(
                fill_timestamp=fill_at,
                fill_price=150.0,
                fill_quantity=10.0,
                slippage=0.01,
                fees=1.0,
            ),
        )
    entry_at = None if status == PositionStatus.PENDING else _NOW - timedelta(hours=3)
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=status,
        direction=Direction.LONG,
        entry_timestamp=entry_at,
        details=details,
        execution_history=fills,
        realized_pnl_to_date_usd=realized_pnl_to_date_usd,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_thesis_record(
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
    *,
    status: ThesisRecordStatus = ThesisRecordStatus.ACTIVE,
    resolution_timestamp: datetime | None = None,
    resolution_category: ThesisResolutionCategory | None = None,
    resolution_pnl_usd: float | None = None,
) -> ThesisRecord:
    component = ThesisComponent(
        component_id=f"{thesis_id}-entry",
        thesis_id=ThesisId(thesis_id),
        component_type=ThesisComponentType.ENTRY_RATIONALE,
        linked_bracket_leg_type=None,
        linked_bracket_leg_id=None,
        instrument_reference="AAPL",
        narrative="Entry rationale narrative",
        key_assumptions=(KeyAssumption(text="Earnings beat", outcome=None),),
        generation_timestamp=_NOW - timedelta(hours=4),
        resolution_outcome=(
            ThesisComponentOutcome.VALIDATED if status == ThesisRecordStatus.RESOLVED else None
        ),
        resolution_notes=None,
    )
    target = ThesisComponent(
        component_id=f"{thesis_id}-target",
        thesis_id=ThesisId(thesis_id),
        component_type=ThesisComponentType.TARGET_RATIONALE,
        linked_bracket_leg_type=None,
        linked_bracket_leg_id=None,
        instrument_reference="AAPL",
        narrative="Target rationale",
        key_assumptions=(),
        generation_timestamp=_NOW - timedelta(hours=4),
        resolution_outcome=(
            ThesisComponentOutcome.VALIDATED if status == ThesisRecordStatus.RESOLVED else None
        ),
        resolution_notes=None,
    )
    invalidation = ThesisComponent(
        component_id=f"{thesis_id}-invalid",
        thesis_id=ThesisId(thesis_id),
        component_type=ThesisComponentType.INVALIDATION_RATIONALE,
        linked_bracket_leg_type=None,
        linked_bracket_leg_id=None,
        instrument_reference="AAPL",
        narrative="Invalidation rationale",
        key_assumptions=(),
        generation_timestamp=_NOW - timedelta(hours=4),
        resolution_outcome=(
            ThesisComponentOutcome.VALIDATED if status == ThesisRecordStatus.RESOLVED else None
        ),
        resolution_notes=None,
    )
    generation_at = _NOW - timedelta(hours=4)
    time_expectation_hours = 24.0
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="AAPL momentum",
        key_catalyst="Q3 earnings beat",
        position_size_rationale="Sized at 5% conviction-3",
        components=(component, target, invalidation),
        status=status,
        generation_timestamp=generation_at,
        time_expectation_hours=time_expectation_hours,
        age_hours=4.0,
        expected_resolution_at=generation_at + timedelta(hours=time_expectation_hours),
        resolution_timestamp=resolution_timestamp,
        resolution_category=resolution_category,
        resolution_pnl_usd=resolution_pnl_usd,
        entry_fill_gap_usd=None,
    )


def _make_bracket_record(
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
) -> BracketRecord:
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
        entry_order_id=OrderId(f"{bracket_id}-ord-entry"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_pending_order(
    order_id: str = "ord-1",
    *,
    status: OrderStatus = OrderStatus.PENDING,
    bracket_id: str = "brk-1",
) -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=None,
        bracket_id=BracketId(bracket_id),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=status,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=10),
        last_update_timestamp=_NOW - timedelta(minutes=5),
        filled_quantity=0.0 if status == OrderStatus.PENDING else 4.0,
        avg_fill_price=None if status == OrderStatus.PENDING else 150.0,
        remaining_quantity=10.0 if status == OrderStatus.PENDING else 6.0,
        modification_count=0,
        originating_thesis_id=None,
        originating_pm_command_id=None,
        age_hours=0.0,
    )


def _make_cash_ledger() -> CashLedger:
    return CashLedger(
        current_cash_usd=10000.0,
        settled_cash_usd=9000.0,
        reserved_capital_usd=500.0,
        available_buying_power_usd=8500.0,
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
        current_drawdown_pct=2.5,
        equity_high_water_mark_usd=100000.0,
        drawdown_duration_hours=12.0,
        lifetime_max_drawdown_pct=5.0,
        intraday_drawdown_pct=0.0,
        daily_zone=RiskZone.NORMAL,
        cumulative_zone=RiskZone.NORMAL,
        cumulative_tier=None,
        drawdown_by_source_pct={},
    )


def _make_pm_decision_entry(
    entry_id: str = "entry-pm-1",
    *,
    invocation_id: str = _INV_ID,
    position_id: str | None = None,
    timestamp: datetime | None = None,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp or _NOW,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.COMMAND_EXECUTOR,
        detail=PMDecisionDetail(
            envelope_id="env-1",
            source_provenance_json={},
            evaluation_json={},
            modifications_json=[],
            resulting_command_ids=(),
            verdict=PMVerdict.APPROVE,
        ),
    )


def _make_position_opened_entry(
    entry_id: str,
    position_id: str,
    *,
    invocation_id: str = _INV_ID,
    timestamp: datetime | None = None,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp or _NOW,
        event_type=EventType.POSITION_OPENED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        position_id=position_id,
        order_id=None,
        thesis_id=None,
        source=EventSource.FILL_PROCESSOR,
        detail=PositionOpenedDetail(
            ticker=Symbol("AAPL"),
            direction="LONG",
            fill_price=price("150.0"),
            quantity=10.0,
            thesis_id=None,
            bracket_id=None,
            mechanism=PositionOpenMechanism.ORDER_FILL,
            parent_position_id=None,
        ),
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_minimal_invocation(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation: InvocationRecord | None = None,
) -> None:
    """Persist a process_lifetime + invocation row pair so FKs satisfy."""
    inv = invocation or _make_invocation_record()
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(inv))
        await sess.commit()


async def _seed_minimal_invocation_extra(
    factory: async_sessionmaker[AsyncSession],
    invocation: InvocationRecord,
) -> None:
    """Persist an additional invocation row reusing the existing process_lifetime."""
    async with factory() as sess:
        sess.add(invocation_record_to_row(invocation))
        await sess.commit()


async def _seed_position(
    factory: async_sessionmaker[AsyncSession],
    record: PositionRecord,
) -> None:
    async with factory() as sess:
        sess.add(position_record_to_row(record))
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


async def _seed_order(
    factory: async_sessionmaker[AsyncSession],
    record: OrderRecord,
) -> None:
    async with factory() as sess:
        sess.add(order_record_to_row(record))
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


def _make_regt_attribution(regt_excess_over_pm: float) -> RegTMarginAttribution:
    """RegT attribution payload exercising only ``regt_excess_over_pm`` (the summed field)."""
    return RegTMarginAttribution(
        regt_margin_before=signed_money(0.0),
        regt_margin_after=signed_money(0.0),
        regt_marginal_consumption=signed_money(0.0),
        pm_equivalent_before=signed_money(0.0),
        pm_equivalent_after=signed_money(0.0),
        pm_marginal_consumption=signed_money(0.0),
        regt_excess_over_pm=signed_money(regt_excess_over_pm),
        pm_model_version="ibkr_mirror_v1_2025Q3",
    )


def _make_fill_record_for_aggregator(
    fill_id: str,
    *,
    order_id: str = "ord-1",
    fill_timestamp: datetime,
    fill_price: float = 150.0,
    processed: bool = True,
    regt_attribution: RegTMarginAttribution | None = None,
) -> FillRecord:
    """Build a fill record exercising only the columns the aggregator query reads.

    ``processed=False`` builds an ``unprocessed`` row (no ``processing_*``
    metadata) so the aggregator's defense-in-depth status filter has
    something to exclude.
    """
    if processed:
        status = FillProcessingStatus.PROCESSED
        processing_invocation_id: str | None = _INV_ID
        processing_timestamp: datetime | None = fill_timestamp
    else:
        status = FillProcessingStatus.UNPROCESSED
        processing_invocation_id = None
        processing_timestamp = None
    return FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        fill_timestamp=fill_timestamp,
        fill_price=price(fill_price),
        fill_quantity=1.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=signed_money(0.0),
        fees_usd=money(0.0),
        execution_venue=None,
        gateway_reference=None,
        persistence_timestamp=fill_timestamp,
        processing_status=status,
        processing_invocation_id=processing_invocation_id,
        processing_timestamp=processing_timestamp,
        regt_attribution=regt_attribution,
        live_execution_estimate=None,
    )


async def _seed_fill_records(
    factory: async_sessionmaker[AsyncSession],
    *fills: FillRecord,
) -> None:
    """Seed fills referencing ord-1 (which the helper auto-creates as a stub)."""
    from tests.state._fk_substrate import (
        stub_bracket_row,
        stub_order_row,
        stub_position_row,
    )

    if not fills:
        return
    order_ids = {f.order_id for f in fills}
    async with factory() as sess:
        for oid in sorted(order_ids):
            stub_pos_id = f"stub-pos-{oid}"
            stub_brk_id = f"stub-brk-{oid}"
            sess.add(stub_position_row(stub_pos_id))
            sess.add(stub_order_row(oid, stub_brk_id))
            sess.add(stub_bracket_row(stub_brk_id, stub_pos_id, oid))
        await sess.flush()
        for fill in fills:
            sess.add(fill_record_to_row(fill))
        await sess.commit()


async def _seed_activity_log_entry(
    factory: async_sessionmaker[AsyncSession],
    entry: ActivityLogEntry,
) -> None:
    async with factory() as sess:
        sess.add(activity_log_entry_to_row(entry))
        await sess.commit()


async def _seed_position_cluster(
    factory: async_sessionmaker[AsyncSession],
    position: PositionRecord,
    thesis: ThesisRecord,
    bracket: BracketRecord,
    *extra_orders: OrderRecord,
) -> None:
    """Seed position + thesis + bracket in one deferred-FK transaction.

    Pass additional OrderRecord objects via *extra_orders to include them in the
    same atomic commit (needed when the bracket's entry_order_id or leg order_ids
    reference specific existing order rows rather than stubs).
    """
    from tests.state._fk_substrate import stub_order_row

    thesis_parent, component_rows = thesis_record_to_rows(thesis)
    bracket_parent, leg_rows = bracket_record_to_rows(bracket)

    extra_order_ids: set[str] = {rec.order_id for rec in extra_orders}
    stub_ids_needed: list[str] = [bracket_parent.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_parent)
        for crow in component_rows:
            sess.add(crow)
        for rec in extra_orders:
            sess.add(order_record_to_row(rec))
        for oid in stub_ids_needed:
            if oid not in extra_order_ids:
                sess.add(stub_order_row(oid, bracket.bracket_id))
                extra_order_ids.add(oid)
        sess.add(bracket_parent)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _seed_stub_positions(
    factory: async_sessionmaker[AsyncSession],
    *position_ids: str,
) -> None:
    """Seed minimal stub position rows so FKs from theses/brackets/activity_log resolve."""
    from tests.state._fk_substrate import stub_position_row

    async with factory() as sess:
        for pid in position_ids:
            sess.add(stub_position_row(pid))
        await sess.commit()


async def _seed_thesis_with_stub_position(
    factory: async_sessionmaker[AsyncSession],
    record: ThesisRecord,
) -> None:
    """Seed thesis + minimal stub position in one deferred-FK transaction."""
    from tests.state._fk_substrate import stub_position_row

    parent_row, child_rows = thesis_record_to_rows(record)
    async with factory() as sess:
        sess.add(stub_position_row(record.position_id))
        sess.add(parent_row)
        for crow in child_rows:
            sess.add(crow)
        await sess.commit()


async def _seed_bracket_cluster(
    factory: async_sessionmaker[AsyncSession],
    record: BracketRecord,
) -> None:
    """Seed bracket + position stub + entry-order stub in one deferred-FK transaction.

    All three reference each other (bracket.position_id → positions, bracket.entry_order_id
    → orders, orders.bracket_id → brackets) so they must commit together.
    bracket_legs.bracket_id is non-deferred — flush bracket before adding legs.
    """
    from tests.state._fk_substrate import (
        stub_order_row,
        stub_position_row,
    )

    parent_row, leg_rows = bracket_record_to_rows(record)
    stub_ids_needed: list[str] = [parent_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]
    async with factory() as sess:
        sess.add(stub_position_row(record.position_id))
        for oid in stub_ids_needed:
            sess.add(stub_order_row(oid, record.bracket_id))
        sess.add(parent_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _seed_order_cluster(
    factory: async_sessionmaker[AsyncSession],
    *records: OrderRecord,
) -> None:
    """Seed multiple orders + one position stub + one bracket stub per unique bracket_id.

    All orders in a single transaction so deferred FKs (orders.bracket_id → brackets,
    brackets.position_id → positions, brackets.entry_order_id → orders) resolve at COMMIT.
    The first order for each bracket_id is used as the entry_order_id on the stub bracket.
    """
    from tests.state._fk_substrate import (
        stub_bracket_row,
        stub_position_row,
    )

    # Group by bracket_id; first order per bracket becomes entry_order_id.
    bracket_entry: dict[str, str] = {}
    for rec in records:
        if rec.bracket_id not in bracket_entry:
            bracket_entry[rec.bracket_id] = rec.order_id

    async with factory() as sess:
        for bkt_id, entry_oid in bracket_entry.items():
            stub_pos_id = f"stub-pos-{bkt_id}"
            sess.add(stub_position_row(stub_pos_id))
            sess.add(stub_bracket_row(bkt_id, stub_pos_id, entry_oid))
        for rec in records:
            sess.add(order_record_to_row(rec))
        await sess.commit()


# ---------------------------------------------------------------------------
# Repository factory helper
# ---------------------------------------------------------------------------


def _build_repo(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str = _INV_ID,
    active_risk_parameters: ActiveRiskParameterSet | None = None,
    prior_active_risk_parameters_for_path: dict[str, ActiveRiskParameterSet] | None = None,
) -> PortfolioStateRepository:
    """Build a SQL-backed PortfolioStateRepository for the given invocation."""
    arp = (
        active_risk_parameters
        if active_risk_parameters is not None
        else _make_active_risk_parameters()
    )

    def _provider() -> ActiveRiskParameterSet:
        return arp

    path_map = prior_active_risk_parameters_for_path or {}

    def _prior_provider(snapshot_path: str) -> ActiveRiskParameterSet:
        return path_map[snapshot_path]

    return build_sql_portfolio_state_repository(
        session_factory=factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=_provider,
        prior_active_risk_parameters_provider=_prior_provider,
        config=_make_state_persistence_config(),
    )


# ---------------------------------------------------------------------------
# Tracer bullet — Tier 1 read of open positions
# ---------------------------------------------------------------------------


async def test_get_open_positions_returns_only_open_status(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    open_pos = _make_open_position(position_id=PositionId("pos-open"), status=PositionStatus.OPEN)
    pending_pos = _make_open_position(
        position_id=PositionId("pos-pending"), status=PositionStatus.PENDING
    )
    closed_pos = _make_open_position(
        position_id=PositionId("pos-closed"),
        status=PositionStatus.CLOSED,
        realized_pnl_to_date_usd=100.0,
        closed_at=_NOW - timedelta(hours=1),
    )
    await _seed_position(factory, open_pos)
    await _seed_position(factory, pending_pos)
    await _seed_position(factory, closed_pos)

    repo = _build_repo(factory)
    result = repo.get_open_positions()

    assert len(result) == 1
    assert result[0].position_id == "pos-open"
    assert result[0] == open_pos


async def test_get_pending_positions_returns_only_pending_status(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    open_pos = _make_open_position(position_id=PositionId("pos-open"), status=PositionStatus.OPEN)
    pending_pos = _make_open_position(
        position_id=PositionId("pos-pending"), status=PositionStatus.PENDING
    )
    await _seed_position(factory, open_pos)
    await _seed_position(factory, pending_pos)

    repo = _build_repo(factory)
    result = repo.get_pending_positions()

    assert len(result) == 1
    assert result[0].position_id == "pos-pending"
    assert result[0] == pending_pos


async def test_get_active_theses_returns_active_with_components(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    active = _make_thesis_record(
        thesis_id=ThesisId("thesis-active"), position_id=PositionId("pos-1")
    )
    resolved = _make_thesis_record(
        thesis_id=ThesisId("thesis-resolved"),
        position_id=PositionId("pos-2"),
        status=ThesisRecordStatus.RESOLVED,
        resolution_timestamp=_NOW - timedelta(hours=1),
        resolution_category=ThesisResolutionCategory.VALIDATED,
        resolution_pnl_usd=250.0,
    )
    await _seed_thesis_with_stub_position(factory, active)
    await _seed_thesis_with_stub_position(factory, resolved)

    repo = _build_repo(factory)
    result = repo.get_active_theses()

    assert len(result) == 1
    assert result[0].thesis_id == "thesis-active"
    assert len(result[0].components) == 3
    assert result[0] == active


async def test_get_recent_thesis_resolutions_projects_resolved(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    active = _make_thesis_record(
        thesis_id=ThesisId("thesis-active"), position_id=PositionId("pos-1")
    )
    resolved = _make_thesis_record(
        thesis_id=ThesisId("thesis-resolved"),
        position_id=PositionId("pos-2"),
        status=ThesisRecordStatus.RESOLVED,
        resolution_timestamp=_NOW - timedelta(hours=1),
        resolution_category=ThesisResolutionCategory.VALIDATED,
        resolution_pnl_usd=250.0,
    )
    await _seed_thesis_with_stub_position(factory, active)
    await _seed_thesis_with_stub_position(factory, resolved)

    repo = _build_repo(factory)
    result = repo.get_recent_thesis_resolutions(lookback_trading_days=5)

    assert len(result) == 1
    assert result[0].thesis_id == "thesis-resolved"
    assert result[0].resolution_category == ThesisResolutionCategory.VALIDATED
    assert result[0].resolution_pnl_usd == 250.0
    assert result[0].expected_duration_hours == resolved.time_expectation_hours
    # Active duration computed from generation_timestamp → resolution_timestamp.
    assert resolved.resolution_timestamp is not None
    expected_active = (
        resolved.resolution_timestamp - resolved.generation_timestamp
    ).total_seconds() / 3600.0
    assert result[0].active_duration_hours == expected_active
    # All three components VALIDATED in the helper → all three component
    # outcomes must round-trip into the projection.
    assert len(result[0].component_outcomes) == 3


async def test_get_cash_ledger_returns_singleton(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    seeded = _make_cash_ledger()
    await _seed_cash_ledger(factory, seeded)

    repo = _build_repo(factory)
    result = repo.get_cash_ledger()

    assert result.current_cash_usd == seeded.current_cash_usd
    assert result.settled_cash_usd == seeded.settled_cash_usd
    assert result.reserved_capital_usd == seeded.reserved_capital_usd


async def test_get_cash_ledger_missing_singleton_raises_consistency_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    with pytest.raises(RepositoryConsistencyError):
        repo.get_cash_ledger()


async def test_get_regt_excess_aggregates_rejects_naive_datetime(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """``get_regt_excess_aggregates`` raises ``ValueError`` on a naive ``now``.

    Naive datetimes produce timezone-implicit ISO strings whose lex order
    against stored UTC timestamps would silently misclassify rows at the
    trailing-window cutoffs; the guard fires before any query runs.
    """
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    naive_now = datetime(2026, 5, 8, 12, 0, 0)  # noqa: DTZ001 — deliberate; exercises the guard
    with pytest.raises(ValueError, match="timezone-aware"):
        repo.get_regt_excess_aggregates(naive_now)


async def test_get_regt_excess_aggregates_empty_table_returns_zeros(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    result = repo.get_regt_excess_aggregates(_NOW)

    assert result == RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )


async def test_get_regt_excess_aggregates_null_attribution_contributes_zero(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    fill_no_attribution = _make_fill_record_for_aggregator(
        "fill-no-attr",
        fill_timestamp=_NOW - timedelta(days=1),
        regt_attribution=None,
    )
    await _seed_fill_records(factory, fill_no_attribution)

    repo = _build_repo(factory)
    result = repo.get_regt_excess_aggregates(_NOW)

    assert result == RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )


async def test_get_regt_excess_aggregates_unprocessed_fill_excluded(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    # Defense-in-depth: production write paths never produce this state, but
    # the aggregator query must filter on processing_status='processed' so
    # that an unprocessed-with-attribution row contributes nothing.
    unprocessed = _make_fill_record_for_aggregator(
        "fill-unprocessed",
        fill_timestamp=_NOW - timedelta(days=1),
        processed=False,
        regt_attribution=_make_regt_attribution(regt_excess_over_pm=42.0),
    )
    await _seed_fill_records(factory, unprocessed)

    repo = _build_repo(factory)
    result = repo.get_regt_excess_aggregates(_NOW)

    assert result == RegTExcessAggregates(
        trailing_30d_usd=0.0,
        trailing_90d_usd=0.0,
        lifetime_usd=0.0,
    )


async def test_get_regt_excess_aggregates_trailing_windows_calendar_days(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    # Distinct fill_price values keep the dedupe constraint satisfied.
    fill_recent = _make_fill_record_for_aggregator(
        "fill-1d",
        fill_timestamp=_NOW - timedelta(days=1),
        fill_price=150.01,
        regt_attribution=_make_regt_attribution(regt_excess_over_pm=10.0),
    )
    fill_outside_30 = _make_fill_record_for_aggregator(
        "fill-31d",
        fill_timestamp=_NOW - timedelta(days=31),
        fill_price=150.02,
        regt_attribution=_make_regt_attribution(regt_excess_over_pm=20.0),
    )
    fill_outside_90 = _make_fill_record_for_aggregator(
        "fill-91d",
        fill_timestamp=_NOW - timedelta(days=91),
        fill_price=150.03,
        regt_attribution=_make_regt_attribution(regt_excess_over_pm=30.0),
    )
    await _seed_fill_records(factory, fill_recent, fill_outside_30, fill_outside_90)

    repo = _build_repo(factory)
    result = repo.get_regt_excess_aggregates(_NOW)

    # 30d: only fill-1d.
    # 90d: fill-1d + fill-31d.
    # Lifetime: all three.
    assert result.trailing_30d_usd == pytest.approx(10.0, abs=1e-9)
    assert result.trailing_90d_usd == pytest.approx(30.0, abs=1e-9)
    assert result.lifetime_usd == pytest.approx(60.0, abs=1e-9)


async def test_get_regt_excess_aggregates_sums_regt_excess_over_pm(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    seeded_excesses = [3.5, 7.25, 11.125]
    fills = tuple(
        _make_fill_record_for_aggregator(
            f"fill-sum-{i}",
            fill_timestamp=_NOW - timedelta(days=1),
            fill_price=150.0 + 0.01 * i,
            regt_attribution=_make_regt_attribution(regt_excess_over_pm=excess),
        )
        for i, excess in enumerate(seeded_excesses)
    )
    await _seed_fill_records(factory, *fills)

    repo = _build_repo(factory)
    result = repo.get_regt_excess_aggregates(_NOW)

    expected_sum = sum(seeded_excesses)
    assert result.trailing_30d_usd == pytest.approx(expected_sum, abs=1e-9)
    assert result.trailing_90d_usd == pytest.approx(expected_sum, abs=1e-9)
    assert result.lifetime_usd == pytest.approx(expected_sum, abs=1e-9)


async def test_get_pending_orders_returns_pending_and_partially_filled(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    pending = _make_pending_order(order_id=OrderId("ord-pending"), status=OrderStatus.PENDING)
    partially = _make_pending_order(
        order_id=OrderId("ord-partial"), status=OrderStatus.PARTIALLY_FILLED
    )
    filled = _make_pending_order(order_id=OrderId("ord-filled"), status=OrderStatus.FILLED)
    cancelled = _make_pending_order(order_id=OrderId("ord-cancelled"), status=OrderStatus.CANCELLED)
    await _seed_order_cluster(factory, pending, partially, filled, cancelled)

    repo = _build_repo(factory)
    result = repo.get_pending_orders()

    assert {o.order_id for o in result} == {"ord-pending", "ord-partial"}


async def test_get_brackets_for_positions_returns_bracket_with_legs(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    bracket = _make_bracket_record(bracket_id=BracketId("brk-1"), position_id=PositionId("pos-1"))
    other = _make_bracket_record(bracket_id=BracketId("brk-2"), position_id=PositionId("pos-2"))
    await _seed_bracket_cluster(factory, bracket)
    await _seed_bracket_cluster(factory, other)

    repo = _build_repo(factory)
    result = repo.get_brackets_for_positions(position_ids=("pos-1",))

    assert len(result) == 1
    assert result[0].bracket_id == "brk-1"
    assert len(result[0].protective_legs) == 1
    assert result[0] == bracket


async def test_get_brackets_for_positions_empty_input_returns_empty(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    bracket = _make_bracket_record()
    await _seed_bracket_cluster(factory, bracket)

    repo = _build_repo(factory)
    result = repo.get_brackets_for_positions(position_ids=())

    assert result == ()


# ---------------------------------------------------------------------------
# Tier 2 — activity log delegations
# ---------------------------------------------------------------------------


async def test_get_intra_invocation_changelog_filters_by_invocation_id(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    other_inv = _make_invocation_record(invocation_id=_PRIOR_INV_ID, start_at=_PRIOR_START)
    await _seed_minimal_invocation_extra(factory, other_inv)
    entry_current = _make_pm_decision_entry(entry_id="entry-current")
    entry_prior = _make_pm_decision_entry(
        entry_id="entry-prior", invocation_id=_PRIOR_INV_ID, timestamp=_PRIOR_START
    )
    await _seed_activity_log_entry(factory, entry_current)
    await _seed_activity_log_entry(factory, entry_prior)

    repo = _build_repo(factory)
    result = repo.get_intra_invocation_changelog(invocation_id=_INV_ID)

    assert {e.entry_id for e in result} == {"entry-current"}


async def test_get_recent_pm_decision_log_returns_pm_decisions_in_window(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    other_inv = _make_invocation_record(invocation_id=_PRIOR_INV_ID, start_at=_PRIOR_START)
    await _seed_minimal_invocation_extra(factory, other_inv)
    await _seed_stub_positions(factory, "pos-1")
    pm_current = _make_pm_decision_entry(entry_id="pm-current")
    pm_prior = _make_pm_decision_entry(
        entry_id="pm-prior", invocation_id=_PRIOR_INV_ID, timestamp=_PRIOR_START
    )
    non_pm = _make_position_opened_entry(entry_id="opened-1", position_id=PositionId("pos-1"))
    await _seed_activity_log_entry(factory, pm_current)
    await _seed_activity_log_entry(factory, pm_prior)
    await _seed_activity_log_entry(factory, non_pm)

    repo = _build_repo(factory)
    result = repo.get_recent_pm_decision_log(sliding_window_invocations=5)

    assert {e.entry_id for e in result} == {"pm-current", "pm-prior"}


async def test_get_position_modification_trail_groups_by_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    await _seed_stub_positions(factory, "pos-1", "pos-2")
    e1 = _make_position_opened_entry(entry_id="pos-1-open", position_id=PositionId("pos-1"))
    e2 = _make_position_opened_entry(entry_id="pos-2-open", position_id=PositionId("pos-2"))
    await _seed_activity_log_entry(factory, e1)
    await _seed_activity_log_entry(factory, e2)

    repo = _build_repo(factory)
    result = repo.get_position_modification_trail(position_ids=("pos-1", "pos-unknown"))

    assert set(result.keys()) == {"pos-1"}
    assert len(result["pos-1"]) == 1
    assert result["pos-1"][0].entry_id == "pos-1-open"


async def test_get_position_modification_trail_empty_input_returns_empty(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    result = repo.get_position_modification_trail(position_ids=())

    assert result == {}


# ---------------------------------------------------------------------------
# Invocation scaffolding
# ---------------------------------------------------------------------------


async def test_get_current_invocation_metadata_returns_committed_metadata(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    result = repo.get_current_invocation_metadata()

    assert result.invocation_id == _INV_ID
    assert result.phase1_committed_at == _PHASE1_AT
    # ``pipeline_invocation_started_at`` is left None at snapshot assembly
    # time per the design (see ``PortfolioStateSnapshot`` field docstring
    # + the archived 04a-master-snapshot spec). Populating it from the
    # row's ``start_at`` violates the
    # ``pipeline_invocation_started_at >= snapshot_assembled_at`` ordering
    # invariant when the snapshot is assembled mid-invocation (which it
    # is under the ALP-449 three-tx model).
    assert result.pipeline_invocation_started_at is None


async def test_get_current_invocation_metadata_phase1_uncommitted_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(
        factory, invocation=_make_invocation_record(phase1_completed_at=None)
    )

    repo = _build_repo(factory)
    with pytest.raises(RepositoryConsistencyError):
        repo.get_current_invocation_metadata()


async def test_get_current_invocation_metadata_missing_row_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    # Seed only the process_lifetime — no invocation row.
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.commit()

    repo = _build_repo(factory)
    with pytest.raises(RepositoryConsistencyError):
        repo.get_current_invocation_metadata()


async def test_get_prior_invocation_context_first_invocation_returns_none(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    result = repo.get_prior_invocation_context()

    assert result.prior_invocation_id is None
    assert result.prior_active_risk_parameters is None
    assert result.prior_phase1_committed_at is None


async def test_get_prior_invocation_context_returns_most_recent_prior(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    prior_path = "/tmp/provenance/prior/resolved.json"
    prior_inv = _make_invocation_record(
        invocation_id=_PRIOR_INV_ID,
        start_at=_PRIOR_START,
        resolved_config_snapshot_path=prior_path,
    )
    await _seed_minimal_invocation(factory, invocation=prior_inv)
    await _seed_minimal_invocation_extra(factory, _make_invocation_record())

    prior_params = _make_active_risk_parameters(rule_value=900.0)
    repo = _build_repo(
        factory,
        prior_active_risk_parameters_for_path={prior_path: prior_params},
    )
    result = repo.get_prior_invocation_context()

    assert result.prior_invocation_id == _PRIOR_INV_ID
    assert result.prior_active_risk_parameters == prior_params
    assert result.prior_phase1_committed_at == _PHASE1_AT


# ---------------------------------------------------------------------------
# Tier 3 — derived/aggregate methods
# ---------------------------------------------------------------------------


async def test_get_drawdown_state_returns_singleton(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    seeded = _make_drawdown_state()
    await _seed_drawdown_state(factory, seeded)

    repo = _build_repo(factory)
    result = repo.get_drawdown_state()

    assert result.equity_high_water_mark_usd == seeded.equity_high_water_mark_usd
    assert result.current_drawdown_pct == seeded.current_drawdown_pct
    assert result.drawdown_duration_hours == seeded.drawdown_duration_hours
    assert result.lifetime_max_drawdown_pct == seeded.lifetime_max_drawdown_pct


async def test_get_drawdown_state_missing_singleton_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    with pytest.raises(RepositoryConsistencyError):
        repo.get_drawdown_state()


async def test_get_portfolio_pnl_inputs_aggregates_realized_over_closed(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)
    await _seed_position(
        factory,
        _make_open_position(
            position_id=PositionId("closed-1"),
            status=PositionStatus.CLOSED,
            realized_pnl_to_date_usd=200.0,
            closed_at=_NOW - timedelta(hours=1),
        ),
    )
    await _seed_position(
        factory,
        _make_open_position(
            position_id=PositionId("closed-2"),
            status=PositionStatus.CLOSED,
            realized_pnl_to_date_usd=-50.0,
            closed_at=_NOW - timedelta(hours=2),
        ),
    )
    # Open position should NOT contribute to realized PnL aggregation.
    await _seed_position(
        factory, _make_open_position(position_id=PositionId("open-1"), status=PositionStatus.OPEN)
    )

    repo = _build_repo(factory)
    result = repo.get_portfolio_pnl_inputs()

    assert result.cumulative_realized_pnl_usd == 150.0
    assert result.win_rate_pct is None
    assert set(result.rolling_realized_pnl.keys()) == {"1d", "3d", "5d", "20d"}


async def test_get_thesis_quality_aggregates_returns_empty_default(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    result = repo.get_thesis_quality_aggregates()

    assert result.resolution_counts_by_window == ()
    assert result.signal_hit_rates == ()
    assert result.performance_attribution == ()


async def test_get_active_risk_parameters_invokes_provider_once(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    invocation_count = 0

    def _provider() -> ActiveRiskParameterSet:
        nonlocal invocation_count
        invocation_count += 1
        return _make_active_risk_parameters(rule_value=2000.0)

    def _prior_provider(snapshot_path: str) -> ActiveRiskParameterSet:
        return _make_active_risk_parameters()

    repo = build_sql_portfolio_state_repository(
        session_factory=factory,
        invocation_id=_INV_ID,
        active_risk_parameters_provider=_provider,
        prior_active_risk_parameters_provider=_prior_provider,
        config=_make_state_persistence_config(),
    )
    result = repo.get_active_risk_parameters()

    assert invocation_count == 1
    assert result.entries[0].value == 2000.0


async def test_get_risk_budget_consumption_returns_empty_passthrough(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    result = repo.get_risk_budget_consumption()

    assert result.entries == ()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


async def test_repository_isinstance_portfolio_state_repository(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    _, factory = db
    await _seed_minimal_invocation(factory)

    repo = _build_repo(factory)
    assert isinstance(repo, PortfolioStateRepository)


# ---------------------------------------------------------------------------
# Parity with StubPortfolioStateRepository
# ---------------------------------------------------------------------------


class _ParityFixture:
    """Records seeded into both the SQL store and the StubRepository fixture."""

    open_pos: PositionRecord
    pending_pos: PositionRecord
    closed_pos: PositionRecord
    active_thesis: ThesisRecord
    bracket: BracketRecord
    pending_order: OrderRecord
    pm_entry: ActivityLogEntry
    pos_entry: ActivityLogEntry

    def __init__(self) -> None:
        self.open_pos = _make_open_position(
            position_id=PositionId("pos-1"), status=PositionStatus.OPEN
        )
        self.pending_pos = _make_open_position(
            position_id=PositionId("pos-2"), status=PositionStatus.PENDING
        )
        self.closed_pos = _make_open_position(
            position_id=PositionId("pos-3"),
            status=PositionStatus.CLOSED,
            realized_pnl_to_date_usd=120.0,
            closed_at=_NOW - timedelta(hours=1),
        )
        self.active_thesis = _make_thesis_record(
            thesis_id=ThesisId("thesis-active"), position_id=PositionId("pos-1")
        )
        self.bracket = _make_bracket_record(
            bracket_id=BracketId("brk-1"), position_id=PositionId("pos-1")
        )
        self.pending_order = _make_pending_order(
            order_id=OrderId("ord-1"), status=OrderStatus.PENDING
        )
        self.pm_entry = _make_pm_decision_entry(entry_id="pm-1")
        self.pos_entry = _make_position_opened_entry(
            entry_id="pos-1-open", position_id=PositionId("pos-1")
        )


async def _seed_parity_fixture(
    factory: async_sessionmaker[AsyncSession],
    fx: _ParityFixture,
) -> None:
    """Seed the parity fixture state into the SQL store.

    Seeding order respects FK dependencies:
    1. Positions first — thesis_id/bracket_id are NULL, so no cyclic deps.
    2. Thesis — requires position (committed above).
    3. Bracket cluster — bracket + entry-order stub committed atomically.
    4. Additional order (the "pending_order" for queries) — uses the bracket
       that was just committed; must seed in same tx or after bracket commit.
    5. Cash, drawdown, activity log — no FK on these (or position already committed).
    """
    await _seed_minimal_invocation(factory)
    # Step 1: Positions (thesis_id=None, bracket_id=None → no cyclic FK)
    await _seed_position(factory, fx.open_pos)
    await _seed_position(factory, fx.pending_pos)
    await _seed_position(factory, fx.closed_pos)
    # Step 2: Thesis (position already committed)
    await _seed_thesis(factory, fx.active_thesis)
    # Step 3 + 4: Bracket + the test's pending_order in one transaction.
    # bracket.position_id=pos-1 (committed), bracket.entry_order_id needs a stub,
    # pending_order.bracket_id=brk-1 needs bracket (committed in same tx).
    from tests.state._fk_substrate import stub_order_row

    bracket_parent, leg_rows = bracket_record_to_rows(fx.bracket)
    stub_ids_needed: list[str] = [bracket_parent.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]
    seeded_order_ids: set[str] = {fx.pending_order.order_id}
    async with factory() as sess:
        for oid in stub_ids_needed:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, fx.bracket.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(order_record_to_row(fx.pending_order))
        sess.add(bracket_parent)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()
    # Step 5: Cash, drawdown, activity log
    await _seed_cash_ledger(factory, _make_cash_ledger())
    await _seed_drawdown_state(factory, _make_drawdown_state())
    await _seed_activity_log_entry(factory, fx.pm_entry)
    await _seed_activity_log_entry(factory, fx.pos_entry)


async def test_parity_with_stub_over_same_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Build the same fixture state via SQL inserts and a RepositoryFixture;
    assert every Tier 1 / Tier 2 / Tier 3 method returns the same shape."""
    from alphamind.portfolio_state.repository import (
        RepositoryFixture,
        StubPortfolioStateRepository,
    )

    _, factory = db
    fx = _ParityFixture()
    await _seed_parity_fixture(factory, fx)
    sql_repo = _build_repo(factory)

    # Stub side — mirror the SQL state. Tier-3 derived values are read from
    # the SQL repo so the comparison is one-shot (no clock-induced drift on
    # the placeholder thesis-quality aggregate's ``as_of_timestamp``).
    sql_drawdown = sql_repo.get_drawdown_state()
    sql_cash = sql_repo.get_cash_ledger()
    pnl_inputs = sql_repo.get_portfolio_pnl_inputs()
    tqa = sql_repo.get_thesis_quality_aggregates()
    arp = sql_repo.get_active_risk_parameters()
    rb = sql_repo.get_risk_budget_consumption()
    current_meta = sql_repo.get_current_invocation_metadata()
    prior_ctx = sql_repo.get_prior_invocation_context()

    fixture = RepositoryFixture(
        open_positions=(fx.open_pos,),
        pending_positions=(fx.pending_pos,),
        drawdown_state=sql_drawdown,
        portfolio_pnl_inputs=pnl_inputs,
        active_theses=(fx.active_thesis,),
        recent_thesis_resolutions=(),
        cash_ledger=sql_cash,
        pending_orders=(fx.pending_order,),
        risk_budget=rb,
        active_risk_parameters=arp,
        intra_invocation_changelog=(fx.pm_entry, fx.pos_entry),
        recent_pm_decision_log=(fx.pm_entry,),
        position_modification_trail={"pos-1": (fx.pos_entry,)},
        thesis_quality_aggregates=tqa,
        brackets=(fx.bracket,),
        current_invocation_metadata=current_meta,
        prior_invocation_context=prior_ctx,
    )
    stub_repo = StubPortfolioStateRepository(fixture)

    # Tier 1
    assert sql_repo.get_open_positions() == stub_repo.get_open_positions()
    assert sql_repo.get_pending_positions() == stub_repo.get_pending_positions()
    assert sql_repo.get_active_theses() == stub_repo.get_active_theses()
    assert sql_repo.get_cash_ledger() == stub_repo.get_cash_ledger()
    assert sql_repo.get_pending_orders() == stub_repo.get_pending_orders()
    assert sql_repo.get_brackets_for_positions(
        position_ids=("pos-1",)
    ) == stub_repo.get_brackets_for_positions(position_ids=("pos-1",))

    # Tier 2
    assert sql_repo.get_intra_invocation_changelog(
        invocation_id=_INV_ID
    ) == stub_repo.get_intra_invocation_changelog(invocation_id=_INV_ID)
    assert {
        e.entry_id for e in sql_repo.get_recent_pm_decision_log(sliding_window_invocations=5)
    } == {e.entry_id for e in stub_repo.get_recent_pm_decision_log(sliding_window_invocations=5)}
    assert sql_repo.get_position_modification_trail(
        position_ids=("pos-1",)
    ) == stub_repo.get_position_modification_trail(position_ids=("pos-1",))

    # Tier 3 (compare cached SQL values against stub fixture mirrors)
    assert sql_drawdown == stub_repo.get_drawdown_state()
    assert pnl_inputs == stub_repo.get_portfolio_pnl_inputs()
    assert tqa == stub_repo.get_thesis_quality_aggregates()
    assert arp == stub_repo.get_active_risk_parameters()
    assert rb == stub_repo.get_risk_budget_consumption()

    # Invocation scaffolding
    assert current_meta == stub_repo.get_current_invocation_metadata()
    assert prior_ctx == stub_repo.get_prior_invocation_context()


# ---------------------------------------------------------------------------
# End-to-end: assemble_snapshot against the SQL repo
# ---------------------------------------------------------------------------


async def test_assemble_snapshot_against_sql_repo_produces_populated_snapshot(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """End-to-end: feed the SQL repo into ``assemble_snapshot`` and verify
    each snapshot category lands in the resulting ``PortfolioStateSnapshot``."""
    from alphamind.portfolio_state import PortfolioStateConfig
    from alphamind.portfolio_state.assembler import assemble_snapshot
    from alphamind.portfolio_state.pricing import (
        PriceQuote,
        PriceSource,
        StubCurrentPriceProvider,
    )

    _, factory = db

    # Seed a one-position portfolio with a thesis + bracket + cash + drawdown.
    # Positions seeded first (thesis_id=None, bracket_id=None → no cyclic FK),
    # then thesis (position exists), then bracket+order atomically.
    await _seed_minimal_invocation(factory)
    await _seed_position(
        factory,
        _make_open_position(position_id=PositionId("pos-1"), status=PositionStatus.OPEN),
    )
    await _seed_position(
        factory,
        _make_open_position(position_id=PositionId("pos-pending"), status=PositionStatus.PENDING),
    )
    await _seed_thesis(
        factory,
        _make_thesis_record(thesis_id=ThesisId("thesis-1"), position_id=PositionId("pos-1")),
    )
    # Bracket and test order seeded atomically: bracket.entry_order_id and
    # the test order both reference brk-1, so they must commit together.
    from tests.state._fk_substrate import stub_order_row

    bracket = _make_bracket_record(bracket_id=BracketId("brk-1"), position_id=PositionId("pos-1"))
    test_order = _make_pending_order(order_id=OrderId("ord-1"))
    bracket_parent, leg_rows = bracket_record_to_rows(bracket)
    stub_ids_needed = [bracket_parent.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]
    seeded_order_ids: set[str] = {test_order.order_id}
    async with factory() as sess:
        for oid in stub_ids_needed:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, bracket.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(order_record_to_row(test_order))
        sess.add(bracket_parent)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    repo = _build_repo(factory)

    config = PortfolioStateConfig(
        pm_decision_log_sliding_window_invocations=5,
        thesis_resolutions_lookback_trading_days=10,
        thesis_quality_aggregates_trailing_windows_days=(5, 20),
        snapshot_freshness_max_phase1_to_snapshot_seconds=300.0,
        snapshot_freshness_max_price_age_seconds=60.0,
    )
    quote = PriceQuote(
        ticker=Symbol("AAPL"),
        price_usd=160.0,
        as_of_timestamp=_NOW,
        source=PriceSource.INTRADAY_QUOTE,
        is_stale=False,
    )
    price_provider = StubCurrentPriceProvider({"AAPL": quote}, now=_NOW)

    def _sector_resolver(_pos: PositionRecord) -> str | None:
        return "Tech"

    assembled = assemble_snapshot(
        repository=repo,
        price_provider=price_provider,
        sector_resolver=_sector_resolver,
        config=config,
        now=_NOW,
    )
    snap = assembled.snapshot

    assert snap.invocation_id == _INV_ID
    assert snap.phase1_committed_at == _PHASE1_AT
    assert {p.position_id for p in snap.open_positions} == {"pos-1"}
    assert {p.position_id for p in snap.pending_positions} == {"pos-pending"}
    assert {t.thesis_id for t in snap.active_theses} == {"thesis-1"}
    assert {b.bracket_id for b in snap.brackets} == {"brk-1"}
    assert {o.order_id for o in snap.pending_orders} == {"ord-1"}
    assert snap.cash_ledger.current_cash_usd == 10000.0
    assert snap.drawdown.equity_high_water_mark_usd == 100000.0
    assert snap.thesis_quality_aggregates.resolution_counts_by_window == ()
    # Open position market value should be enriched (10 shares at $160).
    assert snap.open_positions[0].current_market_value_usd == 1600.0
