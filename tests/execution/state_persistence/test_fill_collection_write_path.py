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

from datetime import UTC, datetime, timedelta

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
from alphamind._kernel.money import money, price, signed_money
from alphamind.execution.broker_adapter.queries import (
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
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
    LocateStatus,
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
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.state.invocation_context.context import (
    InvocationContext,
    InvocationHandle,
)
from alphamind.state.invocation_context.records import (
    invocation_record_to_row,
    process_lifetime_record_to_row,
)
from alphamind.state.records import (
    CorporateActionLedgerStatus,
    FillProcessingStatus,
    FillRecord,
)
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.corporate_action_integration_ledger import (
    CorporateActionIntegrationLedgerRow,
)
from alphamind.state.tables.fill_records import FillRecordRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
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
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)
from tests.execution.state_persistence.conftest import (
    _make_cash_ledger,
    _make_invocation_record,
    _make_process_lifetime,
    _make_state_persistence_config,
    _seed_cash_ledger,
    _seed_drawdown_state,
)

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"
_PROCESS_ID = "proc-1"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_market_inputs() -> MarketInputs:
    """Minimal ``MarketInputs`` covering every ticker the file's positions use.

    The wedge in ``process_unprocessed_fills`` (story 06a / ALP-428) requires
    a price for every open-position underlying; equity-only tests need only
    a positive scalar per ticker (the IV provider is unused).
    """
    return MarketInputs(
        underlying_prices={"AAPL": 150.0, "MSFT": 400.0, "GOOG": 150.0},
        risk_free_rate=0.0425,
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        as_of=_NOW,
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
    limit_price: float | None = None,
) -> OrderRecord:
    # A non-marketable LIMIT entry (limit_price set) is the case that reserves
    # capital — Phase 1's buy-fill release drains the reservation by the entry's
    # ``limit_price * fill_quantity`` notional (ALP-741). A MARKET entry (the
    # default) reserves nothing, so its fill releases nothing.
    order_type = OrderType.LIMIT if limit_price is not None else OrderType.MARKET
    price_parameters = (
        PriceParameters(limit_price=price(limit_price))
        if limit_price is not None
        else PriceParameters()
    )
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id) if position_id else None,
        bracket_id=BracketId(bracket_id),
        role=role,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=direction,
        order_type=order_type,
        order_class=OrderClass.SIMPLE,
        price_parameters=price_parameters,
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=status,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=filled_quantity,
        avg_fill_price=avg_fill_price,
        remaining_quantity=quantity - filled_quantity,
        modification_count=0,
        originating_thesis_id=ThesisId("thesis-1"),
        originating_pm_command_id=None,
        age_hours=0.25,
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
    is_short = direction == Direction.SHORT
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
        borrow_rate_pct=0.0 if is_short else None,
        accrued_borrow_cost_usd=0.0 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=0.0 if is_short else None,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.PENDING,
        direction=direction,
        entry_timestamp=None,
        details=details,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
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
    borrow_rate_pct: float = 15.0,
    accrued_borrow_cost_usd: float = 0.0,
    margin_held_usd: float | None = None,
) -> PositionRecord:
    """Build an OPEN position whose execution_history reflects an entry fill.

    For SHORT positions the four short-only fields default to a borrow rate
    of 15%, zero accrued borrow cost, located locate status, and Reg T initial
    margin (share_count * average_cost_basis * 0.50).
    """
    from alphamind.portfolio_state.records.positions import PositionFill

    is_short = direction == Direction.SHORT
    resolved_margin_held = (
        margin_held_usd
        if margin_held_usd is not None
        else share_count * average_cost_basis_per_share * 0.50
    )
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
        borrow_rate_pct=borrow_rate_pct if is_short else None,
        accrued_borrow_cost_usd=accrued_borrow_cost_usd if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=resolved_margin_held if is_short else None,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=price(fill_price),
            fill_quantity=share_count,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.OPEN,
        direction=direction,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_terminal_position(
    position_id: str = "pos-1",
    *,
    status: PositionStatus = PositionStatus.CANCELLED,
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    direction: Direction = Direction.LONG,
    ticker: str = "AAPL",
) -> PositionRecord:
    """Build a terminal-status (CANCELLED / CLOSED) equity position with no fills.

    CANCELLED mirrors the prod poison-pill state from the 2026-06-01 incident
    (an entry cancelled + bracket dissolved mid-fill, leaving a partial fill that
    lands against a CANCELLED target). CLOSED is the other member of
    ``_NON_INTEGRATABLE_STATUSES`` — a fill landing against either is an orphan
    the pre-integration gate quarantines.
    """
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=0.0,
        average_cost_basis_per_share=0.0,
        borrow_rate_pct=None,
        accrued_borrow_cost_usd=None,
        locate_status=None,
        margin_held_usd=None,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=status,
        direction=direction,
        entry_timestamp=None,
        details=details,
        execution_history=(),
        # A CLOSED position must carry realized P/L (record invariant); a
        # CANCELLED one never opened, so it stays None.
        realized_pnl_to_date_usd=0.0 if status == PositionStatus.CLOSED else None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_pending_bracket(bracket_id: str = "brk-1", position_id: str = "pos-1") -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
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


def _make_active_thesis(
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
) -> ThesisRecord:
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
    time_expectation_hours = 24.0
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
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
        fill_price=price(fill_price),
        fill_quantity=fill_quantity,
        remaining_quantity_after=remaining_quantity_after,
        order_status_after=order_status_after,
        slippage_usd=None if slippage_usd is None else signed_money(slippage_usd),
        fees_usd=money(fees_usd),
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
    from tests.state._fk_substrate import stub_order_row

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


async def _append_fill(
    factory: async_sessionmaker[AsyncSession],
    fill: FillRecord,
) -> None:
    async with factory() as sess:
        await append_fill_record(sess, fill)
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str = _INV_ID + "-phase1",
) -> tuple[InvocationContext, InvocationHandle]:
    """Open an InvocationContext and return (ctx, handle).

    Caller is responsible for ``await ctx.__aexit__(None, None, None)`` after
    Phase 1 completes (or passing an exc to trigger rollback). Pass a distinct
    ``invocation_id`` when a test opens more than one handle in sequence — each
    handle inserts its own ``invocations`` row, so reusing the default id trips
    the primary-key UNIQUE constraint.
    """
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=invocation_id),
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
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        # Limit entry @ $150 reserves capital; the buy fill releases its notional
        # (150 * 10 = $1500) → CAPITAL_RELEASED is emitted (ALP-741).
        _make_pending_entry_order(limit_price=150.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
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


async def test_exit_fill_closes_position_and_leaves_thesis_active(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Happy-path exit fill: position OPEN → CLOSED, realized P/L computed,
    bracket DISSOLVED, linked thesis left ACTIVE (ALP-834 — thesis resolution
    is owned by the analysis pipeline, not execution)."""
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    close_order = _make_pending_entry_order(
        order_id=OrderId("ord-close-1"),
        role=OrderRole.CLOSE,
        direction=OrderDirection.SELL,
        position_id=PositionId("pos-1"),
    )
    # All four entities reference each other cyclically — seed in one transaction.
    # _make_active_bracket uses entry_order_id=OrderId("ord-entry-1") and a protective leg
    # with order_id=OrderId("brk-1-ord-stop"), so we need stubs for all referenced orders.
    from tests.state._fk_substrate import stub_order_row

    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, close_order.order_id}
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
            order_id=OrderId("ord-close-1"),
            fill_price=160.0,
            fill_quantity=10.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
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

        # Thesis is left ACTIVE — resolution (category, P/L, component outcomes)
        # is authored later by the analysis pipeline (ALP-131), not execution.
        thesis_row = (
            await sess.execute(select(ThesisRow).where(ThesisRow.thesis_id == "thesis-1"))
        ).scalar_one()
        assert thesis_row.status == ThesisRecordStatus.ACTIVE.value
        assert thesis_row.resolution_timestamp is None
        assert thesis_row.resolution_category is None

        # Activity log carries position_closed + bracket_dissolved; no thesis_resolved.
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
    assert EventType.THESIS_RESOLVED.value not in types
    assert EventType.CASH_CREDITED.value in types
    assert EventType.ORDER_FILLED.value in types


async def test_take_profit_leg_fill_marks_leg_filled_and_closes_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-746 — a protective TAKE_PROFIT leg fill (the fill the consumer now
    resolves to the leg row by captured UUID) integrates through Phase 1: the
    leg order transitions to FILLED and the OPEN position closes out."""
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )
    from tests.state._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)
    tp_order = _make_pending_entry_order(
        order_id=OrderId("ord-tp-1"),
        role=OrderRole.TAKE_PROFIT,
        direction=OrderDirection.SELL,
        position_id=PositionId("pos-1"),
    )
    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, tp_order.order_id}
    async with factory() as sess:
        sess.add(position_record_to_row(_make_open_position()))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(entry_order))
        sess.add(order_record_to_row(tp_order))
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
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-tp-1",
            order_id=OrderId("ord-tp-1"),
            fill_price=160.0,
            fill_quantity=10.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        tp_row = await sess.get(OrderRow, "ord-tp-1")
        assert tp_row is not None
        assert tp_row.status == OrderStatus.FILLED.value
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        assert position_row_to_record(pos_row).status == PositionStatus.CLOSED


async def test_multi_fill_ordering_produces_cumulative_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Two unprocessed fills on the same order, processed in fill-timestamp
    order, produce the right cumulative state."""
    from alphamind.execution.write_paths.fill_collection import (
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
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
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
    from alphamind.execution.write_paths.fill_collection import (
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
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=4.0,  # 4-for-1 split.
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        ca_activities=(ca,),
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
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


async def test_failed_fill_quarantined_leaves_no_lifecycle_log_entries(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill whose integration raises is quarantined, not propagated (ALP-761).

    The trigger is a closing fill against a PENDING position — the equity
    dispatcher raises ``ValueError`` ("a position cannot close before it
    opens"). The per-fill savepoint rolls back the fill's partial mutations and
    *every* lifecycle activity-log entry it had started to emit, so the only
    activity-log row left for the invocation is the reconciliation alert that
    surfaces the orphan. Phase-1 completes normally.
    """
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # PENDING LONG position + a SELL_TO_OPEN order routes through the new
    # dispatcher's defensive ValueError ("closing fill on PENDING position").
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(
            order_id=OrderId("ord-bogus-sell"),
            direction=OrderDirection.SELL_TO_OPEN,
            position_id=PositionId("pos-1"),
        ),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(
        factory,
        _make_unprocessed_fill(fill_id="fill-1", order_id=OrderId("ord-bogus-sell")),
    )

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    # Phase-1 returns normally — the ValueError did not escape.
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 0
    assert summary.fills_quarantined == 1

    async with factory() as sess:
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-1"))
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.QUARANTINED.value
        assert fill_row.processing_invocation_id == invocation_id

        # The savepoint rolled back every lifecycle entry the fill had begun to
        # emit; only the reconciliation alert remains.
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )
        assert [r.event_type for r in log_rows] == [EventType.RECONCILIATION_ALERT.value]
    await _assert_phase1_completed(factory, invocation_id)


async def test_quarantined_fill_excluded_without_aborting_batch(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill with negative quantity is marked quarantined and excluded from
    integration; other fills in the batch still process normally."""
    from alphamind.execution.write_paths.fill_collection import (
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
        order_id=OrderId("ord-entry-1"),
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
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
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
    reserved_capital_usd by the entry's reserved notional (``limit_price *
    fill_quantity``, ALP-741), NOT the fill consideration. Without this, Phase 2's
    OPEN reserve and Phase 1's fill double-count: current_cash drops AND
    reserved_capital stays — overstating committed capital.
    """
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        # Limit entry @ $100 over 10 shares: $1000 reserved at OPEN, released here.
        _make_pending_entry_order(quantity=10.0, limit_price=100.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # Seed cash with a $1000 reservation already in place (mirroring Phase 2's OPEN).
    seeded = CashLedger(
        current_cash_usd=100_000.0,
        settled_cash_usd=100_000.0,
        reserved_capital_usd=1_000.0,
        available_buying_power_usd=99_000.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
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
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
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
    """When the buy-fill's released notional (``limit_price * fill_quantity``)
    exceeds the seeded reservation (partial reservations, rounding, mid-flight
    reprice), the decrement must clamp at zero rather than going negative
    (ALP-741 defensive floor).
    """
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        # Limit entry @ $100 over 10 shares releases $1000 — more than the $500 seeded.
        _make_pending_entry_order(quantity=10.0, limit_price=100.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # Seed only $500 reserved while the fill releases $1000.
    seeded = CashLedger(
        current_cash_usd=100_000.0,
        settled_cash_usd=100_000.0,
        reserved_capital_usd=500.0,
        available_buying_power_usd=99_500.0,
        margin_held_usd=0.0,
        unsettled_proceeds=(),
        cash_pct_of_portfolio=0.0,
        true_deployable_capital_usd=0.0,
        regt_excess_trailing_30d_usd=0.0,
        regt_excess_trailing_90d_usd=0.0,
        regt_excess_lifetime_usd=0.0,
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
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(0.0)


async def test_reprice_then_fill_returns_reserved_capital_to_zero(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-741 acceptance — reprice-then-fill path: an entry reserved at OPEN,
    repriced to a new limit, then filled at *yet another* price releases exactly
    its repriced reserved notional, so ``reserved_capital_usd`` returns to 0 and
    is never negative.

    Lifecycle: OPEN reserves ``100 * 10 = $1000`` (seeded). The entry-window
    repricer drops the limit ``100 -> 95`` (reserved ``1000 -> 950``). The fill
    then prints at ``$92`` — *below* the limit — but the reservation release
    tracks the order's ``$95`` limit (``95 * 10 = $950``), NOT the ``$92`` fill
    consideration, so reserved nets to exactly 0. Before ALP-741 the fill
    released the consideration (``92 * 10 = $920``), stranding ``$30`` in the
    reservation pool — the kind of drift that, accumulated over reprices, drove
    the singleton negative and crashed every decision-pipeline invocation.
    """
    from alphamind.execution.write_paths.command_execution import (
        persist_entry_window_reprice,
    )
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(quantity=10.0, limit_price=100.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # OPEN reserved the entry notional 100 * 10 = $1000 (mirrors Phase 2).
    await _seed_cash_ledger(
        factory, _make_cash_ledger(current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    )
    await _seed_drawdown_state(factory)

    # Reprice the resting entry 100 -> 95: reserved 1000 -> 950.
    ctx, handle = await _open_handle(factory, invocation_id=_INV_ID + "-reprice")
    await persist_entry_window_reprice(
        handle,
        entry_order_id="ord-entry-1",
        new_limit_price=price("95.0"),
        new_alpaca_order_id="alp-repriced",
        reprice_reason="entry_window_reprice",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(950.0)
        assert cash.reserved_capital_usd >= 0

    # Fill the full 10 shares at $92 — different from the $95 limit.
    await _append_fill(
        factory,
        _make_unprocessed_fill(fill_id="fill-1", fill_price=92.0, fill_quantity=10.0),
    )
    ctx, handle = await _open_handle(factory, invocation_id=_INV_ID + "-fill")
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        # Release = 95 (repriced limit) * 10 = 950 → reserved nets to exactly 0,
        # NOT 950 - 92*10 = 30 (the pre-fix consideration-basis residual).
        assert float(cash.reserved_capital_usd) == pytest.approx(0.0)
        assert cash.reserved_capital_usd >= 0
        # Cash debit is the real consideration (92 * 10 = 920), a separate field —
        # independent of the 950 reservation released above.
        assert float(cash.current_cash_usd) == pytest.approx(100_000.0 - 920.0)


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
    position_row = position_record_to_row(_make_pending_position())  # bracket_id=BracketId("brk-1")
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
    position_row = position_record_to_row(_make_open_position())  # bracket_id=BracketId("brk-1")
    with pytest.raises(IntegrityError):
        async with factory() as sess:
            sess.add(position_row)
            await sess.commit()


async def test_open_position_with_missing_thesis_row_rejected_at_commit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """FK enforcement prevents committing a position that references a non-existent
    thesis row — deferred FK on positions.thesis_id raises IntegrityError at COMMIT.
    This is the schema-level guard ensuring a position can never reference a
    missing thesis row.
    """
    from sqlalchemy.exc import IntegrityError

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Attempt to commit an OPEN position pointing at a thesis that does not exist.
    position_row = position_record_to_row(_make_open_position())  # thesis_id=ThesisId("thesis-1")
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
    # _make_open_position uses bracket_id=BracketId("brk-1") by default.
    position_row = position_record_to_row(_make_open_position(share_count=10.0))
    with pytest.raises(IntegrityError):
        async with factory() as sess:
            sess.add(position_row)
            await sess.commit()


async def test_short_entry_fill_transitions_pending_short_to_open_with_stamped_fields(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A SELL_TO_OPEN fill against a PENDING SHORT EQUITY position transitions
    it to OPEN and stamps the four short-only fields from the borrow-cost
    resolver: borrow_rate_pct, accrued_borrow_cost_usd=0.0,
    locate_status=LOCATED, margin_held_usd = qty * price * 0.50 (Reg T
    initial margin).
    """
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )
    from alphamind.portfolio_state.records.positions import LocateStatus

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(direction=Direction.SHORT),
        _make_pending_entry_order(
            order_id=OrderId("ord-short-entry"),
            direction=OrderDirection.SELL_TO_OPEN,
            position_id=PositionId("pos-1"),
        ),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(
        factory,
        _make_unprocessed_fill(fill_id="fill-short-1", order_id=OrderId("ord-short-entry")),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
        borrow_cost_resolver=lambda _ticker: 15.0,
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 0

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert pos.direction == Direction.SHORT
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == 10.0
        assert pos.details.average_cost_basis_per_share == 150.0
        # Four short-only fields stamped per the design doc.
        assert pos.details.borrow_rate_pct == 15.0
        assert pos.details.accrued_borrow_cost_usd == 0.0
        assert pos.details.locate_status == LocateStatus.LOCATED
        # Reg T initial margin: 10 * 150 * 0.50 = 750.
        assert pos.details.margin_held_usd == pytest.approx(10.0 * 150.0 * 0.50)


# ---------------------------------------------------------------------------
# Unit-level coverage of the equity dispatcher's 8-case routing matrix
# (status x direction x buy_side) + entry-fill resolver contracts. These
# tests skip the DB substrate and call the dispatcher helpers directly so
# the matrix coverage stays focused and fast (ALP-717).
# ---------------------------------------------------------------------------


def _make_fill_record(
    *,
    fill_id: str = "fill-direct",
    order_id: str = "ord-direct",
    fill_quantity: float = 10.0,
    fill_price: float = 150.0,
) -> FillRecord:
    return FillRecord(
        fill_id=fill_id,
        order_id=OrderId(order_id),
        fill_timestamp=_NOW - timedelta(minutes=5),
        fill_price=price(fill_price),
        fill_quantity=fill_quantity,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=signed_money(0.0),
        fees_usd=money(0.0),
        execution_venue="NASDAQ",
        gateway_reference=f"alp-{fill_id}",
        persistence_timestamp=_NOW,
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


def test_dispatcher_pending_long_buy_routes_to_entry_fill() -> None:
    """PENDING LONG + BUY → entry-fill (opens the LONG position)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.LONG)
    fill = _make_fill_record()
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    assert result.details.share_count == 10.0


def test_dispatcher_pending_long_sell_raises_value_error() -> None:
    """PENDING LONG + SELL → ValueError ("cannot close before it opens")."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.LONG)
    fill = _make_fill_record()
    with pytest.raises(ValueError, match="cannot close before it opens") as exc_info:
        _apply_fill_to_position(position, fill, is_buy_side=False, borrow_cost_resolver=None)
    # Defensive message names the position id.
    assert "pos-1" in str(exc_info.value)


def test_dispatcher_pending_short_buy_raises_value_error() -> None:
    """PENDING SHORT + BUY → ValueError ("cannot close before it opens")."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.SHORT)
    fill = _make_fill_record()
    with pytest.raises(ValueError, match="cannot close before it opens") as exc_info:
        _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert "pos-1" in str(exc_info.value)


def test_dispatcher_pending_short_sell_routes_to_entry_fill() -> None:
    """PENDING SHORT + SELL → entry-fill (SELL_TO_OPEN opens the short)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.SHORT)
    fill = _make_fill_record()
    result = _apply_fill_to_position(
        position, fill, is_buy_side=False, borrow_cost_resolver=lambda _t: 12.5
    )
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    assert result.details.share_count == 10.0
    assert result.details.borrow_rate_pct == 12.5


def test_dispatcher_open_long_buy_routes_to_add_fill() -> None:
    """OPEN LONG + BUY → add-fill (grow the long position)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(direction=Direction.LONG, share_count=10.0)
    fill = _make_fill_record(fill_quantity=5.0, fill_price=160.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    assert result.details.share_count == 15.0


def test_dispatcher_open_long_sell_routes_to_exit_fill() -> None:
    """OPEN LONG + SELL → exit-fill (reduce / close the long position)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(direction=Direction.LONG, share_count=10.0)
    fill = _make_fill_record(fill_quantity=10.0, fill_price=160.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=False, borrow_cost_resolver=None)
    assert result.status == PositionStatus.CLOSED


def test_dispatcher_open_short_buy_routes_to_exit_fill() -> None:
    """OPEN SHORT + BUY → exit-fill (cover-to-close)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(direction=Direction.SHORT, share_count=10.0)
    fill = _make_fill_record(fill_quantity=10.0, fill_price=140.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.CLOSED


def test_dispatcher_open_short_sell_routes_to_add_fill() -> None:
    """OPEN SHORT + SELL → add-fill (grow the short position)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(direction=Direction.SHORT, share_count=10.0)
    fill = _make_fill_record(fill_quantity=5.0, fill_price=140.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=False, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    assert result.details.share_count == 15.0


def test_entry_fill_short_with_none_resolver_raises_value_error() -> None:
    """SHORT entry with ``borrow_cost_resolver=None`` raises ValueError naming
    the missing resolver — short stamping cannot proceed without a rate."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.SHORT, ticker="CRWD")
    fill = _make_fill_record()
    with pytest.raises(ValueError, match="requires a borrow_cost_resolver"):
        _apply_fill_to_position(position, fill, is_buy_side=False, borrow_cost_resolver=None)


def test_entry_fill_short_with_resolver_returning_none_raises_value_error() -> None:
    """SHORT entry with a resolver that returns ``None`` for the ticker
    raises ValueError naming the upstream contract violation."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.SHORT, ticker="CRWD")
    fill = _make_fill_record()
    with pytest.raises(ValueError, match="upstream contract violation"):
        _apply_fill_to_position(
            position,
            fill,
            is_buy_side=False,
            borrow_cost_resolver=lambda _t: None,
        )


def test_entry_fill_long_with_none_resolver_succeeds() -> None:
    """LONG entry with ``borrow_cost_resolver=None`` succeeds — the LONG
    path never consults the resolver."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_pending_position(direction=Direction.LONG)
    fill = _make_fill_record()
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    # LONG positions never carry the short-only fields.
    assert result.details.borrow_rate_pct is None
    assert result.details.accrued_borrow_cost_usd is None
    assert result.details.locate_status is None
    assert result.details.margin_held_usd is None


def test_exit_fill_short_cover_to_close_flushes_accrued_borrow_into_pnl() -> None:
    """Cover-to-close on an OPEN SHORT with ``accrued_borrow_cost_usd=5.0``:
    entry $100, exit $90, qty 10 → realized_pnl = (100-90)*10 - 5 = 95.0.
    Position transitions to CLOSED; accrued_borrow_cost_usd is preserved as
    the lifetime borrow total (NOT zeroed out post-flush)."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(
        direction=Direction.SHORT,
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        accrued_borrow_cost_usd=5.0,
    )
    fill = _make_fill_record(fill_quantity=10.0, fill_price=90.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.CLOSED
    # Phase1 computes: pnl_per_share = exit - entry = 90 - 100 = -10;
    # direction_sign(SHORT) = -1; realized = (-10) * 10 * (-1) - 5 = 95.
    # Equivalent intuition: SHORT profits when price falls, so the per-share
    # gain is +10 (entry - exit); minus the $5 borrow drag yields 95.
    assert result.realized_pnl_to_date_usd == pytest.approx(95.0)
    assert isinstance(result.details, EquityPositionDetails)
    # Lifetime borrow total is preserved post-flush (audit-trail readers
    # consume both fields).
    assert result.details.accrued_borrow_cost_usd == pytest.approx(5.0)


def test_exit_fill_short_cover_to_close_with_zero_accrued_skips_flush() -> None:
    """Cover-to-close on an OPEN SHORT with ``accrued_borrow_cost_usd=0.0``:
    no subtraction (zero falsy short-circuits the flush branch); realized
    P/L is ``(entry - exit) * qty`` only."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(
        direction=Direction.SHORT,
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        accrued_borrow_cost_usd=0.0,
    )
    fill = _make_fill_record(fill_quantity=10.0, fill_price=90.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.CLOSED
    # pnl_per_share = exit - entry = 90 - 100 = -10; direction_sign(SHORT) = -1;
    # realized = (-10) * 10 * (-1) - 0 = 100 (zero accrued falsy → flush skipped).
    assert result.realized_pnl_to_date_usd == pytest.approx(100.0)
    assert isinstance(result.details, EquityPositionDetails)
    assert result.details.accrued_borrow_cost_usd == pytest.approx(0.0)


def test_exit_fill_short_partial_cover_leaves_position_open_no_flush() -> None:
    """Partial cover on an OPEN SHORT leaves the position OPEN,
    ``accrued_borrow_cost_usd`` unchanged, and no borrow flush. The
    direction sign still applies to the partial realized P/L delta."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(
        direction=Direction.SHORT,
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        accrued_borrow_cost_usd=5.0,
    )
    fill = _make_fill_record(fill_quantity=4.0, fill_price=90.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=True, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    assert result.details.share_count == pytest.approx(6.0)
    # pnl_per_share = exit - entry = 90 - 100 = -10; direction_sign(SHORT) = -1;
    # partial realized = (-10) * 4 * (-1) = +40 (SHORT profits when price falls).
    # No borrow flush on a partial cover.
    assert result.realized_pnl_to_date_usd == pytest.approx(40.0)
    # Accrued unchanged on partial cover.
    assert result.details.accrued_borrow_cost_usd == pytest.approx(5.0)


def test_add_fill_open_short_preserves_borrow_fields() -> None:
    """ADD on an OPEN SHORT leaves ``borrow_rate_pct`` and
    ``accrued_borrow_cost_usd`` unchanged — the accumulator continues
    against the post-ADD combined notional from the next tick onward,
    and the borrow rate is not re-stamped per the design doc."""
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    position = _make_open_position(
        direction=Direction.SHORT,
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=15.0,
        accrued_borrow_cost_usd=3.5,
    )
    fill = _make_fill_record(fill_quantity=5.0, fill_price=110.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=False, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    # Weighted-average cost basis: (100*10 + 110*5) / 15 = 1550 / 15 = 103.333...
    assert result.details.share_count == pytest.approx(15.0)
    assert result.details.average_cost_basis_per_share == pytest.approx(1550.0 / 15.0)
    # borrow_rate_pct and accrued_borrow_cost_usd are NOT mutated.
    assert result.details.borrow_rate_pct == pytest.approx(15.0)
    assert result.details.accrued_borrow_cost_usd == pytest.approx(3.5)


def test_add_fill_open_short_preserves_margin_held_usd_entry_snapshot() -> None:
    """ADD on an OPEN SHORT leaves ``margin_held_usd`` unchanged — the field
    is the Reg T initial margin stamped at entry (``qty * entry_price *
    0.50``), not a live required-margin recomputation.

    Per the design doc + ``EquityPositionDetails`` docstring, readers needing
    the current required margin should compute it on the fly from
    ``share_count`` and the live close. The persistent field is a frozen
    entry snapshot for audit / attribution; ADD fills must NOT mutate it.

    This test pins that semantic: with a position whose ``margin_held_usd``
    captures the entry-stamp value (10 * 100 * 0.50 = 500), an ADD of 5
    shares at 110 must leave the field at 500 — even though the share count
    grows to 15 and the average cost shifts.
    """
    from alphamind.execution.write_paths.fill_collection import _apply_fill_to_position

    entry_margin_held = 10.0 * 100.0 * 0.50  # 500.0 — Reg T stamp at entry
    position = _make_open_position(
        direction=Direction.SHORT,
        share_count=10.0,
        average_cost_basis_per_share=100.0,
        margin_held_usd=entry_margin_held,
    )
    fill = _make_fill_record(fill_quantity=5.0, fill_price=110.0)
    result = _apply_fill_to_position(position, fill, is_buy_side=False, borrow_cost_resolver=None)
    assert result.status == PositionStatus.OPEN
    assert isinstance(result.details, EquityPositionDetails)
    # margin_held_usd is preserved verbatim — NOT recomputed against the
    # post-ADD share count or the new fill price.
    assert result.details.margin_held_usd == pytest.approx(entry_margin_held)
    # locate_status — also preserved verbatim on ADD (sanity check).
    assert result.details.locate_status == LocateStatus.LOCATED


async def test_phase1_stamps_completion_timestamp_on_invocation_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """After process_unprocessed_fills commits, the bound invocation row's
    fill_collection_completed_at must be a valid ISO-8601 UTC timestamp — the SQL
    repository's snapshot-isolation guard reads this column and raises
    RepositoryConsistencyError when it is NULL.
    """
    from alphamind.execution.write_paths.fill_collection import (
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
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        row = await sess.get(InvocationRow, invocation_id)
        assert row is not None
        assert row.fill_collection_completed_at is not None
        # Must round-trip through fromisoformat (covers both Z-suffix and +00:00 forms).
        parsed = datetime.fromisoformat(row.fill_collection_completed_at)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# ALP-415: chronological fill + CA merge + reconciliation
# ---------------------------------------------------------------------------


def _alpaca_account_snapshot(*, cash: float = 100_000.0) -> TradeAccountSnapshot:
    """Build a typed ``TradeAccountSnapshot`` for the new entry-point signature."""
    return TradeAccountSnapshot(
        account_id="alp-account-1",
        cash=money(cash),
        equity=money(cash),
        buying_power=money(cash * 2.0),
        regt_buying_power=money(cash * 2.0),
        daytrading_buying_power=money(cash * 4.0),
        maintenance_margin=money(0.0),
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
        avg_entry_price=price(150.0),
        market_value=money(qty * 150.0),
        cost_basis=money(qty * 150.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(150.0),
        side="long",
    )


async def test_snapshot_mismatch_rebuilds_without_alert_or_correction(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-854 / W2a AC1 — a snapshot/projection quantity (and cash) mismatch
    triggers a projection *rebuild*, never the deleted ``reconcile()``
    adjudication: NO ``RECONCILIATION_ALERT`` / ``RECONCILIATION_CORRECTION`` row
    is inserted, the local ``share_count`` is left as the event-log-derived value
    (not auto-corrected to Alpaca's), and ``reconciliation_alerts`` stays 0."""
    from alphamind.execution.write_paths.fill_collection import (
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
        # 10 shares local vs 9 shares Alpaca, plus a cash mismatch: the old
        # adjudication would have emitted an alert + correction and zeroed the
        # local share_count toward Alpaca. The rebuild does neither.
        alpaca_positions=(_alpaca_equity_snapshot(qty=9.0),),
        alpaca_account=_alpaca_account_snapshot(cash=50_000.0),
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    # No reconcile-adjudication alerts contributed to the count.
    assert summary.reconciliation_alerts == 0

    async with factory() as sess:
        recon_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type.in_(
                            (
                                EventType.RECONCILIATION_ALERT.value,
                                EventType.RECONCILIATION_CORRECTION.value,
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert recon_rows == []
        # The local projection is NOT auto-corrected toward Alpaca's 9 — the
        # "Alpaca wins" writeback is deleted (ADR-0001).
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(10.0)


async def test_fill_before_ca_reflects_pre_action_quantity_at_fill(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A fill timestamped before a CA's transaction_time integrates first.

    Setup: OPEN position with 10 shares. Add fill (BUY +5 shares) at T-30min.
    A SPLIT 2-for-1 CA at T-10min applies after. Post-state:
      * pre-CA share_count = 10 + 5 = 15 (entry+add fill applied first)
      * post-CA share_count = 15 * 2 = 30 (split applied second)
    """
    from alphamind.execution.write_paths.fill_collection import (
        CorporateActionActivity,
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Open position with 10 shares + an entry order already filled.
    add_order = _make_pending_entry_order(
        order_id=OrderId("ord-add-1"),
        role=OrderRole.ADD_ENTRY,
        position_id=PositionId("pos-1"),
        quantity=5.0,
    )
    from tests.state._fk_substrate import stub_order_row

    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, add_order.order_id}
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
            order_id=OrderId("ord-add-1"),
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
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=2.0,  # 2-for-1 split.
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=10),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        ca_activities=(ca,),
        alpaca_positions=(),
        alpaca_account=None,
        market_inputs=_make_market_inputs(),
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
    from alphamind.execution.write_paths.fill_collection import (
        CorporateActionActivity,
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    add_order = _make_pending_entry_order(
        order_id=OrderId("ord-add-2"),
        role=OrderRole.ADD_ENTRY,
        position_id=PositionId("pos-1"),
        quantity=5.0,
    )
    from tests.state._fk_substrate import stub_order_row

    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, add_order.order_id}
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
            order_id=OrderId("ord-add-2"),
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
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=2.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=30),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        ca_activities=(ca,),
        alpaca_positions=(),
        alpaca_account=None,
        market_inputs=_make_market_inputs(),
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


# ---------------------------------------------------------------------------
# ALP-761 — orphan fill against a terminal position must not wedge the batch
# ---------------------------------------------------------------------------


async def _read_reconciliation_alerts(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> list[ActivityLogRow]:
    async with factory() as sess:
        return list(
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.invocation_id == invocation_id,
                        ActivityLogRow.event_type == EventType.RECONCILIATION_ALERT.value,
                    )
                )
            )
            .scalars()
            .all()
        )


async def _assert_phase1_completed(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> None:
    async with factory() as sess:
        inv_row = await sess.get(InvocationRow, invocation_id)
        assert inv_row is not None
        assert inv_row.fill_collection_completed_at is not None


@pytest.mark.parametrize(
    "terminal_status",
    [PositionStatus.CANCELLED, PositionStatus.CLOSED],
)
async def test_fill_against_terminal_position_quarantined_not_raised(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    terminal_status: PositionStatus,
) -> None:
    """AC (a): a fill whose target equity position is in a terminal status
    (CANCELLED or CLOSED) is quarantined (not raised), fills_quarantined
    increments, a reconciliation alert is emitted, and Phase-1 completes
    normally. Parametrized over both members of _NON_INTEGRATABLE_STATUSES."""
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Entry order resolves its position via the bracket (position_id is None on
    # an entry order); the bracket points at a terminal-status position — the
    # prod poison-pill shape.
    await _seed_position_order_thesis_bracket(
        factory,
        _make_terminal_position(status=terminal_status),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-orphan", fill_quantity=19.0))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    # Phase-1 returns normally — no ValueError escapes.
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 0
    assert summary.fills_quarantined == 1
    # The quarantine alert is counted in the summary (ALP-761).
    assert summary.reconciliation_alerts == 1

    async with factory() as sess:
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-orphan"))
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.QUARANTINED.value
        assert fill_row.processing_invocation_id == handle.invocation_id
        # Quarantined fills retain regt_attribution_json IS NULL (parent decision H).
        assert fill_row.regt_attribution_json is None
        # The terminal position is untouched — no fill integrated into it.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        assert pos_row.status == terminal_status.value

    alerts = await _read_reconciliation_alerts(factory, handle.invocation_id)
    assert len(alerts) == 1
    assert alerts[0].position_id == "pos-1"
    assert "quarantined" in alerts[0].detail_json
    assert "fill-orphan" in alerts[0].detail_json
    await _assert_phase1_completed(factory, handle.invocation_id)


async def test_mixed_batch_poison_pill_quarantined_healthy_processed(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC (b): a batch with one poison-pill fill (target CANCELLED) plus a
    healthy entry fill — the healthy fill integrates, the poison is quarantined,
    and the invocation completes."""
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Healthy PENDING cluster (AVGO/GS analogue from the incident).
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(limit_price=150.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # Poison: a standalone CANCELLED position + an entry order that points
    # directly at it (DVN analogue).
    await _seed_position(
        factory,
        _make_terminal_position(position_id="pos-dvn", thesis_id=None, bracket_id=None),
    )
    await _seed_order(
        factory,
        _make_pending_entry_order(order_id="ord-dvn", position_id="pos-dvn"),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Poison fill sorts first (earlier timestamp); the healthy fill follows.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-dvn",
            order_id="ord-dvn",
            fill_quantity=19.0,
            fill_price=46.15,
            fill_timestamp=_NOW - timedelta(minutes=20),
            order_status_after=OrderStatus.PARTIALLY_FILLED,
            remaining_quantity_after=1.0,
        ),
    )
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-good"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 1

    async with factory() as sess:
        dvn = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-dvn"))
        ).scalar_one()
        good = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-good"))
        ).scalar_one()
        assert dvn.processing_status == FillProcessingStatus.QUARANTINED.value
        assert good.processing_status == FillProcessingStatus.PROCESSED.value
        # The healthy position opened on its entry fill.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == 10.0

    alerts = await _read_reconciliation_alerts(factory, handle.invocation_id)
    assert len(alerts) == 1
    assert alerts[0].position_id == "pos-dvn"
    await _assert_phase1_completed(factory, handle.invocation_id)


async def test_unexpected_integration_error_isolated_to_single_fill(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """AC (c): a fill that raises mid-integration (here, a closing fill against a
    PENDING position) is isolated by the per-fill savepoint — its partial order
    mutation is rolled back, the fill is quarantined + alerted, and a later
    healthy fill in the same batch still processes."""
    from alphamind.execution.write_paths.fill_collection import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(limit_price=150.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    # A SELL order against the still-PENDING LONG position: a fill on it is a
    # "closing fill on a PENDING position", which _apply_fill_to_equity_position
    # raises on — an integration failure that is NOT the terminal-status gate.
    await _seed_order(
        factory,
        _make_pending_entry_order(
            order_id="ord-sell",
            direction=OrderDirection.SELL,
            role=OrderRole.CLOSE,
            position_id="pos-1",
        ),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Poison sorts first (earlier ts) so the position is still PENDING when it
    # hits — proving the loop continues to the later healthy entry fill.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-poison",
            order_id="ord-sell",
            fill_quantity=5.0,
            fill_timestamp=_NOW - timedelta(minutes=20),
            order_status_after=OrderStatus.PARTIALLY_FILLED,
            remaining_quantity_after=5.0,
        ),
    )
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-good"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 1

    async with factory() as sess:
        poison = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-poison"))
        ).scalar_one()
        good = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-good"))
        ).scalar_one()
        assert poison.processing_status == FillProcessingStatus.QUARANTINED.value
        assert good.processing_status == FillProcessingStatus.PROCESSED.value
        # Savepoint rollback: the poison fill's partial order mutation is undone —
        # ord-sell is back to PENDING with zero filled quantity.
        sell_row = (
            await sess.execute(select(OrderRow).where(OrderRow.order_id == "ord-sell"))
        ).scalar_one()
        assert sell_row.status == OrderStatus.PENDING.value
        assert sell_row.filled_quantity == 0.0
        # The later healthy fill still opened the position.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        assert pos_row.status == PositionStatus.OPEN.value

    alerts = await _read_reconciliation_alerts(factory, handle.invocation_id)
    assert len(alerts) == 1
    assert "fill-poison" in alerts[0].detail_json
    await _assert_phase1_completed(factory, handle.invocation_id)


async def test_over_fill_quarantined_not_integrated(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-766: a fill whose quantity exceeds the order's remaining quantity is
    quarantined, not integrated; share_count and order.filled_quantity are unchanged.

    Reproduces the GS 3-partials + recovery-sweep aggregate case: three genuine
    per-execution fills of 1 share each were persisted and processed; the recovery
    sweep appended a 4th aggregate fill (qty 3 @ avg price) that the dedupe
    constraint could not suppress.  Phase-1 must quarantine it at the
    pre-integration gate, leaving share_count=3 and order.filled_quantity=3.
    """
    from alphamind.execution.write_paths.fill_collection import process_unprocessed_fills

    _, factory = db
    await _seed_invocation_substrate(factory)
    # GS position: already OPEN with 3 shares (the 3 genuine partials integrated).
    await _seed_position_order_thesis_bracket(
        factory,
        _make_open_position(share_count=3.0, average_cost_basis_per_share=1021.93),
        _make_pending_entry_order(
            quantity=3.0,
            filled_quantity=3.0,
            status=OrderStatus.FILLED,
        ),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    # Phantom aggregate fill: recovery sweep emitted cumulative qty @ avg price.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-phantom-agg",
            fill_quantity=3.0,
            fill_price=1021.93,
            remaining_quantity_after=0.0,
            order_status_after=OrderStatus.FILLED,
        ),
    )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 0
    assert summary.fills_quarantined == 1
    assert summary.reconciliation_alerts == 1

    async with factory() as sess:
        fill_row = (
            await sess.execute(
                select(FillRecordRow).where(FillRecordRow.fill_id == "fill-phantom-agg")
            )
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.QUARANTINED.value

        # Position share_count unchanged — the phantom did not integrate.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.share_count == pytest.approx(3.0)
        assert pos.status == PositionStatus.OPEN

        # Order filled_quantity unchanged — the phantom did not integrate.
        order_row = (
            await sess.execute(select(OrderRow).where(OrderRow.order_id == "ord-entry-1"))
        ).scalar_one()
        assert order_row.filled_quantity == pytest.approx(3.0)
        assert order_row.remaining_quantity == pytest.approx(0.0)
        assert order_row.status == OrderStatus.FILLED.value

    alerts = await _read_reconciliation_alerts(factory, handle.invocation_id)
    assert len(alerts) == 1
    assert "fill-phantom-agg" in alerts[0].detail_json
    assert "ALP-766" in alerts[0].detail_json
    await _assert_phase1_completed(factory, handle.invocation_id)


async def test_buy_fill_mirrors_settled_cash_alongside_current_cash(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-778 — a buy fill that debits current_cash_usd must also update
    settled_cash_usd to the same value.

    Prior to the fix, _apply_cash_movement only wrote current_cash_usd;
    settled stayed frozen at the seed value, overstating deployable capital
    between reconciliation runs.
    """
    from alphamind.execution.write_paths.fill_collection import process_unprocessed_fills

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_position(),
        _make_pending_entry_order(limit_price=150.0),
        _make_active_thesis(),
        _make_pending_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Buy 10 shares @ $150 = $1500 debit.
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-1"))

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 - 1500.0)
        # ALP-778: settled must track current after every fill.
        assert cash_row.settled_cash_usd == pytest.approx(100_000.0 - 1500.0)


async def test_sell_fill_mirrors_settled_cash_alongside_current_cash(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-778 — a sell fill that credits current_cash_usd must also update
    settled_cash_usd to the same value."""
    from alphamind.execution.write_paths.fill_collection import process_unprocessed_fills
    from tests.state._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)
    close_order = _make_pending_entry_order(
        order_id=OrderId("ord-close-1"),
        role=OrderRole.CLOSE,
        direction=OrderDirection.SELL,
        position_id=PositionId("pos-1"),
    )
    entry_order = _make_pending_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, close_order.order_id}
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
    # Start with cash reflecting the cost of the open position (10 * $150 = $1500 out).
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=98_500.0))
    await _seed_drawdown_state(factory)
    # Sell 10 shares @ $160 = $1600 credit (net P/L = $100).
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-close-1",
            order_id=OrderId("ord-close-1"),
            fill_price=160.0,
            fill_quantity=10.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        # Sell 10 @ $160 = $1600 credit; 98_500 + 1600 = 100_100.
        assert cash_row.current_cash_usd == pytest.approx(98_500.0 + 1600.0)
        # ALP-778: settled must track current after sell fills too.
        assert cash_row.settled_cash_usd == pytest.approx(98_500.0 + 1600.0)
