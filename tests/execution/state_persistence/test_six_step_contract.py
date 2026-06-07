"""End-to-end six-step snapshot-isolation contract test (ALP-119 follow-up).

Per ``docs/design/05-execution-layer/state-persistence.md`` § Read paths the
snapshot-isolation contract is::

    1. Fill collection begins a write transaction.
    2. Fill collection commits (fills integrate, ``fill_collection_completed_at`` stamped).
    3. The ingestion layer reads a snapshot.
    4. The ingestion layer completes its read.
    5. Command execution begins a write transaction.
    6. Command execution commits (envelope writebacks land, ``command_execution_completed_at``
       stamped).

This module exercises steps 1, 2, 5, and 6 plus the snapshot reads bracketing
them (steps 3-4 implemented as direct repository reads). Both phases run
under a single ``invocation_id`` against a schema migrated to ``head`` so the
deferred FK constraints participate in every commit. The concurrent
fill-persistence path (the "Throughout steps 1-6, the gateway may
independently persist new fill records" guarantee from the design doc) is
out of surface for this test — it is exercised in
``test_fill_records_table.py``.

Per-step unit tests live in ``test_fill_collection_write_path.py``,
``test_command_execution_write_path.py``, and ``test_sql_repository.py``. This module
runs the full sequence inside a single invocation cycle to guard against
read/write surfaces drifting out of contract once fill collection + command execution share
real state.
"""

from __future__ import annotations

from argparse import Namespace
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
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
from alphamind._kernel.regime import (
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.commands.command_models import (
    BracketOrderParameters,
    EntryOrder,
    EquityInstrument,
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
    CriterionAssessment,
    OpenCommand,
    PMAnalystEnvelope,
    PMEnvelope,
    ThesisQualityEvaluation,
)
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    SubmissionResult,
)
from alphamind.execution.oms.command_ids import (
    derive_open_thesis_id,
    derive_pm_command_id,
)
from alphamind.execution.write_paths.command_execution import (
    persist_envelope_outcome,
)
from alphamind.execution.write_paths.fill_collection import (
    process_unprocessed_fills,
)
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
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
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.context import (
    InvocationHandle,
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
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-six-step"
_PROCESS_ID = "proc-1"


# ---------------------------------------------------------------------------
# Engine + session fixture (mirrors the pattern in the per-phase test files)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, factory) over a fresh DB migrated to ``head``.

    ``alembic upgrade head`` runs the FK-tightening migration so the
    deferred FK constraints participate in every fill collection / command execution commit.
    """
    db_path = tmp_path / "alphamind.db"
    repo_root = Path(__file__).parents[3]
    cfg = Config(repo_root / "alembic.ini", cmd_opts=Namespace(x=[f"db={db_path}"]))
    command.upgrade(cfg, "head")

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Invocation substrate + initial state seed
# ---------------------------------------------------------------------------


def _config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/pip-freeze",
            "invocation_provenance_root": "/tmp/provenance",
        }
    )


def _market_inputs() -> MarketInputs:
    """Minimal ``MarketInputs`` for the six-step contract's fill collection step.

    The wedge in ``process_unprocessed_fills`` (story 06a / ALP-428) requires
    a price for every open-position underlying; this test seeds AAPL and
    NVDA equity positions only, so the IV provider is unused.
    """
    return MarketInputs(
        underlying_prices={"AAPL": 150.0, "NVDA": 500.0},
        risk_free_rate=0.0425,
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        as_of=_NOW,
    )


def _process_lifetime() -> ProcessLifetimeRecord:
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


def _invocation_record(invocation_id: str = _INV_ID) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=_NOW.isoformat().replace("+00:00", "Z"),
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
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
) -> None:
    async with factory() as sess:
        sess.add(process_lifetime_record_to_row(_process_lifetime()))
        await sess.flush()
        sess.add(invocation_record_to_row(_invocation_record()))
        await sess.commit()


def _pending_entry_order(
    *,
    bracket_id: str,
    position_id: str,
    order_id: str = "ord-entry-six",
) -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("thesis-six"),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _pending_position(
    *,
    position_id: str = "pos-six",
    bracket_id: str = "brk-six",
    thesis_id: str = "thesis-six",
) -> PositionRecord:
    details = EquityPositionDetails(
        ticker=Symbol("AAPL"), share_count=0.0, average_cost_basis_per_share=0.0
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


def _pending_bracket(
    *,
    bracket_id: str = "brk-six",
    position_id: str = "pos-six",
) -> BracketRecord:
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
        entry_order_id=OrderId("ord-entry-six"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _active_thesis(
    *,
    thesis_id: str = "thesis-six",
    position_id: str = "pos-six",
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
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary="AAPL momentum",
        key_catalyst="Q3 earnings",
        position_size_rationale="Sized at 5%",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=generation_at,
        time_expectation_hours=24.0,
        age_hours=4.0,
        expected_resolution_at=generation_at + timedelta(hours=24.0),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


def _cash_ledger() -> CashLedger:
    return CashLedger(
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


def _drawdown_state() -> DrawdownState:
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


def _unprocessed_fill() -> FillRecord:
    return FillRecord(
        fill_id="fill-six-1",
        order_id=OrderId("ord-entry-six"),
        fill_timestamp=_NOW - timedelta(minutes=10),
        fill_price=price(150.0),
        fill_quantity=10.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=signed_money(0.0),
        fees_usd=money(0.0),
        execution_venue="NASDAQ",
        gateway_reference="alp-fill-six-1",
        persistence_timestamp=_NOW - timedelta(minutes=9),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


def _pending_protective_stop_order(
    *,
    bracket_id: str,
    position_id: str,
    order_id: str,
) -> OrderRecord:
    """Companion stop-leg order satisfying the bracket-leg→order FK.

    The seeded ``BracketLeg`` carries ``order_id=OrderId(f"{bracket_id}-ord-stop")``;
    on the FK-tightened schema that reference must resolve. The order is
    minimally shaped (PENDING protective stop) — fill collection's fill integration
    only mutates the entry order, so this row sits inert through the test.
    """
    return OrderRecord(
        order_id=OrderId(order_id),
        position_id=PositionId(position_id),
        bracket_id=BracketId(bracket_id),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol("AAPL")),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(stop_trigger_price=price("140.0")),
        quantity=10.0,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId("thesis-six"),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


async def _seed_initial_state(factory: async_sessionmaker[AsyncSession]) -> None:
    """Seed the entities fill collection needs to integrate one entry fill cleanly.

    All cross-referenced rows commit in a single transaction so the
    DEFERRABLE-INITIALLY-DEFERRED FKs (positions↔theses↔brackets↔orders cycles)
    are validated together at COMMIT time.
    """
    await _seed_invocation_substrate(factory)
    async with factory() as sess:
        entry_order = _pending_entry_order(
            bracket_id=BracketId("brk-six"), position_id=PositionId("pos-six")
        )
        stop_order = _pending_protective_stop_order(
            bracket_id=BracketId("brk-six"),
            position_id=PositionId("pos-six"),
            order_id=OrderId("brk-six-ord-stop"),
        )
        sess.add(order_record_to_row(entry_order))
        sess.add(order_record_to_row(stop_order))
        sess.add(position_record_to_row(_pending_position()))
        await sess.flush()
        parent_thesis_row, child_rows = thesis_record_to_rows(_active_thesis())
        sess.add(parent_thesis_row)
        await sess.flush()
        for crow in child_rows:
            sess.add(crow)
        parent_bracket_row, leg_rows = bracket_record_to_rows(_pending_bracket())
        sess.add(parent_bracket_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        sess.add(cash_ledger_record_to_row(_cash_ledger(), last_updated_at=_NOW))
        sess.add(drawdown_state_record_to_row(_drawdown_state(), last_updated_at=_NOW))
        await sess.commit()
    async with factory() as sess:
        await append_fill_record(sess, _unprocessed_fill())
        await sess.commit()


async def _open_handle_for_existing_invocation(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
) -> tuple[AsyncSession, InvocationHandle]:
    """Open a session bound to a pre-existing invocation row.

    Production uses ``InvocationContext`` to insert the invocation row on
    enter — that's correct for one-transaction-per-invocation flows. The
    six-step contract spans two transactions on the same ``invocation_id``
    (fill collection commit, then snapshot read, then command execution commit), so the row
    must be inserted exactly once before either phase opens. The caller
    commits and closes the returned session.
    """
    session = factory()
    return session, InvocationHandle(session=session, invocation_id=invocation_id)


# ---------------------------------------------------------------------------
# Repository builder + envelope construction helpers
# ---------------------------------------------------------------------------


def _active_risk_parameters() -> ActiveRiskParameterSet:
    entry = ActiveRiskParameterEntry(
        rule_id="max_position_size_usd",
        rule_label="Max position size (USD)",
        value=1_000.0,
        unit="USD",
        regime_multiplier_applied=1.0,
        base_value=1_000.0,
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(entry,),
        active_overlays=(),
    )


def _build_repo(
    factory: async_sessionmaker[AsyncSession],
    invocation_id: str,
) -> PortfolioStateRepository:
    arp = _active_risk_parameters()

    def _provider() -> ActiveRiskParameterSet:
        return arp

    def _prior_provider(_: str) -> ActiveRiskParameterSet:
        return arp

    return build_sql_portfolio_state_repository(
        session_factory=factory,
        invocation_id=invocation_id,
        active_risk_parameters_provider=_provider,
        prior_active_risk_parameters_provider=_prior_provider,
        config=_config(),
    )


def _open_envelope(envelope_id: str = "ENV-REC-7") -> PMEnvelope:
    p = CriterionAssessment(status="pass", note=None)
    return PMAnalystEnvelope(
        envelope_id=EnvelopeId(envelope_id),
        invocation_id=InvocationId(_INV_ID),
        source_provenance="pm_analyst",
        source_recommendation_id=RecommendationId("REC-7"),
        recommendation_type="new_entry",
        verdict="approve",
        evaluation=ThesisQualityEvaluation(
            falsifiability=p,
            sizing_proportionality=p,
            portfolio_coherence=p,
            timing_plausibility=p,
            counterargument_consideration=p,
        ),
        modifications=(),
        concerns=(),
        rationale_narrative="OK proposal.",
        anti_patterns_identified=None,
        commands=(
            OpenCommand(
                command_type="open",
                instrument=EquityInstrument(
                    asset_type="equity", ticker=Symbol("NVDA"), direction="long"
                ),
                entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
                position_size=PositionSize(quantity=10.0, dollar_value=money(10_000.0)),
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
                        trigger_signal="underlying_price",
                        condition=PriceCondition(
                            underlying_trigger="NVDA",
                            comparator="<=",
                            trigger_price=price(750.0),
                        ),
                        order_parameters=BracketOrderParameters(
                            order_type="market", limit_price=None
                        ),
                    ),
                ),
                thesis=Thesis(
                    summary="Long NVDA.",
                    nature="directional",
                    components=(
                        OMSThesisComponent(
                            component_type="entry_rationale",
                            linked_leg="entry",
                            instrument_reference="NVDA",
                            narrative="Capex tailwind.",
                            key_assumptions=("Capex stays elevated.",),
                        ),
                    ),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# The contract test
# ---------------------------------------------------------------------------


async def test_six_step_snapshot_isolation_contract(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Fill collection commit → snapshot read → command execution commit → second snapshot read.

    Both phases run under the SAME ``invocation_id`` (the design contract
    binds the entire six-step sequence to one invocation cycle). The schema
    is migrated to ``head`` so the FK-tightening migration's deferred
    constraints are validated at every commit.

    Asserts:
    * The fill-collection snapshot sees the now-OPEN seeded position.
    * The fill-collection snapshot does NOT include command execution's PENDING position
      (because command execution hasn't run yet).
    * The command-execution snapshot picks up the new PENDING position the OPEN
      command introduced.
    """
    _, factory = db
    await _seed_initial_state(factory)

    # ---- Fill collection commit -------------------------------------------
    # _seed_invocation_substrate already inserted the invocation row, so
    # both phases reuse it (the production InvocationContext insert step
    # is moved up-front for tests that span two transactions on one row).
    session, handle = await _open_handle_for_existing_invocation(factory, invocation_id=_INV_ID)
    try:
        summary = await process_unprocessed_fills(
            handle,
            market_inputs=_market_inputs(),
            config=_config(),
        )
        await session.commit()
    finally:
        await session.close()
    assert summary.fills_processed == 1

    # ---- Snapshot read after fill_collection -------------------------------------
    repo = _build_repo(factory, invocation_id=_INV_ID)
    open_after_fill_collection = repo.get_open_positions()
    pending_after_fill_collection = repo.get_pending_positions()
    assert {p.position_id for p in open_after_fill_collection} == {"pos-six"}
    # command_execution hasn't run; no other PENDING positions exist yet.
    assert pending_after_fill_collection == ()

    # ---- Command execution commit -----------------------------------------
    envelope = _open_envelope()
    # The OPEN command id carries the broker-carried thesis link (ALP-844); the
    # thesis is minted off the link-free base id, exactly as PM-submit derives
    # it, and command execution reads it back by parsing (single source of truth).
    base_command_id = f"inv-{_INV_ID.removeprefix('inv-')}.{envelope.envelope_id}.0.0"
    open_command_id = derive_pm_command_id(
        invocation_id=_INV_ID,
        envelope_id=str(envelope.envelope_id),
        command_ordinal=0,
        attempt_seq=0,
        thesis_id=derive_open_thesis_id("NVDA", base_command_id),
    )
    results: tuple[SubmissionResult, ...] = (
        SubmissionResult(
            command_ordinal=0,
            status="accepted",
            command_id=open_command_id,
            acknowledgment=Acknowledgment(),
        ),
    )
    session2, handle2 = await _open_handle_for_existing_invocation(factory, invocation_id=_INV_ID)
    try:
        await persist_envelope_outcome(
            handle2,
            envelope,
            results,
            config=_config(),
            originating_proposal_json={"recommendation_id": "REC-1"},
        )
        await session2.commit()
    finally:
        await session2.close()

    # ---- Snapshot read after command_execution -------------------------------------
    repo_after_cmd_exec = _build_repo(factory, invocation_id=_INV_ID)
    open_after_cmd_exec = repo_after_cmd_exec.get_open_positions()
    pending_after_cmd_exec = repo_after_cmd_exec.get_pending_positions()
    # fill_collection's OPEN position still surfaces.
    assert {p.position_id for p in open_after_cmd_exec} == {"pos-six"}
    # command_execution introduced exactly one new PENDING position (the NVDA OPEN).
    pending_ids = {p.position_id for p in pending_after_cmd_exec}
    assert len(pending_ids) == 1
    new_pending_id = next(iter(pending_ids))
    assert new_pending_id.startswith("POS-NVDA-")
