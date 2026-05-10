"""Tests for the merger handlers (CASH_MERGER + STOCK_MERGER) — ALP-413.

Exercises the cash-merger and stock-merger handlers via the public
``integrate_ca_activity`` entry point. Cash mergers terminate the position
(quantity zeroed, status CLOSED, realized P/L accumulated, deal proceeds
credited). Stock mergers swap to the acquirer's symbol/qty/basis and flag
the position for strategist re-evaluation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.execution.broker_adapter.queries import PositionSnapshot
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
from alphamind.execution.state_persistence.write_paths.records import (
    CorporateActionLedgerStatus,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.events.activity_log import (
    CashCreditReason,
    CorporateActionType,
    EventType,
    PositionExitMethod,
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
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionFill,
    PositionRecord,
    PositionStatus,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-merger-2026-05-08T12:00:00Z"
_PROCESS_ID = "proc-merger-1"
_TXN_TIME = _NOW - timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind_merger.db"

    import alphamind.execution.state_persistence.tables  # noqa: F401 — side-effect import

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
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-merger-1.txt",
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


def _make_open_equity_position(
    position_id: str = "pos-1",
    *,
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    ticker: str = "TGT",
    share_count: float = 100.0,
    average_cost_basis_per_share: float = 50.0,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=ticker,
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
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": _NOW - timedelta(hours=2),
            "details": details,
            "execution_history": history,
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_open_options_position(
    position_id: str = "pos-opt-1",
    *,
    thesis_id: str | None = "thesis-opt-1",
    bracket_id: str | None = "brk-opt-1",
    underlying_ticker: str = "TGT",
    contract_count: float = 5.0,
    premium_paid_per_contract: float = 2.5,
    contract_multiplier: float = 100.0,
    strike_price: float = 60.0,
) -> PositionRecord:
    details = OptionsPositionDetails(
        underlying_ticker=underlying_ticker,
        strike_price=strike_price,
        expiration_date=date(2026, 12, 18),
        contract_type=OptionContractType.CALL,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=OptionGreeks(delta=0.5, gamma=0.02, theta=-0.05, vega=0.10),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=premium_paid_per_contract,
            fill_quantity=contract_count,
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
            "direction": Direction.LONG,
            "entry_timestamp": _NOW - timedelta(hours=2),
            "details": details,
            "execution_history": history,
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_open_strategy_position(
    position_id: str = "pos-stg-1",
    *,
    thesis_id: str | None = "thesis-stg-1",
    bracket_id: str | None = "brk-stg-1",
    underlying_ticker: str = "TGT",
) -> PositionRecord:
    leg_long = StrategyLeg(
        leg_id="leg-long",
        direction=Direction.LONG,
        options=OptionsPositionDetails(
            underlying_ticker=underlying_ticker,
            strike_price=55.0,
            expiration_date=date(2026, 12, 18),
            contract_type=OptionContractType.CALL,
            contract_count=2.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=3.0,
            greeks=OptionGreeks(delta=0.5, gamma=0.02, theta=-0.05, vega=0.10),
        ),
    )
    leg_short = StrategyLeg(
        leg_id="leg-short",
        direction=Direction.SHORT,
        options=OptionsPositionDetails(
            underlying_ticker=underlying_ticker,
            strike_price=65.0,
            expiration_date=date(2026, 12, 18),
            contract_type=OptionContractType.CALL,
            contract_count=2.0,
            contract_multiplier=100.0,
            premium_paid_per_contract=1.0,
            greeks=OptionGreeks(delta=0.3, gamma=0.02, theta=-0.04, vega=0.08),
        ),
    )
    details = StrategyPositionDetails(
        strategy_type_label="bull_call_spread",
        legs=(leg_long, leg_short),
        net_premium_usd=400.0,
        max_profit_usd=600.0,
        max_loss_usd=400.0,
        breakeven_levels=(57.0,),
        strategy_greeks=OptionGreeks(delta=0.2, gamma=0.0, theta=-0.01, vega=0.02),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=3.0,
            fill_quantity=2.0,
            slippage=0.0,
            fees=0.0,
        ),
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=1.0,
            fill_quantity=2.0,
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
            "direction": Direction.LONG,
            "entry_timestamp": _NOW - timedelta(hours=2),
            "details": details,
            "execution_history": history,
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_pending_entry_order(
    order_id: str = "ord-entry-1",
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    ticker: str = "TGT",
) -> OrderRecord:
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": bracket_id,
            "role": OrderRole.ENTRY,
            "instrument_spec": EquityInstrumentSpec(ticker=ticker),
            "direction": OrderDirection.BUY,
            "order_type": OrderType.MARKET,
            "order_class": OrderClass.SIMPLE,
            "price_parameters": PriceParameters(),
            "quantity": 100.0,
            "duration": OrderDuration.DAY,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{order_id}",
            "alpaca_order_id_chain": (f"alp-{order_id}",),
            "submission_timestamp": _NOW - timedelta(minutes=15),
            "last_update_timestamp": _NOW - timedelta(minutes=15),
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": 100.0,
            "modification_count": 0,
            "originating_thesis_id": "thesis-1",
            "originating_pm_command_id": None,
            "age_hours": 0.25,
        }
    )


def _make_active_bracket(
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    *,
    entry_order_id: str = "ord-entry-1",
    ticker: str = "TGT",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=f"{bracket_id}-ord-stop",
        trigger=PriceTrigger(underlying_ticker=ticker, threshold_usd=45.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id=entry_order_id,
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_thesis(thesis_id: str = "thesis-1", position_id: str = "pos-1") -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference="TGT",
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
        thesis_id=thesis_id,
        position_id=position_id,
        summary="TGT momentum",
        key_catalyst="Acquisition rumor",
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


def _make_drawdown_state() -> DrawdownState:
    return DrawdownState.model_validate(
        {
            "current_drawdown_pct": 0.0,
            "equity_high_water_mark_usd": 100_000.0,
            "drawdown_duration_hours": 0.0,
            "lifetime_max_drawdown_pct": 0.0,
            "intraday_drawdown_pct": 0.0,
            "daily_zone": RiskZone.NORMAL,
            "cumulative_zone": RiskZone.NORMAL,
            "cumulative_tier": None,
            "drawdown_by_source_pct": {},
        }
    )


# ---------------------------------------------------------------------------
# Fake AlpacaPositionLookup
# ---------------------------------------------------------------------------


class _FakePositionLookup:
    """Minimal stand-in for ``AlpacaPositionLookup`` used by stock-merger tests."""

    def __init__(self, positions: dict[str, PositionSnapshot]) -> None:
        self._positions = positions

    def get_position(self, symbol: str) -> PositionSnapshot | None:
        return self._positions.get(symbol)


def _equity_snapshot(symbol: str, qty: float, avg_entry_price: float) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=qty,
        avg_entry_price=avg_entry_price,
        market_value=qty * avg_entry_price,
        cost_basis=qty * avg_entry_price,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
        current_price=avg_entry_price,
        side="long",
    )


def _option_snapshot(symbol: str, qty: float, avg_entry_price: float) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_option",
        qty=qty,
        avg_entry_price=avg_entry_price,
        market_value=qty * avg_entry_price * 100.0,
        cost_basis=qty * avg_entry_price * 100.0,
        unrealized_pl=0.0,
        unrealized_plpc=0.0,
        current_price=avg_entry_price,
        side="long",
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
    thesis: ThesisRecord | None,
    bracket: BracketRecord,
) -> None:
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    bracket_row, leg_rows = bracket_record_to_rows(bracket)

    seeded_order_ids: set[str] = {order.order_id}
    extra_order_ids: list[str] = [bracket_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        if thesis is not None:
            thesis_row, component_rows = thesis_record_to_rows(thesis)
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
    factory: async_sessionmaker[AsyncSession], current_cash_usd: float = 100_000.0
) -> None:
    async with factory() as sess:
        sess.add(
            cash_ledger_record_to_row(_make_cash_ledger(current_cash_usd), last_updated_at=_NOW)
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
# Cash-merger tests — equity
# ---------------------------------------------------------------------------


async def test_cash_merger_equity_zeros_quantity_and_closes_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger zeros share_count, transitions status to CLOSED, accumulates realized P/L."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(share_count=100.0, average_cost_basis_per_share=50.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    # Deal price = $75/share, 100 shares acquired → $7,500 cash; cost basis $50 → $25/share P/L.
    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-equity-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=75.0,
        position_id="pos-1",
        signed_cash_impact_usd=7500.0,
        transaction_time=_TXN_TIME,
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
        assert pos.details.share_count == pytest.approx(0.0)
        assert pos.status == PositionStatus.CLOSED
        # P/L = ($75 deal price - $50 basis) * 100 shares = $2,500.
        assert pos.realized_pnl_to_date_usd == pytest.approx(2500.0)
        # Position is closed → adjustment flag stays False.
        assert pos.corporate_action_adjustment_needed is False


async def test_cash_merger_equity_emits_position_closed_and_cash_credited(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger emits POSITION_CLOSED + CASH_CREDITED with the merger-specific reasons."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(share_count=100.0, average_cost_basis_per_share=50.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-events-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=75.0,
        position_id="pos-1",
        signed_cash_impact_usd=7500.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        rows = (
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
        types = {r.event_type for r in rows}
        assert EventType.POSITION_CLOSED.value in types
        assert EventType.CASH_CREDITED.value in types
        assert EventType.CORPORATE_ACTION_APPLIED.value in types
        assert EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value in types

        # Verify exactly one POSITION_CLOSED with the merger exit method and price.
        closed_rows = [r for r in rows if r.event_type == EventType.POSITION_CLOSED.value]
        assert len(closed_rows) == 1
        import json

        closed_detail = json.loads(closed_rows[0].detail_json)
        assert closed_detail["exit_method"] == PositionExitMethod.CORPORATE_ACTION_CASH_MERGER.value
        # Deal price per share = signed_cash_impact_usd / pre_qty = 7500 / 100 = 75.
        assert closed_detail["exit_price"] == pytest.approx(75.0)
        assert closed_detail["realized_pnl_usd"] == pytest.approx(2500.0)

        # Verify cash credited row carries CASH_MERGER_PROCEEDS reason.
        credit_rows = [r for r in rows if r.event_type == EventType.CASH_CREDITED.value]
        assert len(credit_rows) == 1
        credit_detail = json.loads(credit_rows[0].detail_json)
        assert credit_detail["reason"] == CashCreditReason.CASH_MERGER_PROCEEDS.value
        assert credit_detail["amount_usd"] == pytest.approx(7500.0)

        # Cash ledger balance bumped by the deal proceeds.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(107_500.0)


async def test_cash_merger_resolves_linked_thesis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger marks the linked thesis RESOLVED with resolution_timestamp set."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(thesis_id="thesis-1"),
        _make_pending_entry_order(),
        _make_active_thesis(thesis_id="thesis-1"),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-thesis-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=75.0,
        position_id="pos-1",
        signed_cash_impact_usd=7500.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        thesis_row = await sess.get(ThesisRow, "thesis-1")
        assert thesis_row is not None
        assert thesis_row.status == ThesisRecordStatus.RESOLVED.value
        assert thesis_row.resolution_timestamp is not None


async def test_cash_merger_no_thesis_does_not_raise(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger on a position with thesis_id=None completes without errors."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(thesis_id=None),
        _make_pending_entry_order(),
        thesis=None,
        bracket=_make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-no-thesis-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=75.0,
        position_id="pos-1",
        signed_cash_impact_usd=7500.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)


async def test_cash_merger_writes_ledger_dedup_anchor(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger writes a dedup anchor to the CA integration ledger."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-ledger-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=75.0,
        position_id="pos-1",
        signed_cash_impact_usd=7500.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        ledger_row = (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id
                    == "ca-cash-merger-ledger-1"
                )
            )
        ).scalar_one()
        assert ledger_row.processing_status == CorporateActionLedgerStatus.PROCESSED.value
        assert ledger_row.processing_invocation_id == handle.invocation_id


async def test_cash_merger_missing_position_raises_value_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger against a missing position_id raises ``ValueError``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-missing-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=75.0,
        position_id="pos-missing",
        signed_cash_impact_usd=7500.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError, match="pos-missing"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Cash-merger tests — options
# ---------------------------------------------------------------------------


async def test_cash_merger_options_zeros_contract_count_and_closes_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger on options: contract_count=0, status=CLOSED, multiplier-scaled P/L."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_options_position(
            contract_count=5.0, premium_paid_per_contract=2.5, contract_multiplier=100.0
        ),
        _make_pending_entry_order(position_id="pos-opt-1", bracket_id="brk-opt-1"),
        _make_active_thesis(thesis_id="thesis-opt-1", position_id="pos-opt-1"),
        _make_active_bracket(bracket_id="brk-opt-1", position_id="pos-opt-1"),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    # Options cash merger: 5 contracts at $1.50 settlement, 100 multiplier = $750 proceeds.
    # Pre-merger cost = 5 * $2.50 * 100 = $1,250 -> realized P/L = $750 - $1,250 = -$500.
    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-options-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=1.5,
        position_id="pos-opt-1",
        signed_cash_impact_usd=750.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(0.0)
        assert pos.status == PositionStatus.CLOSED
        # P/L = signed_cash - (count * premium * multiplier) = 750 - 1250 = -500.
        assert pos.realized_pnl_to_date_usd == pytest.approx(-500.0)


# ---------------------------------------------------------------------------
# Cash-merger tests — strategy
# ---------------------------------------------------------------------------


async def test_cash_merger_strategy_closes_all_legs_and_aggregates_pnl(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash merger on a multi-leg strategy: all legs closed, aggregate realized P/L."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_strategy_position(),
        _make_pending_entry_order(position_id="pos-stg-1", bracket_id="brk-stg-1"),
        _make_active_thesis(thesis_id="thesis-stg-1", position_id="pos-stg-1"),
        _make_active_bracket(bracket_id="brk-stg-1", position_id="pos-stg-1"),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    # Total entry cost: long 2*$3*100 + (-1)*short 2*$1*100 = $600 - $200 = $400.
    # Settlement proceeds: $300. Aggregate P/L = $300 - $400 = -$100.
    ca = CorporateActionActivity(
        alpaca_activity_id="ca-cash-merger-strategy-1",
        action_type=CorporateActionType.CASH_MERGER,
        ticker="TGT",
        new_ticker=None,
        ratio_or_amount=1.5,
        position_id="pos-stg-1",
        signed_cash_impact_usd=300.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-stg-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, StrategyPositionDetails)
        assert pos.status == PositionStatus.CLOSED
        for leg in pos.details.legs:
            assert leg.options.contract_count == pytest.approx(0.0)
        # Aggregate P/L = signed_cash - net_premium = 300 - 400 = -100.
        assert pos.realized_pnl_to_date_usd == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# Stock-merger tests — equity
# ---------------------------------------------------------------------------


async def test_stock_merger_equity_swaps_ticker_qty_basis_from_lookup(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stock merger reads from lookup and updates ticker, share_count, avg cost basis."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(share_count=100.0, average_cost_basis_per_share=50.0),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    # Acquirer ACQ allocates 60 shares @ $80/share avg basis.
    lookup = _FakePositionLookup({"ACQ": _equity_snapshot("ACQ", qty=60.0, avg_entry_price=80.0)})

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-equity-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.6,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, EquityPositionDetails)
        assert pos.details.ticker == "ACQ"
        assert pos.details.share_count == pytest.approx(60.0)
        assert pos.details.average_cost_basis_per_share == pytest.approx(80.0)
        assert pos.status == PositionStatus.OPEN
        assert pos.corporate_action_adjustment_needed is True


async def test_stock_merger_emits_corporate_action_applied_with_new_ticker(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stock merger emits CORPORATE_ACTION_APPLIED with new_ticker plus bracket cancellation."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    lookup = _FakePositionLookup({"ACQ": _equity_snapshot("ACQ", qty=60.0, avg_entry_price=80.0)})

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-events-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.6,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        rows = (
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
        types = {r.event_type for r in rows}
        assert EventType.CORPORATE_ACTION_APPLIED.value in types
        assert EventType.BRACKET_CANCELLED_CORPORATE_ACTION.value in types

        applied = [r for r in rows if r.event_type == EventType.CORPORATE_ACTION_APPLIED.value]
        assert len(applied) == 1
        import json

        applied_detail = json.loads(applied[0].detail_json)
        assert applied_detail["new_ticker"] == "ACQ"


async def test_stock_merger_partial_cash_credits_via_cash_credited_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cash-and-stock merger: positive signed_cash_impact_usd credits via CASH_CREDITED."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)
    await _seed_drawdown_state(factory)

    lookup = _FakePositionLookup({"ACQ": _equity_snapshot("ACQ", qty=40.0, avg_entry_price=70.0)})

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-mix-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.4,
        position_id="pos-1",
        signed_cash_impact_usd=1200.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        rows = (
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
        credit_rows = [r for r in rows if r.event_type == EventType.CASH_CREDITED.value]
        assert len(credit_rows) == 1
        import json

        credit_detail = json.loads(credit_rows[0].detail_json)
        assert credit_detail["reason"] == CashCreditReason.CASH_MERGER_PROCEEDS.value
        assert credit_detail["amount_usd"] == pytest.approx(1200.0)

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(101_200.0)


async def test_stock_merger_no_partial_cash_skips_cash_credited(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Pure stock merger (signed_cash_impact_usd=0) emits no CASH_CREDITED entry."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    lookup = _FakePositionLookup({"ACQ": _equity_snapshot("ACQ", qty=60.0, avg_entry_price=80.0)})

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-pure-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.6,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        rows = (
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
        credit_rows = [r for r in rows if r.event_type == EventType.CASH_CREDITED.value]
        assert credit_rows == []


async def test_stock_merger_cancels_bracket_and_writes_ledger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stock merger dissolves the bracket and writes a dedup anchor."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    lookup = _FakePositionLookup({"ACQ": _equity_snapshot("ACQ", qty=60.0, avg_entry_price=80.0)})

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-bracket-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.6,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.DISSOLVED.value
        assert bracket_row.corporate_action_cancellation_reason == "corporate_action_stock_merger"

        ledger_row = (
            await sess.execute(
                select(CorporateActionIntegrationLedgerRow).where(
                    CorporateActionIntegrationLedgerRow.alpaca_activity_id
                    == "ca-stock-merger-bracket-1"
                )
            )
        ).scalar_one()
        assert ledger_row.processing_status == CorporateActionLedgerStatus.PROCESSED.value


async def test_stock_merger_missing_lookup_position_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stock merger when ``lookup.get_position(new_ticker)`` returns None raises ``ValueError``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    lookup = _FakePositionLookup({})

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-no-lookup-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.6,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError, match="ACQ"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)


async def test_stock_merger_missing_lookup_argument_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stock merger called with ``alpaca_position_lookup=None`` raises ``ValueError``."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_equity_position(),
        _make_pending_entry_order(),
        _make_active_thesis(),
        _make_active_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-none-lookup-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker="ACQ",
        ratio_or_amount=0.6,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError, match="alpaca_position_lookup"):
        await integrate_ca_activity(handle, ca, alpaca_position_lookup=None)
    await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# Stock-merger tests — options
# ---------------------------------------------------------------------------


async def test_stock_merger_options_projects_post_adjustment_state_with_none_greeks(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stock merger on options: project post-adjustment fields, set greeks to None."""
    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(
        factory,
        _make_open_options_position(
            contract_count=5.0,
            premium_paid_per_contract=2.5,
            contract_multiplier=100.0,
            strike_price=60.0,
        ),
        _make_pending_entry_order(position_id="pos-opt-1", bracket_id="brk-opt-1"),
        _make_active_thesis(thesis_id="thesis-opt-1", position_id="pos-opt-1"),
        _make_active_bracket(bracket_id="brk-opt-1", position_id="pos-opt-1"),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)

    # New OCC contract on ACQ, 3 contracts @ $4 premium per contract.
    new_symbol = "ACQ_OCC"
    lookup = _FakePositionLookup(
        {new_symbol: _option_snapshot(new_symbol, qty=3.0, avg_entry_price=4.0)}
    )

    ca = CorporateActionActivity(
        alpaca_activity_id="ca-stock-merger-options-1",
        action_type=CorporateActionType.STOCK_MERGER,
        ticker="TGT",
        new_ticker=new_symbol,
        ratio_or_amount=0.6,
        position_id="pos-opt-1",
        signed_cash_impact_usd=0.0,
        transaction_time=_TXN_TIME,
    )

    ctx, handle = await _open_handle(factory)
    await integrate_ca_activity(handle, ca, alpaca_position_lookup=lookup)
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(3.0)
        assert pos.details.premium_paid_per_contract == pytest.approx(4.0)
        # Greeks reset — refresh is the continuous monitor's job. The OptionGreeks
        # fields aren't nullable, so the handler signals staleness via
        # ``refresh_failed=True`` and zeroed numerics.
        assert pos.details.greeks.refresh_failed is True
        assert pos.details.greeks.delta == 0.0
        assert pos.details.greeks.gamma == 0.0
        assert pos.details.greeks.theta == 0.0
        assert pos.details.greeks.vega == 0.0
        assert pos.corporate_action_adjustment_needed is True
