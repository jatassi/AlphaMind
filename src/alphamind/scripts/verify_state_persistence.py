"""State-persistence end-to-end verification (ALP-367).

Operator entry point (via thin shim at ``scripts/verify_state_persistence.py``).
Exercises the durable state-persistence substrate end-to-end against a freshly-
migrated SQLite DB across six phases (schema, InvocationContext round-trip,
Phase 1 fill integration, Phase 2 accepted-envelope writeback, Phase 2 Layer-1
parse-failure writeback, repository read parity).

Phase C and Phase F call ``process_unprocessed_fills``, which since story
06a / ALP-428 requires a ``MarketInputs`` argument for the per-fill Reg T
margin attribution wedge. The script constructs a fixture ``MarketInputs``
(via :func:`_market_inputs`) covering the AAPL equity position used by
Phase C; the IV provider is unused for equity-only state.

No SDK invocation; no live broker contact. Sub-second runtime against a fresh
on-disk DB.

Usage::

    uv run python scripts/verify_state_persistence.py [--db-path PATH] [--output FORMAT]

See ``scripts/RUNBOOK_state_persistence.md`` for the operator runbook.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
from alphamind._kernel.money import money, price
from alphamind._kernel.regime import RiskZone
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
from alphamind.config.guardrails_helpers import (
    load_cumulative_drawdown_progressive_tiers,
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
    FailedSubmissionEntry,
    SubmissionResult,
)
from alphamind.execution.write_paths.fill_persistence import (
    append_fill_record,
)
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
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.context import (
    InvocationContext,
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
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.drawdown_state_codec import (
    drawdown_state_record_to_row,
)
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.orders_codec import (
    record_to_row as order_record_to_row,
)
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

__all__ = [
    "PhaseResult",
    "main",
    "run_phase_a_schema",
    "run_phase_b_invocation_context",
    "run_phase_c_phase1_write_path",
    "run_phase_d_phase2_envelope",
    "run_phase_e_layer1_parse_failure",
    "run_phase_f_repository_read_parity",
]


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhaseResult:
    """One phase's outcome — pass/fail flag + named label + diagnostic detail."""

    label: str
    ok: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# Shared fixture constants — kept narrow and stable so the verify script's
# diagnostic output is deterministic across runs.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_PROCESS_ID = "verify-state-persistence-proc"
_INV_ID_BASE = "verify-state-persistence-inv"


def _state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/verify-state-persistence",
            "invocation_provenance_root": "/tmp/verify-state-persistence",
        }
    )


def _market_inputs() -> MarketInputs:
    """Fixture ``MarketInputs`` for Phase C / Phase F's ``process_unprocessed_fills`` call.

    Story 06a / ALP-428 added a required ``market_inputs`` argument so the
    Phase 1 wedge can compute per-fill Reg T margin attribution. This script
    seeds only an AAPL equity position, so a single underlying price suffices
    and the IV provider is unused.
    """
    return MarketInputs(
        underlying_prices={_TICKER: 150.0},
        risk_free_rate=0.0425,
        iv_provider=FixtureIvProvider(surface={}, realized_vol={}),
        as_of=_NOW,
    )


def _process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id=_PROCESS_ID,
        process_role="pipeline",
        process_start_at=_NOW.isoformat().replace("+00:00", "Z"),
        process_pid=12345,
        hostname="verify-state-persistence",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/verify-state-persistence/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="verify-script",
    )


def _invocation_record(
    invocation_id: str,
    *,
    start_at: datetime | None = None,
) -> InvocationRecord:
    started = start_at if start_at is not None else _NOW
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_ID,
        start_at=started.isoformat().replace("+00:00", "Z"),
        phase1_completed_at=None,
        phase2_completed_at=None,
        trigger_type="manual",
        trigger_source="verify_state_persistence.py",
        trigger_reason="operator-triggered verification",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path="/tmp/verify-state-persistence/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/verify-state-persistence/calib.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


async def _seed_process_lifetime(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the shared parent process_lifetime row required by the FK on invocations.

    Phases share a single process_lifetime by design — they all run under the
    same simulated operator-triggered verification process — so the seed is
    idempotent: a row already on disk satisfies the FK and the seed is a no-op.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    from alphamind.state.tables.process_lifetimes import (
        ProcessLifetimeRow,
    )

    row = process_lifetime_record_to_row(_process_lifetime_record())
    values = {col.name: getattr(row, col.name) for col in ProcessLifetimeRow.__table__.columns}
    stmt = sqlite_insert(ProcessLifetimeRow).values(**values)
    stmt = stmt.on_conflict_do_nothing(index_elements=["process_lifetime_id"])
    async with factory() as sess:
        await sess.execute(stmt)
        await sess.commit()


def _open_async_factory(
    db_path: Path,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Return (engine, factory) pair the caller is responsible for disposing."""
    engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(engine)
    return engine, factory


# ---------------------------------------------------------------------------
# Phase C fixture builders — minimal Tier 1 entities sufficient to exercise
# the Phase 1 entry-fill happy path through process_unprocessed_fills().
# ---------------------------------------------------------------------------


_TICKER = "AAPL"
_BRACKET_ID = "verify-brk-1"
_POSITION_ID = "verify-pos-1"
_THESIS_ID = "verify-thesis-1"
_ENTRY_ORDER_ID = "verify-ord-entry-1"
_FILL_ID = "verify-fill-1"


def _pending_entry_order() -> OrderRecord:
    return OrderRecord(
        order_id=OrderId(_ENTRY_ORDER_ID),
        position_id=None,
        bracket_id=BracketId(_BRACKET_ID),
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(_TICKER)),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{_ENTRY_ORDER_ID}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{_ENTRY_ORDER_ID}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId(_THESIS_ID),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _pending_protective_stop_order() -> OrderRecord:
    """Stop-loss order linked to the bracket's PRICE_STOP leg.

    Phase 2's OPEN writeback creates protective leg orders alongside the entry
    order, both in PENDING state. The bracket leg's ``order_id`` references the
    protective order; the FK migration enforces that reference at COMMIT.
    """
    stop_order_id = f"{_BRACKET_ID}-ord-stop"
    return OrderRecord(
        order_id=OrderId(stop_order_id),
        position_id=None,
        bracket_id=BracketId(_BRACKET_ID),
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker=Symbol(_TICKER)),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.SIMPLE,
        price_parameters=PriceParameters(stop_trigger_price=140.0),
        quantity=10.0,
        duration=OrderDuration.GTC,
        status=OrderStatus.PENDING,
        alpaca_order_id=AlpacaOrderId(f"alp-{stop_order_id}"),
        alpaca_order_id_chain=(AlpacaOrderId(f"alp-{stop_order_id}"),),
        submission_timestamp=_NOW - timedelta(minutes=15),
        last_update_timestamp=_NOW - timedelta(minutes=15),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=ThesisId(_THESIS_ID),
        originating_pm_command_id=None,
        age_hours=0.25,
    )


def _pending_position() -> PositionRecord:
    return PositionRecord(
        position_id=PositionId(_POSITION_ID),
        thesis_id=ThesisId(_THESIS_ID),
        bracket_id=BracketId(_BRACKET_ID),
        status=PositionStatus.PENDING,
        direction=Direction.LONG,
        entry_timestamp=None,
        details=EquityPositionDetails(
            ticker=Symbol(_TICKER),
            share_count=0.0,
            average_cost_basis_per_share=0.0,
        ),
        execution_history=(),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )


def _pending_bracket() -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{_BRACKET_ID}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{_BRACKET_ID}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol(_TICKER), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.PENDING_ACTIVATION,
    )
    return BracketRecord(
        bracket_id=BracketId(_BRACKET_ID),
        position_id=PositionId(_POSITION_ID),
        status=BracketStatus.PENDING_ENTRY,
        entry_order_id=OrderId(_ENTRY_ORDER_ID),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


def _active_thesis() -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{_THESIS_ID}-{ct.value.lower()}",
            thesis_id=ThesisId(_THESIS_ID),
            component_type=ct,
            linked_bracket_leg_type=None,
            linked_bracket_leg_id=None,
            instrument_reference=_TICKER,
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
        thesis_id=ThesisId(_THESIS_ID),
        position_id=PositionId(_POSITION_ID),
        summary="AAPL momentum",
        key_catalyst="Q3 earnings beat",
        position_size_rationale="Sized at 5% conviction-3",
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


def _cash_ledger(current_cash_usd: float = 100_000.0) -> CashLedger:
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


def _unprocessed_entry_fill() -> FillRecord:
    # ALP-462 — wrap fixture floats at the FillRecord boundary.
    return FillRecord(
        fill_id=_FILL_ID,
        order_id=_ENTRY_ORDER_ID,
        fill_timestamp=_NOW - timedelta(minutes=10),
        fill_price=price("150"),
        fill_quantity=10.0,
        remaining_quantity_after=0.0,
        order_status_after=OrderStatus.FILLED,
        slippage_usd=money("0"),
        fees_usd=money("0"),
        execution_venue="NASDAQ",
        gateway_reference=f"alp-{_FILL_ID}",
        persistence_timestamp=_NOW - timedelta(minutes=10) + timedelta(seconds=1),
        processing_status=FillProcessingStatus.UNPROCESSED,
        processing_invocation_id=None,
        processing_timestamp=None,
        regt_attribution=None,
        live_execution_estimate=None,
    )


# ---------------------------------------------------------------------------
# Phase D fixture builders — accepted-envelope inputs for persist_envelope_outcome.
# ---------------------------------------------------------------------------


_PHASE_D_TICKER = "NVDA"
_PHASE_D_ENVELOPE_ID = "ENV-REC-1"
_PHASE_D_RECOMMENDATION_ID = "REC-1"


def _all_pass_thesis_evaluation() -> ThesisQualityEvaluation:
    pass_assessment = CriterionAssessment(status="pass", note=None)
    return ThesisQualityEvaluation(
        falsifiability=pass_assessment,
        sizing_proportionality=pass_assessment,
        portfolio_coherence=pass_assessment,
        timing_plausibility=pass_assessment,
        counterargument_consideration=pass_assessment,
    )


def _phase_d_open_envelope(invocation_id: str) -> PMEnvelope:
    open_command = OpenCommand(
        command_type="open",
        instrument=EquityInstrument(
            asset_type="equity",
            ticker=_PHASE_D_TICKER,
            direction="long",
        ),
        entry_order=EntryOrder(type="market", limit_price=None, stop_price=None),
        position_size=PositionSize(quantity=10.0, dollar_value=money("10000")),
        target=Target(
            target_type="absolute_price",
            price=price("950"),
            pl_percentage=None,
            pl_dollar=None,
            order_type="limit",
        ),
        invalidation_legs=(
            PriceLeg(
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=_PHASE_D_TICKER,
                    comparator="<=",
                    trigger_price=price("750"),
                ),
                order_parameters=BracketOrderParameters(order_type="market", limit_price=None),
            ),
        ),
        thesis=Thesis(
            summary=f"Long {_PHASE_D_TICKER}.",
            components=(
                OMSThesisComponent(
                    component_type="entry_rationale",
                    linked_leg="entry",
                    instrument_reference=_PHASE_D_TICKER,
                    narrative="Capex tailwind.",
                    key_assumptions=("Capex stays elevated.",),
                ),
            ),
        ),
    )
    return PMAnalystEnvelope(
        envelope_id=EnvelopeId(_PHASE_D_ENVELOPE_ID),
        invocation_id=InvocationId(invocation_id),
        source_provenance="pm_analyst",
        source_recommendation_id=RecommendationId(_PHASE_D_RECOMMENDATION_ID),
        recommendation_type="new_entry",
        verdict="approve",
        evaluation=_all_pass_thesis_evaluation(),
        modifications=(),
        concerns=(),
        rationale_narrative="verification fixture envelope",
        anti_patterns_identified=None,
        commands=(open_command,),
    )


def _phase_d_accepted_result(envelope_id: str) -> SubmissionResult:
    return SubmissionResult(
        command_ordinal=0,
        status="accepted",
        command_id=f"{envelope_id}.0.0",
        acknowledgment=Acknowledgment(),
    )


async def _seed_cash_ledger_singleton_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Persist the cash_ledger singleton iff it does not already exist.

    Multiple phases need it (Phase 2's OPEN reserves capital; the SQL repo's
    snapshot read requires it). Idempotent so phases can each call it without
    racing on the singleton's PK.
    """
    from alphamind.state.tables.cash_ledger import (
        CASH_LEDGER_SINGLETON_ID,
        CashLedgerRow,
    )

    async with factory() as sess:
        if await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID) is None:
            sess.add(cash_ledger_record_to_row(_cash_ledger(), last_updated_at=_NOW))
            await sess.commit()


async def _seed_drawdown_state_singleton_idempotent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Persist the drawdown_state singleton iff it does not already exist.

    Symmetrical with the cash_ledger helper above; the SQL repo's snapshot
    read requires both singletons to be present.
    """
    from alphamind.state.tables.drawdown_state import (
        DRAWDOWN_STATE_SINGLETON_ID,
        DrawdownStateRow,
    )

    async with factory() as sess:
        if await sess.get(DrawdownStateRow, DRAWDOWN_STATE_SINGLETON_ID) is None:
            sess.add(drawdown_state_record_to_row(_drawdown_state(), last_updated_at=_NOW))
            await sess.commit()


async def _seed_phase_c_state(factory: async_sessionmaker[AsyncSession]) -> None:
    """Persist the order/position/bracket/thesis/cash/drawdown rows + the unprocessed fill."""
    bracket_parent, leg_rows = bracket_record_to_rows(_pending_bracket())
    thesis_parent, component_rows = thesis_record_to_rows(_active_thesis())

    async with factory() as sess:
        # Deferred FKs (per the FK-tightening migration) defer all parent ↔ child
        # checks to COMMIT, so add-order doesn't matter for correctness; we still
        # add parents before children for readability.
        sess.add(order_record_to_row(_pending_entry_order()))
        sess.add(order_record_to_row(_pending_protective_stop_order()))
        sess.add(position_record_to_row(_pending_position()))
        sess.add(bracket_parent)
        sess.add(thesis_parent)
        sess.add(cash_ledger_record_to_row(_cash_ledger(), last_updated_at=_NOW))
        sess.add(drawdown_state_record_to_row(_drawdown_state(), last_updated_at=_NOW))
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        for crow in component_rows:
            sess.add(crow)
        await sess.commit()

    async with factory() as sess:
        await append_fill_record(sess, _unprocessed_entry_fill())
        await sess.commit()


# ---------------------------------------------------------------------------
# Required state-persistence tables (from package __init__).
# ---------------------------------------------------------------------------


_STATE_PERSISTENCE_TABLES: tuple[str, ...] = (
    "process_lifetimes",
    "invocations",
    "activity_log",
    "positions",
    "theses",
    "thesis_components",
    "orders",
    "brackets",
    "bracket_legs",
    "cash_ledger",
    "drawdown_state",
    "fill_records",
    "corporate_action_integration_ledger",
)


# ---------------------------------------------------------------------------
# Phase A — schema verification
# ---------------------------------------------------------------------------


_PHASE_A_LABEL = "Phase A — schema"


def run_phase_a_schema(db_path: Path) -> PhaseResult:
    """Inspect ``sqlite_master`` and confirm every state-persistence table exists."""
    from sqlalchemy import inspect

    # Side-effect import: registers state-persistence tables on Base.metadata
    # so test DBs created from Base.metadata.create_all also see them.
    import alphamind.state.tables  # noqa: F401

    engine = make_engine(str(db_path))
    try:
        existing = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    missing = [t for t in _STATE_PERSISTENCE_TABLES if t not in existing]
    if missing:
        return PhaseResult(
            label=_PHASE_A_LABEL,
            ok=False,
            detail=f"missing tables: {', '.join(missing)}",
        )
    return PhaseResult(label=_PHASE_A_LABEL, ok=True)


# ---------------------------------------------------------------------------
# Phase B — InvocationContext atomicity round-trip
# ---------------------------------------------------------------------------


class _SyntheticDownstreamError(RuntimeError):
    """Sentinel raised inside Phase B's rollback probe; never propagates outside."""


def _raise_synthetic_downstream() -> None:
    """Raise the Phase B sentinel — extracted so the inline raise stays terse
    and the linter's TRY301 'abstract raise to inner function' guidance is met."""
    raise _SyntheticDownstreamError("simulated downstream failure")


_PHASE_B_LABEL = "Phase B — invocation context"


async def run_phase_b_invocation_context(db_path: Path) -> PhaseResult:
    """Probe the ALP-449 three-tx ``InvocationContext`` semantics.

    Probe 1 — clean exit: the invocation row persists *and* the phase
    session's writes commit (the stamp on ``phase1_completed_at`` lands).

    Probe 2 — exception inside the context: the invocation row stays
    (its commit happened before the phase opened) but the phase session's
    writes roll back (``phase1_completed_at`` stays NULL).
    """
    from sqlalchemy import select

    from alphamind.state.invocation_context.context import (
        stamp_phase_completion,
    )

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_process_lifetime(factory)

        commit_inv_id = f"{_INV_ID_BASE}-phase-b-commit"
        rollback_inv_id = f"{_INV_ID_BASE}-phase-b-rollback"

        # Probe 1 — clean exit: row + phase writes commit.
        async with InvocationContext(
            session_factory=factory, record=_invocation_record(commit_inv_id)
        ) as handle:
            await stamp_phase_completion(handle, column="phase1_completed_at")

        async with factory() as sess:
            committed = (
                await sess.execute(
                    select(InvocationRow).where(InvocationRow.invocation_id == commit_inv_id)
                )
            ).scalar_one_or_none()
        if committed is None or committed.phase1_completed_at is None:
            return PhaseResult(
                label=_PHASE_B_LABEL,
                ok=False,
                detail="clean-exit invocation row or phase1 stamp did not persist",
            )

        # Probe 2 — exception inside the context: row stays, phase writes
        # roll back. The pre-ALP-449 model rolled back the row too; the
        # three-tx model commits the row up-front so it stays for the
        # snapshot-isolation guard to refuse reads against it.
        try:
            async with InvocationContext(
                session_factory=factory, record=_invocation_record(rollback_inv_id)
            ) as handle:
                await stamp_phase_completion(handle, column="phase1_completed_at")
                _raise_synthetic_downstream()
        except _SyntheticDownstreamError:
            pass

        async with factory() as sess:
            rolled = (
                await sess.execute(
                    select(InvocationRow).where(InvocationRow.invocation_id == rollback_inv_id)
                )
            ).scalar_one_or_none()
        if rolled is None:
            return PhaseResult(
                label=_PHASE_B_LABEL,
                ok=False,
                detail="rollback-probe invocation row missing — row commit should precede phase",
            )
        if rolled.phase1_completed_at is not None:
            return PhaseResult(
                label=_PHASE_B_LABEL,
                ok=False,
                detail="rollback probe persisted the phase1 stamp instead of rolling back",
            )

        return PhaseResult(label=_PHASE_B_LABEL, ok=True)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Phase C — Phase 1 fill-integration write path
# ---------------------------------------------------------------------------


_PHASE_C_LABEL = "Phase C — Phase 1 write path"

_PHASE_C_REQUIRED_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.ORDER_FILLED,
        EventType.POSITION_OPENED,
        EventType.BRACKET_ACTIVATED,
        EventType.CASH_DEBITED,
    }
)


async def run_phase_c_phase1_write_path(db_path: Path) -> PhaseResult:
    """Append an unprocessed entry fill, drain it via process_unprocessed_fills,
    and assert (a) the fill transitions to processed, (b) the position transitions
    PENDING→OPEN, (c) the activity log carries the documented event chain."""
    from sqlalchemy import select

    from alphamind.execution.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from alphamind.state.tables.activity_log import ActivityLogRow
    from alphamind.state.tables.fill_records import FillRecordRow
    from alphamind.state.tables.positions import PositionRow

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_process_lifetime(factory)
        await _seed_phase_c_state(factory)

        invocation_id = f"{_INV_ID_BASE}-phase-c"
        async with InvocationContext(
            session_factory=factory, record=_invocation_record(invocation_id)
        ) as handle:
            summary = await process_unprocessed_fills(
                handle,
                market_inputs=_market_inputs(),
                config=_state_persistence_config(),
            )

        if summary.fills_processed != 1:
            return PhaseResult(
                label=_PHASE_C_LABEL,
                ok=False,
                detail=f"expected 1 fill processed, got {summary.fills_processed}",
            )

        async with factory() as sess:
            fill_row = (
                await sess.execute(select(FillRecordRow).where(FillRecordRow.fill_id == _FILL_ID))
            ).scalar_one()
            if fill_row.processing_status != FillProcessingStatus.PROCESSED.value:
                return PhaseResult(
                    label=_PHASE_C_LABEL,
                    ok=False,
                    detail=(
                        f"fill processing_status is {fill_row.processing_status!r}, "
                        f"expected {FillProcessingStatus.PROCESSED.value!r}"
                    ),
                )

            position_row = (
                await sess.execute(
                    select(PositionRow).where(PositionRow.position_id == _POSITION_ID)
                )
            ).scalar_one()
            if position_row.status != PositionStatus.OPEN.value:
                return PhaseResult(
                    label=_PHASE_C_LABEL,
                    ok=False,
                    detail=(
                        f"position status is {position_row.status!r}, "
                        f"expected {PositionStatus.OPEN.value!r}"
                    ),
                )

            log_rows = (
                (
                    await sess.execute(
                        select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                    )
                )
                .scalars()
                .all()
            )

        observed_events = {EventType(r.event_type) for r in log_rows}
        missing_events = _PHASE_C_REQUIRED_EVENTS - observed_events
        if missing_events:
            return PhaseResult(
                label=_PHASE_C_LABEL,
                ok=False,
                detail=(
                    "activity log missing required events: "
                    + ", ".join(sorted(e.value for e in missing_events))
                ),
            )

        return PhaseResult(label=_PHASE_C_LABEL, ok=True)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Phase D — Phase 2 accepted-envelope writeback
# ---------------------------------------------------------------------------


_PHASE_D_REQUIRED_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.ORDER_SUBMITTED,
        EventType.THESIS_CREATED,
        EventType.CAPITAL_RESERVED,
        EventType.PM_DECISION,
    }
)


_PHASE_D_LABEL = "Phase D — Phase 2 envelope"


async def _phase_d_validate_state(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> str | None:
    """Inspect what Phase 2's OPEN writeback wrote for *invocation_id*; return
    a failure detail string on the first violation, or ``None`` on success.

    Implementation note: each per-entity check is encapsulated as an async
    helper that returns (failure_detail, context). Returning early on the
    first failure keeps the cyclomatic complexity low and lets the linter's
    PLR0911 budget hold.
    """
    from sqlalchemy import select

    from alphamind.state.tables.activity_log import ActivityLogRow

    async with factory() as sess:
        log_rows = (
            (
                await sess.execute(
                    select(ActivityLogRow).where(ActivityLogRow.invocation_id == invocation_id)
                )
            )
            .scalars()
            .all()
        )

        new_position_ids = [
            r.position_id
            for r in log_rows
            if r.event_type == EventType.THESIS_CREATED.value and r.position_id is not None
        ]
        if len(new_position_ids) != 1:
            return (
                f"expected exactly 1 thesis_created entry with a "
                f"position_id, got {len(new_position_ids)}"
            )

        entity_failure = await _phase_d_check_entities(sess, new_position_ids[0])
        if entity_failure is not None:
            return entity_failure

    observed_events = {EventType(r.event_type) for r in log_rows}
    missing_events = _PHASE_D_REQUIRED_EVENTS - observed_events
    if missing_events:
        return "activity log missing required events: " + ", ".join(
            sorted(e.value for e in missing_events)
        )

    return None


async def _phase_d_check_entities(sess: AsyncSession, position_id: str) -> str | None:
    """Confirm position/thesis/bracket/orders shape for the OPEN-writeback
    *position_id*; return a failure detail or ``None``."""
    from sqlalchemy import select

    from alphamind.state.tables.brackets import BracketRow
    from alphamind.state.tables.orders import OrderRow
    from alphamind.state.tables.positions import PositionRow
    from alphamind.state.tables.theses import ThesisRow

    position = (
        await sess.execute(select(PositionRow).where(PositionRow.position_id == position_id))
    ).scalar_one()
    if position.status != PositionStatus.PENDING.value:
        return (
            f"new position {position_id} status is "
            f"{position.status!r}, expected {PositionStatus.PENDING.value!r}"
        )

    theses_for_position = (
        (await sess.execute(select(ThesisRow).where(ThesisRow.position_id == position_id)))
        .scalars()
        .all()
    )
    if len(theses_for_position) != 1 or (
        theses_for_position[0].status != ThesisRecordStatus.ACTIVE.value
    ):
        return f"expected 1 ACTIVE thesis for {position_id}, got {len(theses_for_position)}"

    brackets_for_position = (
        (await sess.execute(select(BracketRow).where(BracketRow.position_id == position_id)))
        .scalars()
        .all()
    )
    if len(brackets_for_position) != 1 or (
        brackets_for_position[0].status != BracketStatus.PENDING_ENTRY.value
    ):
        return (
            f"expected 1 PENDING_ENTRY bracket for {position_id}, got {len(brackets_for_position)}"
        )

    new_bracket_id = brackets_for_position[0].bracket_id
    orders_for_bracket = (
        (await sess.execute(select(OrderRow).where(OrderRow.bracket_id == new_bracket_id)))
        .scalars()
        .all()
    )
    if len(orders_for_bracket) < 2:
        return (
            f"expected ≥2 orders (entry + protective) for bracket "
            f"{new_bracket_id}, got {len(orders_for_bracket)}"
        )

    return None


async def run_phase_d_phase2_envelope(db_path: Path) -> PhaseResult:
    """Persist a synthetic OPEN-command envelope via persist_envelope_outcome
    and assert the new position/thesis/bracket plus PENDING orders land for the
    NVDA underlying, and the activity log carries the documented event chain.

    Assertions filter by the NVDA underlying / Phase D invocation_id rather
    than by absolute table counts so the phase tolerates Phase C running
    against the same DB first.
    """
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_process_lifetime(factory)
        await _seed_cash_ledger_singleton_idempotent(factory)

        invocation_id = f"{_INV_ID_BASE}-phase-d"
        envelope = _phase_d_open_envelope(invocation_id)
        results = (_phase_d_accepted_result(envelope.envelope_id),)

        async with InvocationContext(
            session_factory=factory, record=_invocation_record(invocation_id)
        ) as handle:
            await persist_envelope_outcome(
                handle, envelope, results, config=_state_persistence_config()
            )

        failure_detail = await _phase_d_validate_state(factory, invocation_id)
        if failure_detail is not None:
            return PhaseResult(label=_PHASE_D_LABEL, ok=False, detail=failure_detail)

        return PhaseResult(label=_PHASE_D_LABEL, ok=True)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Phase E — Phase 2 Layer-1 parse-failure writeback
# ---------------------------------------------------------------------------


async def run_phase_e_layer1_parse_failure(db_path: Path) -> PhaseResult:
    """Simulate a Layer-1 parse failure and assert both parallel surfaces are
    populated: (a) the in-memory ``failed_submission_log`` carries one entry,
    (b) one ``envelope_parse_failed`` row lands in ``activity_log`` with
    ``raw_args_json`` + ``validation_error_repr`` populated.

    The test bypasses ``_handle_submit_envelope``'s deep ValidationToolState
    fixture and directly mirrors its Layer-1 branch — the two side effects
    (in-memory cell mutation + ``persist_envelope_parse_failure``) are the
    parallel-surface contract. The unit-test surface
    (``test_phase2_write_path.py``) exercises the
    ``_handle_submit_envelope`` integration directly.
    """
    from pydantic import ValidationError
    from sqlalchemy import select

    from alphamind.decision.portfolio_manager.submit_envelope import _validate_envelope_payload
    from alphamind.execution.write_paths.phase2 import (
        persist_envelope_parse_failure,
    )
    from alphamind.state.tables.activity_log import ActivityLogRow

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_process_lifetime(factory)

        # Malformed envelope: missing the discriminator (``source_provenance``)
        # so Pydantic union dispatch fails at Layer-1.
        bogus_args: dict[str, Any] = {"envelope_id": "ENV-REC-99", "garbage": "value"}

        try:
            _validate_envelope_payload(bogus_args)
        except ValidationError as exc:
            error_repr = str(exc)
        else:
            return PhaseResult(
                label=_PHASE_E_LABEL,
                ok=False,
                detail="malformed payload unexpectedly parsed (envelope schema regressed?)",
            )

        # Mirror _handle_submit_envelope's Layer-1 branch: build the in-memory
        # surface entry AND persist its SQL counterpart. The two surfaces share
        # the same FailedSubmissionEntry — story 08's parallel-surface contract.
        invocation_id = f"{_INV_ID_BASE}-phase-e"
        failed_entry = FailedSubmissionEntry(
            raw_args=dict(bogus_args),
            validation_error_repr=error_repr,
            command_id=f"{invocation_id}.ENV-REC-99.0.0",
        )

        async with InvocationContext(
            session_factory=factory, record=_invocation_record(invocation_id)
        ) as handle:
            await persist_envelope_parse_failure(
                handle, failed_entry, config=_state_persistence_config()
            )

        async with factory() as sess:
            log_rows = (
                (
                    await sess.execute(
                        select(ActivityLogRow).where(
                            ActivityLogRow.invocation_id == invocation_id,
                            ActivityLogRow.event_type == EventType.ENVELOPE_PARSE_FAILED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )

        sql_failure = _phase_e_validate_sql_row(log_rows, failed_entry)
        if sql_failure is not None:
            return PhaseResult(label=_PHASE_E_LABEL, ok=False, detail=sql_failure)

        return PhaseResult(label=_PHASE_E_LABEL, ok=True)
    finally:
        await engine.dispose()


_PHASE_E_LABEL = "Phase E — Layer-1 parse failure"


def _phase_e_validate_sql_row(
    log_rows: Sequence[Any], failed_entry: FailedSubmissionEntry
) -> str | None:
    """Confirm the persisted ``envelope_parse_failed`` row carries the same
    raw payload + error text the in-memory surface holds (parallel-surface
    contract). Returns a failure detail or ``None`` on success.
    """
    if len(log_rows) != 1:
        return f"expected exactly 1 envelope_parse_failed row, got {len(log_rows)}"
    detail_payload = json.loads(log_rows[0].detail_json)
    if "raw_args_json" not in detail_payload:
        return "envelope_parse_failed detail missing raw_args_json"
    if "validation_error_repr" not in detail_payload:
        return "envelope_parse_failed detail missing validation_error_repr"
    persisted_raw = json.loads(detail_payload["raw_args_json"])
    if persisted_raw != failed_entry.raw_args:
        return (
            "envelope_parse_failed.raw_args_json content does not match the "
            "in-memory FailedSubmissionEntry's raw_args"
        )
    if detail_payload["validation_error_repr"] != failed_entry.validation_error_repr:
        return (
            "envelope_parse_failed.validation_error_repr does not match the "
            "in-memory FailedSubmissionEntry's validation_error_repr"
        )
    return None


# ---------------------------------------------------------------------------
# Phase F — SqlPortfolioStateRepository read parity
# ---------------------------------------------------------------------------


def _phase_f_active_risk_parameters() -> Any:
    from alphamind._kernel.regime import (
        RegimeLabel,
        RegimeTransitionState,
    )
    from alphamind.portfolio_state.aggregates.risk_parameters import (
        ActiveRiskParameterEntry,
        ActiveRiskParameterSet,
    )

    entry = ActiveRiskParameterEntry(
        rule_id="max_position_size_usd",
        rule_label="Max position size (USD)",
        value=1000.0,
        unit="USD",
        regime_multiplier_applied=1.0,
        base_value=1000.0,
    )
    return ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(entry,),
        active_overlays=(),
    )


def _phase_f_phase_1_enforcement_result() -> Any:
    """Compose a ``Phase1EnforcementResult`` representative of Phase F's setup.

    Wraps :func:`_phase_f_active_risk_parameters` in a synthetic
    ``RegimeAdaptationOutput`` (NORMAL regime, no transition, no overlays) and
    a zero-drawdown ``DrawdownState`` so the cumulative-drawdown tier
    classifier yields ``None`` and the active-risk parameters pass through
    unchanged. The resulting bundle is the canonical input to
    :func:`make_active_risk_parameters_provider`.
    """
    from alphamind._kernel.regime import RegimeTransitionState, RiskZone
    from alphamind.config.models.regimes import Regime
    from alphamind.execution.guardrail_enforcement import compose_phase_1_enforcement
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
    from alphamind.risk_guardrails.regime_adaptation import (
        RegimeAdaptationOutput,
        RegimeAdaptationState,
    )

    parameters = _phase_f_active_risk_parameters()
    regime_state = RegimeAdaptationState(
        as_of=_NOW.isoformat().replace("+00:00", "Z"),
        invocation_id=_INV_ID_BASE,
        active_regime=Regime.normal,
        prior_regime=None,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        transition_started_invocation_id=None,
        transition_origin_regime=None,
        active_overlays=(),
        distillation_regime_label="normal",
        distillation_vix_level=18.0,
        regime_skip_emergency=False,
    )
    regime_output = RegimeAdaptationOutput(
        runtime_dimensions_active_regime=Regime.normal,
        runtime_dimensions_active_overlays=(),
        overlay_activation_decisions=(),
        effective_limits={},
        active_risk_parameter_set=parameters,
        regime_transition_breaches=(),
        regime_skip_emergency=False,
        new_persisted_state=regime_state,
        audit_log_entries=(),
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
    return compose_phase_1_enforcement(
        regime_output=regime_output,
        drawdown_state=drawdown,
        progressive_tiers=load_cumulative_drawdown_progressive_tiers(),
    )


_PHASE_F_LABEL = "Phase F — repository read parity"


async def run_phase_f_repository_read_parity(db_path: Path) -> PhaseResult:
    """Build a ``SqlPortfolioStateRepository`` against a Phase-1-committed
    invocation and call ``assemble_snapshot``; assert the resulting snapshot
    surfaces an open position. Then point the repo at an invocation whose
    ``phase1_completed_at`` is NULL and assert ``RepositoryConsistencyError``
    fires.

    Phase C must run against the same DB first so there is at least one
    OPEN position to surface in the snapshot; the verify orchestrator
    sequences Phases C then F to honour this.
    """
    from alphamind.execution.guardrail_enforcement import (
        make_active_risk_parameters_provider,
    )
    from alphamind.execution.write_paths.phase1 import (
        process_unprocessed_fills,
    )
    from alphamind.portfolio_state import PortfolioStateConfig
    from alphamind.portfolio_state.assembler import assemble_snapshot
    from alphamind.portfolio_state.pricing import (
        PriceQuote,
        PriceSource,
        StubCurrentPriceProvider,
    )
    from alphamind.portfolio_state.repository import RepositoryConsistencyError
    from alphamind.state.repository import (
        build_sql_portfolio_state_repository,
    )

    engine, factory = _open_async_factory(db_path)
    try:
        await _seed_process_lifetime(factory)

        # Time anchoring: Phase 1's write path stamps ``phase1_completed_at``
        # to wall time when it runs. The snapshot validator requires
        # ``phase1_committed_at`` to be at or before ``snapshot_assembled_at``
        # AND ``pipeline_invocation_started_at`` to be at or after
        # ``snapshot_assembled_at``. So ``start_at`` is set to a
        # wall-time-plus-buffer point in the future; later we sample
        # ``now`` strictly between Phase 1's stamp and that buffered
        # ``start_at``.
        committed_inv_id = f"{_INV_ID_BASE}-phase-f-committed"
        uncommitted_inv_id = f"{_INV_ID_BASE}-phase-f-uncommitted"
        future_start = datetime.now(UTC) + timedelta(seconds=5)
        committed_record = _invocation_record(committed_inv_id, start_at=future_start)
        uncommitted_record = _invocation_record(uncommitted_inv_id, start_at=future_start)

        # Insert the uncommitted record directly; the committed record gets
        # inserted by InvocationContext below and then naturally stamped.
        async with factory() as sess:
            sess.add(invocation_record_to_row(uncommitted_record))
            await sess.commit()

        # The cash_ledger + drawdown_state singletons must exist for the SQL repo
        # to satisfy assemble_snapshot's reads. Phase C will have seeded them on
        # the orchestrator's path; the idempotent helpers tolerate either ordering
        # so the phase is callable in isolation.
        await _seed_cash_ledger_singleton_idempotent(factory)
        await _seed_drawdown_state_singleton_idempotent(factory)

        # Drive Phase 1's natural completion-timestamp stamp on the
        # committed invocation — no fills remain after Phase C, but the
        # write path always touches ``phase1_completed_at`` as its final
        # step before the surrounding context commits.
        async with InvocationContext(session_factory=factory, record=committed_record) as handle:
            await process_unprocessed_fills(
                handle,
                market_inputs=_market_inputs(),
                config=_state_persistence_config(),
            )

        # Sample ``now`` between Phase 1's just-completed stamp and the
        # buffered ``future_start`` so both invariants hold (phase1_at at
        # or before now, and now at or before future_start).
        now = datetime.now(UTC)

        # Construct the active-risk parameters via the guardrail-enforcement
        # orchestrator (story 02 + 03a) so the verify script exercises the
        # same composition path production wiring will use.
        phase_1_result = _phase_f_phase_1_enforcement_result()
        provider = make_active_risk_parameters_provider(phase_1_result)

        def _prior_provider(_path: str) -> Any:
            return _phase_f_active_risk_parameters()

        committed_repo = build_sql_portfolio_state_repository(
            session_factory=factory,
            invocation_id=committed_inv_id,
            active_risk_parameters_provider=provider,
            prior_active_risk_parameters_provider=_prior_provider,
            config=_state_persistence_config(),
        )

        portfolio_config = PortfolioStateConfig(
            pm_decision_log_sliding_window_invocations=5,
            thesis_resolutions_lookback_trading_days=10,
            thesis_quality_aggregates_trailing_windows_days=(5, 20),
            snapshot_freshness_max_phase1_to_snapshot_seconds=300.0,
            snapshot_freshness_max_price_age_seconds=60.0,
        )
        # Provide quotes for every underlying any earlier phase may have
        # opened a position against (Phase C: AAPL, Phase D: NVDA). Missing
        # quotes degrade to a stale-sentinel and surface as warnings; the
        # snapshot still assembles but the verifier prefers a clean signal.
        price_provider = StubCurrentPriceProvider(
            {
                _TICKER: PriceQuote(
                    ticker=_TICKER,
                    price_usd=160.0,
                    as_of_timestamp=now,
                    source=PriceSource.INTRADAY_QUOTE,
                    is_stale=False,
                ),
                _PHASE_D_TICKER: PriceQuote(
                    ticker=_PHASE_D_TICKER,
                    price_usd=120.0,
                    as_of_timestamp=now,
                    source=PriceSource.INTRADAY_QUOTE,
                    is_stale=False,
                ),
            },
            now=now,
        )

        def _sector_resolver(_pos: PositionRecord) -> str | None:
            return "tech"

        try:
            assembled = assemble_snapshot(
                repository=committed_repo,
                price_provider=price_provider,
                sector_resolver=_sector_resolver,
                config=portfolio_config,
                now=now,
            )
        except RepositoryConsistencyError as exc:
            return PhaseResult(
                label=_PHASE_F_LABEL,
                ok=False,
                detail=f"assemble_snapshot raised RepositoryConsistencyError: {exc}",
            )

        snap = assembled.snapshot
        if snap.invocation_id != committed_inv_id:
            return PhaseResult(
                label=_PHASE_F_LABEL,
                ok=False,
                detail=(
                    f"snapshot invocation_id is {snap.invocation_id!r}, "
                    f"expected {committed_inv_id!r}"
                ),
            )
        if not snap.open_positions:
            return PhaseResult(
                label=_PHASE_F_LABEL,
                ok=False,
                detail="snapshot.open_positions is empty (run Phase C first to populate)",
            )

        # Negative branch — RepositoryConsistencyError must fire when phase1
        # has not committed for the bound invocation.
        uncommitted_repo = build_sql_portfolio_state_repository(
            session_factory=factory,
            invocation_id=uncommitted_inv_id,
            active_risk_parameters_provider=provider,
            prior_active_risk_parameters_provider=_prior_provider,
            config=_state_persistence_config(),
        )
        try:
            uncommitted_repo.get_current_invocation_metadata()
        except RepositoryConsistencyError:
            pass
        else:
            return PhaseResult(
                label=_PHASE_F_LABEL,
                ok=False,
                detail=(
                    "expected RepositoryConsistencyError when reading "
                    "current_invocation_metadata before Phase 1 commit"
                ),
            )

        return PhaseResult(label=_PHASE_F_LABEL, ok=True)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _render_text(results: Sequence[PhaseResult]) -> str:
    lines = ["=" * 70, "AlphaMind State-Persistence Verification", "=" * 70]
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        lines.append(f"  {r.label:<55} {status}")
        if r.detail:
            lines.append(f"      {r.detail}")
    lines.append("=" * 70)
    if all(r.ok for r in results):
        lines.append("ALL PHASES PASS")
    else:
        failed = [r.label for r in results if not r.ok]
        lines.append(f"FAIL: {len(failed)} phase(s) failed: {', '.join(failed)}")
    lines.append("=" * 70)
    return "\n".join(lines)


def _render_json(results: Sequence[PhaseResult]) -> str:
    payload = {
        "all_pass": all(r.ok for r in results),
        "phases": [{"label": r.label, "ok": r.ok, "detail": r.detail} for r in results],
    }
    return json.dumps(payload, indent=2)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify the State-persistence substrate end-to-end against a freshly-"
            "migrated SQLite DB across six phases (schema, InvocationContext, "
            "Phase 1 fills, Phase 2 envelope, Phase 2 parse failure, repo reads)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="Path to a freshly-migrated SQLite DB (overrides DATABASE_PATH and main.yaml).",
    )
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser


async def _run_all_phases(db_path: Path) -> list[PhaseResult]:
    results: list[PhaseResult] = []

    schema = run_phase_a_schema(db_path)
    results.append(schema)
    if not schema.ok:
        # Bail early — every later phase requires the schema present.
        return results

    results.append(await run_phase_b_invocation_context(db_path))
    results.append(await run_phase_c_phase1_write_path(db_path))
    results.append(await run_phase_d_phase2_envelope(db_path))
    results.append(await run_phase_e_layer1_parse_failure(db_path))
    results.append(await run_phase_f_repository_read_parity(db_path))

    return results


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on all-PASS, 1 if any phase FAILed."""
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = args.db_path
    if db_path is None:
        # Fall back to the persistence-session resolution chain (env var or main.yaml)
        # by asking make_engine to resolve and read the URL.
        engine = make_engine(None)
        db_path = Path(str(engine.url.database or ""))
        engine.dispose()

    results = asyncio.run(_run_all_phases(db_path))

    if args.output == "json":
        print(_render_json(results))
    else:
        print(_render_text(results))

    return 0 if all(r.ok for r in results) else 1
