"""Tests for the engine-stub coordinated swap — story 03e (ALP-390).

Drives the broker-routing path of ``submit_envelope_mcp`` (PM envelopes) and
``submit_engine_envelope`` (engine envelopes). When the runner supplies a
``TradingClient`` + ``AccountStateQueries`` to the factory / submission
function, accepted commands route through ``dispatch_command_to_broker``
instead of producing synthetic acknowledgments.

Tests use ``unittest.mock.MagicMock`` for the alpaca-py client so the suite
runs offline. Mirrors the sibling ``tests/execution/broker_adapter/test_*``
fixture pattern.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, OrderStatus, TimeInForce
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    ClientOrderId,
    EnvelopeId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.commands.command_models import (
    BracketOrderParameters,
    CloseCommand,
    EntryOrder,
    EquityInstrument,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    StrategyInstrument,
    StrategyLeg,
    Target,
    Thesis,
    ThesisComponent,
)
from alphamind.commands.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.events.activity_log import EventSource, EventType
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
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
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.portfolio_state.records.theses import (
    ThesisComponent as PersistedThesisComponent,
)
from alphamind.state.config import StatePersistenceConfig
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
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

_NOW = datetime(2026, 5, 9, 14, 30, 0, tzinfo=UTC)
_TRIGGER_TS = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
_INV_ID = "inv-2026-05-09T14:30:00Z-aaaa"
_PROCESS_ID = "proc-engine-1"
_MONITOR_SESSION = "session-abc"


# ---------------------------------------------------------------------------
# Engine + session fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Substrate helpers (mirrors test_submit_engine_envelope.py)
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
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-engine-1.txt",
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


async def _seed_invocation_substrate(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str = _INV_ID,
) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_make_invocation_record(invocation_id)))
        await sess.commit()


async def _seed_cash_ledger(
    factory: async_sessionmaker[AsyncSession],
    current_cash_usd: float = 100_000.0,
    reserved_capital_usd: float = 0.0,
) -> None:
    record = CashLedger(
        current_cash_usd=current_cash_usd,
        settled_cash_usd=current_cash_usd,
        reserved_capital_usd=reserved_capital_usd,
        available_buying_power_usd=current_cash_usd - reserved_capital_usd,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(record, last_updated_at=_NOW))
        await sess.commit()


def _open_position(
    position_id: str = "POS-NVDA-001",
    *,
    thesis_id: str = "THE-NVDA-1",
    bracket_id: str = "BRK-NVDA-1",
    ticker: str = "NVDA",
) -> PositionRecord:
    from alphamind.portfolio_state.records.positions import PositionFill

    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=150.0,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=price(150.0),
            fill_quantity=10.0,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id),
        bracket_id=BracketId(bracket_id),
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


def _active_thesis(
    thesis_id: str = "THE-NVDA-1", position_id: str = "POS-NVDA-001"
) -> ThesisRecord:
    components = tuple(
        PersistedThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=ThesisId(thesis_id),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="NVDA",
            narrative=f"{ct.value} narrative",
            key_assumptions=(KeyAssumption(text="K", outcome=None),),
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
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="NVDA momentum",
        key_catalyst="Earnings beat",
        position_size_rationale="5% sized.",
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


def _active_bracket(
    bracket_id: str = "BRK-NVDA-1",
    position_id: str = "POS-NVDA-001",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=140.0, direction="LTE"
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


async def _seed_position_cluster(
    factory: async_sessionmaker[AsyncSession],
    position: PositionRecord,
    thesis: ThesisRecord,
    bracket: BracketRecord,
) -> None:
    from tests.state._fk_substrate import stub_order_row

    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_parent, leg_rows = bracket_record_to_rows(bracket)

    stub_ids_needed: list[str] = [bracket_parent.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        for oid in stub_ids_needed:
            sess.add(stub_order_row(oid, bracket.bracket_id))
        sess.add(bracket_parent)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str = _INV_ID + "-engine",
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=invocation_id),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


async def _seed_substrate_with_cash(
    factory: async_sessionmaker[AsyncSession],
    *,
    cash_usd: float = 100_000.0,
) -> None:
    """Seed process lifetime + cash ledger in one transaction."""
    cash = CashLedger(
        current_cash_usd=cash_usd,
        settled_cash_usd=cash_usd,
        reserved_capital_usd=0.0,
        available_buying_power_usd=cash_usd,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW))
        await sess.commit()


def _default_execution_config(retry_window_seconds: int = 30) -> Any:
    """Build a default ExecutionConfig for the broker-routing tests."""
    from alphamind.config.models.execution import (
        ExecutionConfig,
        FeeSchedule,
        GreeksRefresh,
        OrderType,
        PaperHarness,
    )

    return ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=5, move_trigger_pct=0.01),
        conservative_delta_buffer_pct=0.0,
        submission_retry_window_seconds=retry_window_seconds,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.0,
            impact_coefficients={
                OrderType.market: 0.1,
                OrderType.limit: 0.05,
                OrderType.stop: 0.08,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.0,
    )


def _build_db_factory(tmp_path: Path) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """Create a fresh on-disk SQLite DB + async session factory for the test."""
    db_path = tmp_path / "test.db"
    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    async_engine = make_async_engine(str(db_path))
    return async_engine, make_async_session_factory(async_engine)


# ---------------------------------------------------------------------------
# Engine envelope builders
# ---------------------------------------------------------------------------


def _engine_close_command() -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=PositionId("POS-NVDA-001"),
        quantity="all",
        order_type="market",
        close_rationale_type="risk_management",
        risk_management_subtype="engine_guardrail",
    )


def _engine_envelope() -> EngineEnvelope:
    return EngineEnvelope(
        envelope_id=EnvelopeId("MON.session-abc.42"),
        invocation_id=None,
        trigger_timestamp=_TRIGGER_TS,
        source_provenance="engine_guardrail",
        guardrail_trigger_record=GuardrailTriggerRecord(
            rule_breached="per_position_max_loss",
            trigger_timestamp=_TRIGGER_TS,
            breach_details=BreachDetails(
                current_value=12_500.0,
                limit_value=10_000.0,
                overage=2_500.0,
                unit="usd",
                regime_at_breach="risk_off",
            ),
            position_selection_rationale="position triggering the position-level max loss limit",
            cascade_id=None,
            secondary_breach_check_result=None,
        ),
        commands=(_engine_close_command(),),
    )


# ---------------------------------------------------------------------------
# Mock alpaca client builders
# ---------------------------------------------------------------------------


def _make_fake_alpaca_order(
    *,
    order_class: OrderClass = OrderClass.SIMPLE,
    status: OrderStatus = OrderStatus.ACCEPTED,
    client_order_id: str = "client-id-stub",
) -> MagicMock:
    order = MagicMock()
    order.id = uuid.uuid4()
    order.client_order_id = client_order_id
    order.status = status
    order.order_class = order_class
    return order


def _make_mock_client_with_order(
    *,
    order_class: OrderClass = OrderClass.SIMPLE,
    expected_client_order_id: str | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Return (client, fake_order). The client's submit_order returns the order."""
    fake_order = _make_fake_alpaca_order(
        order_class=order_class,
        client_order_id=expected_client_order_id or "client-id-stub",
    )

    def _submit_order(req: Any) -> MagicMock:
        # Echo back the client_order_id from the request so EquitySubmission
        # carries the same id the dispatcher passed in.
        if hasattr(req, "client_order_id"):
            fake_order.client_order_id = req.client_order_id
        return fake_order

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)
    return client, fake_order


# ---------------------------------------------------------------------------
# Engine envelope tests — coordinated swap
# ---------------------------------------------------------------------------


async def test_engine_envelope_routes_close_through_dispatcher(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When ``client`` + ``queries`` + ``execution_config`` are supplied,
    ``submit_engine_envelope`` dispatches the embedded CLOSE through the broker
    adapter — Alpaca returns a real order id which lands on the persisted
    OrderRecord."""
    from alphamind.config.models.execution import (
        ExecutionConfig,
        FeeSchedule,
        GreeksRefresh,
        OrderType,
        PaperHarness,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)

    # Mock alpaca client returning a fake order with a stable id we can assert on.
    expected_alpaca_order_id = uuid.uuid4()
    fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
    fake_order.id = expected_alpaca_order_id

    def _submit_order(req: Any) -> MagicMock:
        if hasattr(req, "client_order_id"):
            fake_order.client_order_id = req.client_order_id
        return fake_order

    client = MagicMock()
    client.submit_order = MagicMock(side_effect=_submit_order)

    # Mock TradingClient isinstance check for AccountStateQueries.
    client.__class__ = type("MockTradingClient", (MagicMock,), {})
    queries = MagicMock(spec=AccountStateQueries)
    execution_config = ExecutionConfig(
        greeks_refresh=GreeksRefresh(scheduled_interval_minutes=5, move_trigger_pct=0.01),
        conservative_delta_buffer_pct=0.0,
        submission_retry_window_seconds=30,
        paper_harness=PaperHarness(
            spread_buffer_pct=0.0,
            impact_coefficients={
                OrderType.market: 0.1,
                OrderType.limit: 0.05,
                OrderType.stop: 0.08,
            },
            fee_schedule=FeeSchedule(
                cat_per_executed_share=0.0,
                taf_per_share_sells=0.0,
                sec_pct_of_notional_sells=0.0,
                orf_per_options_contract=0.0,
                occ_per_options_contract=0.0,
            ),
        ),
        pl_target_margin_pct=0.0,
    )

    ctx, handle = await _open_handle(factory)
    result, _state = await submit_engine_envelope(
        _engine_envelope(),
        handle=handle,
        state=state,
        config=_make_state_persistence_config(),
        client=client,
        queries=queries,
        execution_config=execution_config,
    )
    await ctx.__aexit__(None, None, None)

    assert result.status == "accepted"

    # The persisted CLOSE order carries the broker's real alpaca_order_id.
    async with factory() as sess:
        orders = (await sess.execute(select(OrderRow))).scalars().all()
        close_orders = [o for o in orders if o.order_role == "CLOSE"]
        assert len(close_orders) == 1
        assert close_orders[0].alpaca_order_id == str(expected_alpaca_order_id)
        assert close_orders[0].alpaca_order_id_chain_json == json.dumps(
            [str(expected_alpaca_order_id)]
        )

    # The Alpaca client was called with a SIMPLE-class market sell order for NVDA.
    submitted = client.submit_order.call_args[0][0]
    assert submitted.symbol == "NVDA"
    assert submitted.side == OrderSide.SELL
    assert submitted.order_class == OrderClass.SIMPLE
    assert submitted.time_in_force == TimeInForce.DAY
    assert submitted.qty == 10.0  # full position close


async def test_engine_envelope_legacy_path_unchanged_without_client(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When ``client`` is None, ``submit_engine_envelope`` retains the legacy
    behavior — synthetic acks, no broker dispatch. Existing tests rely on this."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)

    ctx, handle = await _open_handle(factory)
    result, _state = await submit_engine_envelope(
        _engine_envelope(),
        handle=handle,
        state=state,
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert result.status == "accepted"
    # Synthetic ack format from the engine-stub.
    assert result.acknowledgment is not None
    assert result.acknowledgment.order_id == "ORD-CLOSE-POS-NVDA-001"


# ---------------------------------------------------------------------------
# PM envelope tests — coordinated swap (OPEN equity)
# ---------------------------------------------------------------------------


async def test_pm_envelope_open_equity_routes_through_dispatcher(
    tmp_path: Any,
) -> None:
    """When ``client`` + ``queries`` + ``execution_config`` are supplied to the
    MCP factory, an accepted OPEN equity command routes through
    ``dispatch_command_to_broker``; the persisted entry order carries the real
    Alpaca order id."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries
    from tests.execution.oms.test_submit_envelope_mcp import (
        _DEFAULT_ACTIVE_SECTORS,
        _make_analyst_envelope,
        _make_bundle,
        _make_pm_view,
        _make_validation_state,
        _recommendation_stub,
        _retrieval_store,
        _sector_resolver,
    )

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_substrate_with_cash(factory)

        invocation_id = "inv-broker-routed-1"
        ctx = InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=invocation_id),
        )
        handle = await ctx.__aenter__()

        envelope = _make_analyst_envelope()
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-1"),))

        # Build a mock client whose submit_order returns a fake order with a real id.
        expected_alpaca_order_id = uuid.uuid4()
        fake_order = _make_fake_alpaca_order(order_class=OrderClass.BRACKET)
        fake_order.id = expected_alpaca_order_id

        def _submit_order(req: Any) -> MagicMock:
            if hasattr(req, "client_order_id"):
                fake_order.client_order_id = req.client_order_id
            return fake_order

        client = MagicMock()
        client.submit_order = MagicMock(side_effect=_submit_order)
        queries = MagicMock(spec=AccountStateQueries)

        _response, state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=bundle,
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_make_state_persistence_config(),
            invocation_handle=handle,
            client=client,
            queries=queries,
            execution_config=_default_execution_config(),
        )
        await ctx.__aexit__(None, None, None)

        # The Alpaca client was called once for the entry order.
        assert client.submit_order.call_count == 1
        submitted = client.submit_order.call_args[0][0]
        assert submitted.symbol == "NVDA"
        assert submitted.side == OrderSide.BUY

        # The persisted entry order carries the real alpaca_order_id.
        async with factory() as sess:
            order_rows = (await sess.execute(select(OrderRow))).scalars().all()
            entry_orders = [o for o in order_rows if o.order_role == "ENTRY"]
            assert len(entry_orders) == 1
            assert entry_orders[0].alpaca_order_id == str(expected_alpaca_order_id)
            assert entry_orders[0].alpaca_order_id_chain_json == json.dumps(
                [str(expected_alpaca_order_id)]
            )

            # Acknowledgment (in submission_log) carries the real id too.
            assert len(state.submission_log) == 1
            log_entry = state.submission_log[0]
            ack = log_entry.submission_results[0].acknowledgment
            assert ack is not None
            assert ack.order_id == str(expected_alpaca_order_id)
    finally:
        await async_engine.dispose()


async def test_pm_envelope_open_equity_routes_through_injected_broker_dispatch(
    tmp_path: Any,
) -> None:
    """Composition-root-injected ``BrokerDispatch`` is consulted in lieu of the
    concrete ``dispatch_command_to_broker`` when the engine-stub processes
    an accepted OPEN command.

    Architectural integration test for ALP-458: stubbing
    ``dispatch_command_to_broker`` would not catch a regression where the
    ``broker_dispatch`` kwarg got dropped between
    ``build_submit_envelope_mcp_server`` and ``_route_through_broker``.
    This test passes a fake Protocol implementer end-to-end and confirms
    the fake — not the concrete dispatcher — is invoked.
    """
    from alphamind.commands.command_models import OMSCommand
    from alphamind.commands.protocols import BrokerDispatch
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import (
        AccountStateQueries,
        EquitySubmission,
        Submitted,
    )
    from tests.execution.oms.test_submit_envelope_mcp import (
        _DEFAULT_ACTIVE_SECTORS,
        _make_analyst_envelope,
        _make_bundle,
        _make_pm_view,
        _make_validation_state,
        _recommendation_stub,
        _retrieval_store,
        _sector_resolver,
    )

    captured: list[OMSCommand] = []
    expected_alpaca_order_id = uuid.uuid4()

    class _FakeBrokerDispatch:
        async def __call__(
            self,
            command: OMSCommand,
            *,
            client_order_id: str,
            **context: Any,
        ) -> Any:
            captured.append(command)
            payload = EquitySubmission(
                alpaca_order_id=AlpacaOrderId(str(expected_alpaca_order_id)),
                client_order_id=ClientOrderId(client_order_id),
                status="accepted",
                order_class="simple",
            )
            from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult

            return Submitted(
                payload=BrokerDispatchResult(
                    alpaca_order_id=AlpacaOrderId(str(expected_alpaca_order_id)),
                    client_order_id=ClientOrderId(client_order_id),
                    status="accepted",
                    order_class="simple",
                    payload_kind="equity",
                    raw_submission=payload,
                ),
                attempt_count=1,
            )

    fake: BrokerDispatch = _FakeBrokerDispatch()
    assert isinstance(fake, BrokerDispatch)

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_substrate_with_cash(factory)
        invocation_id = "inv-broker-dispatch-injected-1"
        ctx = InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=invocation_id),
        )
        handle = await ctx.__aenter__()

        envelope = _make_analyst_envelope()
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-1"),))

        # client/queries/execution_config still supplied so the engine-stub
        # routes through *some* dispatcher; the injected fake replaces the
        # concrete one without monkey-patching.
        client = MagicMock()
        queries = MagicMock(spec=AccountStateQueries)

        _response, state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=bundle,
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_make_state_persistence_config(),
            invocation_handle=handle,
            client=client,
            queries=queries,
            execution_config=_default_execution_config(),
            broker_dispatch=fake,
        )
        await ctx.__aexit__(None, None, None)

        # The injected fake was invoked; the concrete client.submit_order
        # was NOT (the fake bypasses it entirely).
        assert len(captured) == 1
        client.submit_order.assert_not_called()

        # The acknowledgment carries the fake's alpaca_order_id, proving the
        # full path: invoked fake → result wrap → acknowledgment writeback.
        assert len(state.submission_log) == 1
        log_entry = state.submission_log[0]
        ack = log_entry.submission_results[0].acknowledgment
        assert ack is not None
        assert ack.order_id == str(expected_alpaca_order_id)
    finally:
        await async_engine.dispose()


async def test_pm_envelope_gateway_failure_writes_command_abandoned(
    tmp_path: Any,
) -> None:
    """When the broker dispatch returns ``GatewaySubmissionFailed`` (retry
    window exhausted), the OMS writes a ``command_abandoned`` activity-log
    entry and the per-command result is rejected — the entry order is NOT
    persisted."""
    import httpx

    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries
    from tests.execution.oms.test_submit_envelope_mcp import (
        _DEFAULT_ACTIVE_SECTORS,
        _make_analyst_envelope,
        _make_bundle,
        _make_pm_view,
        _make_validation_state,
        _recommendation_stub,
        _retrieval_store,
        _sector_resolver,
    )

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_substrate_with_cash(factory)

        invocation_id = "inv-gateway-fail-1"
        ctx = InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=invocation_id),
        )
        handle = await ctx.__aenter__()

        envelope = _make_analyst_envelope()
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-1"),))

        client = MagicMock()
        client.submit_order = MagicMock(side_effect=httpx.ConnectError("network down"))
        queries = MagicMock(spec=AccountStateQueries)

        _response, state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=bundle,
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_make_state_persistence_config(),
            invocation_handle=handle,
            client=client,
            queries=queries,
            execution_config=_default_execution_config(retry_window_seconds=1),
        )
        await ctx.__aexit__(None, None, None)

        # No entry order persisted — the writeback was skipped on gateway failure.
        async with factory() as sess:
            order_rows = (await sess.execute(select(OrderRow))).scalars().all()
            entry_orders = [o for o in order_rows if o.order_role == "ENTRY"]
            assert entry_orders == []

            log_rows = (
                (
                    await sess.execute(
                        select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                    )
                )
                .scalars()
                .all()
            )
        types = {r.event_type for r in log_rows}
        assert EventType.COMMAND_ABANDONED.value in types

        # Per-command result is rejected with a gateway-failure rationale.
        assert len(state.submission_log) == 1
        result = state.submission_log[0].submission_results[0]
        assert result.status == "rejected"
        assert result.rejection_payload is not None
    finally:
        await async_engine.dispose()


async def test_pm_envelope_close_equity_routes_through_dispatcher(
    tmp_path: Any,
) -> None:
    """A PM-originated CLOSE on an equity position routes through
    ``submit_equity_close``; the persisted close order carries Alpaca's real
    ``alpaca_order_id``."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries
    from tests.execution.oms.test_submit_envelope_mcp import (
        _DEFAULT_ACTIVE_SECTORS,
        _close_command,
        _make_bundle,
        _make_pm_view,
        _make_strategist_envelope,
        _make_validation_state,
        _position_assessment_stub,
        _position_view,
        _retrieval_store,
        _sector_resolver,
    )

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_substrate_with_cash(factory)

        # Seed the position the CLOSE references.
        await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

        invocation_id = "inv-close-routed-1"
        ctx = InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=invocation_id),
        )
        handle = await ctx.__aenter__()

        envelope = _make_strategist_envelope(
            verdict="approve",
            commands=(_close_command(position_id=PositionId("POS-NVDA-001")),),
        )
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(position_assessments=(_position_assessment_stub("SA-1"),))

        expected_alpaca_order_id = uuid.uuid4()
        fake_order = _make_fake_alpaca_order(order_class=OrderClass.SIMPLE)
        fake_order.id = expected_alpaca_order_id

        def _submit_order(req: Any) -> MagicMock:
            if hasattr(req, "client_order_id"):
                fake_order.client_order_id = req.client_order_id
            return fake_order

        client = MagicMock()
        client.submit_order = MagicMock(side_effect=_submit_order)
        queries = MagicMock(spec=AccountStateQueries)

        pm_view = _make_pm_view(positions=(_position_view("POS-NVDA-001"),))

        _response, state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=bundle,
            pm_view=pm_view,
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_make_state_persistence_config(),
            invocation_handle=handle,
            client=client,
            queries=queries,
            execution_config=_default_execution_config(),
        )
        await ctx.__aexit__(None, None, None)

        # The Alpaca client was called once for the close order — equity SIMPLE sell.
        assert client.submit_order.call_count == 1
        submitted = client.submit_order.call_args[0][0]
        assert submitted.symbol == "NVDA"
        assert submitted.side == OrderSide.SELL  # closing a long position
        assert submitted.qty == 10.0  # full position
        assert submitted.order_class == OrderClass.SIMPLE

        # The persisted close order carries the broker's alpaca_order_id.
        async with factory() as sess:
            order_rows = (await sess.execute(select(OrderRow))).scalars().all()
            close_orders = [o for o in order_rows if o.order_role == "CLOSE"]
            assert len(close_orders) == 1
            assert close_orders[0].alpaca_order_id == str(expected_alpaca_order_id)
    finally:
        await async_engine.dispose()


async def test_pm_envelope_permanent_rejection_carries_code_in_gateway_reason(
    tmp_path: Any,
) -> None:
    """When the broker raises ``PermanentRejectionError``, the per-command
    result is a synchronous OMS rejection; ``rejection_payload.gateway_reason``
    carries the ``PermanentRejection.code`` per the coordinated-swap
    acceptance criteria."""
    from typing import cast as _cast

    from alpaca.common.exceptions import APIError

    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.execution.broker_adapter import AccountStateQueries
    from tests.execution.oms.test_submit_envelope_mcp import (
        _DEFAULT_ACTIVE_SECTORS,
        _make_analyst_envelope,
        _make_bundle,
        _make_pm_view,
        _make_validation_state,
        _recommendation_stub,
        _retrieval_store,
        _sector_resolver,
    )

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_substrate_with_cash(factory)

        invocation_id = "inv-perm-reject-1"
        ctx = InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=invocation_id),
        )
        handle = await ctx.__aenter__()

        envelope = _make_analyst_envelope()
        validation_state = _make_validation_state()
        state = build_initial_submit_envelope_state(
            invocation_id=validation_state.invocation_id,
            starting_validation_state=validation_state,
        )
        bundle = _make_bundle(recommendations=(_recommendation_stub("REC-1"),))

        # Build an APIError that classifies to insufficient_buying_power (403).
        body = json.dumps({"code": 42, "message": "insufficient buying power"})
        fake_http_error = MagicMock()
        fake_http_error.response.status_code = 403
        api_error = _cast(APIError, _cast(Any, APIError)(body, http_error=fake_http_error))
        client = MagicMock()
        client.submit_order = MagicMock(side_effect=api_error)
        queries = MagicMock(spec=AccountStateQueries)

        _response, state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=bundle,
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_make_state_persistence_config(),
            invocation_handle=handle,
            client=client,
            queries=queries,
            execution_config=_default_execution_config(),
        )
        await ctx.__aexit__(None, None, None)

        assert len(state.submission_log) == 1
        result = state.submission_log[0].submission_results[0]
        assert result.status == "rejected"
        assert result.rejection_payload is not None
        # The PermanentRejection code lands in gateway_reason.
        assert result.rejection_payload.gateway_reason == "insufficient_buying_power"

        # No entry order was persisted — rejection short-circuits writeback.
        async with factory() as sess:
            order_rows = (await sess.execute(select(OrderRow))).scalars().all()
            assert [o for o in order_rows if o.order_role == "ENTRY"] == []
    finally:
        await async_engine.dispose()


# ---------------------------------------------------------------------------
# _engine_close_dispatch_kwargs — engine-CLOSE on options / strategy positions
# is no longer blocked behind a hardcoded NotImplementedError. The helper
# projects the persisted position's details into the per-asset kwargs the
# dispatcher needs (OCC + intent for options; legs + strategy_type for
# strategy). Regression coverage for the S6 fix.
# ---------------------------------------------------------------------------


def test_engine_close_dispatch_kwargs_routes_options_position() -> None:
    """Engine CLOSE on an options position threads OCC + sell-to-close intent.

    Regression: the helper used to raise ``NotImplementedError`` for any
    non-equity position, blocking the ALP-123 continuous monitor from
    closing options positions through the broker-routed engine path.
    """
    from alphamind.execution.broker_adapter.order_options import build_occ_symbol
    from alphamind.execution.oms.submit_engine_envelope import (
        _engine_close_dispatch_kwargs,
    )
    from alphamind.portfolio_state.records.positions import OptionsPositionDetails

    position = _options_open_position()
    kwargs = _engine_close_dispatch_kwargs(position, position_id=position.position_id)

    assert kwargs["position_asset_type"] == "option"
    assert kwargs["position_intent"] == "sell_to_close"
    assert kwargs["position_qty"] == 2.0
    assert isinstance(position.details, OptionsPositionDetails)
    expected_occ = build_occ_symbol(
        position.details.underlying_ticker,
        position.details.expiration_date,
        position.details.contract_type,
        position.details.strike_price,
    )
    assert kwargs["occ_symbol"] == expected_occ


def test_engine_close_dispatch_kwargs_routes_strategy_position() -> None:
    """Engine CLOSE on a strategy position threads close-side legs + strategy_type.

    Each leg reverses the position it opened: the LONG-opened leg closes
    sell / sell_to_close, the SHORT-opened leg closes buy / buy_to_close.
    The inversion happens once at the shared seam.
    """
    from alphamind.execution.oms.submit_engine_envelope import (
        _engine_close_dispatch_kwargs,
    )

    position = _strategy_open_position()
    kwargs = _engine_close_dispatch_kwargs(position, position_id=position.position_id)

    assert kwargs["position_asset_type"] == "strategy"
    assert kwargs["strategy_type"] == "vertical_spread"
    close_legs = kwargs["close_legs"]
    assert len(close_legs) == 2
    # Long-opened leg → sell_to_close; short-opened leg → buy_to_close.
    assert close_legs[0].side == "sell"
    assert close_legs[0].position_intent == "sell_to_close"
    assert close_legs[1].side == "buy"
    assert close_legs[1].position_intent == "buy_to_close"
    # No leg of a strategy CLOSE carries a *_to_open intent.
    assert all(not leg.position_intent.endswith("_to_open") for leg in close_legs)


def test_engine_close_dispatch_kwargs_strategy_leg_without_direction_raises() -> None:
    """A strategy CLOSE leg whose ``direction`` is unset raises ValueError.

    The leg-direction-is-None guard is preserved in the shared close-leg seam.
    """
    import dataclasses

    from alphamind.execution.oms.submit_engine_envelope import (
        _engine_close_dispatch_kwargs,
    )
    from alphamind.portfolio_state.records.positions import StrategyPositionDetails

    position = _strategy_open_position()
    assert isinstance(position.details, StrategyPositionDetails)
    legs = position.details.legs
    directionless_first = dataclasses.replace(legs[0], direction=None)
    broken_details = dataclasses.replace(position.details, legs=(directionless_first, *legs[1:]))
    broken_position = dataclasses.replace(position, details=broken_details)

    with pytest.raises(ValueError, match="direction"):
        _engine_close_dispatch_kwargs(broken_position, position_id=broken_position.position_id)


def test_engine_close_dispatch_kwargs_equity_unchanged() -> None:
    """Equity routing remains symbol/qty/side — same as the prior behavior."""
    from alphamind.execution.oms.submit_engine_envelope import (
        _engine_close_dispatch_kwargs,
    )

    position = _open_position()
    kwargs = _engine_close_dispatch_kwargs(position, position_id=position.position_id)

    assert kwargs == {
        "position_asset_type": "equity",
        "position_symbol": "NVDA",
        "position_qty": 10.0,
        "position_side": "long",
    }


# ---------------------------------------------------------------------------
# _adjust_command_context — derives target_asset_class / target_order_class
# from the persisted position's details, and targets the protective leg whose
# role matches the ADJUST's change-fields. Regression coverage for the
# previously-hardcoded ``us_equity`` / ``simple`` plus the silent-drift
# scenario where an ADJUST against options/strategy was sent to the
# replace-order surface with the wrong asset class.
# ---------------------------------------------------------------------------


def _options_open_position(
    position_id: str = "POS-OPT-001",
    *,
    bracket_id: str | None = "BRK-OPT-1",
) -> PositionRecord:
    """Build an OPEN options position with a single CALL leg."""
    from datetime import date as _date

    from alphamind.portfolio_state.records.positions import (
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
        PositionFill,
    )

    details = OptionsPositionDetails(
        underlying_ticker=Symbol("NVDA"),
        strike_price=420.0,
        expiration_date=_date(2026, 6, 19),
        contract_type=OptionContractType.CALL,
        contract_count=2.0,
        contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
        premium_paid_per_contract=8.75,
        greeks=OptionGreeks(
            delta=0.5,
            gamma=0.02,
            theta=-0.1,
            vega=0.3,
            iv_used=0.25,
        ),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=price(8.75),
            fill_quantity=2.0,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THE-OPT-1"),
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


def _strategy_open_position(
    position_id: str = "POS-STRAT-001",
    *,
    bracket_id: str | None = "BRK-STRAT-1",
) -> PositionRecord:
    """Build an OPEN 2-leg vertical-spread strategy position."""
    from datetime import date as _date

    from alphamind.portfolio_state.records.positions import (
        OptionContractType,
        OptionGreeks,
        OptionsPositionDetails,
        PositionFill,
        StrategyPositionDetails,
    )
    from alphamind.portfolio_state.records.positions import (
        StrategyLeg as PersistedStrategyLeg,
    )

    expiration = _date(2026, 6, 19)
    long_leg = PersistedStrategyLeg(
        leg_id="leg-1",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=420.0,
            expiration_date=expiration,
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=8.75,
            greeks=OptionGreeks(
                delta=0.5,
                gamma=0.02,
                theta=-0.1,
                vega=0.3,
                iv_used=0.25,
            ),
        ),
    )
    short_leg = PersistedStrategyLeg(
        leg_id="leg-2",
        direction=Direction.SHORT,
        options=OptionsPositionDetails(
            underlying_ticker=Symbol("NVDA"),
            strike_price=425.0,
            expiration_date=expiration,
            contract_type=OptionContractType.CALL,
            contract_count=1.0,
            contract_multiplier=LISTED_OPTION_CONTRACT_MULTIPLIER,
            premium_paid_per_contract=5.25,
            greeks=OptionGreeks(
                delta=0.4,
                gamma=0.02,
                theta=-0.08,
                vega=0.25,
                iv_used=0.22,
            ),
        ),
    )
    details = StrategyPositionDetails(
        strategy_type_label="vertical_spread",
        legs=(long_leg, short_leg),
        net_premium_usd=350.0,
        max_profit_usd=500.0,
        max_loss_usd=350.0,
        breakeven_levels=(423.5,),
        strategy_greeks=OptionGreeks(
            delta=0.1,
            gamma=0.0,
            theta=-0.02,
            vega=0.05,
            iv_used=0.23,
        ),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=price(3.5),
            fill_quantity=1.0,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId("THE-STRAT-1"),
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _seed_pending_protective_orders(
    factory: async_sessionmaker[AsyncSession],
    *,
    bracket_id: str,
    position_id: str,
    thesis_id: str,
) -> Any:
    """Seed PENDING PRICE_STOP + TAKE_PROFIT rows on *bracket_id*.

    Returns a coroutine (callers ``await`` it). Mirrors the seeding pattern
    in test_phase2_write_path's adjust tests but trimmed to the rows needed
    by ``_adjust_command_context``.
    """
    from alphamind.portfolio_state.records.orders import (
        EquityInstrumentSpec,
        OrderClass,
        OrderDirection,
        OrderDuration,
        OrderRecord,
        OrderRole,
        OrderStatus,
        OrderType,
        PriceParameters,
    )
    from alphamind.state.tables.orders_codec import (
        record_to_row as order_record_to_row,
    )

    def _build(order_id: str, role: OrderRole, params: PriceParameters) -> OrderRecord:
        return OrderRecord(
            order_id=OrderId(order_id),
            position_id=PositionId(position_id),
            bracket_id=BracketId(bracket_id),
            role=role,
            instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
            direction=OrderDirection.SELL,
            order_type=OrderType.STOP if role is OrderRole.PRICE_STOP else OrderType.LIMIT,
            order_class=OrderClass.OTO,
            price_parameters=params,
            quantity=10.0,
            duration=OrderDuration.DAY,
            status=OrderStatus.PENDING,
            alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
            alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
            submission_timestamp=_NOW - timedelta(hours=1),
            last_update_timestamp=_NOW - timedelta(hours=1),
            filled_quantity=0.0,
            avg_fill_price=None,
            remaining_quantity=10.0,
            modification_count=0,
            originating_thesis_id=ThesisId(thesis_id),
            originating_pm_command_id=None,
            age_hours=1.0,
        )

    rows = (
        _build("ord-stop", OrderRole.PRICE_STOP, PriceParameters(stop_trigger_price=140.0)),
        _build("ord-target", OrderRole.TAKE_PROFIT, PriceParameters(limit_price=200.0)),
    )

    async def _seed() -> None:
        async with factory() as sess:
            for r in rows:
                sess.add(order_record_to_row(r))
            await sess.commit()

    return _seed()


def _adjust_stop_command(position_id: str) -> Any:
    """Build an ADJUST command targeting only the stop leg."""
    from alphamind.commands.command_models import AdjustCommand, NewStopLevel

    return AdjustCommand(
        command_type="adjust",
        position_id=PositionId(position_id),
        adjustment_rationale="Tighten stop.",
        new_stop_level=NewStopLevel(
            trigger_price=price(145.0), order_type="stop", limit_price=None
        ),
        new_target_level=None,
        new_time_expiration=None,
        new_event_invalidation=None,
        thesis_component_updates=None,
    )


def _adjust_target_command(position_id: str) -> Any:
    """Build an ADJUST command targeting only the take-profit leg."""
    from alphamind.commands.command_models import AdjustCommand, NewTargetLevel

    return AdjustCommand(
        command_type="adjust",
        position_id=PositionId(position_id),
        adjustment_rationale="Raise target.",
        new_stop_level=None,
        new_target_level=NewTargetLevel(price=price(210.0), order_type="limit"),
        new_time_expiration=None,
        new_event_invalidation=None,
        thesis_component_updates=None,
    )


async def test_adjust_command_context_options_position_routes_us_option_simple(
    tmp_path: Path,
) -> None:
    """ADJUST against an options position derives ``us_option`` / ``simple``
    from the position's details, not the previously-hardcoded equity values.
    """
    from alphamind.decision.portfolio_manager.submit_envelope import _adjust_command_context

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_invocation_substrate(factory)
        await _seed_cash_ledger(factory)
        await _seed_position_cluster(
            factory,
            _options_open_position(),
            _active_thesis(thesis_id=ThesisId("THE-OPT-1"), position_id=PositionId("POS-OPT-001")),
            _active_bracket(
                bracket_id=BracketId("BRK-OPT-1"), position_id=PositionId("POS-OPT-001")
            ),
        )
        await _seed_pending_protective_orders(
            factory,
            bracket_id=BracketId("BRK-OPT-1"),
            position_id=PositionId("POS-OPT-001"),
            thesis_id=ThesisId("THE-OPT-1"),
        )

        ctx, handle = await _open_handle(factory)
        try:
            kwargs = await _adjust_command_context(
                _adjust_stop_command("POS-OPT-001"), invocation_handle=handle
            )
        finally:
            await ctx.__aexit__(None, None, None)

        assert kwargs["target_asset_class"] == "us_option"
        assert kwargs["target_order_class"] == "simple"
        assert kwargs["target_alpaca_order_id"] == "alp-ord-stop"
    finally:
        await async_engine.dispose()


async def test_adjust_command_context_strategy_position_routes_mleg(
    tmp_path: Path,
) -> None:
    """ADJUST against a strategy position derives ``us_option_strategy`` /
    ``mleg`` from the position's details. Regression: the previously
    hardcoded ``us_equity`` / ``simple`` would have produced a
    field-out-of-surface ValueError or silently mis-routed at the broker.
    """
    from alphamind.decision.portfolio_manager.submit_envelope import _adjust_command_context

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_invocation_substrate(factory)
        await _seed_cash_ledger(factory)
        await _seed_position_cluster(
            factory,
            _strategy_open_position(),
            _active_thesis(
                thesis_id=ThesisId("THE-STRAT-1"), position_id=PositionId("POS-STRAT-001")
            ),
            _active_bracket(
                bracket_id=BracketId("BRK-STRAT-1"), position_id=PositionId("POS-STRAT-001")
            ),
        )
        await _seed_pending_protective_orders(
            factory,
            bracket_id=BracketId("BRK-STRAT-1"),
            position_id=PositionId("POS-STRAT-001"),
            thesis_id=ThesisId("THE-STRAT-1"),
        )

        ctx, handle = await _open_handle(factory)
        try:
            kwargs = await _adjust_command_context(
                _adjust_stop_command("POS-STRAT-001"), invocation_handle=handle
            )
        finally:
            await ctx.__aexit__(None, None, None)

        assert kwargs["target_asset_class"] == "us_option_strategy"
        assert kwargs["target_order_class"] == "mleg"
        assert kwargs["target_alpaca_order_id"] == "alp-ord-stop"
    finally:
        await async_engine.dispose()


async def test_close_command_context_strategy_position_threads_close_side_legs(
    tmp_path: Path,
) -> None:
    """A PM-originated CLOSE on a strategy position threads close-side legs.

    Each leg reverses the position it opened: the LONG-opened leg closes
    sell / sell_to_close, the SHORT-opened leg closes buy / buy_to_close.
    The open→close inversion happens once at the shared seam, and the
    dispatcher receives the ``close_legs`` kwarg.
    """
    from alphamind.decision.portfolio_manager.submit_envelope.dispatch import (
        _close_command_context,
    )
    from tests.execution.oms.test_submit_envelope_mcp import _close_command

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_invocation_substrate(factory)
        await _seed_cash_ledger(factory)
        await _seed_position_cluster(
            factory,
            _strategy_open_position(),
            _active_thesis(
                thesis_id=ThesisId("THE-STRAT-1"), position_id=PositionId("POS-STRAT-001")
            ),
            _active_bracket(
                bracket_id=BracketId("BRK-STRAT-1"), position_id=PositionId("POS-STRAT-001")
            ),
        )

        ctx, handle = await _open_handle(factory)
        try:
            kwargs = await _close_command_context(
                _close_command(position_id=PositionId("POS-STRAT-001")),
                invocation_handle=handle,
            )
        finally:
            await ctx.__aexit__(None, None, None)

        assert kwargs["position_asset_type"] == "strategy"
        assert kwargs["strategy_type"] == "vertical_spread"
        close_legs = kwargs["close_legs"]
        assert len(close_legs) == 2
        # Long-opened leg → sell_to_close; short-opened leg → buy_to_close.
        assert close_legs[0].side == "sell"
        assert close_legs[0].position_intent == "sell_to_close"
        assert close_legs[1].side == "buy"
        assert close_legs[1].position_intent == "buy_to_close"
        assert all(not leg.position_intent.endswith("_to_open") for leg in close_legs)
    finally:
        await async_engine.dispose()


async def test_adjust_command_context_targets_take_profit_when_target_change(
    tmp_path: Path,
) -> None:
    """A target-only ADJUST resolves to the TAKE_PROFIT leg's alpaca_order_id,
    not the PRICE_STOP leg's. Regression: the old "first protective leg"
    selection silently sent the wrong order ID to ``submit_replace`` whenever
    the bracket's PRICE_STOP appeared first in the result set.
    """
    from alphamind.decision.portfolio_manager.submit_envelope import _adjust_command_context

    async_engine, factory = _build_db_factory(tmp_path)
    try:
        await _seed_invocation_substrate(factory)
        await _seed_cash_ledger(factory)
        await _seed_position_cluster(
            factory,
            _open_position(),
            _active_thesis(),
            _active_bracket(),
        )
        await _seed_pending_protective_orders(
            factory,
            bracket_id=BracketId("BRK-NVDA-1"),
            position_id=PositionId("POS-NVDA-001"),
            thesis_id=ThesisId("THE-NVDA-1"),
        )

        ctx, handle = await _open_handle(factory)
        try:
            kwargs = await _adjust_command_context(
                _adjust_target_command("POS-NVDA-001"), invocation_handle=handle
            )
        finally:
            await ctx.__aexit__(None, None, None)

        assert kwargs["target_alpaca_order_id"] == "alp-ord-target"
        # Equity position keeps the simple/us_equity routing.
        assert kwargs["target_asset_class"] == "us_equity"
        assert kwargs["target_order_class"] == "simple"
    finally:
        await async_engine.dispose()


# Suppress unused-import warning.
_ = (
    BracketOrderParameters,
    EntryOrder,
    EquityInstrument,
    OpenCommand,
    OptionInstrument,
    PositionSize,
    PriceCondition,
    PriceLeg,
    StrategyInstrument,
    StrategyLeg,
    Target,
    Thesis,
    ThesisComponent,
    EventSource,
    BracketLegEnforcement,
)
