"""Tests for consumer-side recovery of unattributed fills (ALP-763).

The fill-stream consumer used to silently drop a fill-bearing event that
arrived before its local ``orders`` row was committed (deferred Phase-2
writeback) — a permanent position/cash divergence. The fix never drops:

* ``persist_fill_report`` does a short in-process retry, then quarantines the
  raw ``FillReport`` to the ``unattributed_fills`` queue and emits a one-time
  loud ``log.warning`` alert.
* ``drain_unattributed_fills`` re-resolves each queued fill and integrates it
  the moment its order row exists; a fill that never resolves (out-of-band
  manual order) stays queued + alerted, never integrated.

These are sociable tests against a real in-process SQLite session and a fake
``FillReport`` built from the broker-adapter translator, mirroring
``test_task.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alpaca.trading.enums import (
    AssetClass,
    OrderClass,
    OrderSide,
    OrderType,
    TimeInForce,
)
from alpaca.trading.enums import OrderStatus as AlpacaOrderStatus
from alpaca.trading.models import Order, TradeUpdate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import BracketId, OrderId, PositionId, ThesisId
from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.broker_adapter.fill_stream import translate_trade_update
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    persist_fill_report,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    derive_broker_fill_key,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.unattributed_drain import (
    drain_unattributed_fills,
)
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    append_unattributed_fill,
    list_unattributed_fills,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.records import FillRecord, UnattributedFill
from alphamind.state.tables.fill_records import FillRecordRow
from tests.state._fk_substrate import seed_position_cluster, stub_order_row


def _now_utc() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """On-disk SQLite engine with the full ORM schema and one seeded order."""
    db_path = tmp_path / "alphamind.db"
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    with make_session_factory(sync_engine)() as sess:
        seed_position_cluster(
            sess,
            position_id=PositionId("pos-1"),
            thesis_id=ThesisId("thesis-1"),
            bracket_id=BracketId("bracket-1"),
            entry_order_id=OrderId("order-1"),
        )
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _build_order(
    *,
    client_order_id: str,
    order_id: UUID,
    qty: str = "1",
    filled_qty: str = "1",
    status: AlpacaOrderStatus = AlpacaOrderStatus.FILLED,
) -> Order:
    return Order(
        id=order_id,
        client_order_id=client_order_id,
        created_at=_now_utc(),
        updated_at=_now_utc(),
        submitted_at=_now_utc(),
        symbol="AAPL",
        asset_class=AssetClass.US_EQUITY,
        order_class=OrderClass.SIMPLE,
        order_type=OrderType.LIMIT,
        type=OrderType.LIMIT,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        status=status,
        extended_hours=False,
        qty=qty,
        filled_qty=filled_qty,
    )


def _fill_report(*, order_id: UUID, client_order_id: str, price: float, qty: float) -> FillReport:
    update = TradeUpdate(
        event="fill",
        order=_build_order(order_id=order_id, client_order_id=client_order_id),
        timestamp=_now_utc(),
        price=price,
        qty=qty,
    )
    reports = translate_trade_update(update)
    assert len(reports) == 1
    return reports[0]


async def _read_fill_records(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[FillRecordRow]:
    async with session_factory() as session:
        result = await session.execute(select(FillRecordRow))
        return list(result.scalars().all())


async def _seed_order_row_for(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    alpaca_order_id: str,
) -> None:
    """Insert the matching local order row (the deferred Phase-2 writeback)."""
    async with session_factory() as session:
        session.add(
            stub_order_row(
                order_id,
                "bracket-1",
                position_id="pos-1",
                role="ENTRY",
                status="PENDING",
                alpaca_order_id=alpaca_order_id,
            )
        )
        await session.commit()


class TestFillBeforeOrderCommitRace:
    async def test_fill_is_quarantined_then_drained_when_order_appears(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A fill arrives with no matching orders row (entry UUID + command-id
        # client_order_id, neither resolves).
        entry_uuid = uuid4()
        report = _fill_report(
            order_id=entry_uuid,
            client_order_id="inv-20260601.CMD-1.0.0",
            price=150.0,
            qty=1.0,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        # Not dropped: parked in the queue, zero fill_records.
        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].broker_fill_key == derive_broker_fill_key(report)
        assert queued[0].alerted is True

        # The deferred Phase-2 writeback lands: the order row now exists,
        # keyed by the captured broker UUID.
        await _seed_order_row_for(
            session_factory,
            order_id="ORD-DEFERRED-1",
            alpaca_order_id=str(entry_uuid),
        )

        integrated = await drain_unattributed_fills(session_factory=session_factory)

        assert integrated == 1
        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].order_id == "ORD-DEFERRED-1"
        assert rows[0].processing_status == "unprocessed"
        async with session_factory() as session:
            assert await list_unattributed_fills(session) == []

    async def test_drain_resolved_fill_also_reaches_the_broker_event_log(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """When the drain resolves a quarantined fill it must ALSO append to
        ``broker_event_log`` — not just ``fill_records`` — so a native-bracket
        child fill that was quarantined (B1) and later resolves reaches the
        gap-free PnL substrate (B2)."""
        from alphamind.state.tables.broker_event_log import BrokerEventLogRow

        entry_uuid = uuid4()
        report = _fill_report(
            order_id=entry_uuid,
            client_order_id="alpaca-generated-native-child",
            price=150.0,
            qty=1.0,
        )
        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        # The order materializes carrying the position→thesis edge.
        await _seed_order_row_for(
            session_factory,
            order_id="ORD-DEFERRED-2",
            alpaca_order_id=str(entry_uuid),
        )

        integrated = await drain_unattributed_fills(session_factory=session_factory)
        assert integrated == 1

        async with session_factory() as session:
            events = list((await session.execute(select(BrokerEventLogRow))).scalars().all())
        assert len(events) == 1
        assert events[0].event_type == "FILL"
        assert events[0].thesis_id == "thesis-1"
        assert events[0].position_id == "pos-1"


class TestTrulyUnknownOrder:
    async def test_unknown_order_stays_queued_and_alerted(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A fill for an out-of-band manual order that never gets a local row.
        report = _fill_report(
            order_id=uuid4(),
            client_order_id="manual-out-of-band",
            price=200.0,
            qty=1.0,
        )

        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)

        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].alerted is True
        assert queued[0].retry_count == 0

        integrated = await drain_unattributed_fills(session_factory=session_factory)

        # Still unresolved → not integrated, stays queued, retry bumped.
        assert integrated == 0
        assert await _read_fill_records(session_factory) == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].retry_count == 1
        assert queued[0].alerted is True

    async def test_drain_applies_enrichment_callable_on_integrate(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        from alphamind._kernel.money import money, price
        from alphamind.portfolio_state.records.positions import LiveExecutionEstimate

        entry_uuid = uuid4()
        report = _fill_report(
            order_id=entry_uuid,
            client_order_id="inv-20260601.CMD-2.0.0",
            price=150.0,
            qty=1.0,
        )
        await persist_fill_report(report, session_factory=session_factory, enrichment_callable=None)
        await _seed_order_row_for(
            session_factory,
            order_id="ORD-DEFERRED-2",
            alpaca_order_id=str(entry_uuid),
        )

        async def fake_enrichment(record: FillRecord) -> FillRecord:
            return record.model_copy(
                update={
                    "live_execution_estimate": LiveExecutionEstimate(
                        estimated_spread_usd=money("0.01"),
                        estimated_impact_usd=money("0.02"),
                        estimated_regulatory_fees_usd=money("0.03"),
                        live_adjusted_fill_price=price("150.50"),
                    )
                }
            )

        integrated = await drain_unattributed_fills(
            session_factory=session_factory, enrichment_callable=fake_enrichment
        )

        assert integrated == 1
        rows = await _read_fill_records(session_factory)
        assert len(rows) == 1
        assert rows[0].live_execution_estimate_json is not None


class TestQuarantineAlertOnce:
    """ALP-763 #3 — the loud QUARANTINED warning + alerted mark fire exactly
    once, on the genuine first park, not on every re-delivery of the same
    fill (which ON CONFLICT DO NOTHING collapses onto the existing row)."""

    async def test_re_park_does_not_re_alert(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        report = _fill_report(
            order_id=uuid4(),
            client_order_id="manual-out-of-band",
            price=200.0,
            qty=1.0,
        )

        logger = "alphamind.execution.continuous_monitor.fill_stream_consumer.persistence"
        # First park: one loud warning, row inserted + alerted.
        with caplog.at_level("WARNING", logger=logger):
            await persist_fill_report(
                report, session_factory=session_factory, enrichment_callable=None
            )
        first_warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(first_warnings) == 1
        assert "QUARANTINED" in first_warnings[0].getMessage()
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1
        assert queued[0].alerted is True

        # Re-deliver the SAME fill (websocket + recovery overlap). The append
        # no-ops via ON CONFLICT; the alert must NOT re-fire.
        caplog.clear()
        with caplog.at_level("WARNING", logger=logger):
            await persist_fill_report(
                report, session_factory=session_factory, enrichment_callable=None
            )
        assert [r for r in caplog.records if r.levelname == "WARNING"] == []
        async with session_factory() as session:
            queued = await list_unattributed_fills(session)
        assert len(queued) == 1


class TestQuarantineWriteFailureDoesNotCrash:
    """ALP-763 #4 — a quarantine-write failure (transient DB lock, etc.) must
    NOT propagate: it would otherwise reach the consumer's reconnect-budget
    supervisor and burn an attempt / crash the task. Degrade, don't crash."""

    async def test_quarantine_write_error_is_swallowed_and_logged(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        report = _fill_report(
            order_id=uuid4(),
            client_order_id="manual-out-of-band",
            price=200.0,
            qty=1.0,
        )

        async def boom(*_args: object, **_kwargs: object) -> bool:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(
            "alphamind.execution.continuous_monitor.fill_stream_consumer."
            "persistence.append_unattributed_fill",
            boom,
        )

        logger = "alphamind.execution.continuous_monitor.fill_stream_consumer.persistence"
        with caplog.at_level("ERROR", logger=logger):
            # Must return normally — no exception escapes to the consumer loop.
            await persist_fill_report(
                report, session_factory=session_factory, enrichment_callable=None
            )

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "failed to quarantine" in errors[0].getMessage()
        # The write failed, so nothing landed — but the consumer survived.
        assert await _read_fill_records(session_factory) == []


# ---------------------------------------------------------------------------
# ALP-767 — drain triggers immediate Phase-1 integration
# ---------------------------------------------------------------------------


_PLT_ID = "plt-767-test"
_ENTRY_ORDER_ID = "ORD-767-ENTRY"
_POSITION_ID = "POS-767"
_BRACKET_ID = "BRK-767"
_THESIS_ID = "THX-767"
_LEG_ID = "BRK-767-leg-stop"
_LEG_ORDER_ID = "ORD-767-leg-stop"
_NOW_767 = datetime(2026, 6, 2, 14, 0, 0, tzinfo=UTC)


async def _seed_fill_collection_substrate_for_767(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    alpaca_order_id: str,
) -> None:
    """Seed all rows fill_collection needs: process_lifetime, position, order, thesis,
    bracket (with one PENDING_ACTIVATION leg), cash_ledger, drawdown_state.

    The entry order is PENDING with remaining_quantity=10 so a single-fill of
    qty=10 transitions it to FILLED and the position PENDING→OPEN.
    """
    from alphamind._kernel.ids import AlpacaOrderId, Symbol
    from alphamind._kernel.regime import RiskZone
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
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
        ThesisComponentType,
        ThesisRecord,
        ThesisRecordStatus,
    )
    from alphamind.state.invocation_context.records import (
        ProcessLifetimeRecord,
        process_lifetime_record_to_row,
    )
    from alphamind.state.tables.brackets_codec import record_to_rows as bracket_to_rows
    from alphamind.state.tables.cash_ledger_codec import cash_ledger_record_to_row
    from alphamind.state.tables.drawdown_state_codec import drawdown_state_record_to_row
    from alphamind.state.tables.orders_codec import record_to_row as order_to_row
    from alphamind.state.tables.positions_codec import record_to_row as position_to_row
    from alphamind.state.tables.theses_codec import record_to_rows as thesis_to_rows
    from tests.state._fk_substrate import stub_order_row

    iso = _NOW_767.isoformat().replace("+00:00", "Z")

    plt_row = process_lifetime_record_to_row(
        ProcessLifetimeRecord(
            process_lifetime_id=_PLT_ID,
            process_role="monitor",
            process_start_at=iso,
            process_pid=99999,
            hostname="test-host",
            git_sha="a" * 40,
            git_branch="main",
            git_dirty=False,
            python_version="3.13.0",
            pip_freeze_hash="0" * 64,
            pip_freeze_snapshot_path="",
            anthropic_sdk_version="0.40.0",
            claude_agent_sdk_version="0.1.69",
            os_release="Linux-6.5.0",
        )
    )

    from alphamind._kernel.ids import BracketId, OrderId, PositionId, ThesisId

    order = OrderRecord(
        order_id=OrderId(_ENTRY_ORDER_ID),
        position_id=PositionId(_POSITION_ID),
        bracket_id=BracketId(_BRACKET_ID),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(alpaca_order_id),
        alpaca_order_id_chain=(AlpacaOrderId(alpaca_order_id),),
        submission_timestamp=_NOW_767,
        last_update_timestamp=_NOW_767,
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId(_THESIS_ID),
        originating_pm_command_id=None,
        age_hours=0.0,
    )
    position = PositionRecord(
        position_id=PositionId(_POSITION_ID),
        thesis_id=ThesisId(_THESIS_ID),
        bracket_id=BracketId(_BRACKET_ID),
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        details=EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=0.0,
            average_cost_basis_per_share=0.0,
            borrow_rate_pct=None,
            accrued_borrow_cost_usd=None,
            locate_status=None,
            margin_held_usd=None,
        ),
        entry_timestamp=None,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    thesis_component = tuple(
        ThesisComponent(
            component_id=f"{_THESIS_ID}-{ct.value.lower()}",
            thesis_id=ThesisId(_THESIS_ID),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="AAPL",
            narrative=f"{ct.value} rationale",
            key_assumptions=(KeyAssumption(text="Earnings beat", outcome=None),),
            generation_timestamp=_NOW_767,
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    thesis = ThesisRecord(
        thesis_id=ThesisId(_THESIS_ID),
        position_id=PositionId(_POSITION_ID),
        summary="AAPL entry",
        key_catalyst="momentum",
        position_size_rationale="5%",
        components=thesis_component,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=_NOW_767,
        time_expectation_hours=24.0,
        age_hours=0.0,
        expected_resolution_at=_NOW_767 + timedelta(hours=24),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )
    leg = BracketLeg(
        leg_id=_LEG_ID,
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(_LEG_ORDER_ID),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    bracket = BracketRecord(
        bracket_id=BracketId(_BRACKET_ID),
        position_id=PositionId(_POSITION_ID),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(_ENTRY_ORDER_ID),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )
    cash = CashLedger(
        current_cash_usd=100_000.0,
        settled_cash_usd=100_000.0,
        reserved_capital_usd=0.0,
        available_buying_power_usd=100_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
    )
    drawdown = DrawdownState(
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

    bracket_row, leg_rows = bracket_to_rows(bracket)
    thesis_row, component_rows = thesis_to_rows(thesis)

    async with session_factory() as sess:
        sess.add(plt_row)
        sess.add(position_to_row(position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_to_row(order))
        sess.add(stub_order_row(_LEG_ORDER_ID, _BRACKET_ID))
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        sess.add(cash_ledger_record_to_row(cash, last_updated_at=_NOW_767))
        sess.add(drawdown_state_record_to_row(drawdown, last_updated_at=_NOW_767))
        await sess.commit()


class TestFillCollectionIntegrationOnDrain:
    """ALP-767 — drain immediately converges local state via fill_collection integration.

    Verifies that supplying ``process_lifetime_id`` to
    :func:`drain_unattributed_fills` triggers Phase-1 fill integration right
    after a fill is recovered, flipping the position from PENDING→OPEN and
    activating bracket legs within the same drain cycle rather than waiting for
    the next scheduled pipeline run.
    """

    @pytest.fixture()
    async def substrate_factory(
        self,
        tmp_path: Path,
    ) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], str]]:
        """Yield ``(factory, alpaca_order_id)`` over a fresh DB with the
        full Phase-1 substrate seeded (position/order/bracket/leg/cash/drawdown)."""
        db_path = tmp_path / "alphamind_767.db"
        import alphamind.state.tables  # noqa: F401
        from alphamind.persistence.models import Base
        from alphamind.persistence.session import (
            make_async_engine,
            make_async_session_factory,
            make_engine,
        )

        sync_engine = make_engine(str(db_path))
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()

        async_engine = make_async_engine(str(db_path))
        factory = make_async_session_factory(async_engine)
        alpaca_uuid = str(uuid4())
        await _seed_fill_collection_substrate_for_767(factory, alpaca_order_id=alpaca_uuid)
        try:
            yield factory, alpaca_uuid
        finally:
            await async_engine.dispose()

    async def test_drain_with_process_lifetime_id_opens_position_and_activates_bracket(
        self,
        substrate_factory: tuple[async_sessionmaker[AsyncSession], str],
    ) -> None:
        """ALP-767: integrate_recovered_fills transitions position PENDING→OPEN
        and flips the bracket leg PENDING_ACTIVATION→ACTIVE in one cycle."""
        from alphamind._kernel.money import money
        from alphamind._kernel.money import price as mk_price
        from alphamind.execution.write_paths.fill_collection import integrate_recovered_fills
        from alphamind.execution.write_paths.fill_persistence import append_fill_record
        from alphamind.portfolio_state.records.orders import (
            BracketLegStatus,
            BracketStatus,
            OrderStatus,
        )
        from alphamind.portfolio_state.records.positions import (
            EquityPositionDetails,
            PositionStatus,
        )
        from alphamind.state.records import FillProcessingStatus
        from alphamind.state.tables.bracket_legs import BracketLegRow
        from alphamind.state.tables.brackets import BracketRow
        from alphamind.state.tables.positions import PositionRow
        from alphamind.state.tables.positions_codec import row_to_record as pos_row_to_record

        factory, _alpaca_uuid = substrate_factory

        fill = FillRecord(
            fill_id="fill-767-test",
            order_id=_ENTRY_ORDER_ID,
            fill_timestamp=_NOW_767,
            fill_price=mk_price(150.0),
            fill_quantity=10.0,
            remaining_quantity_after=0.0,
            order_status_after=OrderStatus.FILLED,
            slippage_usd=None,
            fees_usd=money(1.50),
            execution_venue="NASDAQ",
            gateway_reference="alp-fill-767-test",
            persistence_timestamp=_NOW_767,
            processing_status=FillProcessingStatus.UNPROCESSED,
            processing_invocation_id=None,
            processing_timestamp=None,
            regt_attribution=None,
            live_execution_estimate=None,
        )
        async with factory() as sess:
            await append_fill_record(sess, fill)
            await sess.commit()

        fills_processed = await integrate_recovered_fills(
            factory,
            process_lifetime_id=_PLT_ID,
        )

        assert fills_processed == 1

        async with factory() as sess:
            fill_row = (
                await sess.execute(
                    select(FillRecordRow).where(FillRecordRow.fill_id == "fill-767-test")
                )
            ).scalar_one()
            assert fill_row.processing_status == FillProcessingStatus.PROCESSED.value

            pos_row = (
                await sess.execute(
                    select(PositionRow).where(PositionRow.position_id == _POSITION_ID)
                )
            ).scalar_one()
            pos = pos_row_to_record(pos_row)
            assert pos.status == PositionStatus.OPEN
            assert isinstance(pos.details, EquityPositionDetails)
            assert pos.details.share_count == pytest.approx(10.0)

            brk_row = (
                await sess.execute(select(BracketRow).where(BracketRow.bracket_id == _BRACKET_ID))
            ).scalar_one()
            assert brk_row.status == BracketStatus.ACTIVE.value

            leg_row = (
                await sess.execute(
                    select(BracketLegRow).where(BracketLegRow.bracket_leg_id == _LEG_ID)
                )
            ).scalar_one()
            assert leg_row.leg_status == BracketLegStatus.ACTIVE.value

    async def test_drain_with_process_lifetime_id_runs_fill_collection_end_to_end(
        self,
        substrate_factory: tuple[async_sessionmaker[AsyncSession], str],
    ) -> None:
        """ALP-767: the full drain path — unattributed fill → drain → fill_collection —
        converges the position to OPEN without waiting for the next scheduled run."""
        from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
            derive_broker_fill_key,
        )
        from alphamind.execution.write_paths.unattributed_fill_persistence import (
            append_unattributed_fill,
        )
        from alphamind.portfolio_state.records.positions import PositionStatus
        from alphamind.state.records import UnattributedFill
        from alphamind.state.tables.positions import PositionRow
        from alphamind.state.tables.positions_codec import row_to_record as pos_row_to_record

        factory, alpaca_uuid = substrate_factory

        report = _fill_report(
            order_id=UUID(alpaca_uuid),
            client_order_id="inv-20260602.CMD-767.1.0",
            price=150.0,
            qty=10.0,
        )

        # Directly insert into unattributed_fills, simulating the quarantine path
        # where the fill arrived before its orders row committed.
        unattributed = UnattributedFill(
            broker_fill_key=derive_broker_fill_key(report),
            alpaca_order_id=alpaca_uuid,
            client_order_id="inv-20260602.CMD-767.1.0",
            event_type="fill",
            fill_timestamp=_NOW_767,
            fill_price=150.0,
            fill_quantity=10.0,
            raw_report_json=report.model_dump_json(),
            first_seen_at=_NOW_767,
            last_retry_at=None,
            retry_count=0,
            alerted=True,
        )
        async with factory() as sess:
            await append_unattributed_fill(sess, unattributed)
            await sess.commit()

        # Run drain with process_lifetime_id → Phase-1 fires immediately after
        # the fill is resolved and appended to fill_records.
        integrated = await drain_unattributed_fills(
            session_factory=factory,
            process_lifetime_id=_PLT_ID,
        )

        assert integrated == 1

        # Position must be OPEN — no scheduled pipeline run needed.
        async with factory() as sess:
            pos_row = (
                await sess.execute(
                    select(PositionRow).where(PositionRow.position_id == _POSITION_ID)
                )
            ).scalar_one()
        pos = pos_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN


# ---------------------------------------------------------------------------
# ALP-771 — one-shot terminal escalation for long-unresolved fills
# ---------------------------------------------------------------------------

_DRAIN_LOG = "alphamind.execution.continuous_monitor.fill_stream_consumer.unattributed_drain"

_SEEN_AT_771 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _park_out_of_band_fill(
    *,
    client_order_id: str,
    first_seen_at: datetime,
    alerted: bool = True,
    retry_count: int = 0,
) -> UnattributedFill:
    """Build an out-of-band UnattributedFill with a valid FillReport JSON payload."""
    report = _fill_report(
        order_id=uuid4(),
        client_order_id=client_order_id,
        price=50.0,
        qty=10.0,
    )
    return UnattributedFill(
        broker_fill_key=derive_broker_fill_key(report),
        alpaca_order_id=report.alpaca_order_id,
        client_order_id=client_order_id,
        event_type="fill",
        fill_timestamp=first_seen_at,
        fill_price=50.0,
        fill_quantity=10.0,
        raw_report_json=report.model_dump_json(),
        first_seen_at=first_seen_at,
        last_retry_at=None,
        retry_count=retry_count,
        alerted=alerted,
    )


class TestEscalation:
    """ALP-771 — one-shot terminal ERROR escalation for long-unresolved fills.

    After the configured TTL the drain emits a single ERROR and marks the row
    ``escalated``; subsequent drains must not re-fire the alert.
    """

    async def test_escalation_fires_once_when_ttl_exceeded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fill = _park_out_of_band_fill(client_order_id="oob-1", first_seen_at=_SEEN_AT_771)
        async with session_factory() as sess:
            await append_unattributed_fill(sess, fill)
            await sess.commit()

        # 'now' is 2 hours past first_seen_at — well beyond the 30-min TTL.
        observed = _SEEN_AT_771 + timedelta(hours=2)
        with caplog.at_level("ERROR", logger=_DRAIN_LOG):
            await drain_unattributed_fills(
                session_factory=session_factory,
                escalation_ttl_seconds=1800,
                now=lambda: observed,
            )

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "ESCALATED" in errors[0].getMessage()

        async with session_factory() as sess:
            queued = await list_unattributed_fills(sess)
        assert len(queued) == 1
        assert queued[0].escalated is True

        # Second drain at an even later 'now': no further ERROR alert.
        caplog.clear()
        with caplog.at_level("ERROR", logger=_DRAIN_LOG):
            await drain_unattributed_fills(
                session_factory=session_factory,
                escalation_ttl_seconds=1800,
                now=lambda: observed + timedelta(hours=1),
            )
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    async def test_no_escalation_before_ttl(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fill = _park_out_of_band_fill(client_order_id="oob-2", first_seen_at=_SEEN_AT_771)
        async with session_factory() as sess:
            await append_unattributed_fill(sess, fill)
            await sess.commit()

        # 'now' is only 5 min after first_seen_at — under the 30-min TTL.
        observed = _SEEN_AT_771 + timedelta(minutes=5)
        with caplog.at_level("ERROR", logger=_DRAIN_LOG):
            await drain_unattributed_fills(
                session_factory=session_factory,
                escalation_ttl_seconds=1800,
                now=lambda: observed,
            )

        assert [r for r in caplog.records if r.levelname == "ERROR"] == []
        async with session_factory() as sess:
            queued = await list_unattributed_fills(sess)
        assert queued[0].escalated is False

    async def test_no_escalation_when_ttl_not_configured(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No escalation_ttl_seconds → fills accumulate retries indefinitely without escalating."""
        fill = _park_out_of_band_fill(client_order_id="oob-3", first_seen_at=_SEEN_AT_771)
        async with session_factory() as sess:
            await append_unattributed_fill(sess, fill)
            await sess.commit()

        # 'now' is years later — but no TTL configured.
        observed = _SEEN_AT_771 + timedelta(days=365)
        with caplog.at_level("ERROR", logger=_DRAIN_LOG):
            await drain_unattributed_fills(
                session_factory=session_factory,
                escalation_ttl_seconds=None,
                now=lambda: observed,
            )

        assert [r for r in caplog.records if r.levelname == "ERROR"] == []
        async with session_factory() as sess:
            queued = await list_unattributed_fills(sess)
        assert queued[0].escalated is False
