"""Tests for the Phase 2 command-execution write path (story 08 / ALP-366).

Covers ``persist_envelope_outcome`` and ``persist_envelope_parse_failure`` —
the two entry points the engine-stub ``submit_envelope`` MCP wrapper calls
once an ``InvocationHandle`` is supplied. Per parent issue Pre-resolved
decision (C), the envelope provenance + per-command results stay embedded in
the ``activity_log`` table; no separate ``envelopes`` or ``oms_commands``
tables.

All writes join the open ``InvocationContext`` transaction so the surrounding
context commits or rolls back atomically per the design doc's Phase 2
contract.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import (
    AlpacaOrderId,
    BracketId,
    EnvelopeId,
    InvocationId,
    OrderId,
    PositionId,
    RecommendationId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.commands.command_models import (
    BracketAdjustment,
    BracketOrderParameters,
    EntryOrder,
    EntryWindow,
    EquityInstrument,
    NewStopLevel,
    NewTargetLevel,
    PositionSize,
    PriceCondition,
    PriceLeg,
    Target,
    Thesis,
)
from alphamind.commands.command_models import (
    ThesisComponent as OMSThesisComponent,
)
from alphamind.decision.portfolio_manager.models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    CriterionAssessment,
    OpenCommand,
    PMAnalystEnvelope,
    PMEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
)
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    FailedSubmissionEntry,
    SubmissionResult,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.events.activity_log import (
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
    EventTrigger,
    PLAnchorSpec,
    PriceTrigger,
    TimeTrigger,
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
from alphamind.state.tables.bracket_legs import BracketLegRow
from alphamind.state.tables.brackets import BracketRow
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.brackets_codec import (
    row_to_leg,
)
from alphamind.state.tables.brackets_codec import (
    rows_to_record as bracket_rows_to_record,
)
from alphamind.state.tables.cash_ledger import (
    CASH_LEDGER_SINGLETON_ID,
    CashLedgerRow,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"
_PROCESS_ID = "proc-1"


# ---------------------------------------------------------------------------
# Engine + session fixtures
# ---------------------------------------------------------------------------


# Bypass-init helpers — replace Pydantic ``model_construct``. The dataclass __init__
# enforces all fields; these helpers skip validation so tests can inject sparse fixtures.


def _bypass_init_PortfolioManagerView(**kwargs: object) -> PortfolioManagerView:  # noqa: N802
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

    obj = object.__new__(PortfolioManagerView)
    for k, v in kwargs.items():
        object.__setattr__(obj, k, v)
    return obj


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB."""
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
# Builders — invocation substrate
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


async def _seed_position(factory: async_sessionmaker[AsyncSession], record: PositionRecord) -> None:
    async with factory() as sess:
        sess.add(position_record_to_row(record))
        await sess.commit()


async def _seed_thesis(factory: async_sessionmaker[AsyncSession], record: ThesisRecord) -> None:
    parent_row, child_rows = thesis_record_to_rows(record)
    async with factory() as sess:
        sess.add(parent_row)
        await sess.flush()
        for crow in child_rows:
            sess.add(crow)
        await sess.commit()


async def _seed_bracket(factory: async_sessionmaker[AsyncSession], record: BracketRecord) -> None:
    parent_row, leg_rows = bracket_record_to_rows(record)
    async with factory() as sess:
        sess.add(parent_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _seed_position_cluster(
    factory: async_sessionmaker[AsyncSession],
    position: PositionRecord,
    thesis: ThesisRecord,
    bracket: BracketRecord,
    *extra_orders: Any,
) -> None:
    """Seed position + thesis + bracket in a single deferred-FK transaction.

    All three rows reference each other cyclically, so they must commit
    together.  bracket_legs.bracket_id is a non-deferred FK — the bracket row
    is flushed before legs are added.

    Pass additional OrderRecord values (already row-converted) via *extra_orders
    to include them in the same atomic transaction (e.g. a pre-existing
    protective order that the bracket's entry_order_id references).
    """
    from alphamind.state.tables.orders_codec import (
        record_to_row as order_record_to_row,
    )
    from tests.state._fk_substrate import stub_order_row

    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_parent, leg_rows = bracket_record_to_rows(bracket)

    # Collect order_ids that must already exist: entry_order_id + all leg order_ids.
    stub_ids_needed: list[str] = [bracket_parent.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]
    extra_order_ids: set[str] = {r.order_id for r in extra_orders}

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_row)
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


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str = _INV_ID + "-phase2",
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=invocation_id),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Builders — PMEnvelope / OMSCommand variants
# ---------------------------------------------------------------------------


def _all_pass_thesis_eval() -> ThesisQualityEvaluation:
    p = CriterionAssessment(status="pass", note=None)
    return ThesisQualityEvaluation(
        falsifiability=p,
        sizing_proportionality=p,
        portfolio_coherence=p,
        timing_plausibility=p,
        counterargument_consideration=p,
    )


def _all_pass_position_eval() -> PositionActionEvaluation:
    p = CriterionAssessment(status="pass", note=None)
    return PositionActionEvaluation(
        status_classification_warrant=p,
        action_status_alignment=p,
        action_specific_justification=p,
        portfolio_coherence=p,
    )


def _open_command(
    underlying: str = "NVDA",
    *,
    quantity: float = 10.0,
    dollar_value: float = 10_000.0,
    entry_window: EntryWindow | None = None,
    entry_order: EntryOrder | None = None,
) -> OpenCommand:
    return OpenCommand(
        command_type="open",
        entry_window=entry_window,
        instrument=EquityInstrument(asset_type="equity", ticker=underlying, direction="long"),
        entry_order=entry_order or EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=quantity, dollar_value=money(dollar_value)),
        target=Target(
            target_type="absolute_price",
            price=price(950.0),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=underlying,
                    comparator="<=",
                    trigger_price=price(750.0),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary=f"Long {underlying}.",
            components=(
                OMSThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference=underlying,
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
    )


def _close_command(position_id: str = "POS-NVDA-001") -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=PositionId(position_id),
        quantity="all",
        order_type="market",
        limit_price=None,
        close_rationale_type="thesis_invalidated",
        invalidation_reason="Thesis broken.",
        risk_management_subtype=None,
    )


def _adjust_command(
    position_id: str = "POS-NVDA-001",
    *,
    with_stop_level: bool = True,
) -> AdjustCommand:
    """Build a canonical ADJUST command.

    By default, includes a ``new_stop_level`` so the writeback produces a
    replacement protective order. Pass ``with_stop_level=False`` for a
    thesis-only ADJUST that exercises the no-broker-mutation path.
    """
    return AdjustCommand(
        command_type="adjust",
        position_id=PositionId(position_id),
        adjustment_rationale="Tighten stop.",
        new_stop_level=(
            NewStopLevel(trigger_price=price(145.0), order_type="stop", limit_price=None)
            if with_stop_level
            else None
        ),
        new_target_level=None,
        new_time_expiration=None,
        new_event_invalidation=None,
        thesis_component_updates=(
            OMSThesisComponent(
                component_type="entry_rationale",
                linked_leg="entry",
                instrument_reference="NVDA",
                narrative="Updated rationale.",
                key_assumptions=("Updated.",),
            ),
        ),
    )


def _cancel_command(order_id: str = "ord-entry-1") -> CancelCommand:
    return CancelCommand(command_type="cancel", order_id=OrderId(order_id), cancel_reason="stale")


def _add_command(
    position_id: str = "POS-NVDA-001",
    *,
    bracket_adjustment: BracketAdjustment | None = None,
    entry_order: EntryOrder | None = None,
) -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id=PositionId(position_id),
        additional_quantity=5.0,
        additional_dollar_value=money(5_000.0),
        entry_order=entry_order or EntryOrder(type="market", limit_price=None, stop_price=None),
        thesis_addition_component=OMSThesisComponent(
            component_type="entry_rationale",
            linked_leg="add",
            instrument_reference="NVDA",
            narrative="Add to NVDA.",
            key_assumptions=("Setup intact.",),
        ),
        bracket_adjustment=bracket_adjustment,
    )


def _make_analyst_envelope(
    envelope_id: str = "ENV-REC-1",
    commands: tuple[Any, ...] = (),
) -> PMEnvelope:
    if not commands:
        commands = (_open_command(),)
    return PMAnalystEnvelope(
        envelope_id=EnvelopeId(envelope_id),
        invocation_id=InvocationId(_INV_ID),
        source_provenance="pm_analyst",
        source_recommendation_id=RecommendationId("REC-1"),
        recommendation_type="new_entry",
        verdict="approve",
        evaluation=_all_pass_thesis_eval(),
        modifications=(),
        concerns=(),
        rationale_narrative="OK proposal.",
        anti_patterns_identified=None,
        commands=commands,
    )


def _make_strategist_envelope(
    envelope_id: str = "ENV-SA-1",
    position_id: str = "POS-NVDA-001",
    commands: tuple[Any, ...] = (),
) -> PMEnvelope:
    if not commands:
        commands = (_close_command(position_id=position_id),)
    return PMStrategistEnvelope(
        envelope_id=EnvelopeId(envelope_id),
        invocation_id=InvocationId(_INV_ID),
        source_provenance="pm_strategist",
        source_recommendation_id=RecommendationId("SA-1"),
        recommendation_type="position_assessment",
        position_id=PositionId(position_id),
        verdict="approve",
        evaluation=_all_pass_position_eval(),
        modifications=(),
        concerns=(),
        rationale_narrative="OK assessment.",
        anti_patterns_identified=None,
        commands=commands,
    )


def _accepted_result(command_ordinal: int, command_id: str) -> SubmissionResult:
    return SubmissionResult(
        command_ordinal=command_ordinal,
        status="accepted",
        command_id=command_id,
        acknowledgment=Acknowledgment(),
    )


# ---------------------------------------------------------------------------
# Existing-state seed helpers used by CLOSE / ADJUST / CANCEL / ADD tests
# ---------------------------------------------------------------------------


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


def _pending_never_filled_position(
    position_id: str = "POS-NVDA-001",
    *,
    thesis_id: str = "THE-NVDA-1",
    bracket_id: str = "BRK-NVDA-1",
    ticker: str = "NVDA",
) -> PositionRecord:
    """A PENDING position whose entry has not filled — empty execution_history,
    ``entry_timestamp=None`` (ALP-744). This is the shape the cancel/dissolve
    write path must drive to the terminal CANCELLED state so it can't strand."""
    details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=10.0,
        average_cost_basis_per_share=150.0,
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id),
        bracket_id=BracketId(bracket_id),
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


def _active_thesis(
    thesis_id: str = "THE-NVDA-1", position_id: str = "POS-NVDA-001"
) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
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
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _pending_entry_bracket_with_event_leg(
    bracket_id: str = "BRK-NVDA-1",
    position_id: str = "POS-NVDA-001",
) -> BracketRecord:
    """A never-filled PENDING_ENTRY bracket: a mechanical PRICE_STOP leg (with
    a protective order) plus an order-less EVENT_INVALIDATION advisory leg.

    The ALP-731 incident shape: an entry that never fills, dissolved by a PM
    CANCEL. The order-less event leg is the one an orders-only cancel sweep can
    never reach — it has no broker order. All legs are PENDING_ACTIVATION,
    as the PENDING_ENTRY read invariant requires.
    """
    price_stop = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("NVDA"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    event_leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-event",
        leg_type=BracketLegType.EVENT_INVALIDATION,
        order_id=None,
        trigger=EventTrigger(description="Guidance withdrawn", condition_evaluator_id=None),
        enforcement=BracketLegEnforcement.ADVISORY,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(price_stop, event_leg),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _three_leg_bracket(
    *,
    bracket_id: str = "BRK-NVDA-1",
    position_id: str = "POS-NVDA-001",
    underlying: str = "NVDA",
    target_pl_anchor: PLAnchorSpec | None = None,
) -> BracketRecord:
    """An ACTIVE bracket carrying one TAKE_PROFIT + PRICE_STOP + TIME_EXPIRATION
    leg — the shape the ADJUST / ADD leg-modification path re-persists in place.

    One leg of each modifiable type so the stop / target / time change-fields
    each resolve to exactly one matching leg. ``target_pl_anchor`` seeds the
    OPEN-time anchor on the take-profit leg (set for a strategy bracket).
    """
    take_profit = BracketLeg(
        leg_id=f"{bracket_id}-leg-target",
        leg_type=BracketLegType.TAKE_PROFIT,
        order_id=OrderId(f"{bracket_id}-ord-target"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol(underlying), threshold_usd=950.0, direction="GTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
        pl_anchor=target_pl_anchor,
    )
    price_stop = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol(underlying), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    time_leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-time",
        leg_type=BracketLegType.TIME_EXPIRATION,
        order_id=OrderId(f"{bracket_id}-ord-time"),
        trigger=TimeTrigger(deadline=_NOW + timedelta(hours=24)),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId("ord-entry-1"),
        protective_legs=(take_profit, price_stop, time_leg),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _strategy_position(
    position_id: str = "POS-NVDA-001",
    *,
    thesis_id: str = "THE-NVDA-1",
    bracket_id: str = "BRK-NVDA-1",
    net_premium_usd: float = -300.0,
) -> PositionRecord:
    """A bull put credit spread on NVDA (short 850 put / long 840 put).

    Mirrors the strategy-evaluator test fixture: ``net_premium_usd`` is the
    strategy cost basis (credit-negative), ``max_profit_usd`` its magnitude.
    At a spot far above both strikes both puts decay worthless, so the
    strategy's net P/L climbs toward the full credit.
    """

    def _leg(leg_id: str, strike: float, direction: Direction) -> StrategyLeg:
        return StrategyLeg(
            leg_id=leg_id,
            options=OptionsPositionDetails(
                underlying_ticker=Symbol("NVDA"),
                strike_price=strike,
                expiration_date=date(2026, 6, 19),
                contract_type=OptionContractType.PUT,
                contract_count=1.0,
                contract_multiplier=100.0,
                premium_paid_per_contract=0.0,
                greeks=OptionGreeks(
                    delta=-0.3,
                    gamma=0.02,
                    theta=-0.04,
                    vega=0.2,
                    as_of_timestamp=_NOW,
                    iv_used=0.30,
                ),
            ),
            direction=direction,
        )

    details = StrategyPositionDetails(
        strategy_type_label="vertical_spread",
        legs=(
            _leg("leg-short", 850.0, Direction.SHORT),
            _leg("leg-long", 840.0, Direction.LONG),
        ),
        net_premium_usd=net_premium_usd,
        max_profit_usd=abs(net_premium_usd),
        max_loss_usd=-(1000.0 - abs(net_premium_usd)),
        breakeven_levels=(),
        strategy_greeks=OptionGreeks(delta=0.1, gamma=0.0, theta=0.01, vega=-0.05),
    )
    return PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=ThesisId(thesis_id),
        bracket_id=BracketId(bracket_id),
        status=PositionStatus.OPEN,
        direction=None,
        entry_timestamp=_NOW - timedelta(hours=2),
        details=details,
        execution_history=(
            PositionFill(
                fill_timestamp=_NOW - timedelta(hours=2),
                fill_price=price(3.0),
                fill_quantity=1.0,
                slippage=signed_money(0.0),
                fees=money(0.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


# ---------------------------------------------------------------------------
# Helper — read all activity log entries written under one invocation
# ---------------------------------------------------------------------------


async def _read_activity_log_for(
    factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
) -> list[ActivityLogRow]:
    async with factory() as sess:
        return list(
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )


async def _read_leg_rows(
    factory: async_sessionmaker[AsyncSession], *, leg_type: str
) -> list[BracketLegRow]:
    """Every ``bracket_legs`` row of a given ``leg_type``."""
    async with factory() as sess:
        return list(
            (await sess.execute(select(BracketLegRow).where(BracketLegRow.leg_type == leg_type)))
            .scalars()
            .all()
        )


# ===========================================================================
# Tests — persist_envelope_parse_failure
# ===========================================================================


async def test_envelope_parse_failure_writes_one_log_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A Layer-1 ValidationError appends one ENVELOPE_PARSE_FAILED activity
    log entry whose detail carries the raw args, the error string, and the
    synthetic command_id."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_parse_failure,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    failed_entry = FailedSubmissionEntry(
        raw_args={"envelope_id": "ENV-REC-99", "garbage": "value"},
        validation_error_repr="source_provenance: Field required",
        command_id="inv-2026-05-08T12:00:00Z-aaaa.ENV-REC-99.0.0",
    )

    ctx, handle = await _open_handle(factory)
    await persist_envelope_parse_failure(
        handle, failed_entry, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == EventType.ENVELOPE_PARSE_FAILED.value
    detail = json.loads(row.detail_json)
    assert detail["attempted_envelope_id"] == "ENV-REC-99"
    assert detail["attempted_command_id"] == "inv-2026-05-08T12:00:00Z-aaaa.ENV-REC-99.0.0"
    assert "source_provenance" in detail["validation_error_repr"]
    assert "ENV-REC-99" in detail["raw_args_json"]


# ===========================================================================
# Tests — persist_envelope_rejection (Layer-2/3 envelope-level rejection)
# ===========================================================================


async def test_persist_envelope_rejection_writes_one_log_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A Layer-2/3 envelope-level rejection (e.g. halt-mode invariant
    violation) appends one ENVELOPE_REJECTED activity log entry capturing
    the envelope id and the criterion ids of every blocking ValidationError.
    """
    from alphamind.decision.portfolio_manager.validation import ValidationError as PMValError
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_rejection,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    envelope = _make_analyst_envelope(envelope_id=EnvelopeId("ENV-REC-2"))
    errors = (
        PMValError(
            field_path="commands[0]",
            message="halt_mode forbids OPEN commands",
            criterion="halt_mode_invariant",
        ),
    )

    ctx, handle = await _open_handle(factory)
    await persist_envelope_rejection(
        handle, envelope, errors, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == EventType.ENVELOPE_REJECTED.value
    detail = json.loads(row.detail_json)
    assert detail["envelope_id"] == "ENV-REC-2"
    assert detail["referenced_position_id"] is None
    assert detail["attempted_command_count"] == len(envelope.commands)
    assert detail["blocking_criteria"] == ["halt_mode_invariant"]
    errors_list = json.loads(detail["validation_errors_json"])
    assert errors_list[0]["message"] == "halt_mode forbids OPEN commands"
    assert errors_list[0]["criterion"] == "halt_mode_invariant"


async def test_persist_envelope_rejection_nullifies_orphan_position_id_on_fk_schema(
    tmp_path: Path,
) -> None:
    """A strategist envelope referencing an unknown ``position_id`` rejects
    cleanly without rolling back the surrounding invocation transaction.

    The FK on ``activity_log.position_id`` (DEFERRABLE INITIALLY DEFERRED) is
    validated at COMMIT — if ``persist_envelope_rejection`` were to forward
    the envelope's orphan position_id into the activity_log row verbatim, the
    invocation commit would raise ``IntegrityError`` and lose the entire
    rejection record (along with any earlier work in the same handle).

    Contract: the activity_log row commits with ``position_id IS NULL`` and
    the orphan envelope position_id preserved in the JSON detail (via
    ``referenced_position_id``) so operator forensics still get full context.
    """
    from argparse import Namespace

    from alembic import command
    from alembic.config import Config

    from alphamind.decision.portfolio_manager.validation import ValidationError as PMValError
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_rejection,
    )

    db_path = tmp_path / "alphamind.db"
    repo_root = Path(__file__).parents[3]
    cfg = Config(repo_root / "alembic.ini", cmd_opts=Namespace(x=[f"db={db_path}"]))
    command.upgrade(cfg, "head")

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        await _seed_invocation_substrate(factory)

        envelope = _make_strategist_envelope(
            envelope_id=EnvelopeId("ENV-SA-99"), position_id=PositionId("POS-NONEXISTENT")
        )
        errors = (
            PMValError(
                field_path="position_id",
                message=(
                    "position_id='POS-NONEXISTENT' does not match any open "
                    "position in pm_view.positions"
                ),
                criterion="position_id_resolves",
            ),
        )

        ctx, handle = await _open_handle(factory)
        await persist_envelope_rejection(
            handle, envelope, errors, config=_make_state_persistence_config()
        )
        # Commit must succeed — the FK at COMMIT must not reject the row.
        await ctx.__aexit__(None, None, None)

        rows = await _read_activity_log_for(factory, handle.invocation_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == EventType.ENVELOPE_REJECTED.value
        assert row.position_id is None  # nullified to satisfy the FK
        detail = json.loads(row.detail_json)
        # Envelope context preserved for operator forensics.
        assert detail["envelope_id"] == "ENV-SA-99"
        assert detail["referenced_position_id"] == "POS-NONEXISTENT"
        assert detail["blocking_criteria"] == ["position_id_resolves"]
    finally:
        await async_engine.dispose()


async def test_handle_submit_envelope_writes_envelope_rejection_on_layer23_failure(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When the engine-stub passes Layer-1 but fails Layer-2/3, both surfaces
    populate: in-memory submission_log AND SQL envelope_rejected entry."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)

    state = build_initial_submit_envelope_state(
        invocation_id=_INV_ID,
        starting_validation_state=_minimal_validation_state(),
    )

    envelope = _make_analyst_envelope()
    bundle = _bundle_with_recommendation("REC-1")

    ctx, handle = await _open_handle(factory)
    response, state = await _handle_submit_envelope(
        envelope.model_dump(mode="json"),
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=bundle,
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=True,  # forces Layer-2/3 failure on the OPEN command
        sector_resolver=lambda _: "semis",
        state_persistence_config=_make_state_persistence_config(),
        invocation_handle=handle,
    )
    await ctx.__aexit__(None, None, None)

    payload = json.loads(response["content"][0]["text"])
    assert payload["submission_results"][0]["status"] == "rejected"
    # Both surfaces populated: in-memory log AND SQL writeback.
    assert len(state.submission_log) == 1
    assert state.submission_log[0].envelope.envelope_id == envelope.envelope_id

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ENVELOPE_REJECTED.value in types


# ===========================================================================
# Tests — persist_envelope_outcome
# ===========================================================================


async def test_open_command_writes_position_thesis_bracket_orders_and_events(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """OPEN command writeback: position (PENDING) + thesis (ACTIVE) + bracket
    (PENDING_ENTRY) + entry order + protective leg orders, with capital
    reserved and the documented activity-log entries emitted."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # A non-marketable LIMIT entry is the case that reserves capital (ALP-741):
    # OPEN now reserves the order notional (limit * quantity), so a market entry
    # would reserve nothing. 1000 * 10 = $10k reserved.
    envelope = _make_analyst_envelope(
        commands=(
            _open_command(
                underlying=Symbol("NVDA"),
                entry_order=EntryOrder(type="limit", limit_price=price(1000.0), stop_price=None),
            ),
        )
    )
    results = (
        _accepted_result(
            command_ordinal=0,
            command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0",
        ),
    )

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    # Position row written: status PENDING, ticker NVDA.
    async with factory() as sess:
        positions = (await sess.execute(select(PositionRow))).scalars().all()
        assert len(positions) == 1
        assert positions[0].status == PositionStatus.PENDING.value

        # Thesis row written: status ACTIVE.
        theses = (await sess.execute(select(ThesisRow))).scalars().all()
        assert len(theses) == 1
        assert theses[0].status == ThesisRecordStatus.ACTIVE.value

        # Bracket row written: status PENDING_ENTRY.
        brackets = (await sess.execute(select(BracketRow))).scalars().all()
        assert len(brackets) == 1
        assert brackets[0].status == BracketStatus.PENDING_ENTRY.value

        # At least one entry order + one protective leg order (status PENDING).
        orders = (await sess.execute(select(OrderRow))).scalars().all()
        assert len(orders) >= 2
        statuses = {o.status for o in orders}
        assert statuses == {"PENDING"}

        # Cash ledger reservation reflects the OPEN.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.reserved_capital_usd > 0.0

    # Activity log entries: order_submitted, thesis_created, capital_reserved,
    # pm_decision.
    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_SUBMITTED.value in types
    assert EventType.THESIS_CREATED.value in types
    assert EventType.CAPITAL_RESERVED.value in types
    assert EventType.PM_DECISION.value in types


async def test_open_command_threads_entry_window_deadline_onto_bracket(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """OPEN writeback threads ``command.entry_window.deadline`` onto the
    persisted bracket's ``entry_window_deadline`` (ALP-737 AC1).

    Without an ``entry_window`` the bracket keeps ``entry_window_deadline=None``
    (covered by the sibling OPEN tests); when the analyst's window rides through
    the OPEN command, the deadline must round-trip analyst → OpenCommand →
    writeback → DB so the continuous monitor can enforce it.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    deadline = datetime(2026, 5, 29, 17, 30, tzinfo=UTC)
    command = _open_command(
        underlying=Symbol("NVDA"),
        entry_window=EntryWindow(
            deadline=deadline,
            decay_type="gradual",
            rationale="overnight gap retest window",
        ),
    )
    envelope = _make_analyst_envelope(commands=(command,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        brackets = (await sess.execute(select(BracketRow))).scalars().all()
        assert len(brackets) == 1
        # Stored as ISO-8601 text; the codec round-trips it back to a tz-aware
        # datetime equal to the analyst's deadline.
        assert brackets[0].entry_window_deadline is not None
        assert datetime.fromisoformat(brackets[0].entry_window_deadline) == deadline


async def test_open_command_persists_real_position_size_and_capital_reservation(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """OPEN writeback reads ``command.position_size`` end-to-end:

    * the entry order's quantity equals ``command.position_size.quantity``
    * the cash ledger reserves the entry order's NOTIONAL (``limit_price *
      quantity``), NOT ``command.position_size.dollar_value`` (ALP-741) — the
      basis the reprice / cancel / fill release paths use, so the reservation
      lifecycle conserves and ``reserved_capital_usd`` never drifts negative.
    * the capital_reserved activity-log detail records the same notional.

    A limit entry at $1050 over 10 shares reserves $10,500, deliberately !=
    the command's $10,000 ``dollar_value`` to prove the basis changed.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    # quantity=10.0, dollar_value=10_000.0, limit=1050 → notional 10_500 != dollar_value.
    cmd = _open_command(
        underlying=Symbol("NVDA"),
        entry_order=EntryOrder(type="limit", limit_price=price(1050.0), stop_price=None),
    )
    expected_notional = Decimal("1050.0") * Decimal("10.0")
    envelope = _make_analyst_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        # Entry order quantity is the command's position_size.quantity, not 1.0.
        entry_orders = (
            (await sess.execute(select(OrderRow).where(OrderRow.order_role == "ENTRY")))
            .scalars()
            .all()
        )
        assert len(entry_orders) == 1
        assert entry_orders[0].quantity == pytest.approx(cmd.position_size.quantity)

        # Capital reservation is the entry NOTIONAL (limit * quantity), not dollar_value.
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.reserved_capital_usd == expected_notional
        assert cash_row.reserved_capital_usd != Decimal(str(cmd.position_size.dollar_value))

    # capital_reserved activity-log detail matches the reserved notional.
    rows = await _read_activity_log_for(factory, handle.invocation_id)
    capital_rows = [r for r in rows if r.event_type == EventType.CAPITAL_RESERVED.value]
    assert len(capital_rows) == 1
    detail = json.loads(capital_rows[0].detail_json)
    # ALP-463: ``amount_usd`` is stored as the Decimal-exact string repr.
    assert Decimal(detail["amount_usd"]) == expected_notional


async def test_persist_envelope_outcome_stamps_phase2_completion_on_invocation_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An accepted envelope's writeback must set the bound invocation row's
    phase2_completed_at as the final step of the open transaction so observers
    can distinguish "Phase 2 in flight" from "Phase 2 committed".
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    envelope = _make_analyst_envelope(commands=(_open_command(underlying=Symbol("NVDA")),))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        row = await sess.get(InvocationRow, invocation_id)
        assert row is not None
        assert row.phase2_completed_at is not None
        parsed = datetime.fromisoformat(row.phase2_completed_at)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)


async def test_persist_envelope_parse_failure_does_not_stamp_phase2_completion(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A Layer-1 parse failure is NOT a Phase 2 commit — the audit-trail
    activity-log entry persists but phase2_completed_at must remain NULL so
    observers can tell rejection apart from a real Phase 2 commit.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_parse_failure,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    failed_entry = FailedSubmissionEntry(
        raw_args={"envelope_id": "ENV-REC-99", "garbage": "value"},
        validation_error_repr="source_provenance: Field required",
        command_id="inv-2026-05-08T12:00:00Z-aaaa.ENV-REC-99.0.0",
    )

    ctx, handle = await _open_handle(factory)
    invocation_id = handle.invocation_id
    await persist_envelope_parse_failure(
        handle, failed_entry, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        row = await sess.get(InvocationRow, invocation_id)
        assert row is not None
        assert row.phase2_completed_at is None


async def _seed_pending_protective_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str,
    bracket_id: str,
    position_id: str,
    thesis_id: str | None,
) -> None:
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
        record_to_row,
    )

    rec = OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
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
        originating_thesis_id=ThesisId(thesis_id) if thesis_id is not None else None,
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    async with factory() as sess:
        sess.add(record_to_row(rec))
        await sess.commit()


async def _seed_pending_entry_order(
    factory: async_sessionmaker[AsyncSession],
    *,
    order_id: str = "ord-entry-1",
    bracket_id: str = "BRK-NVDA-1",
    position_id: str = "POS-NVDA-001",
    thesis_id: str | None = "THE-NVDA-1",
) -> None:
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
        record_to_row,
    )

    rec = OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(),
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
        originating_thesis_id=ThesisId(thesis_id) if thesis_id is not None else None,
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    async with factory() as sess:
        sess.add(record_to_row(rec))
        await sess.commit()


async def test_close_command_writes_close_order_and_emits_order_submitted(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CLOSE command writeback: insert one PENDING close order; emit
    order_submitted + pm_decision. Position closure happens in Phase 1."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(
        commands=(_close_command(position_id=PositionId("POS-NVDA-001")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        orders = (await sess.execute(select(OrderRow))).scalars().all()
        close_orders = [o for o in orders if o.order_role == "CLOSE"]
        assert len(close_orders) == 1
        assert close_orders[0].status == "PENDING"
        assert close_orders[0].position_id == "POS-NVDA-001"

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_SUBMITTED.value in types
    assert EventType.PM_DECISION.value in types


async def test_close_command_surfaces_rationale_metadata_on_order_submitted(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CLOSE writeback threads ``close_rationale_type``, ``invalidation_reason``,
    ``risk_management_subtype``, and the requested quantity through to the
    ``order_submitted`` activity-log detail so post-fill thesis resolution can
    classify without re-fetching the command."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    cmd = _close_command(position_id=PositionId("POS-NVDA-001"))
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    submitted_rows = [r for r in rows if r.event_type == EventType.ORDER_SUBMITTED.value]
    assert len(submitted_rows) == 1
    detail = json.loads(submitted_rows[0].detail_json)
    params = detail["order_parameters_json"]
    assert params["close_rationale_type"] == cmd.close_rationale_type
    assert params["invalidation_reason"] == cmd.invalidation_reason
    assert params["risk_management_subtype"] == cmd.risk_management_subtype
    assert params["requested_quantity"] == "all"


async def test_close_command_with_partial_quantity_uses_command_quantity(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A partial CLOSE with ``quantity=3.0`` produces a close order of qty=3.0."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    partial_close = CloseCommand(
        command_type="close",
        position_id=PositionId("POS-NVDA-001"),
        quantity=3.0,
        order_type="market",
        limit_price=None,
        close_rationale_type="conviction_reduced",
        invalidation_reason=None,
        risk_management_subtype=None,
    )
    envelope = _make_strategist_envelope(commands=(partial_close,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        close_orders = (
            (await sess.execute(select(OrderRow).where(OrderRow.order_role == "CLOSE")))
            .scalars()
            .all()
        )
        assert len(close_orders) == 1
        assert close_orders[0].quantity == pytest.approx(3.0)


async def test_close_all_against_pending_position_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CLOSE-all against a PENDING (zero-fill) position must raise rather than
    fabricate a phantom 1-share close order — closing-before-fill is a
    structural contract violation per oms-commands.md § Command origins."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    # PENDING position has zero fills; share_count=0 so close-all → close_qty=0.
    pending_position = PositionRecord(
        position_id=PositionId("POS-NVDA-001"),
        thesis_id=ThesisId("THE-NVDA-1"),
        bracket_id=BracketId("BRK-NVDA-1"),
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=EquityPositionDetails(
            ticker=Symbol("NVDA"),
            share_count=0.0,
            average_cost_basis_per_share=0.0,
        ),
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    await _seed_position_cluster(factory, pending_position, _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(
        commands=(_close_command(position_id=PositionId("POS-NVDA-001")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError, match="PENDING"):
        await persist_envelope_outcome(
            handle, envelope, results, config=_make_state_persistence_config()
        )
    await ctx.__aexit__(None, None, None)


async def test_adjust_command_dispatches_on_thesis_only(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A thesis-only ADJUST (no stop/target/time/event change-fields) does not
    insert a new protective order; it emits BRACKET_MODIFIED with the
    ``adjustment_rationale`` and one THESIS_COMPONENT_UPDATED per updated
    component."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    cmd = _adjust_command(position_id=PositionId("POS-NVDA-001"), with_stop_level=False)
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = [r.event_type for r in rows]
    assert EventType.BRACKET_MODIFIED.value in types
    assert EventType.THESIS_COMPONENT_UPDATED.value in types

    # rationale comes from command.adjustment_rationale.
    bracket_mod_rows = [r for r in rows if r.event_type == EventType.BRACKET_MODIFIED.value]
    detail = json.loads(bracket_mod_rows[0].detail_json)
    assert detail["rationale"] == cmd.adjustment_rationale


async def test_reserve_capital_decimal_arithmetic_preserves_precision(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Seven ``money("0.1")`` debits on the cash row land at exactly
    ``Decimal("-0.7")``.

    The float-era ``max(... - ..., 0.0)`` floor at line 1349 of phase2.py was
    masking binary-rounding drift on the buying-power source of truth. With
    Decimal arithmetic on a ``Numeric``-backed ``cash_row.reserved_capital_usd``
    column, subtraction is exact and the floor is no longer needed. This test
    pins the invariant directly against the migrated accumulator: seven 0.1
    debits accumulate to -0.7 exactly, not the ``-0.7000000000000001`` a float
    accumulator would produce. The test mutates the row directly via the same
    ``signed_money`` cast ``_release_capital`` performs, bypassing the
    ``CAPITAL_RELEASED`` activity-log emission so the precision check stays
    independent of the FK-anchored event row. (ALP-462.)
    """
    from decimal import Decimal

    from alphamind._kernel.money import money, signed_money

    _, factory = db
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=0.0)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        debit = money("0.1")
        for _ in range(7):
            cash_row.reserved_capital_usd = signed_money(cash_row.reserved_capital_usd - debit)
        await sess.commit()

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        # Decimal-exact equality, not pytest.approx; the whole point of this
        # test is that float drift cannot survive the migration.
        assert cash_row.reserved_capital_usd == Decimal("-0.7")


async def test_cancel_command_releases_capital_from_order_notional(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CANCEL of a LIMIT entry releases capital based on the order's
    limit_price x remaining_quantity, not a hardcoded stub value."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Seed 5000 reserved capital; the LIMIT entry at $50 x 100 shares = $5000.
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=5_000.0)
    entry_rec = OrderRecord(
        order_id=OrderId("ord-entry-bigsize"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price("50.0")),
        quantity=100.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-entry-bigsize"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-entry-bigsize"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=100.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _active_bracket(),
        entry_rec,
    )

    envelope = _make_strategist_envelope(
        commands=(_cancel_command(order_id=OrderId("ord-entry-bigsize")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        # 5000 reserved - 5000 released = 0 remaining.
        assert cash_row.reserved_capital_usd == pytest.approx(0.0)


async def test_cancel_command_on_protective_leg_does_not_release_capital(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CANCEL of a protective leg (PRICE_STOP / TAKE_PROFIT / TIME_STOP) must
    NOT release capital — those legs never reserved any (only the entry order
    reserves at OPEN time). Releasing for a protective leg would shift the
    cash ledger off by the leg's notional for the remainder of the cell's
    life; the ``max(... - amount_usd, 0.0)`` floor in ``_release_capital``
    would mask the symptom.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    # Seed the entry's reservation (1000 USD) so a phantom release would be
    # observable in cash_ledger.reserved_capital_usd.
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    # Protective leg: PRICE_STOP with stop_trigger=140 x 10 shares = 1400 USD
    # notional. If the writeback wrongly called _release_capital with this
    # amount, reserved_capital_usd would clamp to 0 (1000 - 1400 floored at
    # 0); we'd lose the 1000 USD entry reservation invisibly.
    stop_leg_rec = OrderRecord(
        order_id=OrderId("ord-protective-stop"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-protective-stop"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-protective-stop"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _active_bracket(),
        stop_leg_rec,
    )

    envelope = _make_strategist_envelope(
        commands=(_cancel_command(order_id=OrderId("ord-protective-stop")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        # Entry's reservation must remain intact — protective-leg CANCEL is a
        # no-op for capital accounting.
        assert cash_row.reserved_capital_usd == pytest.approx(1_000.0)
        # The leg row itself transitioned to CANCELLED.
        leg = await sess.get(OrderRow, "ord-protective-stop")
        assert leg is not None
        assert leg.status == "CANCELLED"

    # No capital_released entry should have been emitted for the protective leg.
    rows = await _read_activity_log_for(factory, handle.invocation_id)
    cap_released = [r for r in rows if r.event_type == EventType.CAPITAL_RELEASED.value]
    assert cap_released == []


async def test_add_command_persists_real_quantity_and_dollar_value(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADD writeback reads ``additional_quantity`` and reserves the add-entry
    order's NOTIONAL (``limit_price * additional_quantity``), NOT
    ``additional_dollar_value`` (ALP-741) — symmetric with OPEN and the release
    paths. A limit add at $1100 over 5 shares reserves $5,500, deliberately !=
    the command's $5,000 ``additional_dollar_value`` to prove the basis."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    cmd = _add_command(
        position_id=PositionId("POS-NVDA-001"),
        entry_order=EntryOrder(type="limit", limit_price=price(1100.0), stop_price=None),
    )
    expected_notional = Decimal("1100.0") * Decimal("5.0")
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        add_orders = (
            (await sess.execute(select(OrderRow).where(OrderRow.order_role == "ADD_ENTRY")))
            .scalars()
            .all()
        )
        assert len(add_orders) == 1
        assert add_orders[0].quantity == pytest.approx(cmd.additional_quantity)

        cash_row = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash_row is not None
        assert cash_row.reserved_capital_usd == expected_notional
        assert cash_row.reserved_capital_usd != Decimal(str(cmd.additional_dollar_value))


async def test_open_command_persists_target_and_invalidation_legs(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """OPEN writeback constructs one bracket leg per ``command.invalidation_legs``
    plus a TAKE_PROFIT leg from ``command.target``. Each price/time leg has an
    associated PENDING broker order; event legs have order_id=None."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    envelope = _make_analyst_envelope(commands=(_open_command(underlying=Symbol("NVDA")),))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        leg_rows = (await sess.execute(select(BracketLegRow))).scalars().all()
        leg_types = {r.leg_type for r in leg_rows}
        # _open_command sets one PriceLeg (PRICE_STOP) plus a price target ⇒
        # bracket has TAKE_PROFIT + PRICE_STOP legs.
        assert "TAKE_PROFIT" in leg_types
        assert "PRICE_STOP" in leg_types


async def test_adjust_command_cancels_old_protective_order_and_submits_new(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADJUST command writeback: existing PENDING protective order goes
    CANCELLED, a new PENDING protective order is inserted, and the bracket's
    modification history is appended to. Activity log carries order_cancelled
    + order_submitted + bracket_modified + pm_decision."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    old_stop = OrderRecord(
        order_id=OrderId("ord-old-stop"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-old-stop"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-old-stop"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory, _open_position(), _active_thesis(), _active_bracket(), old_stop
    )

    envelope = _make_strategist_envelope(
        commands=(_adjust_command(position_id=PositionId("POS-NVDA-001")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        old_order = await sess.get(OrderRow, "ord-old-stop")
        assert old_order is not None
        assert old_order.status == "CANCELLED"

        # Filter for the newly submitted PRICE_STOP order (excluding the old stop
        # and any stub orders seeded for FK satisfaction, which use ENTRY role).
        new_protective = (
            (
                await sess.execute(
                    select(OrderRow).where(
                        OrderRow.order_id != "ord-old-stop",
                        OrderRow.bracket_id == "BRK-NVDA-1",
                        OrderRow.order_role == "PRICE_STOP",
                        OrderRow.status == "PENDING",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(new_protective) == 1
        assert new_protective[0].status == "PENDING"

        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        history = json.loads(bracket.modification_history_json)
        assert len(history) == 1
        assert history[0]["new_value"] == new_protective[0].order_id

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_CANCELLED.value in types
    assert EventType.ORDER_SUBMITTED.value in types
    assert EventType.BRACKET_MODIFIED.value in types
    assert EventType.PM_DECISION.value in types


async def test_adjust_stop_only_leaves_take_profit_leg_pending(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Stop-only ADJUST must CANCEL only the PRICE_STOP leg, leaving the
    coexisting TAKE_PROFIT leg PENDING.

    Regression: the writeback previously cancelled every PENDING protective
    leg on the bracket whenever an ADJUST landed, but the broker dispatcher
    only PATCHes the single targeted leg's alpaca_order_id. The result was an
    OMS-vs-broker drift where the OMS believed the take-profit leg was
    cancelled while the broker still held it live.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)

    def _build(order_id: str, role: OrderRole, params: PriceParameters) -> OrderRecord:
        return OrderRecord(
            order_id=OrderId(order_id),
            position_id=PositionId("POS-NVDA-001"),
            bracket_id=BracketId("BRK-NVDA-1"),
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
            originating_thesis_id=ThesisId("THE-NVDA-1"),
            originating_pm_command_id=None,
            age_hours=1.0,
        )

    old_stop = _build(
        "ord-old-stop", OrderRole.PRICE_STOP, PriceParameters(stop_trigger_price=price("140.0"))
    )
    old_target = _build(
        "ord-old-target", OrderRole.TAKE_PROFIT, PriceParameters(limit_price=price("200.0"))
    )
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _active_bracket(),
        old_stop,
        old_target,
    )

    envelope = _make_strategist_envelope(
        commands=(_adjust_command(position_id=PositionId("POS-NVDA-001")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cancelled_stop = await sess.get(OrderRow, "ord-old-stop")
        assert cancelled_stop is not None
        assert cancelled_stop.status == "CANCELLED"

        # The take-profit leg must remain PENDING — the ADJUST targeted only
        # the stop, so the broker dispatcher PATCHed only the stop leg and the
        # OMS state must mirror that.
        untouched_target = await sess.get(OrderRow, "ord-old-target")
        assert untouched_target is not None
        assert untouched_target.status == "PENDING"

        new_protective = (
            (
                await sess.execute(
                    select(OrderRow).where(
                        OrderRow.order_id.notin_(("ord-old-stop", "ord-old-target")),
                        OrderRow.bracket_id == "BRK-NVDA-1",
                        OrderRow.order_role == "PRICE_STOP",
                        OrderRow.status == "PENDING",
                    )
                )
            )
            .scalars()
            .all()
        )
        # Exactly one new PRICE_STOP replacement; no second TAKE_PROFIT
        # replacement was inserted.
        assert len(new_protective) == 1
        all_take_profit = (
            (
                await sess.execute(
                    select(OrderRow).where(
                        OrderRow.bracket_id == "BRK-NVDA-1",
                        OrderRow.order_role == "TAKE_PROFIT",
                    )
                )
            )
            .scalars()
            .all()
        )
        # The single pre-existing TAKE_PROFIT leg is the only one — no
        # spurious replacement was created from the stop-only ADJUST.
        assert len(all_take_profit) == 1
        assert all_take_profit[0].order_id == "ord-old-target"


async def test_adjust_stop_repersists_price_stop_leg_trigger(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A stop-level ADJUST re-persists the bracket's PRICE_STOP leg so the
    continuous-monitor watcher evaluates the new threshold, not the stale
    OPEN-time one (ALP-613). The LTE/GTE side is preserved — an ADJUST moves
    a leg's level, not its direction."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(
        commands=(_adjust_command(position_id=PositionId("POS-NVDA-001")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)
    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    leg_rows = await _read_leg_rows(factory, leg_type="PRICE_STOP")
    assert len(leg_rows) == 1
    trigger = json.loads(leg_rows[0].trigger_payload_json)
    # _adjust_command sets new_stop_level trigger_price=145.0; _active_bracket
    # seeds the PRICE_STOP leg at the OPEN-time 140.0.
    assert trigger["threshold_usd"] == pytest.approx(145.0)
    assert trigger["direction"] == "LTE"


async def test_adjust_target_repersists_equity_take_profit_as_plain_price(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An equity take-profit ADJUST re-persists the TAKE_PROFIT leg with the
    new underlying-price threshold and no pl_anchor — equity / single-option
    targets keep a plain price trigger, mirroring the OPEN path (ALP-613)."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _three_leg_bracket())

    cmd = AdjustCommand(
        command_type="adjust",
        position_id=PositionId("POS-NVDA-001"),
        adjustment_rationale="Raise the take-profit.",
        new_target_level=NewTargetLevel(
            target_type="absolute_price", price=price(980.0), order_type="limit"
        ),
    )
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)
    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    leg_rows = await _read_leg_rows(factory, leg_type="TAKE_PROFIT")
    assert len(leg_rows) == 1
    # _three_leg_bracket seeds the TAKE_PROFIT leg at the OPEN-time 950.0.
    assert json.loads(leg_rows[0].trigger_payload_json)["threshold_usd"] == pytest.approx(980.0)
    assert leg_rows[0].pl_anchor_json is None


async def test_adjust_target_repersists_strategy_take_profit_as_pl_anchored(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A strategy position's bracket take-profit modified via ADJUST with a
    pl_percentage NewTargetLevel produces a P/L-anchored replacement leg the
    strategy net-P/L evaluator fires on — the modification counterpart of the
    OPEN path's _strategy_target_to_bracket_leg (ALP-613)."""
    from alphamind.execution.continuous_monitor.bracket_stops.triggers import (
        evaluate_strategy_pl_target_trigger,
    )
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    # OPEN-time anchor captures 60% of max profit; the ADJUST drops it to 50%.
    await _seed_position_cluster(
        factory,
        _strategy_position(),
        _active_thesis(),
        _three_leg_bracket(
            target_pl_anchor=PLAnchorSpec(spec_type="target", pct=0.60, planned_entry_price=3.0)
        ),
    )

    cmd = AdjustCommand(
        command_type="adjust",
        position_id=PositionId("POS-NVDA-001"),
        adjustment_rationale="Lower the strategy take-profit to 50% of max profit.",
        new_target_level=NewTargetLevel(
            target_type="pl_percentage",
            pl_percentage=50.0,
            price=price(3.0),
            order_type="limit",
        ),
    )
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)
    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    leg_rows = await _read_leg_rows(factory, leg_type="TAKE_PROFIT")
    assert len(leg_rows) == 1
    leg = row_to_leg(leg_rows[0])

    assert leg.pl_anchor is not None
    assert leg.pl_anchor.spec_type == "target"
    # pl_percentage=50 → 0.50 fraction; the OPEN-time 0.60 anchor is gone.
    assert leg.pl_anchor.pct == pytest.approx(0.50)
    # The strategy net-P/L evaluator fires on the re-persisted leg: at a spot
    # far above both put strikes the credit spread has captured ~the full
    # credit, clearing the 50%-of-max-profit target.
    fired = evaluate_strategy_pl_target_trigger(
        position=_strategy_position(),
        leg=leg,
        spot=1000.0,
        risk_free_rate=0.045,
        as_of=_NOW,
    )
    assert fired is True


async def test_adjust_rejects_non_pl_percentage_target_on_strategy_position(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An ADJUST replacement take-profit with a non-pl_percentage target on a
    strategy position is rejected with a clear error. AdjustCommand carries no
    instrument, so the strategy-ness is read from the persisted position — the
    write-path counterpart of OpenCommand's ALP-611 model validator."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(
        factory,
        _strategy_position(),
        _active_thesis(),
        _three_leg_bracket(
            target_pl_anchor=PLAnchorSpec(spec_type="target", pct=0.60, planned_entry_price=3.0)
        ),
    )

    cmd = AdjustCommand(
        command_type="adjust",
        position_id=PositionId("POS-NVDA-001"),
        adjustment_rationale="Set an absolute-price take-profit.",
        new_target_level=NewTargetLevel(
            target_type="absolute_price", price=price(890.0), order_type="limit"
        ),
    )
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    with pytest.raises(ValueError, match=r"strategy position requires.+pl_percentage"):
        async with InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=_INV_ID + "-phase2"),
        ) as handle:
            await persist_envelope_outcome(
                handle, envelope, results, config=_make_state_persistence_config()
            )


async def test_adjust_time_expiration_repersists_time_leg_deadline(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A time-expiration ADJUST re-persists the bracket's TIME_EXPIRATION leg
    so the watcher fires on the new deadline, not the stale OPEN-time one
    (ALP-613)."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _three_leg_bracket())

    new_deadline = _NOW + timedelta(hours=48)
    cmd = AdjustCommand(
        command_type="adjust",
        position_id=PositionId("POS-NVDA-001"),
        adjustment_rationale="Extend the time stop.",
        new_time_expiration=new_deadline,
    )
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)
    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    leg_rows = await _read_leg_rows(factory, leg_type="TIME_EXPIRATION")
    assert len(leg_rows) == 1
    trigger = json.loads(leg_rows[0].trigger_payload_json)
    # _three_leg_bracket seeds the time leg at the OPEN-time _NOW + 24h.
    assert datetime.fromisoformat(trigger["deadline"]) == new_deadline


async def test_add_bracket_adjustment_repersists_modified_leg(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADD's optional bracket_adjustment re-persists the modified bracket leg
    exactly as ADJUST does — both route through the shared
    _build_replacement_order_for_change_fields + _apply_protective_leg_modification
    (ALP-613)."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _three_leg_bracket())

    cmd = _add_command(
        position_id=PositionId("POS-NVDA-001"),
        bracket_adjustment=BracketAdjustment(
            new_target_level=NewTargetLevel(
                target_type="absolute_price", price=price(990.0), order_type="limit"
            )
        ),
    )
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)
    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    leg_rows = await _read_leg_rows(factory, leg_type="TAKE_PROFIT")
    assert len(leg_rows) == 1
    # _three_leg_bracket seeds the TAKE_PROFIT leg at the OPEN-time 950.0.
    assert json.loads(leg_rows[0].trigger_payload_json)["threshold_usd"] == pytest.approx(990.0)


async def test_adjust_targeting_absent_leg_type_fails_closed_with_clear_error(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An ADJUST whose change-field targets a leg type the bracket was opened
    without fails closed with a clear error rather than silently mismatching.
    _active_bracket carries only a PRICE_STOP leg, so a new_time_expiration
    ADJUST has no TIME_EXPIRATION leg to re-persist (ALP-613)."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    cmd = AdjustCommand(
        command_type="adjust",
        position_id=PositionId("POS-NVDA-001"),
        adjustment_rationale="Extend a time stop the bracket never had.",
        new_time_expiration=_NOW + timedelta(hours=48),
    )
    envelope = _make_strategist_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    with pytest.raises(ValueError, match=r"opened without a TIME_EXPIRATION leg"):
        async with InvocationContext(
            session_factory=factory,
            record=_make_invocation_record(invocation_id=_INV_ID + "-phase2"),
        ) as handle:
            await persist_envelope_outcome(
                handle, envelope, results, config=_make_state_persistence_config()
            )


async def test_cancel_command_on_entry_dissolves_bracket_and_resolves_thesis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CANCEL of an entry order: target → CANCELLED, all bracket legs →
    CANCELLED, bracket → DISSOLVED, thesis → CANCELLED, capital released.
    Activity log carries order_cancelled + capital_released + thesis_resolved
    + bracket_dissolved + pm_decision."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    # LIMIT order at $100 x 10 shares = $1,000 notional — the CANCEL writeback
    # releases the order's notional (limit_price x remaining_quantity).
    entry_order_rec = OrderRecord(
        order_id=OrderId("ord-entry-1"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price("100.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-entry-1"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-entry-1"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    old_stop_rec = OrderRecord(
        order_id=OrderId("ord-old-stop"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-old-stop"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-old-stop"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _active_bracket(),
        entry_order_rec,
        old_stop_rec,
    )

    envelope = _make_strategist_envelope(
        commands=(_cancel_command(order_id=OrderId("ord-entry-1")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        entry_order = await sess.get(OrderRow, "ord-entry-1")
        assert entry_order is not None
        assert entry_order.status == "CANCELLED"

        # The position was OPEN (its entry already filled), so cancelling a
        # leftover entry-order reference must NOT mark it CANCELLED — that
        # terminal state is reserved for never-filled positions (ALP-744).
        position = await sess.get(PositionRow, "POS-NVDA-001")
        assert position is not None
        assert position.status == PositionStatus.OPEN.value

        leg_order = await sess.get(OrderRow, "ord-old-stop")
        assert leg_order is not None
        assert leg_order.status == "CANCELLED"

        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.DISSOLVED.value

        thesis = await sess.get(ThesisRow, "THE-NVDA-1")
        assert thesis is not None
        assert thesis.status == ThesisRecordStatus.CANCELLED.value
        assert thesis.resolution_timestamp is not None

        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(0.0)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_CANCELLED.value in types
    assert EventType.CAPITAL_RELEASED.value in types
    assert EventType.THESIS_RESOLVED.value in types
    assert EventType.BRACKET_DISSOLVED.value in types
    assert EventType.PM_DECISION.value in types


async def test_persist_entry_window_cancel_dissolves_pending_entry_bracket(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-737 AC2 / verification: ``persist_entry_window_cancel`` — the engine-
    originated cancel the continuous monitor runs when a ``PENDING_ENTRY`` entry
    outlives its ``entry_window_deadline`` — drives the full CANCEL state
    sequence: entry → CANCELLED, all legs → CANCELLED, bracket → DISSOLVED,
    thesis → CANCELLED_NEVER_ENTERED, capital released, with an order_cancelled
    activity-log entry carrying the ``entry_window_expired`` reason. No PM
    command and no pm_decision entry (this is not a PM-originated cancel).
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_entry_window_cancel,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    entry_order_rec = OrderRecord(
        order_id=OrderId("ord-entry-1"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price("100.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-entry-1"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-entry-1"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    stop_order_rec = OrderRecord(
        order_id=OrderId("BRK-NVDA-1-ord-stop"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-BRK-NVDA-1-ord-stop"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-BRK-NVDA-1-ord-stop"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _pending_entry_bracket_with_event_leg(),
        entry_order_rec,
        stop_order_rec,
    )

    ctx, handle = await _open_handle(factory)
    await persist_entry_window_cancel(
        handle,
        entry_order_id="ord-entry-1",
        cancel_reason="entry_window_expired",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        entry_order = await sess.get(OrderRow, "ord-entry-1")
        assert entry_order is not None
        assert entry_order.status == "CANCELLED"

        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.DISSOLVED.value

        thesis = await sess.get(ThesisRow, "THE-NVDA-1")
        assert thesis is not None
        assert thesis.status == ThesisRecordStatus.CANCELLED.value
        assert thesis.resolution_category == "CANCELLED_NEVER_ENTERED"

        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(0.0)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_CANCELLED.value in types
    assert EventType.BRACKET_DISSOLVED.value in types
    assert EventType.THESIS_RESOLVED.value in types
    assert EventType.CAPITAL_RELEASED.value in types
    # Not a PM-originated cancel — no pm_decision entry is written.
    assert EventType.PM_DECISION.value not in types
    # The order_cancelled detail carries the entry-window provenance.
    cancel_rows = [r for r in rows if r.event_type == EventType.ORDER_CANCELLED.value]
    assert cancel_rows
    assert any("entry_window_expired" in r.detail_json for r in cancel_rows)


async def test_cancel_command_marks_never_filled_position_cancelled(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-744: a PM CANCEL of a never-filled entry transitions the PENDING
    position to the terminal CANCELLED state instead of leaving it stranded in
    PENDING forever. The bracket still dissolves as before."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    await _seed_position_cluster(
        factory,
        _pending_never_filled_position(),
        _active_thesis(),
        _active_bracket(),
        _pending_entry_limit_order_rec(),
    )

    envelope = _make_strategist_envelope(
        commands=(_cancel_command(order_id=OrderId("ord-entry-1")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        position = await sess.get(PositionRow, "POS-NVDA-001")
        assert position is not None
        assert position.status == PositionStatus.CANCELLED.value

        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.DISSOLVED.value


async def test_persist_entry_window_cancel_marks_never_filled_position_cancelled(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-744: the ALP-737 entry-window auto-cancel routes through the same
    CANCEL writeback, so a never-filled PENDING position it dissolves must also
    transition to the terminal CANCELLED state."""
    from alphamind.execution.write_paths.phase2 import (
        persist_entry_window_cancel,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    await _seed_position_cluster(
        factory,
        _pending_never_filled_position(),
        _active_thesis(),
        _pending_entry_bracket_with_event_leg(),
        _pending_entry_limit_order_rec(),
    )

    ctx, handle = await _open_handle(factory)
    await persist_entry_window_cancel(
        handle,
        entry_order_id="ord-entry-1",
        cancel_reason="entry_window_expired",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        position = await sess.get(PositionRow, "POS-NVDA-001")
        assert position is not None
        assert position.status == PositionStatus.CANCELLED.value

        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.DISSOLVED.value


def _pending_entry_limit_order_rec(
    *,
    limit_price: str = "100.0",
    quantity: float = 10.0,
    modification_count: int = 0,
    alpaca_order_id: str = "alp-ord-entry-1",
) -> Any:
    """A resting ``PENDING_ENTRY`` LIMIT entry order (ALP-740 reprice target)."""
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

    return OrderRecord(
        order_id=OrderId("ord-entry-1"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price(limit_price)),
        quantity=quantity,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(alpaca_order_id),
        alpaca_order_id_chain=(AlpacaOrderId(alpaca_order_id),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=quantity,
        modification_count=modification_count,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )


async def test_persist_entry_window_reprice_keeps_bracket_pending_and_reconciles(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-740 AC2: the non-terminal reprice writeback moves the entry limit
    toward the market, extends the broker id chain, bumps ``modification_count``,
    and *adjusts* (does not release) reserved capital — while leaving the bracket
    ``PENDING_ENTRY`` and the thesis unresolved (a fill is still being pursued).

    A short entry repriced down from 100 -> 95 over 10 shares frees notional
    ``(95-100)*10 = -50``, so reserved capital drops by 50, not the full 1000 a
    terminal cancel would release.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_entry_window_reprice,
    )
    from alphamind.portfolio_state.records.orders import OrderStatus
    from alphamind.state.tables.orders_codec import (
        row_to_record as order_row_to_record,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _pending_entry_bracket_with_event_leg(),
        _pending_entry_limit_order_rec(limit_price="100.0"),
    )

    ctx, handle = await _open_handle(factory)
    await persist_entry_window_reprice(
        handle,
        entry_order_id="ord-entry-1",
        new_limit_price=price("95.0"),
        new_alpaca_order_id="alpaca-new-uuid",
        reprice_reason="entry_window_reprice",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        entry_row = await sess.get(OrderRow, "ord-entry-1")
        assert entry_row is not None
        # The entry order is repriced in place — same OMS order_id, still PENDING.
        assert entry_row.status == OrderStatus.PENDING.value
        entry = order_row_to_record(entry_row)
        assert entry.price_parameters.limit_price == price("95.0")
        assert entry.alpaca_order_id == "alpaca-new-uuid"
        assert entry.alpaca_order_id_chain == (
            AlpacaOrderId("alp-ord-entry-1"),
            AlpacaOrderId("alpaca-new-uuid"),
        )
        assert entry.modification_count == 1

        # Bracket stays PENDING_ENTRY; thesis is NOT resolved — fill still pursued.
        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.PENDING_ENTRY.value
        thesis = await sess.get(ThesisRow, "THE-NVDA-1")
        assert thesis is not None
        assert thesis.status == ThesisRecordStatus.ACTIVE.value
        assert thesis.resolution_category is None

        # Reservation adjusted by the notional delta (-50), not released in full.
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(950.0)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_MODIFIED.value in types
    assert EventType.CAPITAL_RELEASED.value in types
    # Non-terminal: never dissolves the bracket nor resolves the thesis.
    assert EventType.BRACKET_DISSOLVED.value not in types
    assert EventType.THESIS_RESOLVED.value not in types
    modified_rows = [r for r in rows if r.event_type == EventType.ORDER_MODIFIED.value]
    assert modified_rows
    assert any("entry_window_reprice" in r.detail_json for r in modified_rows)
    assert any("limit_price" in r.detail_json for r in modified_rows)


async def test_persist_entry_window_reprice_raising_limit_reserves_more_capital(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-740: a long entry repriced *up* toward the ask (100 -> 110 over 10
    shares) reserves the extra notional ``(110-100)*10 = +100`` rather than
    releasing — the reservation tracks the live limit in both directions."""
    from alphamind.execution.write_paths.phase2 import (
        persist_entry_window_reprice,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _pending_entry_bracket_with_event_leg(),
        _pending_entry_limit_order_rec(limit_price="100.0"),
    )

    ctx, handle = await _open_handle(factory)
    await persist_entry_window_reprice(
        handle,
        entry_order_id="ord-entry-1",
        new_limit_price=price("110.0"),
        new_alpaca_order_id="alpaca-new-uuid",
        reprice_reason="entry_window_reprice",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(1_100.0)
        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.PENDING_ENTRY.value

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.CAPITAL_RESERVED.value in types
    assert EventType.CAPITAL_RELEASED.value not in types


async def test_open_reprice_cancel_returns_reserved_capital_to_zero(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-741 acceptance — reprice-then-cancel path across a *real* OPEN: the
    reservation lifecycle conserves and ``reserved_capital_usd`` returns to 0,
    never negative.

    A limit entry @ $1000 over 10 shares with PM ``dollar_value=$9000`` reserves
    the order NOTIONAL ($10,000), not ``dollar_value``. The entry-window repricer
    raises the limit ``1000 -> 1050`` (reserved ``10,000 -> 10,500``). The
    terminal cancel then releases the live reserved notional (``1050 * 10 =
    $10,500``) → reserved nets to exactly 0.

    Before ALP-741, OPEN reserved ``dollar_value=$9000`` while the cancel released
    ``1050 * 10 = $10,500`` (after a ``+$500`` reprice on the $9000 base = $9500
    reserved), driving the singleton to ``9500 - 10,500 = -$1000`` — exactly the
    negative ``reserved_capital_usd`` that crashed every decision-pipeline
    invocation via the ``pending_order_capital_pct`` rule's ``_classify_zone``
    negative guard.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_entry_window_cancel,
        persist_entry_window_reprice,
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=0.0)

    cmd = _open_command(
        underlying=Symbol("NVDA"),
        quantity=10.0,
        dollar_value=9_000.0,
        entry_order=EntryOrder(type="limit", limit_price=price(1000.0), stop_price=None),
    )
    envelope = _make_analyst_envelope(commands=(cmd,))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        entry_orders = (
            (await sess.execute(select(OrderRow).where(OrderRow.order_role == "ENTRY")))
            .scalars()
            .all()
        )
        assert len(entry_orders) == 1
        entry_order_id = entry_orders[0].order_id
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        # Reserved is the entry NOTIONAL (1000 * 10), not dollar_value (9000).
        assert cash.reserved_capital_usd == Decimal(10000)
        assert cash.reserved_capital_usd >= 0

    # Reprice the resting entry 1000 -> 1050: reserved 10,000 -> 10,500.
    ctx, handle = await _open_handle(factory, invocation_id=_INV_ID + "-reprice")
    await persist_entry_window_reprice(
        handle,
        entry_order_id=entry_order_id,
        new_limit_price=price("1050.0"),
        new_alpaca_order_id="alp-repriced",
        reprice_reason="entry_window_reprice",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == Decimal(10500)
        assert cash.reserved_capital_usd >= 0

    # Cancel the repriced entry: release 1050 * 10 = 10,500 → reserved nets to 0.
    ctx, handle = await _open_handle(factory, invocation_id=_INV_ID + "-cancel")
    await persist_entry_window_cancel(
        handle,
        entry_order_id=entry_order_id,
        cancel_reason="entry_window_expired",
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd == pytest.approx(0.0)
        assert cash.reserved_capital_usd >= 0


async def test_cancel_entry_dissolve_cancels_all_legs_and_reloads(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-731 regression: CANCELling a never-filled entry must leave every
    ``bracket_legs`` row CANCELLED — including the order-less EVENT_INVALIDATION
    advisory leg the order-cancel sweep can never reach — so the DISSOLVED
    bracket reloads through the read codec (the path
    ``get_brackets_for_positions`` uses) without raising. Before the fix the
    leg rows stayed PENDING_ACTIVATION and every state load raised
    ``ValueError: DISSOLVED bracket requires all legs to have status CANCELLED``.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
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

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0, reserved_capital_usd=1_000.0)
    entry_order_rec = OrderRecord(
        order_id=OrderId("ord-entry-1"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.BUY,
        order_type=OrderType.LIMIT,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(limit_price=price("100.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-ord-entry-1"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-ord-entry-1"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    # The PRICE_STOP leg's protective order — PENDING, so the order sweep
    # cancels it. The EVENT_INVALIDATION leg has no order (order_id=None).
    stop_order_rec = OrderRecord(
        order_id=OrderId("BRK-NVDA-1-ord-stop"),
        position_id=PositionId("POS-NVDA-001"),
        bracket_id=BracketId("BRK-NVDA-1"),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("NVDA")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId("alp-BRK-NVDA-1-ord-stop"),
        alpaca_order_id_chain=(AlpacaOrderId("alp-BRK-NVDA-1-ord-stop"),),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("THE-NVDA-1"),
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory,
        _open_position(),
        _active_thesis(),
        _pending_entry_bracket_with_event_leg(),
        entry_order_rec,
        stop_order_rec,
    )

    envelope = _make_strategist_envelope(
        commands=(_cancel_command(order_id=OrderId("ord-entry-1")),)
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        assert bracket.status == BracketStatus.DISSOLVED.value

        leg_rows = tuple(
            (
                await sess.execute(
                    select(BracketLegRow)
                    .where(BracketLegRow.bracket_id == "BRK-NVDA-1")
                    .order_by(BracketLegRow.leg_index.asc())
                )
            ).scalars()
        )
        # Both legs — the order-backed stop AND the order-less event leg —
        # must be CANCELLED.
        assert {row.leg_status for row in leg_rows} == {BracketLegStatus.CANCELLED.value}
        event_legs = [r for r in leg_rows if r.leg_type == BracketLegType.EVENT_INVALIDATION.value]
        assert len(event_legs) == 1
        assert event_legs[0].order_id is None
        assert event_legs[0].leg_status == BracketLegStatus.CANCELLED.value

        # The DISSOLVED bracket round-trips through the read codec — the exact
        # call ``get_brackets_for_positions`` makes — without raising.
        reloaded = bracket_rows_to_record(bracket, leg_rows)
        assert reloaded.status == BracketStatus.DISSOLVED
        assert all(leg.status == BracketLegStatus.CANCELLED for leg in reloaded.protective_legs)


async def test_assert_bracket_readable_rejects_dissolved_with_non_cancelled_leg(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-731 write-time guard: a DISSOLVED bracket whose legs are not all
    CANCELLED is rejected before it can be relied upon. Seeds the exact
    orders-vs-legs corruption (bracket flipped to DISSOLVED, leg left ACTIVE)
    and asserts the guard raises the same invariant the read path enforces."""
    from alphamind.execution.write_paths.phase2._shared import (
        _assert_bracket_readable,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    # Corrupt: flip only the bracket to DISSOLVED, leaving its single leg
    # ACTIVE — the asymmetry the orders-only cancel path used to commit.
    async with factory() as sess:
        bracket = await sess.get(BracketRow, "BRK-NVDA-1")
        assert bracket is not None
        bracket.status = BracketStatus.DISSOLVED.value
        await sess.commit()

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError, match="DISSOLVED bracket requires all legs"):
        await _assert_bracket_readable(handle, bracket_id="BRK-NVDA-1")
    await ctx.__aexit__(None, None, None)


async def test_add_command_writes_add_entry_order_thesis_component_capital_reservation(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADD command writeback: insert PENDING add-entry order, append a new
    thesis component, reserve capital. Activity log carries order_submitted
    + thesis_component_added + capital_reserved + pm_decision."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    # Limit add reserves its notional (ALP-741); a market add would reserve nothing.
    envelope = _make_strategist_envelope(
        commands=(
            _add_command(
                position_id=PositionId("POS-NVDA-001"),
                entry_order=EntryOrder(type="limit", limit_price=price(1100.0), stop_price=None),
            ),
        )
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    async with factory() as sess:
        add_orders = (
            (await sess.execute(select(OrderRow).where(OrderRow.order_role == "ADD_ENTRY")))
            .scalars()
            .all()
        )
        assert len(add_orders) == 1
        assert add_orders[0].status == "PENDING"
        assert add_orders[0].position_id == "POS-NVDA-001"

        cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
        assert cash is not None
        assert cash.reserved_capital_usd > 0.0

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_SUBMITTED.value in types
    assert EventType.THESIS_COMPONENT_ADDED.value in types
    assert EventType.CAPITAL_RESERVED.value in types
    assert EventType.PM_DECISION.value in types


async def test_multi_command_envelope_emits_single_pm_decision(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A strategist envelope with three commands produces ONE pm_decision
    activity log entry whose detail's resulting_command_ids tuple references
    all three command IDs."""
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(
        commands=(
            _close_command(position_id=PositionId("POS-NVDA-001")),
            _close_command(position_id=PositionId("POS-NVDA-001")),
            _close_command(position_id=PositionId("POS-NVDA-001")),
        )
    )
    results = tuple(
        _accepted_result(command_ordinal=i, command_id=f"inv-{_INV_ID}.ENV-SA-1.{i}.0")
        for i in range(3)
    )

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    pm_rows = [r for r in rows if r.event_type == EventType.PM_DECISION.value]
    assert len(pm_rows) == 1
    detail = json.loads(pm_rows[0].detail_json)
    assert tuple(detail["resulting_command_ids"]) == tuple(r.command_id for r in results)


async def test_persist_envelope_outcome_override_verdict(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An ``override_with_corrective_action`` strategist envelope writes a
    PM_DECISION activity-log entry whose detail.verdict maps to
    ``PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION`` — Wave-2 finding 16.

    Pins the wire-literal → PMVerdict mapping in
    ``alphamind.execution.write_paths.phase2._VERDICT_TO_PM_VERDICT`` so a
    future verdict-set change cannot silently drop the override mapping and
    cause Phase-2 persistence to fall back to the default (or fail).
    """
    from alphamind.commands.pm_envelope import ConcernRecord
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )
    from alphamind.portfolio_state.events.types import PMVerdict

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = PMStrategistEnvelope(
        envelope_id=EnvelopeId("ENV-SA-1"),
        invocation_id=InvocationId(_INV_ID),
        source_provenance="pm_strategist",
        source_recommendation_id=RecommendationId("SA-1"),
        recommendation_type="position_assessment",
        position_id=PositionId("POS-NVDA-001"),
        verdict="override_with_corrective_action",
        evaluation=_all_pass_position_eval(),
        modifications=(),
        concerns=(
            ConcernRecord(
                source="action_status_alignment",
                summary="Hold inappropriate given size breach; coordinated reduce required.",
            ),
        ),
        rationale_narrative="Override the strategist HOLD with a corrective close.",
        anti_patterns_identified=None,
        commands=(_close_command(position_id="POS-NVDA-001"),),
    )
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-SA-1.0.0"),)

    ctx, handle = await _open_handle(factory)
    await persist_envelope_outcome(
        handle, envelope, results, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    pm_rows = [r for r in rows if r.event_type == EventType.PM_DECISION.value]
    assert len(pm_rows) == 1
    detail = json.loads(pm_rows[0].detail_json)
    assert detail["verdict"] == PMVerdict.OVERRIDE_WITH_CORRECTIVE_ACTION.value


async def test_command_abandoned_emission_survives_per_command_rollback(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A failed Phase 2 transaction rolls back its state mutations but a
    follow-up ``persist_command_abandoned`` (in a fresh transaction) writes
    one COMMAND_ABANDONED activity log entry that survives the rollback."""
    from alphamind.execution.write_paths.phase2 import (
        persist_command_abandoned,
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)

    envelope = _make_analyst_envelope(commands=(_open_command(),))
    results = (_accepted_result(command_ordinal=0, command_id=f"inv-{_INV_ID}.ENV-REC-1.0.0"),)

    # Open a transaction, persist outcome, then synthetically raise so the
    # InvocationContext rolls back.
    main_ctx, main_handle = await _open_handle(factory, invocation_id=_INV_ID + "-phase2-fail")
    synthetic = RuntimeError("synthetic Phase 2 failure")

    def _trip_failure() -> None:
        """Helper extracted so the ``raise`` lives outside the try block (TRY301)."""
        raise synthetic

    try:
        await persist_envelope_outcome(
            main_handle,
            envelope,
            results,
            config=_make_state_persistence_config(),
        )
        _trip_failure()
    except RuntimeError:
        await main_ctx.__aexit__(RuntimeError, synthetic, None)

    # No state mutations should have committed.
    async with factory() as sess:
        positions = (await sess.execute(select(PositionRow))).scalars().all()
        assert positions == []

    # Now open a post-rollback transaction and emit COMMAND_ABANDONED.
    abandon_ctx, abandon_handle = await _open_handle(
        factory, invocation_id=_INV_ID + "-phase2-abandon"
    )
    await persist_command_abandoned(
        abandon_handle,
        envelope_id=envelope.envelope_id,
        command_id=results[0].command_id,
        originating_agent="pm_analyst",
        command_type="OPEN",
        failure_reason="gateway_submission_failed",
        retry_attempt_count=3,
    )
    await abandon_ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, abandon_handle.invocation_id)
    abandoned = [r for r in rows if r.event_type == EventType.COMMAND_ABANDONED.value]
    assert len(abandoned) == 1
    detail = json.loads(abandoned[0].detail_json)
    assert detail["envelope_id"] == envelope.envelope_id
    assert detail["command_id"] == results[0].command_id
    assert detail["command_type"] == "OPEN"
    assert detail["failure_reason"] == "gateway_submission_failed"
    assert detail["retry_attempt_count"] == 3


async def test_persist_envelope_parse_failure_without_handle_is_noop(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When the engine-stub wrapper has no InvocationHandle (legacy fixture
    callers), the in-memory failed_submission_log is mutated but no SQL
    write attempt is made — the SQL path is opt-in via the handle injection."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    # Build a SubmitEnvelopeState with no handle wiring.
    state = build_initial_submit_envelope_state(
        invocation_id=_INV_ID,
        starting_validation_state=_minimal_validation_state(),
    )

    # Bogus payload — fails Layer-1 (no source_provenance discriminator).
    bogus_args: dict[str, Any] = {"envelope_id": "ENV-REC-99", "garbage": "value"}
    response, state = await _handle_submit_envelope(
        bogus_args,
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=_minimal_bundle(),
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=False,
        sector_resolver=lambda _: "tech",
        state_persistence_config=_make_state_persistence_config(),
        invocation_handle=None,
    )

    assert "submission_results" in json.loads(response["content"][0]["text"])
    # In-memory log records the failure.
    assert len(state.failed_submission_log) == 1
    # No activity log rows for this invocation.
    rows = await _read_activity_log_for(factory, _INV_ID)
    assert rows == []


async def test_handle_submit_envelope_wires_sql_writeback_on_layer1_failure(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When InvocationHandle is supplied AND Layer-1 parse fails, both the
    in-memory failed_submission_log AND an envelope_parse_failed activity log
    entry are written."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    state = build_initial_submit_envelope_state(
        invocation_id=_INV_ID,
        starting_validation_state=_minimal_validation_state(),
    )

    bogus_args: dict[str, Any] = {"envelope_id": "ENV-REC-99", "garbage": "value"}

    ctx, handle = await _open_handle(factory)
    response, state = await _handle_submit_envelope(
        bogus_args,
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=_minimal_bundle(),
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=False,
        sector_resolver=lambda _: "tech",
        state_persistence_config=_make_state_persistence_config(),
        invocation_handle=handle,
    )
    await ctx.__aexit__(None, None, None)

    assert "submission_results" in json.loads(response["content"][0]["text"])
    assert len(state.failed_submission_log) == 1
    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ENVELOPE_PARSE_FAILED.value in types


async def test_handle_submit_envelope_wires_sql_writeback_on_accepted_envelope(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When InvocationHandle is supplied AND the envelope is accepted, the
    Phase 2 writeback runs alongside the in-memory cumulative-state advance."""
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)

    state = build_initial_submit_envelope_state(
        invocation_id=_INV_ID,
        starting_validation_state=_minimal_validation_state(),
    )

    # Use a small position size so the OPEN passes per-rule guardrails against
    # the minimal validation-state's $100k portfolio + 10% per-position limit.
    envelope = _make_analyst_envelope(
        commands=(_open_command(underlying=Symbol("NVDA"), quantity=1.0, dollar_value=1_000.0),)
    )
    bundle = _bundle_with_recommendation("REC-1")

    ctx, handle = await _open_handle(factory)
    response, state = await _handle_submit_envelope(
        envelope.model_dump(mode="json"),
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=bundle,
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=False,
        sector_resolver=lambda _: "semis",
        state_persistence_config=_make_state_persistence_config(),
        invocation_handle=handle,
    )
    await ctx.__aexit__(None, None, None)

    payload = json.loads(response["content"][0]["text"])
    assert payload["submission_results"][0]["status"] == "accepted"

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.PM_DECISION.value in types
    assert EventType.ORDER_SUBMITTED.value in types
    assert EventType.THESIS_CREATED.value in types
    # No CAPITAL_RESERVED: this OPEN is a market entry, which reserves nothing
    # under the notional reservation basis (ALP-741) — a marketable order needs
    # no pending-order capital reservation. The reserve path is covered by the
    # dedicated limit-entry OPEN tests above.
    assert EventType.CAPITAL_RESERVED.value not in types


# ---------------------------------------------------------------------------
# Minimal fixtures to drive _handle_submit_envelope without the full MCP harness
# ---------------------------------------------------------------------------


def _minimal_validation_state() -> Any:
    from collections.abc import Mapping
    from types import MappingProxyType

    from alphamind._kernel.regime import (
        RegimeLabel,
        RegimeTransitionState,
    )
    from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption
    from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
    from alphamind.risk_guardrails.guardrail_evaluation import (
        ContractType,
        EscalationZones,
        FeatureFlagsView,
        FixtureIvProvider,
        IvQuote,
        IvSurfaceEntry,
        LibraryConfig,
        MarketInputs,
        PortfolioStateSnapshot,
    )
    from alphamind.risk_guardrails.state_delivery.validation_tool import (
        ValidationToolState,
    )

    zones = EscalationZones(warning=70.0, critical=85.0, hard_block=95.0)
    effective_limits: Mapping[str, float] = {
        "position_max_size_pct": 10.0,
        "sector_concentration_pct": 25.0,
        "net_long_pct": 60.0,
        "net_short_pct": 40.0,
        "gross_exposure_pct": 100.0,
        "options_delta_pct": 30.0,
        "portfolio_theta_pct_per_day": 0.5,
        "portfolio_vega_pct_per_iv_point": 1.0,
        "total_short_pct": 30.0,
        "single_short_max_pct": 5.0,
        "borrow_cost_budget_pct_per_day": 0.05,
        "min_cash_reserve_pct": 10.0,
        "pending_order_capital_pct": 20.0,
    }
    cfg = LibraryConfig(
        effective_limits=MappingProxyType(dict(effective_limits)),
        escalation_zones=MappingProxyType({k: zones for k in effective_limits}),
        feature_flags=FeatureFlagsView(options_enabled=True, short_selling_enabled=True),
        active_sectors=("tech", "semis", "financials", "energy"),
        active_regime="normal",
        active_profile="medium",
        conservative_buffer_pct=10.0,
    )
    snap = PortfolioStateSnapshot(
        portfolio_value_usd=100_000.0,
        cash_usd=70_000.0,
        reserved_for_pending_orders_usd=0.0,
        sector_exposure_pct=MappingProxyType(
            {"tech": 5.0, "semis": 5.0, "financials": 0.0, "energy": 0.0}
        ),
        net_long_pct=10.0,
        net_short_pct=0.0,
        gross_pct=10.0,
        options_delta_pct=0.0,
        portfolio_theta_pct_per_day=0.0,
        portfolio_vega_pct_per_iv_point=0.0,
        total_short_pct=0.0,
        single_short_max_pct=0.0,
        daily_borrow_cost_pct=0.0,
        position_max_size_pct=5.0,
        existing_positions=MappingProxyType({}),
    )
    market = MarketInputs(
        underlying_prices=MappingProxyType({"NVDA": 100.0, "AAPL": 100.0}),
        risk_free_rate=0.045,
        iv_provider=FixtureIvProvider(
            surface={
                "NVDA": IvSurfaceEntry(
                    underlying=Symbol("NVDA"),
                    quotes=(
                        IvQuote(
                            strike=100.0,
                            expiration=_NOW.date(),
                            contract_type=ContractType.CALL,
                            implied_volatility=0.30,
                        ),
                    ),
                ),
            },
            realized_vol={},
        ),
        as_of=_NOW,
    )
    return ValidationToolState(
        invocation_id=_INV_ID,
        starting_snapshot=snap,
        starting_risk_budget=RiskBudgetConsumption(entries=()),
        starting_active_risk_parameters=ActiveRiskParameterSet(
            regime_label=RegimeLabel.NORMAL,
            transition_state=RegimeTransitionState.STABLE,
            transition_invocations_remaining=0,
            parameter_change_flag=False,
            entries=(),
            active_overlays=(),
        ),
        profile_feature_flags=cfg.feature_flags,
        library_config=cfg,
        library_market=market,
        sector_resolver=lambda _: "semis",
        accumulated_deltas=(),
    )


def _minimal_retrieval_store() -> Any:
    from alphamind.analysis.synthesizer.models import BriefSource
    from alphamind.analysis.synthesizer.retrieval import RetrievalStore

    return RetrievalStore(entries={}, freshness_by_source={BriefSource.SA_TECH: _NOW})


def _minimal_bundle() -> Any:
    from alphamind.decision.proposal_pre_processor.models import (
        AggregateObservations,
        AnalystSection,
        BasisSection,
        BookHealthSummary,
        ByRecommendedAction,
        ByThesisStatus,
        CombinedSetImpact,
        ConvictionDistribution,
        ConvictionHistogram,
        ProposalPreProcessorBundle,
        StrategistSection,
    )

    histogram = ConvictionHistogram.model_validate({"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
    by_thesis = ByThesisStatus.model_validate(
        {"on-track": 0, "partially-realized": 0, "at-risk": 0, "stale": 0, "invalidated": 0}
    )
    by_action = ByRecommendedAction.model_validate(
        {"hold": 0, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
    )
    return ProposalPreProcessorBundle.model_construct(
        invocation_id=_INV_ID,
        timestamp=_NOW,
        aggregate_observations=AggregateObservations(
            combined_set_impact=CombinedSetImpact(
                basis=BasisSection(
                    analyst_proposal_ids=(),
                    strategist_action_ids=(),
                    strategist_holds_excluded_count=0,
                    snapshot_timestamp=_NOW,
                ),
                per_rule=(),
                breaches=(),
            ),
            conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
            book_health_summary=BookHealthSummary(
                by_thesis_status=by_thesis,
                by_recommended_action=by_action,
                remedy_flagged_count=0,
                total=0,
            ),
        ),
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="normal", recommendations=(), watchlist=None
        ),
    )


def _bundle_with_recommendation(recommendation_id: str) -> Any:
    from alphamind.decision.analyst.models import Recommendation
    from alphamind.decision.proposal_pre_processor.models import (
        AggregateObservations,
        AnalystSection,
        AnalystSideAnnotations,
        BasisSection,
        BookHealthSummary,
        ByRecommendedAction,
        ByThesisStatus,
        CombinedSetImpact,
        ConvictionDistribution,
        ConvictionHistogram,
        ProposalPreProcessorBundle,
        StrategistSection,
        WrappedRecommendation,
    )

    histogram = ConvictionHistogram.model_validate({"1": 0, "2": 0, "3": 0, "4": 0, "5": 0})
    by_thesis = ByThesisStatus.model_validate(
        {"on-track": 0, "partially-realized": 0, "at-risk": 0, "stale": 0, "invalidated": 0}
    )
    by_action = ByRecommendedAction.model_validate(
        {"hold": 0, "reduce": 0, "close": 0, "adjust-bracket": 0, "add": 0}
    )
    rec = Recommendation.model_construct(recommendation_id=recommendation_id)
    return ProposalPreProcessorBundle.model_construct(
        invocation_id=_INV_ID,
        timestamp=_NOW,
        aggregate_observations=AggregateObservations(
            combined_set_impact=CombinedSetImpact(
                basis=BasisSection(
                    analyst_proposal_ids=(),
                    strategist_action_ids=(),
                    strategist_holds_excluded_count=0,
                    snapshot_timestamp=_NOW,
                ),
                per_rule=(),
                breaches=(),
            ),
            conviction_distribution=ConvictionDistribution(by_level=histogram, total=0),
            book_health_summary=BookHealthSummary(
                by_thesis_status=by_thesis,
                by_recommended_action=by_action,
                remedy_flagged_count=0,
                total=0,
            ),
        ),
        strategist_section=StrategistSection.model_construct(
            mode="normal",
            position_assessments=(),
            pending_order_assessments=(),
            portfolio_level_observations=None,
        ),
        analyst_section=AnalystSection.model_construct(
            mode="normal",
            recommendations=(
                WrappedRecommendation.model_construct(
                    recommendation=rec,
                    pre_processor_annotations=AnalystSideAnnotations(conflicts=()),
                ),
            ),
            watchlist=None,
        ),
    )


def _minimal_pm_view() -> Any:

    return _bypass_init_PortfolioManagerView(
        positions=(),
        recent_thesis_resolutions=(),
        portfolio_pnl=None,
        drawdown=None,
        sector_exposure=(),
        directional_exposure=None,
        risk_budget=None,
        active_risk_parameters=None,
        intra_invocation_changelog=(),
        recent_pm_decision_log=(),
        abandoned_openings=(),
        abandoned_actions=(),
        thesis_quality_aggregates=None,
        position_modification_trail={},
    )
