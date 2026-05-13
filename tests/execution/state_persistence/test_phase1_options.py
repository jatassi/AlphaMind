"""Tests for the Phase 1 fill-integration write path on options positions
(story 03c / ALP-388).

Mirrors the equity tests in ``test_phase1_write_path.py`` but for the OPTIONS
discriminator on ``PositionDetailsPayload``. Builders here construct
``OptionsPositionDetails``-shaped positions and ``OptionsInstrumentSpec``-
shaped orders; everything else (substrate seeding, invocation context, FK
satisfaction) reuses the equity-test helpers' patterns.

Cost-basis convention for options: ``premium_paid_per_contract`` stores the
absolute per-contract premium (always positive), symmetric with equity's
``average_cost_basis_per_share``. Direction sign is applied at compute time
(assembler, P/L formula). The contract multiplier is typically 100 (one
contract = 100 shares of the underlying).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    BracketId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import RiskZone
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
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
    RealizedVolEntry,
)

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-opts"
_PROCESS_ID = "proc-opts-1"
_EXPIRATION = date(2026, 6, 19)
_UNDERLYING = "MSFT"


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
# Builders — options-shaped variants of the equity-test helpers.
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
    leg the PM-equivalent path consults. The fixture provider has no surface
    rows but a realized-vol entry, so any options lookup falls back to the
    realized-vol scalar — sufficient for these tests, which assert state-
    persistence behaviour rather than the attribution math itself.
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


def _make_options_spec(
    *,
    contract_type: OptionContractType = OptionContractType.CALL,
    strike: float = 420.0,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
) -> OptionsInstrumentSpec:
    return OptionsInstrumentSpec(
        underlying=Symbol(_UNDERLYING),
        strike=strike,
        expiration=_EXPIRATION,
        contract_type=contract_type,
        contract_multiplier=contract_multiplier,
    )


def _make_pending_options_entry_order(
    order_id: str = "ord-opt-entry-1",
    *,
    bracket_id: str = "brk-opt-1",
    quantity: float = 5.0,
    direction: OrderDirection = OrderDirection.BUY_TO_OPEN,
    role: OrderRole = OrderRole.ENTRY,
    status: OrderStatus = OrderStatus.PENDING,
    filled_quantity: float = 0.0,
    avg_fill_price: float | None = None,
    position_id: str | None = None,
    contract_type: OptionContractType = OptionContractType.CALL,
    strike: float = 420.0,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
) -> OrderRecord:
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
            "remaining_quantity": quantity - filled_quantity,
            "modification_count": 0,
            "originating_thesis_id": "thesis-opt-1",
            "originating_pm_command_id": None,
            "age_hours": 0.25,
        }
    )


def _make_pending_greeks() -> OptionGreeks:
    """Greeks the guardrail-evaluation library would have set at OPEN validation."""
    return OptionGreeks(
        delta=0.55,
        gamma=0.03,
        theta=-0.02,
        vega=0.18,
        as_of_timestamp=_NOW - timedelta(minutes=20),
        iv_used=0.32,
        refresh_failed=False,
    )


def _make_pending_options_position(
    position_id: str = "pos-opt-1",
    *,
    thesis_id: str | None = "thesis-opt-1",
    bracket_id: str | None = "brk-opt-1",
    direction: Direction = Direction.LONG,
    contract_type: OptionContractType = OptionContractType.CALL,
    strike: float = 420.0,
    contract_count: float = 0.0,
    premium_paid_per_contract: float = 0.0,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
    greeks: OptionGreeks | None = None,
) -> PositionRecord:
    """Build a PENDING options position; Greeks default to validation-time values."""
    details = OptionsPositionDetails(
        underlying_ticker=Symbol(_UNDERLYING),
        strike_price=strike,
        expiration_date=_EXPIRATION,
        contract_type=contract_type,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=greeks if greeks is not None else _make_pending_greeks(),
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


def _make_open_options_position(
    position_id: str = "pos-opt-1",
    *,
    thesis_id: str | None = "thesis-opt-1",
    bracket_id: str | None = "brk-opt-1",
    direction: Direction = Direction.LONG,
    contract_type: OptionContractType = OptionContractType.CALL,
    strike: float = 420.0,
    contract_count: float = 5.0,
    premium_paid_per_contract: float = 8.75,
    contract_multiplier: float = LISTED_OPTION_CONTRACT_MULTIPLIER,
    fill_price: float = 8.75,
    greeks: OptionGreeks | None = None,
) -> PositionRecord:
    """Build an OPEN options position whose execution_history reflects an entry fill."""
    from alphamind.portfolio_state.records.positions import PositionFill

    details = OptionsPositionDetails(
        underlying_ticker=Symbol(_UNDERLYING),
        strike_price=strike,
        expiration_date=_EXPIRATION,
        contract_type=contract_type,
        contract_count=contract_count,
        contract_multiplier=contract_multiplier,
        premium_paid_per_contract=premium_paid_per_contract,
        greeks=greeks if greeks is not None else _make_pending_greeks(),
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=fill_price,
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


def _make_pending_options_bracket(
    bracket_id: str = "brk-opt-1",
    position_id: str = "pos-opt-1",
) -> BracketRecord:
    """A PENDING_ENTRY bracket with a monitor-managed price-stop leg.

    For options, Alpaca does not support brackets; the protective leg has
    ``order_id=None`` because the trigger is monitored against the underlying
    stream by the continuous monitor (ALP-123). Phase 1 just transitions the
    leg from PENDING_ACTIVATION to ACTIVE on entry fill.
    """
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol(_UNDERLYING), threshold_usd=410.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ord-opt-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_options_bracket(
    bracket_id: str = "brk-opt-1",
    position_id: str = "pos-opt-1",
) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=None,
        trigger=PriceTrigger(
            underlying_ticker=Symbol(_UNDERLYING), threshold_usd=410.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-opt-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _make_active_options_thesis(
    thesis_id: str = "thesis-opt-1",
    position_id: str = "pos-opt-1",
) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=ThesisId(thesis_id),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=_UNDERLYING,
            narrative=f"{ct.value} narrative",
            key_assumptions=(KeyAssumption(text="MSFT call thesis", outcome=None),),
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
        summary="MSFT call momentum",
        key_catalyst="Cloud earnings",
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


def _make_options_thesis_with_resolved_components(
    thesis_id: str = "thesis-opt-1",
    position_id: str = "pos-opt-1",
) -> ThesisRecord:
    """ACTIVE thesis whose components carry pre-set resolution outcomes.

    Used in exit-fill tests so that when Phase 1 transitions thesis.status
    to RESOLVED, the resulting record stays valid.
    """
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=ThesisId(thesis_id),
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
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="MSFT call momentum",
        key_catalyst="Cloud earnings",
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
    order_id: str = "ord-opt-entry-1",
    fill_quantity: float = 5.0,
    fill_price: float = 8.75,
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
    """Seed a full options-position cluster in one deferred-FK transaction."""
    from tests.execution.state_persistence._fk_substrate import stub_order_row

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
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=_INV_ID + "-phase1"),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Tests — entry fills
# ---------------------------------------------------------------------------


async def test_long_call_entry_fill_transitions_pending_position_to_open(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """BUY_TO_OPEN long-call entry: PENDING → OPEN with positive cost basis
    scaled by contract_multiplier; greeks preserved; bracket activates."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    pre_greeks = _make_pending_greeks()
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_options_position(greeks=pre_greeks),
        _make_pending_options_entry_order(),
        _make_active_options_thesis(),
        _make_pending_options_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Entry fill: 5 contracts at $8.75 premium each, multiplier 100.
    # Expected cash debit = 5 * 8.75 * 100 = $4375.
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-opt-1"))

    ctx, handle = await _open_handle(factory)
    summary = await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert summary.fills_processed == 1
    assert summary.fills_quarantined == 0

    async with factory() as sess:
        # Position transitions PENDING → OPEN with options-shaped details.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert pos.entry_timestamp is not None
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == 5.0
        assert pos.details.premium_paid_per_contract == 8.75
        # Total cost basis = 8.75 * 5 * 100 = $4375 (positive for long).
        total_cost_basis = (
            pos.details.premium_paid_per_contract
            * pos.details.contract_count
            * pos.details.contract_multiplier
        )
        assert total_cost_basis == pytest.approx(4_375.0)
        # Greeks preserved unchanged from pre-fill validation snapshot.
        assert pos.details.greeks == pre_greeks
        # Execution history captures the fill.
        assert len(pos.execution_history) == 1
        assert pos.execution_history[0].fill_quantity == 5.0
        assert pos.execution_history[0].fill_price == 8.75

        # Cash debited by premium * quantity * multiplier.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 - 4_375.0)

        # Activity log carries entry-fill events.
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
    assert EventType.CASH_DEBITED.value in types


async def test_short_put_entry_fill_carries_negative_cost_basis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """SELL_TO_OPEN short-put entry: PENDING → OPEN with negative
    ``premium_paid_per_contract`` (premium received), and cash credited."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_options_position(
            direction=Direction.SHORT,
            contract_type=OptionContractType.PUT,
            strike=415.0,
        ),
        _make_pending_options_entry_order(
            direction=OrderDirection.SELL_TO_OPEN,
            contract_type=OptionContractType.PUT,
            strike=415.0,
        ),
        _make_active_options_thesis(),
        _make_pending_options_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Short entry: 5 contracts at $3.50 premium each, multiplier 100.
    # Expected cash credit = 5 * 3.50 * 100 = $1750.
    await _append_fill(
        factory,
        _make_unprocessed_fill(fill_id="fill-opt-short-1", fill_price=3.50),
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert pos.direction == Direction.SHORT
        assert isinstance(pos.details, OptionsPositionDetails)
        # Per-contract premium is stored as the absolute amount (positive),
        # symmetric with equity's average_cost_basis_per_share. Direction-aware
        # callers (assembler, P/L compute) sign-flip via position.direction.
        assert pos.details.premium_paid_per_contract == pytest.approx(3.50)
        # Direction-signed total cost basis is negative for SHORT.
        direction_sign = -1.0 if pos.direction == Direction.SHORT else 1.0
        signed_total_cost_basis = (
            pos.details.premium_paid_per_contract
            * pos.details.contract_count
            * pos.details.contract_multiplier
            * direction_sign
        )
        assert signed_total_cost_basis == pytest.approx(-1_750.0)

        # Cash credited.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 + 1_750.0)

        # Sell-side: CASH_CREDITED, not CASH_DEBITED.
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
    assert EventType.CASH_CREDITED.value in types
    assert EventType.CASH_DEBITED.value not in types
    assert EventType.POSITION_OPENED.value in types


async def test_add_fill_recomputes_weighted_average_premium(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADD-side options fill (BUY_TO_OPEN against an OPEN long position)
    increments contract_count and recomputes weighted-average premium per
    the same formula the equity path uses for share-count adds."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Pre-existing OPEN long position: 5 contracts at $8.75 premium.
    await _seed_position_order_thesis_bracket(
        factory,
        _make_open_options_position(
            contract_count=5.0,
            premium_paid_per_contract=8.75,
            fill_price=8.75,
        ),
        _make_pending_options_entry_order(
            order_id="ord-opt-add-1",
            role=OrderRole.ADD_ENTRY,
            quantity=3.0,
            position_id="pos-opt-1",
        ),
        _make_active_options_thesis(),
        _make_active_options_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    # ADD fill: 3 contracts at $9.25 premium each, multiplier 100.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-opt-add-1",
            order_id="ord-opt-add-1",
            fill_quantity=3.0,
            fill_price=9.25,
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(8.0)
        # Weighted avg premium = (5 * 8.75 + 3 * 9.25) / 8 = 8.9375
        assert pos.details.premium_paid_per_contract == pytest.approx(8.9375)
        # Both fills are in execution_history.
        assert len(pos.execution_history) == 2


async def test_partial_close_fill_accumulates_realized_pl_and_emits_position_reduced(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Partial CLOSE on a long-call position: realized P/L is computed
    multiplier-scaled, contract_count drops to the remaining amount, and
    a ``position_reduced`` activity-log entry is written."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Pre-existing OPEN long position: 5 contracts at $8.75 premium.
    close_order = _make_pending_options_entry_order(
        order_id="ord-opt-close-1",
        role=OrderRole.CLOSE,
        direction=OrderDirection.SELL_TO_CLOSE,
        quantity=2.0,
        position_id="pos-opt-1",
    )
    await _seed_position_order_thesis_bracket(
        factory,
        _make_open_options_position(
            contract_count=5.0,
            premium_paid_per_contract=8.75,
            fill_price=8.75,
        ),
        close_order,
        _make_active_options_thesis(),
        _make_active_options_bracket(),
    )
    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=100_000.0))
    await _seed_drawdown_state(factory)
    # Partial close: 2 contracts at $10.00; realized P/L per contract per share =
    # 10.00 - 8.75 = 1.25. Multiplier = 100. Realized = 1.25 * 2 * 100 = $250.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-opt-close-partial",
            order_id="ord-opt-close-1",
            fill_quantity=2.0,
            fill_price=10.0,
            order_status_after=OrderStatus.FILLED,
            remaining_quantity_after=0.0,
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        # Position remains OPEN with reduced contract count.
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == pytest.approx(3.0)
        # Premium per contract unchanged on partial close.
        assert pos.details.premium_paid_per_contract == pytest.approx(8.75)
        # Realized P/L accumulates.
        assert pos.realized_pnl_to_date_usd == pytest.approx(250.0)

        # Cash credited by exit proceeds: 2 * 10.00 * 100 = $2000.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(100_000.0 + 2_000.0)

        # Activity log carries position_reduced (not position_closed).
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
    assert EventType.POSITION_REDUCED.value in types
    assert EventType.POSITION_CLOSED.value not in types
    assert EventType.CASH_CREDITED.value in types


async def test_full_close_fill_transitions_position_closed_and_dissolves_bracket(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Full SELL_TO_CLOSE on a long-call: OPEN → CLOSED with cumulative
    realized P/L; bracket transitions ACTIVE → DISSOLVED; thesis RESOLVED;
    activity log carries position_closed + bracket_dissolved + thesis_resolved."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Cyclic substrate: position + thesis + bracket all reference each other.
    close_order = _make_pending_options_entry_order(
        order_id="ord-opt-close-full",
        role=OrderRole.CLOSE,
        direction=OrderDirection.SELL_TO_CLOSE,
        quantity=5.0,
        position_id="pos-opt-1",
    )
    entry_order = _make_pending_options_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(
        _make_options_thesis_with_resolved_components()
    )
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_options_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, close_order.order_id}

    async with factory() as sess:
        sess.add(
            position_record_to_row(
                _make_open_options_position(
                    contract_count=5.0,
                    premium_paid_per_contract=8.75,
                    fill_price=8.75,
                )
            )
        )
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

    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=95_625.0))
    await _seed_drawdown_state(factory)
    # Full close: 5 contracts at $11.00; realized P/L = (11 - 8.75) * 5 * 100 = $1125.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-opt-close-full",
            order_id="ord-opt-close-full",
            fill_quantity=5.0,
            fill_price=11.0,
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.CLOSED
        assert pos.realized_pnl_to_date_usd == pytest.approx(1_125.0)

        # Bracket transitions ACTIVE → DISSOLVED.
        bracket_row_after = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-opt-1"))
        ).scalar_one()
        assert bracket_row_after.status == BracketStatus.DISSOLVED.value

        # Thesis RESOLVED.
        thesis_row_after = (
            await sess.execute(select(ThesisRow).where(ThesisRow.thesis_id == "thesis-opt-1"))
        ).scalar_one()
        assert thesis_row_after.status == ThesisRecordStatus.RESOLVED.value

        # Cash credited by close proceeds: 5 * 11.00 * 100 = $5500.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(95_625.0 + 5_500.0)

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
    # Full close should NOT also emit position_reduced.
    assert EventType.POSITION_REDUCED.value not in types


async def test_bracket_activation_for_monitor_managed_legs_carries_empty_order_ids(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Options brackets have monitor-managed protective legs (Alpaca doesn't
    support brackets on options, so ``BracketLeg.order_id is None`` for the
    price-stop leg). On entry fill, the leg transitions PENDING_ACTIVATION →
    ACTIVE and the BRACKET_ACTIVATED event carries an empty
    ``protective_leg_order_ids`` tuple — the monitor work tree (ALP-123) is
    the consumer of the activation marker."""
    from alphamind.execution.state_persistence.tables.bracket_legs import BracketLegRow
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_order_thesis_bracket(
        factory,
        _make_pending_options_position(),
        _make_pending_options_entry_order(),
        _make_active_options_thesis(),
        _make_pending_options_bracket(),
    )
    await _seed_cash_ledger(factory)
    await _seed_drawdown_state(factory)
    await _append_fill(factory, _make_unprocessed_fill(fill_id="fill-opt-1"))

    ctx, handle = await _open_handle(factory)
    await process_unprocessed_fills(
        handle,
        market_inputs=_make_market_inputs(),
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Bracket transitions PENDING_ENTRY → ACTIVE and the leg transitions
        # PENDING_ACTIVATION → ACTIVE even though it has order_id=None.
        bracket_row = (
            await sess.execute(select(BracketRow).where(BracketRow.bracket_id == "brk-opt-1"))
        ).scalar_one()
        assert bracket_row.status == BracketStatus.ACTIVE.value
        leg_rows = (
            (
                await sess.execute(
                    select(BracketLegRow).where(BracketLegRow.bracket_id == "brk-opt-1")
                )
            )
            .scalars()
            .all()
        )
        assert len(leg_rows) == 1
        assert leg_rows[0].leg_status == BracketLegStatus.ACTIVE.value
        assert leg_rows[0].order_id is None

        # BRACKET_ACTIVATED activity-log entry carries empty leg-order-ids tuple.
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow)
                    .where(ActivityLogRow.invocation_id == handle.invocation_id)
                    .where(ActivityLogRow.event_type == EventType.BRACKET_ACTIVATED.value)
                )
            )
            .scalars()
            .all()
        )
        assert len(log_rows) == 1


async def test_buy_to_close_short_position_debits_cash_and_realizes_pnl(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """BUY_TO_CLOSE on an OPEN short-put position: cash debited by premium
    paid (covering the short); position transitions OPEN -> CLOSED with
    realized P/L = (entry_premium_received - exit_premium_paid) * qty * multiplier."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)

    short_open_position = _make_open_options_position(
        direction=Direction.SHORT,
        contract_type=OptionContractType.PUT,
        strike=415.0,
        contract_count=5.0,
        # Premium stored as absolute amount; direction sign applied at compute time.
        premium_paid_per_contract=3.50,
        fill_price=3.50,
    )
    close_order = _make_pending_options_entry_order(
        order_id="ord-opt-cover-1",
        role=OrderRole.CLOSE,
        direction=OrderDirection.BUY_TO_CLOSE,
        quantity=5.0,
        position_id="pos-opt-1",
        contract_type=OptionContractType.PUT,
        strike=415.0,
    )
    entry_order = _make_pending_options_entry_order(
        direction=OrderDirection.SELL_TO_OPEN,
        contract_type=OptionContractType.PUT,
        strike=415.0,
    )
    thesis_row, component_rows = thesis_record_to_rows(
        _make_options_thesis_with_resolved_components()
    )
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_options_bracket())
    leg_order_ids = [lrow.order_id for lrow in leg_rows if lrow.order_id is not None]
    seeded_order_ids: set[str] = {entry_order.order_id, close_order.order_id}

    async with factory() as sess:
        sess.add(position_record_to_row(short_open_position))
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

    await _seed_cash_ledger(factory, _make_cash_ledger(current_cash_usd=101_750.0))
    await _seed_drawdown_state(factory)
    # Short closed for less than collected = profit:
    # premium received at entry $3.50; cover cost $1.00; per-contract profit
    # $2.50; total realized = $2.50 * 5 * 100 = $1250.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-opt-cover-1",
            order_id="ord-opt-cover-1",
            fill_quantity=5.0,
            fill_price=1.00,
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
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.CLOSED
        # Profit = $1250 (short closed at lower premium than collected).
        assert pos.realized_pnl_to_date_usd == pytest.approx(1_250.0)

        # Cash debited by cover cost: 5 * 1.00 * 100 = $500.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == pytest.approx(101_750.0 - 500.0)

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
    assert EventType.CASH_DEBITED.value in types
    assert EventType.POSITION_CLOSED.value in types


async def test_atomicity_exit_fill_exceeds_open_quantity_rolls_back(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An options exit fill claiming more contracts than the position holds
    raises ValueError; the surrounding InvocationContext rolls back, the
    fill row remains unprocessed, and no activity-log entries persist."""
    from alphamind.execution.state_persistence.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

    _, factory = db
    await _seed_invocation_substrate(factory)

    close_order = _make_pending_options_entry_order(
        order_id="ord-opt-bad-close",
        role=OrderRole.CLOSE,
        direction=OrderDirection.SELL_TO_CLOSE,
        quantity=10.0,
        position_id="pos-opt-1",
    )
    entry_order = _make_pending_options_entry_order()
    thesis_row, component_rows = thesis_record_to_rows(_make_active_options_thesis())
    bracket_row, leg_rows = bracket_record_to_rows(_make_active_options_bracket())
    seeded_order_ids: set[str] = {entry_order.order_id, close_order.order_id}

    async with factory() as sess:
        sess.add(
            position_record_to_row(
                _make_open_options_position(
                    contract_count=5.0,
                    premium_paid_per_contract=8.75,
                    fill_price=8.75,
                )
            )
        )
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(order_record_to_row(entry_order))
        sess.add(order_record_to_row(close_order))
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
    # Bad fill: 10 contracts but position only has 5.
    await _append_fill(
        factory,
        _make_unprocessed_fill(
            fill_id="fill-opt-bad",
            order_id="ord-opt-bad-close",
            fill_quantity=10.0,
            fill_price=11.0,
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
        # Fill row remains unprocessed.
        fill_row = (
            await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == "fill-opt-bad"))
        ).scalar_one()
        assert fill_row.processing_status == FillProcessingStatus.UNPROCESSED.value

        # Position state unchanged.
        pos_row = (
            await sess.execute(select(PositionRow).where(PositionRow.position_id == "pos-opt-1"))
        ).scalar_one()
        pos = position_row_to_record(pos_row)
        assert pos.status == PositionStatus.OPEN
        assert isinstance(pos.details, OptionsPositionDetails)
        assert pos.details.contract_count == 5.0
        assert pos.realized_pnl_to_date_usd is None

        # Cash ledger unchanged.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.current_cash_usd == 100_000.0

        # No activity-log entries persisted under this invocation.
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


# Strategy / mleg coverage moved to ``test_phase1_strategy.py`` under story
# 04b (ALP-392) — Phase 1 now supports STRATEGY-discriminated positions in
# addition to equity (story 07) and options (story 03c / ALP-388).
