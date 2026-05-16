"""Tests for the cash-dividend handlers (ALP-412 / story 03b).

Exercises the public ``integrate_ca_activity`` entry point for
``CASH_DIVIDEND_LONG`` and ``CASH_DIVIDEND_SHORT`` and verifies the
shared post-conditions every cash-dividend handler must satisfy:

* The position's quantity / cost basis are unchanged.
* ``corporate_action_adjustment_needed`` is set to ``True``.
* The cash ledger moves by ``signed_cash_impact_usd``.
* Exactly one ``CASH_CREDITED`` (long) / ``CASH_DEBITED`` (short
  obligation) activity-log entry is emitted with the appropriate reason.
* Exactly one ``CORPORATE_ACTION_APPLIED`` and one
  ``BRACKET_CANCELLED_CORPORATE_ACTION`` entry are emitted.
* The bracket is dissolved and a CA-integration-ledger row is written.
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
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import RiskZone
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.events.activity_log import (
    CashCreditReason,
    CashDebitReason,
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
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
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

_NOW = datetime(2026, 5, 9, 13, 0, 0, tzinfo=UTC)
_INV_ID = "inv-cash-div-2026-05-09T13:00:00Z"
_PROCESS_ID = "proc-cash-div-1"
_INITIAL_CASH = 100_000.0


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind_cash_div.db"

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
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-cash-div-1.txt",
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
    *,
    position_id: str = "pos-1",
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    direction: Direction = Direction.LONG,
    share_count: float = 100.0,
    average_cost_basis_per_share: float = 50.0,
) -> PositionRecord:
    if direction == Direction.SHORT:
        details = EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=share_count,
            average_cost_basis_per_share=average_cost_basis_per_share,
            borrow_rate_pct=0.025,
            locate_status=LocateStatus.LOCATED,
            margin_held_usd=share_count * average_cost_basis_per_share * 0.5,
        )
    else:
        details = EquityPositionDetails(
            ticker=Symbol("AAPL"),
            share_count=share_count,
            average_cost_basis_per_share=average_cost_basis_per_share,
        )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=price(average_cost_basis_per_share),
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
        quantity=100.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=100.0,
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
        trigger=PriceTrigger(underlying_ticker=Symbol("AAPL"), threshold_usd=40.0, direction="LTE"),
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


def _make_cash_ledger(current_cash_usd: float = _INITIAL_CASH) -> CashLedger:
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
        equity_high_water_mark_usd=_INITIAL_CASH,
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


async def _seed_cash_ledger(
    factory: async_sessionmaker[AsyncSession],
    current_cash_usd: float = _INITIAL_CASH,
) -> None:
    async with factory() as sess:
        sess.add(
            cash_ledger_record_to_row(
                _make_cash_ledger(current_cash_usd=current_cash_usd),
                last_updated_at=_NOW,
            )
        )
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
# Long-side cash dividend
# ---------------------------------------------------------------------------


async def test_cash_dividend_long_credits_cash_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A long cash dividend credits the cash ledger by ``signed_cash_impact_usd``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.LONG, share_count=100.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-long-1",
        action_type=CorporateActionType.CASH_DIVIDEND_LONG,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.50,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=50.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = (
            await sess.execute(
                select(CashLedgerRow).where(CashLedgerRow.id == CASH_LEDGER_SINGLETON_ID)
            )
        ).scalar_one()
        assert cash_row.current_cash_usd == pytest.approx(_INITIAL_CASH + 50.0)


async def test_cash_dividend_long_emits_cash_credited_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The long handler emits exactly one CASH_CREDITED entry with reason=CASH_DIVIDEND_LONG."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity
    from alphamind.portfolio_state.events.activity_log import CashCreditedDetail
    from alphamind.state.invocation_context.activity_log import (
        activity_log_entry_from_row,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.LONG),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    txn_time = _NOW - timedelta(minutes=5)
    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-long-2",
        action_type=CorporateActionType.CASH_DIVIDEND_LONG,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.25,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=25.0,
        transaction_time=txn_time,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow)
                    .where(ActivityLogRow.invocation_id == handle.invocation_id)
                    .where(ActivityLogRow.event_type == EventType.CASH_CREDITED.value)
                )
            )
            .scalars()
            .all()
        )
        assert len(log_rows) == 1
        entry = activity_log_entry_from_row(log_rows[0])
        detail = entry.detail
        assert isinstance(detail, CashCreditedDetail)
        assert detail.reason == CashCreditReason.CASH_DIVIDEND_LONG
        assert detail.amount_usd == pytest.approx(25.0)
        assert detail.new_balance_usd == pytest.approx(_INITIAL_CASH + 25.0)
        # CA-driven cash entries anchor at the activity's transaction_time
        # (NOT wall-clock now) so the chronological log invariant holds.
        assert entry.timestamp == txn_time
        # And they thread the originating position_id so operators can filter
        # the cash audit by position.
        assert entry.position_id == "pos-1"


async def test_cash_dividend_long_leaves_quantity_and_basis_unchanged(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The long handler does not modify share count or cost basis."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(
            direction=Direction.LONG,
            share_count=100.0,
            average_cost_basis_per_share=50.0,
        ),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-long-3",
        action_type=CorporateActionType.CASH_DIVIDEND_LONG,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=10.0,
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
        assert pos.details.share_count == pytest.approx(100.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(50.0)
        assert pos.corporate_action_adjustment_needed is True


async def test_cash_dividend_long_cancels_bracket_and_writes_ledger_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The long handler dissolves the bracket, emits CORPORATE_ACTION_APPLIED + bracket-cancelled,
    and writes a single CA-integration-ledger row."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.LONG),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-long-4",
        action_type=CorporateActionType.CASH_DIVIDEND_LONG,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=10.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value
        assert (
            bracket_row.corporate_action_cancellation_reason
            == "corporate_action_cash_dividend_long"
        )

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
        type_counts: dict[str, int] = {}
        for row in log_rows:
            type_counts[row.event_type] = type_counts.get(row.event_type, 0) + 1
        assert type_counts.get(EventType.CORPORATE_ACTION_APPLIED.value) == 1
        assert type_counts.get(EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value) == 1

        ledger_rows = (
            (
                await sess.execute(
                    select(CorporateActionIntegrationLedgerRow).where(
                        CorporateActionIntegrationLedgerRow.alpaca_activity_id
                        == "ca-cash-div-long-4"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ledger_rows) == 1
        assert ledger_rows[0].processing_status == CorporateActionLedgerStatus.PROCESSED.value


async def test_cash_dividend_long_raises_when_position_missing(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The long handler raises ValueError when the referenced position is missing."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-long-missing",
        action_type=CorporateActionType.CASH_DIVIDEND_LONG,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-does-not-exist"),
        signed_cash_impact_usd=10.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError, match="pos-does-not-exist"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Short-side cash dividend (obligation pass-through)
# ---------------------------------------------------------------------------


async def test_cash_dividend_short_debits_cash_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A short-obligation dividend debits cash by ``abs(signed_cash_impact_usd)``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.SHORT),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-short-1",
        action_type=CorporateActionType.CASH_DIVIDEND_SHORT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.50,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=-50.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = (
            await sess.execute(
                select(CashLedgerRow).where(CashLedgerRow.id == CASH_LEDGER_SINGLETON_ID)
            )
        ).scalar_one()
        assert cash_row.current_cash_usd == pytest.approx(_INITIAL_CASH - 50.0)


async def test_cash_dividend_short_emits_cash_debited_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """One CASH_DEBITED entry is emitted with reason=CASH_DIVIDEND_SHORT_OBLIGATION."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity
    from alphamind.portfolio_state.events.activity_log import CashDebitedDetail
    from alphamind.state.invocation_context.activity_log import (
        activity_log_entry_from_row,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.SHORT),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    txn_time = _NOW - timedelta(minutes=5)
    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-short-2",
        action_type=CorporateActionType.CASH_DIVIDEND_SHORT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.30,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=-30.0,
        transaction_time=txn_time,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow)
                    .where(ActivityLogRow.invocation_id == handle.invocation_id)
                    .where(ActivityLogRow.event_type == EventType.CASH_DEBITED.value)
                )
            )
            .scalars()
            .all()
        )
        assert len(log_rows) == 1
        entry = activity_log_entry_from_row(log_rows[0])
        detail = entry.detail
        assert isinstance(detail, CashDebitedDetail)
        assert detail.reason == CashDebitReason.CASH_DIVIDEND_SHORT_OBLIGATION
        assert detail.amount_usd == pytest.approx(30.0)
        assert detail.new_balance_usd == pytest.approx(_INITIAL_CASH - 30.0)
        # CA-driven cash debits also anchor at transaction_time and carry
        # the originating position_id (mirrors the long-side credit).
        assert entry.timestamp == txn_time
        assert entry.position_id == "pos-1"


async def test_cash_dividend_short_leaves_quantity_and_basis_unchanged(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The short handler does not modify share count or cost basis."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(
            direction=Direction.SHORT,
            share_count=100.0,
            average_cost_basis_per_share=50.0,
        ),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-short-3",
        action_type=CorporateActionType.CASH_DIVIDEND_SHORT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=-10.0,
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
        assert pos.details.share_count == pytest.approx(100.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(50.0)
        assert pos.corporate_action_adjustment_needed is True


async def test_cash_dividend_short_cancels_bracket_and_writes_ledger_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The short handler dissolves the bracket, emits CORPORATE_ACTION_APPLIED + bracket-cancelled,
    and writes a single CA-integration-ledger row."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.SHORT),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-short-4",
        action_type=CorporateActionType.CASH_DIVIDEND_SHORT,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.10,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=-10.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value
        assert (
            bracket_row.corporate_action_cancellation_reason
            == "corporate_action_cash_dividend_short"
        )

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
        type_counts: dict[str, int] = {}
        for row in log_rows:
            type_counts[row.event_type] = type_counts.get(row.event_type, 0) + 1
        assert type_counts.get(EventType.CORPORATE_ACTION_APPLIED.value) == 1
        assert type_counts.get(EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value) == 1

        ledger_rows = (
            (
                await sess.execute(
                    select(CorporateActionIntegrationLedgerRow).where(
                        CorporateActionIntegrationLedgerRow.alpaca_activity_id
                        == "ca-cash-div-short-4"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(ledger_rows) == 1
        assert ledger_rows[0].processing_status == CorporateActionLedgerStatus.PROCESSED.value


# ---------------------------------------------------------------------------
# Zero-amount short-circuit
# ---------------------------------------------------------------------------


async def test_zero_amount_cash_dividend_does_not_emit_cash_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A zero-rate cash dividend (``signed_cash_impact_usd=0``) does not
    emit a spurious ``CASH_CREDITED`` entry with ``amount_usd=0`` — the
    movement short-circuits before any state mutation."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_position(direction=Direction.LONG),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-div-zero",
        action_type=CorporateActionType.CASH_DIVIDEND_LONG,
        ticker=Symbol("AAPL"),
        new_ticker=None,
        ratio_or_amount=0.0,
        position_id=PositionId("pos-1"),
        signed_cash_impact_usd=0.0,
        transaction_time=_NOW - timedelta(minutes=5),
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # No CASH_CREDITED or CASH_DEBITED entry should appear for a zero rate.
        cash_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow)
                    .where(ActivityLogRow.invocation_id == handle.invocation_id)
                    .where(
                        ActivityLogRow.event_type.in_(
                            (
                                EventType.CASH_CREDITED.value,
                                EventType.CASH_DEBITED.value,
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert cash_rows == []

        # Cash ledger balance is untouched.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(_INITIAL_CASH)
