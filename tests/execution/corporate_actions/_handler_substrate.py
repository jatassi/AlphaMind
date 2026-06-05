"""Shared fixtures and builders for ALP-411 handler tests.

Reuses the same in-memory SQLite scaffolding as ``test_splits.py`` but
factored out so the three new handler test modules
(``test_reverse_splits``, ``test_stock_dividends``, ``test_ticker_changes``)
share the seed helpers without duplicating ~250 lines per file.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from alphamind.execution.broker_adapter.queries import PositionSnapshot
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
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
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

NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
INV_ID = "inv-ca-2026-05-08T12:00:00Z"
PROCESS_ID = "proc-ca-1"


# ---------------------------------------------------------------------------
# Builders — invocation/process substrate
# ---------------------------------------------------------------------------


def make_process_lifetime() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=PROCESS_ID,
        process_role="pipeline",
        process_start_at=NOW.isoformat().replace("+00:00", "Z"),
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


def make_invocation_record(invocation_id: str = INV_ID) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=PROCESS_ID,
        start_at=NOW.isoformat().replace("+00:00", "Z"),
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


# ---------------------------------------------------------------------------
# Builders — positions
# ---------------------------------------------------------------------------


def make_open_equity_position(
    position_id: str = "pos-1",
    *,
    ticker: str = "AAPL",
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    share_count: float = 10.0,
    average_cost_basis_per_share: float = 150.0,
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=average_cost_basis_per_share,
    )
    history = (
        PositionFill(
            fill_timestamp=NOW - timedelta(hours=2),
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
        direction=Direction.LONG,
        entry_timestamp=NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def make_pending_equity_position(
    position_id: str = "pos-1",
    *,
    ticker: str = "AAPL",
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
) -> PositionRecord:
    """A PENDING equity position: share_count 0, no fills, no entry timestamp.

    The just-authored-not-yet-filled state. A nonzero Alpaca holding against
    this row is a dropped/un-integrated entry fill (RD1).
    """
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=0.0,
        average_cost_basis_per_share=0.0,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=details,
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _make_options_details(
    *,
    underlying_ticker: str = "AAPL",
    strike: float = 150.0,
    contract_count: float = 5.0,
    contract_multiplier: float = 100.0,
    premium_paid_per_contract: float = 250.0,
) -> OptionsPositionDetails:
    return OptionsPositionDetails(
        underlying_ticker=Symbol(underlying_ticker),
        strike_price=strike,
        expiration_date=date(2026, 9, 18),
        contract_type=OptionContractType.CALL,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=OptionGreeks(delta=0.45, gamma=0.02, theta=-0.05, vega=0.10),
    )


def make_open_options_position(
    position_id: str = "pos-1",
    *,
    underlying_ticker: str = "AAPL",
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
    strike: float = 150.0,
    contract_count: float = 5.0,
    contract_multiplier: float = 100.0,
    premium_paid_per_contract: float = 250.0,
) -> PositionRecord:
    details = _make_options_details(
        underlying_ticker=underlying_ticker,
        strike=strike,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=premium_paid_per_contract,
    )
    history = (
        PositionFill(
            fill_timestamp=NOW - timedelta(hours=2),
            fill_price=price(premium_paid_per_contract / contract_multiplier),
            fill_quantity=contract_count,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.OPEN,
        direction=Direction.LONG,
        entry_timestamp=NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def make_open_strategy_position(
    position_id: str = "pos-1",
    *,
    underlying_ticker: str = "AAPL",
    thesis_id: str | None = "thesis-1",
    bracket_id: str | None = "brk-1",
) -> PositionRecord:
    leg_a = StrategyLeg(
        leg_id="leg-long",
        direction=Direction.LONG,
        options=_make_options_details(
            underlying_ticker=underlying_ticker,
            strike=150.0,
            contract_count=5.0,
            premium_paid_per_contract=250.0,
        ),
    )
    leg_b = StrategyLeg(
        leg_id="leg-short",
        direction=Direction.SHORT,
        options=_make_options_details(
            underlying_ticker=underlying_ticker,
            strike=160.0,
            contract_count=5.0,
            premium_paid_per_contract=120.0,
        ),
    )
    details = StrategyPositionDetails(
        strategy_type_label="vertical-call-spread",
        legs=(leg_a, leg_b),
        net_premium_usd=650.0,
        max_profit_usd=4350.0,
        max_loss_usd=650.0,
        breakeven_levels=(151.30,),
        strategy_greeks=OptionGreeks(delta=0.20, gamma=0.01, theta=-0.02, vega=0.05),
    )
    history = (
        PositionFill(
            fill_timestamp=NOW - timedelta(hours=2),
            fill_price=price(1.30),
            fill_quantity=5.0,
            slippage=signed_money(0.0),
            fees=money(0.0),
        ),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id) if thesis_id else None,
        bracket_id=BracketId(bracket_id) if bracket_id else None,
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=NOW - timedelta(hours=2),
        details=details,
        execution_history=history,
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def make_pending_entry_order(
    order_id: str = "ord-entry-1",
    bracket_id: str = "brk-1",
    ticker: str = "AAPL",
) -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId("pos-1"),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(ticker)),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=NOW - timedelta(minutes=15),
        last_update_timestamp=NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("thesis-1"),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def make_active_bracket(
    bracket_id: str = "brk-1",
    position_id: str = "pos-1",
    ticker: str = "AAPL",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol(ticker), threshold_usd=140.0, direction="LTE"
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


def make_active_thesis(thesis_id: str = "thesis-1", position_id: str = "pos-1") -> ThesisRecord:
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
            generation_timestamp=NOW - timedelta(hours=4),
            resolution_outcome=None,
            resolution_notes=None,
        )
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    generation_at = NOW - timedelta(hours=4)
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


def make_cash_ledger(current_cash_usd: float = 100_000.0) -> CashLedger:
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


def make_drawdown_state() -> DrawdownState:
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


async def seed_invocation_substrate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(make_invocation_record()))
        await sess.commit()


async def seed_position_cluster(
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


async def seed_cash_ledger(
    factory: async_sessionmaker[AsyncSession],
    *,
    current_cash_usd: float = 100_000.0,
) -> None:
    async with factory() as sess:
        sess.add(
            cash_ledger_record_to_row(
                make_cash_ledger(current_cash_usd=current_cash_usd), last_updated_at=NOW
            )
        )
        await sess.commit()


async def seed_drawdown_state(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as sess:
        sess.add(drawdown_state_record_to_row(make_drawdown_state(), last_updated_at=NOW))
        await sess.commit()


async def open_handle(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=make_invocation_record(invocation_id=INV_ID + "-direct"),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Test helpers — mock AlpacaPositionLookup
# ---------------------------------------------------------------------------


class FakeAlpacaPositionLookup:
    """In-memory ``AlpacaPositionLookup`` for handler tests.

    Returns the snapshot keyed by symbol passed to :meth:`get_position`, or
    ``None`` if no entry was registered.  Tracks calls for assertions.
    """

    def __init__(self, by_symbol: dict[str, PositionSnapshot] | None = None) -> None:
        self._by_symbol = dict(by_symbol or {})
        self.calls: list[str] = []

    def register(self, symbol: str, snapshot: PositionSnapshot) -> None:
        self._by_symbol[symbol] = snapshot

    def get_position(self, symbol: str) -> PositionSnapshot | None:
        self.calls.append(symbol)
        return self._by_symbol.get(symbol)


def make_options_position_snapshot(
    *,
    symbol: str = "AAPL250918C00150000",
    qty: float = 1.0,
    avg_entry_price: float = 4.50,
) -> PositionSnapshot:
    """Build a ``PositionSnapshot`` shaped like an Alpaca options position.

    ``avg_entry_price`` is the per-share-of-underlying basis Alpaca reports;
    callers multiply by ``contract_multiplier`` for the per-contract premium.
    """
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_option",
        qty=qty,
        avg_entry_price=price(avg_entry_price),
        market_value=money(qty * avg_entry_price * 100.0),
        cost_basis=money(qty * avg_entry_price * 100.0),
        unrealized_pl=money(0.0),
        unrealized_plpc=0.0,
        current_price=price(avg_entry_price),
        side="long",
    )
