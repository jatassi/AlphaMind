"""Tests for the Phase 1 fill-integration write path on strategy / mleg
positions (story 04b / ALP-392).

Mirrors :mod:`tests.execution.state_persistence.test_phase1_options` but for
the STRATEGY discriminator on ``PositionDetailsPayload``. Strategy positions
are multi-leg options structures (vertical spreads, iron condors, straddles,
etc.) that submit as a single Alpaca ``mleg`` order and surface per-leg
fills correlated back to the strategy via ``parent_client_order_id``
(:mod:`alphamind.execution.broker_adapter.fill_stream`).

Per ``broker-adapter.md § Multi-leg fill events § Atomicity``, the strategy
position is not considered open until **every leg** has reached ``filled``
status. Per-leg fills accumulate against the strategy's
``execution_history`` and per-leg ``StrategyLeg.options.contract_count``
incrementally; the PENDING → OPEN transition fires only at the last leg's
filled event. Net cost basis follows the signed-sum-across-legs convention
described in ``orders-and-brackets.md § Multi-leg strategies`` (positive =
net debit / paid premium; negative = net credit / received premium).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
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
from alphamind.execution.state_persistence.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.execution.state_persistence.tables.fill_records import FillRecordRow
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
from alphamind.portfolio_state.events.activity_log import EventType
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    OptionsInstrumentSpec,
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
    ThesisComponentOutcome,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
    RealizedVolEntry,
)
from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-strat"
_PROCESS_ID = "proc-strat-1"
_EXPIRATION = date(2026, 6, 19)
_UNDERLYING = "MSFT"


# ---------------------------------------------------------------------------
# Engine + session fixture
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
# Builders — strategy-shaped variants of the options-test helpers.
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


def _make_market_inputs() -> MarketInputs:
    """Minimal ``MarketInputs`` covering the underlying these tests use.

    The Reg T attribution wedge (story 06a / ALP-428) requires a price for
    every open-position underlying and an IV provider that can serve every
    leg the PM-equivalent path consults. Strategy positions iterate every
    leg through Black-Scholes; the realized-vol fallback handles arbitrary
    strikes without requiring per-test surface seeding.
    """
    return MarketInputs(
        underlying_prices={_UNDERLYING: 410.0},
        risk_free_rate=0.0425,
        iv_provider=FixtureIvProvider(
            surface={},
            realized_vol={
                _UNDERLYING: RealizedVolEntry(
                    underlying=_UNDERLYING,
                    trailing_30d_realized_vol=0.30,
                )
            },
        ),
        as_of=_NOW,
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


def _make_pending_greeks() -> OptionGreeks:
    return OptionGreeks(
        delta=0.30,
        gamma=0.02,
        theta=-0.01,
        vega=0.10,
        as_of_timestamp=_NOW - timedelta(minutes=20),
        iv_used=0.28,
        refresh_failed=False,
    )


def _make_options_spec(
    *,
    contract_type: OptionContractType,
    strike: float,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
) -> OptionsInstrumentSpec:
    return OptionsInstrumentSpec(
        underlying=_UNDERLYING,
        strike=strike,
        expiration=_EXPIRATION,
        contract_type=contract_type,
        contract_multiplier=contract_multiplier,
    )


def _make_options_details(
    *,
    contract_type: OptionContractType,
    strike: float,
    contract_count: float = 0.0,
    premium_paid_per_contract: float = 0.0,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
) -> OptionsPositionDetails:
    return OptionsPositionDetails(
        underlying_ticker=_UNDERLYING,
        strike_price=strike,
        expiration_date=_EXPIRATION,
        contract_type=contract_type,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=_make_pending_greeks(),
    )


def _make_strategy_leg(
    *,
    leg_id: str,
    contract_type: OptionContractType,
    strike: float,
    direction: Direction,
    contract_count: float = 0.0,
    premium_paid_per_contract: float = 0.0,
) -> StrategyLeg:
    return StrategyLeg(
        leg_id=leg_id,
        direction=direction,
        options=_make_options_details(
            contract_type=contract_type,
            strike=strike,
            contract_count=contract_count,
            premium_paid_per_contract=premium_paid_per_contract,
        ),
    )


def _iron_condor_legs() -> tuple[StrategyLeg, ...]:
    """Standard 4-leg iron condor (short put, long lower put, short call, long higher call)."""
    return (
        _make_strategy_leg(
            leg_id="leg-short-put",
            contract_type=OptionContractType.PUT,
            strike=415.0,
            direction=Direction.SHORT,
        ),
        _make_strategy_leg(
            leg_id="leg-long-put",
            contract_type=OptionContractType.PUT,
            strike=410.0,
            direction=Direction.LONG,
        ),
        _make_strategy_leg(
            leg_id="leg-short-call",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=Direction.SHORT,
        ),
        _make_strategy_leg(
            leg_id="leg-long-call",
            contract_type=OptionContractType.CALL,
            strike=430.0,
            direction=Direction.LONG,
        ),
    )


def _make_strategy_details(
    *,
    legs: tuple[StrategyLeg, ...] | None = None,
    strategy_type_label: str = "iron-condor",
) -> StrategyPositionDetails:
    return StrategyPositionDetails(
        strategy_type_label=strategy_type_label,
        legs=legs if legs is not None else _iron_condor_legs(),
        net_premium_usd=0.0,
        max_profit_usd=200.0,
        max_loss_usd=300.0,
        breakeven_levels=(417.0, 423.0),
        strategy_greeks=_make_pending_greeks(),
    )


def _make_pending_strategy_position(
    position_id: str = "pos-strat-1",
    *,
    thesis_id: str | None = "thesis-strat-1",
    bracket_id: str | None = "brk-strat-1",
    legs: tuple[StrategyLeg, ...] | None = None,
    strategy_type_label: str = "iron-condor",
) -> PositionRecord:
    """Build a PENDING strategy position with no fills yet."""
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.PENDING,
            "direction": Direction.LONG,
            "entry_timestamp": None,
            "details": _make_strategy_details(legs=legs, strategy_type_label=strategy_type_label),
            "execution_history": (),
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_open_strategy_position(
    position_id: str = "pos-strat-1",
    *,
    thesis_id: str | None = "thesis-strat-1",
    bracket_id: str | None = "brk-strat-1",
    legs: tuple[StrategyLeg, ...],
    execution_history: tuple[PositionFill, ...],
    strategy_type_label: str = "iron-condor",
) -> PositionRecord:
    """Build an OPEN strategy position whose ``execution_history`` reflects entry fills."""
    return PositionRecord.model_validate(
        {
            "position_id": position_id,
            "thesis_id": thesis_id,
            "bracket_id": bracket_id,
            "status": PositionStatus.OPEN,
            "direction": Direction.LONG,
            "entry_timestamp": _NOW - timedelta(hours=2),
            "details": _make_strategy_details(legs=legs, strategy_type_label=strategy_type_label),
            "execution_history": execution_history,
            "realized_pnl_to_date_usd": None,
            "corporate_action_adjustment_needed": False,
            "parent_position_id": None,
            "origin": None,
        }
    )


def _make_strategy_parent_order(
    order_id: str = "ord-strat-parent",
    *,
    bracket_id: str = "brk-strat-1",
    quantity: float = 1.0,
    legs: tuple[OptionsInstrumentSpec, ...] | None = None,
    position_id: str | None = None,
    role: OrderRole = OrderRole.ENTRY,
) -> OrderRecord:
    """Build the parent strategy order (``order_class=MLEG``).

    The parent is what the broker actually receives — Alpaca executes as a
    single mleg order; per-leg children carry per-leg orders separately.
    """
    from alphamind.portfolio_state.records.orders import StrategyInstrumentSpec

    spec_legs = (
        legs
        if legs is not None
        else (
            _make_options_spec(contract_type=OptionContractType.PUT, strike=415.0),
            _make_options_spec(contract_type=OptionContractType.PUT, strike=410.0),
            _make_options_spec(contract_type=OptionContractType.CALL, strike=425.0),
            _make_options_spec(contract_type=OptionContractType.CALL, strike=430.0),
        )
    )
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": bracket_id,
            "role": role,
            "instrument_spec": StrategyInstrumentSpec(legs=spec_legs),
            "direction": OrderDirection.BUY,
            "order_type": OrderType.MARKET,
            "order_class": OrderClass.MLEG,
            "price_parameters": PriceParameters(),
            "quantity": quantity,
            "duration": OrderDuration.DAY,
            "status": OrderStatus.PENDING,
            "alpaca_order_id": f"alp-{order_id}",
            "alpaca_order_id_chain": (f"alp-{order_id}",),
            "submission_timestamp": _NOW - timedelta(minutes=15),
            "last_update_timestamp": _NOW - timedelta(minutes=15),
            "filled_quantity": 0.0,
            "avg_fill_price": None,
            "remaining_quantity": quantity,
            "modification_count": 0,
            "originating_thesis_id": "thesis-strat-1",
            "originating_pm_command_id": None,
            "age_hours": 0.25,
        }
    )


def _make_leg_order(
    *,
    order_id: str,
    bracket_id: str = "brk-strat-1",
    position_id: str = "pos-strat-1",
    contract_type: OptionContractType,
    strike: float,
    direction: OrderDirection,
    quantity: float = 1.0,
    role: OrderRole = OrderRole.ENTRY,
    status: OrderStatus = OrderStatus.PENDING,
    filled_quantity: float = 0.0,
    avg_fill_price: float | None = None,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
) -> OrderRecord:
    """Build a per-leg ``OrderRecord`` (``OptionsInstrumentSpec`` + ``SIMPLE``).

    Per-leg orders carry the strategy ``position_id`` so Phase 1 can correlate
    sibling legs; ``order_class`` stays ``SIMPLE`` because the leg itself is
    a single-instrument identifier — the strategy-as-mleg structure lives on
    the parent order.
    """
    return OrderRecord.model_validate(
        {
            "order_id": order_id,
            "position_id": position_id,
            "bracket_id": bracket_id,
            "role": role,
            "instrument_spec": _make_options_spec(
                contract_type=contract_type,
                strike=strike,
                contract_multiplier=contract_multiplier,
            ),
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
            "remaining_quantity": max(quantity - filled_quantity, 0.0),
            "modification_count": 0,
            "originating_thesis_id": "thesis-strat-1",
            "originating_pm_command_id": None,
            "age_hours": 0.25,
        }
    )


def _make_iron_condor_leg_orders(
    *,
    quantity: float = 1.0,
    role: OrderRole = OrderRole.ENTRY,
    status: OrderStatus = OrderStatus.PENDING,
    filled_quantity: float = 0.0,
) -> tuple[OrderRecord, ...]:
    """Per-leg orders aligned with the iron-condor leg structure."""
    spec = (
        ("leg-short-put", OptionContractType.PUT, 415.0, OrderDirection.SELL_TO_OPEN),
        ("leg-long-put", OptionContractType.PUT, 410.0, OrderDirection.BUY_TO_OPEN),
        ("leg-short-call", OptionContractType.CALL, 425.0, OrderDirection.SELL_TO_OPEN),
        ("leg-long-call", OptionContractType.CALL, 430.0, OrderDirection.BUY_TO_OPEN),
    )
    return tuple(
        _make_leg_order(
            order_id=leg_id,
            contract_type=ct,
            strike=strike,
            direction=direction,
            quantity=quantity,
            role=role,
            status=status,
            filled_quantity=filled_quantity,
        )
        for leg_id, ct, strike, direction in spec
    )


def _make_pending_strategy_bracket(
    bracket_id: str = "brk-strat-1",
    position_id: str = "pos-strat-1",
) -> BracketRecord:
    """A PENDING_ENTRY bracket with one monitor-managed price-stop on the underlying.

    Strategy brackets watch the underlying equity (per
    ``orders-and-brackets.md § Multi-leg strategies``) and close every leg via
    market orders when the trigger fires — so the leg has ``order_id=None``
    and the continuous monitor arms the trigger on the underlying stream.
    """
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(underlying_ticker=_UNDERLYING, threshold_usd=405.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id="ord-strat-parent",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_strategy_bracket(
    bracket_id: str = "brk-strat-1",
    position_id: str = "pos-strat-1",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(underlying_ticker=_UNDERLYING, threshold_usd=405.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-strat-parent",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_strategy_thesis(
    thesis_id: str = "thesis-strat-1",
    position_id: str = "pos-strat-1",
) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=_UNDERLYING,
            narrative=f"{ct.value} narrative",
            key_assumptions=(KeyAssumption(text=f"{_UNDERLYING} pinned thesis", outcome=None),),
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
        summary=f"{_UNDERLYING} iron condor — defined-risk neutral",
        key_catalyst="Range-bound trade plan",
        position_size_rationale="Sized to max-loss budget",
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


def _make_strategy_thesis_with_resolved_components(
    thesis_id: str = "thesis-strat-1",
    position_id: str = "pos-strat-1",
) -> ThesisRecord:
    """ACTIVE thesis whose components carry pre-set resolution outcomes.

    Used in exit-fill tests so when Phase 1 transitions thesis.status to
    RESOLVED, the resulting record stays valid.
    """
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=_UNDERLYING,
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
        summary=f"{_UNDERLYING} iron condor",
        key_catalyst="Range-bound",
        position_size_rationale="Sized to max-loss budget",
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
    order_id: str,
    fill_quantity: float = 1.0,
    fill_price: float = 1.50,
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
        execution_venue="OPRA",
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
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_make_invocation_record()))
        await sess.commit()


async def _seed_strategy_cluster(
    factory: async_sessionmaker[AsyncSession],
    *,
    position: PositionRecord,
    parent_order: OrderRecord,
    leg_orders: tuple[OrderRecord, ...],
    thesis: ThesisRecord,
    bracket: BracketRecord,
) -> None:
    """Seed strategy position + parent mleg order + per-leg orders + thesis + bracket.

    Mirrors :func:`_seed_position_order_thesis_bracket` from the options
    tests but persists every per-leg order as its own ``OrderRow`` so per-leg
    fills find their target in :func:`_read_order`.
    """
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_row, leg_rows = bracket_record_to_rows(bracket)

    seeded_order_ids: set[str] = {parent_order.order_id, *(o.order_id for o in leg_orders)}
    extra_order_ids: list[str] = [bracket_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(parent_order))
        for leg_order in leg_orders:
            sess.add(order_record_to_row(leg_order))
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


async def _set_order_status(
    factory: async_sessionmaker[AsyncSession],
    order_id: str,
    status: OrderStatus,
) -> None:
    """Force a leg order's status — used to simulate parent-cancel side effects.

    Real cancel events flow through the continuous monitor's run-loop and
    update per-leg order rows directly; for tests we mutate the row in place
    so Phase 1 sees the post-cancel substrate.
    """
    async with factory() as sess:
        row = await sess.get(OrderRow, order_id)
        assert row is not None
        row.status = status.value
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id_suffix: str = "-phase1",
) -> tuple[InvocationContext, InvocationHandle]:
    """Open an invocation context for a Phase 1 invocation.

    Multi-invocation tests (e.g., staggered fills across T1/T2) must pass
    distinct ``invocation_id_suffix`` values so each invocation row obeys
    the ``invocations.invocation_id`` UNIQUE constraint.
    """
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=_INV_ID + invocation_id_suffix),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Shared open-strategy fixtures (long call vertical) — close + ADD + rollback
# tests reuse these to keep their statement counts under PLR0915.
# ---------------------------------------------------------------------------


def _vertical_long_call_open_legs() -> tuple[StrategyLeg, ...]:
    """Two-leg long-call vertical with both legs at 2 contracts."""
    return (
        _make_strategy_leg(
            leg_id="leg-long-lower",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=Direction.LONG,
            contract_count=2.0,
            premium_paid_per_contract=5.00,
        ),
        _make_strategy_leg(
            leg_id="leg-short-upper",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=Direction.SHORT,
            contract_count=2.0,
            premium_paid_per_contract=2.00,
        ),
    )


def _vertical_long_call_history() -> tuple[PositionFill, ...]:
    """Execution history reflecting the entry fills for the vertical above."""
    return (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=5.00,
            fill_quantity=2.0,
            slippage=0.0,
            fees=0.0,
        ),
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=2.00,
            fill_quantity=2.0,
            slippage=0.0,
            fees=0.0,
        ),
    )


def _vertical_call_parent_legs() -> tuple[OptionsInstrumentSpec, ...]:
    return (
        _make_options_spec(contract_type=OptionContractType.CALL, strike=420.0),
        _make_options_spec(contract_type=OptionContractType.CALL, strike=425.0),
    )


# ---------------------------------------------------------------------------
# Tests — atomic OPEN + cost-basis sign convention
# ---------------------------------------------------------------------------


async def test_all_legs_filled_atomic_open_with_signed_net_cost_basis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """4-leg iron condor: all legs fill on the same timestamp; the position
    transitions PENDING → OPEN at the last leg's filled event with cost
    basis = signed sum across legs."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    leg_orders = _make_iron_condor_leg_orders(quantity=1.0)
    await _seed_strategy_cluster(
        factory,
        position=_make_pending_strategy_position(),
        parent_order=_make_strategy_parent_order(),
        leg_orders=leg_orders,
        thesis=_make_active_strategy_thesis(),
        bracket=_make_pending_strategy_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # Per-leg fill prices (atomic — one timestamp). Net cost basis = sum
    # over legs of (sign * price). Short put 4.20 (sign -1), long put 1.80
    # (sign +1), short call 3.50 (sign -1), long call 1.50 (sign +1) sums to
    # -4.40 per strategy unit. Scaled by 1 contract * multiplier 100 = -440
    # (net credit, premium received).
    fill_specs = (
        ("leg-short-put", 4.20),
        ("leg-long-put", 1.80),
        ("leg-short-call", 3.50),
        ("leg-long-call", 1.50),
    )
    for idx, (leg_order_id, price) in enumerate(fill_specs):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-{leg_order_id}",
                order_id=leg_order_id,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
            ),
        )

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 4
    assert summary.fills_quarantined == 0

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert pos.entry_timestamp is not None
        assert isinstance(pos.details, StrategyPositionDetails)

        # Each leg's contract_count is updated and premium captured.
        leg_state = {leg.leg_id: leg for leg in pos.details.legs}
        assert leg_state["leg-short-put"].options.contract_count == pytest.approx(1.0)
        assert leg_state["leg-short-put"].options.premium_paid_per_contract == pytest.approx(4.20)
        assert leg_state["leg-long-put"].options.contract_count == pytest.approx(1.0)
        assert leg_state["leg-long-put"].options.premium_paid_per_contract == pytest.approx(1.80)
        assert leg_state["leg-short-call"].options.contract_count == pytest.approx(1.0)
        assert leg_state["leg-short-call"].options.premium_paid_per_contract == pytest.approx(3.50)
        assert leg_state["leg-long-call"].options.contract_count == pytest.approx(1.0)
        assert leg_state["leg-long-call"].options.premium_paid_per_contract == pytest.approx(1.50)

        # Net cost basis (signed sum across legs, scaled by contract multiplier).
        # Long sign +1, Short sign -1.
        net_cost_basis = (
            -1.0 * 4.20 * 1.0 * 100  # short put: -420
            + 1.0 * 1.80 * 1.0 * 100  # long put: +180
            + -1.0 * 3.50 * 1.0 * 100  # short call: -350
            + 1.0 * 1.50 * 1.0 * 100  # long call: +150
        )
        assert net_cost_basis == pytest.approx(-440.0)

        # Cash position reflects the net credit (sell-side net): +440.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 + 440.0)

        # Activity log carries one POSITION_OPENED + one BRACKET_ACTIVATED.
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
        types = [r.event_type for r in log_rows]
    assert types.count(EventType.POSITION_OPENED.value) == 1
    assert types.count(EventType.BRACKET_ACTIVATED.value) == 1


async def test_long_call_spread_has_positive_net_debit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Long call spread (long lower, short higher): net cost basis is positive
    (paid premium = net debit)."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    legs = (
        _make_strategy_leg(
            leg_id="leg-long-lower",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=Direction.LONG,
        ),
        _make_strategy_leg(
            leg_id="leg-short-upper",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=Direction.SHORT,
        ),
    )
    leg_orders = (
        _make_leg_order(
            order_id="leg-long-lower",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=OrderDirection.BUY_TO_OPEN,
            quantity=2.0,
        ),
        _make_leg_order(
            order_id="leg-short-upper",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=OrderDirection.SELL_TO_OPEN,
            quantity=2.0,
        ),
    )
    parent_legs = (
        _make_options_spec(contract_type=OptionContractType.CALL, strike=420.0),
        _make_options_spec(contract_type=OptionContractType.CALL, strike=425.0),
    )
    await _seed_strategy_cluster(
        factory,
        position=_make_pending_strategy_position(
            legs=legs, strategy_type_label="long-call-vertical"
        ),
        parent_order=_make_strategy_parent_order(quantity=2.0, legs=parent_legs),
        leg_orders=leg_orders,
        thesis=_make_active_strategy_thesis(),
        bracket=_make_pending_strategy_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # Long call @ $5.00, short call @ $2.00. Net debit = (5.00 - 2.00) * 2 * 100 = $600.
    for idx, (leg_id, price) in enumerate((("leg-long-lower", 5.00), ("leg-short-upper", 2.00))):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-{leg_id}",
                order_id=leg_id,
                fill_quantity=2.0,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, StrategyPositionDetails)
        # Net debit = +$600 (paid premium).
        net = (
            +1.0 * 5.00 * 2.0 * 100  # long call: +1000
            + -1.0 * 2.00 * 2.0 * 100  # short call: -400
        )
        assert net == pytest.approx(600.0)

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 - 600.0)


async def test_short_put_spread_has_negative_net_credit(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Short put spread (short higher, long lower): net cost basis is negative
    (received premium = net credit)."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    legs = (
        _make_strategy_leg(
            leg_id="leg-short-higher",
            contract_type=OptionContractType.PUT,
            strike=415.0,
            direction=Direction.SHORT,
        ),
        _make_strategy_leg(
            leg_id="leg-long-lower",
            contract_type=OptionContractType.PUT,
            strike=410.0,
            direction=Direction.LONG,
        ),
    )
    leg_orders = (
        _make_leg_order(
            order_id="leg-short-higher",
            contract_type=OptionContractType.PUT,
            strike=415.0,
            direction=OrderDirection.SELL_TO_OPEN,
        ),
        _make_leg_order(
            order_id="leg-long-lower",
            contract_type=OptionContractType.PUT,
            strike=410.0,
            direction=OrderDirection.BUY_TO_OPEN,
        ),
    )
    parent_legs = (
        _make_options_spec(contract_type=OptionContractType.PUT, strike=415.0),
        _make_options_spec(contract_type=OptionContractType.PUT, strike=410.0),
    )
    await _seed_strategy_cluster(
        factory,
        position=_make_pending_strategy_position(
            legs=legs, strategy_type_label="short-put-vertical"
        ),
        parent_order=_make_strategy_parent_order(legs=parent_legs),
        leg_orders=leg_orders,
        thesis=_make_active_strategy_thesis(),
        bracket=_make_pending_strategy_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # Short put @ $4.00, long put @ $1.50. Net credit = (4.00 - 1.50) * 1 * 100 = $250.
    for idx, (leg_id, price) in enumerate((("leg-short-higher", 4.00), ("leg-long-lower", 1.50))):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-{leg_id}",
                order_id=leg_id,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        # Negative net = received premium.
        net = -1.0 * 4.00 * 1.0 * 100 + 1.0 * 1.50 * 1.0 * 100
        assert net == pytest.approx(-250.0)

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 + 250.0)


# ---------------------------------------------------------------------------
# Tests — staggered fills, partial fills
# ---------------------------------------------------------------------------


async def test_staggered_legs_only_open_at_last_filled_event(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """3 legs filled on T1, 4th leg still PENDING → position stays PENDING.
    Once the 4th leg fills on T2 → position transitions OPEN."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    leg_orders = _make_iron_condor_leg_orders()
    await _seed_strategy_cluster(
        factory,
        position=_make_pending_strategy_position(),
        parent_order=_make_strategy_parent_order(),
        leg_orders=leg_orders,
        thesis=_make_active_strategy_thesis(),
        bracket=_make_pending_strategy_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # T1 — 3 of 4 legs fill.
    for idx, (leg_id, price) in enumerate(
        (
            ("leg-short-put", 4.20),
            ("leg-long-put", 1.80),
            ("leg-short-call", 3.50),
        )
    ):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-t1-{leg_id}",
                order_id=leg_id,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        # Position still PENDING — last leg (long call) hasn't filled.
        assert pos.status == PositionStatus.PENDING
        assert pos.entry_timestamp is None

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
        types_after_t1 = {r.event_type for r in log_rows}
    assert EventType.POSITION_OPENED.value not in types_after_t1
    assert EventType.BRACKET_ACTIVATED.value not in types_after_t1

    # T2 — last leg (long call) fills → atomic transition to OPEN.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-t2-leg-long-call",
            order_id="leg-long-call",
            fill_price=1.50,
            fill_timestamp=_NOW - timedelta(minutes=5),
        ),
    )

    ctx2, handle2 = await _open_handle(factory, invocation_id_suffix="-phase1-t2")
    handle2_invocation_id = handle2.invocation_id
    await process_unprocessed_fills(
        handle2,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx2.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert pos.entry_timestamp is not None

        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.invocation_id == handle2_invocation_id
                    )
                )
            )
            .scalars()
            .all()
        )
        types_after_t2 = {r.event_type for r in log_rows}
    # POSITION_OPENED + BRACKET_ACTIVATED fire on the T2 invocation.
    assert EventType.POSITION_OPENED.value in types_after_t2
    assert EventType.BRACKET_ACTIVATED.value in types_after_t2


async def test_partial_leg_fill_position_stays_pending(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """One leg arrives with a partial fill (order_status_after=PARTIALLY_FILLED);
    position stays PENDING and the bracket is not activated."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # 2-leg vertical so we can isolate the partial-fill leg cleanly.
    legs = (
        _make_strategy_leg(
            leg_id="leg-long",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=Direction.LONG,
        ),
        _make_strategy_leg(
            leg_id="leg-short",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=Direction.SHORT,
        ),
    )
    leg_orders = (
        _make_leg_order(
            order_id="leg-long",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=OrderDirection.BUY_TO_OPEN,
            quantity=4.0,
        ),
        _make_leg_order(
            order_id="leg-short",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=OrderDirection.SELL_TO_OPEN,
            quantity=4.0,
        ),
    )
    parent_legs = (
        _make_options_spec(contract_type=OptionContractType.CALL, strike=420.0),
        _make_options_spec(contract_type=OptionContractType.CALL, strike=425.0),
    )
    await _seed_strategy_cluster(
        factory,
        position=_make_pending_strategy_position(
            legs=legs, strategy_type_label="long-call-vertical"
        ),
        parent_order=_make_strategy_parent_order(quantity=4.0, legs=parent_legs),
        leg_orders=leg_orders,
        thesis=_make_active_strategy_thesis(),
        bracket=_make_pending_strategy_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # Only 2 of 4 contracts fill on the long leg; remaining 2 still pending.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-partial-long",
            order_id="leg-long",
            fill_quantity=2.0,
            fill_price=5.00,
            order_status_after=OrderStatus.PARTIALLY_FILLED,
            remaining_quantity_after=2.0,
        ),
    )

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.PENDING
        assert pos.entry_timestamp is None

        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )
        types = [r.event_type for r in log_rows]
    # Per-fill ORDER_FILLED still emits, but no atomic OPEN / BRACKET_ACTIVATED yet.
    assert EventType.POSITION_OPENED.value not in types
    assert EventType.BRACKET_ACTIVATED.value not in types


# ---------------------------------------------------------------------------
# Tests — cancel mid-fill
# ---------------------------------------------------------------------------


async def test_cancel_mid_fill_writes_bracket_incomplete_warning(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Cancel mid-fill with 2 of 4 legs filled: the position stays PENDING,
    a ``bracket_incomplete_warning`` activity-log entry is written naming the
    unfilled leg(s), and the bracket is dissolved."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    leg_orders = _make_iron_condor_leg_orders()
    await _seed_strategy_cluster(
        factory,
        position=_make_pending_strategy_position(),
        parent_order=_make_strategy_parent_order(),
        leg_orders=leg_orders,
        thesis=_make_active_strategy_thesis(),
        bracket=_make_pending_strategy_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)

    # Cancel side effect: 2 legs filled, 2 legs CANCELLED on their order rows.
    await _set_order_status(factory, "leg-short-call", OrderStatus.CANCELLED)
    await _set_order_status(factory, "leg-long-call", OrderStatus.CANCELLED)

    # Per-leg fills for the 2 legs that did fill before cancel.
    for idx, (leg_id, price) in enumerate((("leg-short-put", 4.20), ("leg-long-put", 1.80))):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-{leg_id}",
                order_id=leg_id,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
            ),
        )

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        # No PENDING → OPEN transition because not all legs filled.
        assert pos.status == PositionStatus.PENDING

        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )
        warnings = [
            r for r in log_rows if r.event_type == EventType.BRACKET_INCOMPLETE_WARNING.value
        ]
        types = {r.event_type for r in log_rows}
    assert len(warnings) == 1
    assert EventType.BRACKET_ACTIVATED.value not in types
    assert EventType.POSITION_OPENED.value not in types

    # Warning detail names the cancelled legs.
    import json

    detail = json.loads(warnings[0].detail_json)
    missing = detail["missing_leg_types"]
    assert "leg-short-call" in missing or "leg-long-call" in missing


# ---------------------------------------------------------------------------
# Tests — strategy CLOSE
# ---------------------------------------------------------------------------


async def test_strategy_close_transitions_open_to_closed_with_net_realized_pnl(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """All legs close-fill (atomic timing): OPEN → CLOSED transition happens
    when the last close fill arrives; net realized P/L computed across legs."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Pre-existing OPEN long-call vertical: long @ $5.00, short @ $2.00 (net debit $3.00).
    open_position = _make_open_strategy_position(
        legs=_vertical_long_call_open_legs(),
        execution_history=_vertical_long_call_history(),
        strategy_type_label="long-call-vertical",
    )

    # Close orders for each leg.
    close_leg_orders = (
        _make_leg_order(
            order_id="close-leg-long-lower",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=OrderDirection.SELL_TO_CLOSE,
            quantity=2.0,
            role=OrderRole.CLOSE,
        ),
        _make_leg_order(
            order_id="close-leg-short-upper",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=OrderDirection.BUY_TO_CLOSE,
            quantity=2.0,
            role=OrderRole.CLOSE,
        ),
    )
    parent_legs = _vertical_call_parent_legs()
    parent_close_order = _make_strategy_parent_order(
        order_id="ord-strat-close",
        quantity=2.0,
        legs=parent_legs,
        position_id="pos-strat-1",
        role=OrderRole.CLOSE,
    )
    parent_open_order = _make_strategy_parent_order(quantity=2.0, legs=parent_legs)

    thesis_row, component_rows = thesis_record_to_rows(
        _make_strategy_thesis_with_resolved_components()
    )
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_strategy_bracket())

    seeded_order_ids: set[str] = {
        parent_open_order.order_id,
        parent_close_order.order_id,
        *(o.order_id for o in close_leg_orders),
    }
    extra_order_ids: list[str] = [bracket_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(open_position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(parent_open_order))
        sess.add(order_record_to_row(parent_close_order))
        for leg_order in close_leg_orders:
            sess.add(order_record_to_row(leg_order))
        for oid in extra_order_ids:
            if oid not in seeded_order_ids:
                sess.add(stub_order_row(oid, bracket_row.bracket_id))
                seeded_order_ids.add(oid)
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()

    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=99_400.0))
    await _seed_drawdown_state(factory)

    # Close fills: long leg sells at 7.00 (entry was 5.00, +2.00/contract);
    # short leg bought back at 3.00 (entry was 2.00, -1.00/contract). Realised
    # P/L = long(+2 * 2 contracts * multiplier 100 * sign +1) +
    # short(+1 * 2 contracts * multiplier 100 * sign -1) = +400 - 200 = +200.
    for idx, (leg_id, price) in enumerate(
        (("close-leg-long-lower", 7.00), ("close-leg-short-upper", 3.00))
    ):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-{leg_id}",
                order_id=leg_id,
                fill_quantity=2.0,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.CLOSED
        assert pos.realized_pnl_to_date_usd == pytest.approx(200.0)

        bracket_after = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-strat-1"))
        ).scalar_one()
        assert bracket_after.status == BracketStatus.DISSOLVED.value

        thesis_after = (
            await sess.execute(select(ThesisRow).where(ThesisRow.thesis_id == "thesis-strat-1"))
        ).scalar_one()
        assert thesis_after.status == ThesisRecordStatus.RESOLVED.value

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


# ---------------------------------------------------------------------------
# Tests — ADD on a strategy
# ---------------------------------------------------------------------------


async def test_strategy_add_recomputes_average_cost_basis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADD on an OPEN strategy: per-leg ratios scale by additional_quantity;
    new entry fills correlate to the addition; per-leg average cost basis
    recomputes as a weighted average of original and added contracts."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Pre-existing OPEN long call vertical: 2 contracts @ $5.00 / $2.00.
    open_position = _make_open_strategy_position(
        legs=_vertical_long_call_open_legs(),
        execution_history=_vertical_long_call_history(),
        strategy_type_label="long-call-vertical",
    )

    # ADD orders for each leg with additional_quantity=2 (scales each leg ratio by 2).
    add_leg_orders = (
        _make_leg_order(
            order_id="add-leg-long-lower",
            contract_type=OptionContractType.CALL,
            strike=420.0,
            direction=OrderDirection.BUY_TO_OPEN,
            quantity=2.0,
            role=OrderRole.ADD_ENTRY,
        ),
        _make_leg_order(
            order_id="add-leg-short-upper",
            contract_type=OptionContractType.CALL,
            strike=425.0,
            direction=OrderDirection.SELL_TO_OPEN,
            quantity=2.0,
            role=OrderRole.ADD_ENTRY,
        ),
    )
    parent_legs = _vertical_call_parent_legs()
    parent_open_order = _make_strategy_parent_order(quantity=2.0, legs=parent_legs)
    parent_add_order = _make_strategy_parent_order(
        order_id="ord-strat-add",
        quantity=2.0,
        legs=parent_legs,
        position_id="pos-strat-1",
        role=OrderRole.ADD_ENTRY,
    )

    thesis_row, component_rows = thesis_record_to_rows(_make_active_strategy_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_strategy_bracket())

    seeded_order_ids: set[str] = {
        parent_open_order.order_id,
        parent_add_order.order_id,
        *(o.order_id for o in add_leg_orders),
    }
    extra_order_ids: list[str] = [bracket_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(open_position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(parent_open_order))
        sess.add(order_record_to_row(parent_add_order))
        for leg_order in add_leg_orders:
            sess.add(order_record_to_row(leg_order))
        for oid in extra_order_ids:
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

    # ADD fills: long @ $6.00, short @ $2.50 (different premium → weighted avg shift).
    for idx, (leg_id, price) in enumerate(
        (("add-leg-long-lower", 6.00), ("add-leg-short-upper", 2.50))
    ):
        await _append_fill(
            factory,
            _make_unprocessed_fill(
                fill_id=f"fill-{leg_id}",
                order_id=leg_id,
                fill_quantity=2.0,
                fill_price=price,
                fill_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=idx),
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, StrategyPositionDetails)

        leg_state = {leg.leg_id: leg for leg in pos.details.legs}
        # Long leg: total contracts = 2 + 2 = 4, weighted avg = (2*5 + 2*6) / 4 = 5.5.
        long_leg = leg_state["leg-long-lower"]
        assert long_leg.options.contract_count == pytest.approx(4.0)
        assert long_leg.options.premium_paid_per_contract == pytest.approx(5.5)
        # Short leg: 2 + 2 = 4 contracts, weighted avg = (2*2.0 + 2*2.5) / 4 = 2.25.
        short_leg = leg_state["leg-short-upper"]
        assert short_leg.options.contract_count == pytest.approx(4.0)
        assert short_leg.options.premium_paid_per_contract == pytest.approx(2.25)


# ---------------------------------------------------------------------------
# Tests — atomicity (rollback on failure)
# ---------------------------------------------------------------------------


async def test_strategy_fill_failure_rolls_back_all_state(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Failure mid-integration (close fill claiming more contracts than the
    leg holds) rolls back: per-leg fills stay unprocessed; cash unchanged;
    no activity-log entries persisted."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)
    open_position = _make_open_strategy_position(
        legs=_vertical_long_call_open_legs(),
        execution_history=_vertical_long_call_history(),
        strategy_type_label="long-call-vertical",
    )
    bad_close = _make_leg_order(
        order_id="bad-close-long",
        contract_type=OptionContractType.CALL,
        strike=420.0,
        direction=OrderDirection.SELL_TO_CLOSE,
        quantity=10.0,
        role=OrderRole.CLOSE,
    )
    parent_legs = _vertical_call_parent_legs()
    parent_open_order = _make_strategy_parent_order(quantity=2.0, legs=parent_legs)
    thesis_row, component_rows = thesis_record_to_rows(_make_active_strategy_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_strategy_bracket())
    seeded_order_ids: set[str] = {parent_open_order.order_id, bad_close.order_id}

    async with factory() as sess:
        sess.add(position_record_to_row(open_position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(parent_open_order))
        sess.add(order_record_to_row(bad_close))
        for lrow in leg_rows:
            if lrow.order_id is not None and lrow.order_id not in seeded_order_ids:
                sess.add(stub_order_row(lrow.order_id, bracket_row.bracket_id))
                seeded_order_ids.add(lrow.order_id)
        sess.add(bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()

    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-bad-close",
            order_id="bad-close-long",
            fill_quantity=10.0,
            fill_price=7.00,
        ),
    )

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    try:
        with pytest.raises(ValueError, match="exit fill quantity"):
            await process_unprocessed_fills(
                handle,
                market_inputs=_make_market_inputs(),
                config=_make_state_persistence_config(),
            )
    finally:
        await ctx.__aexit__(ValueError, ValueError("forced"), None)

    async with factory() as sess:
        # Fill row remains UNPROCESSED.
        fill_row = (
            await sess.execute(
                select(FillRecordRow).where(FillRecordRow.fill_id == "fill-bad-close")
            )
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.UNPROCESSED.value

        # Strategy position is unchanged (still OPEN, 2 contracts each leg).
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-strat-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, StrategyPositionDetails)
        for leg in pos.details.legs:
            assert leg.options.contract_count == 2.0

        # Cash unchanged.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == 100_000.0

        # No activity log entries persisted under this invocation.
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
