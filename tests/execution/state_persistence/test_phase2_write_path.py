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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.decision.portfolio_manager.models import (
    AddCommand,
    AdjustCommand,
    CancelCommand,
    CloseCommand,
    CriterionAssessment,
    OMSInstrument,
    OMSPositionSize,
    OpenCommand,
    PMAnalystEnvelope,
    PMEnvelope,
    PMStrategistEnvelope,
    PositionActionEvaluation,
    ThesisQualityEvaluation,
)
from alphamind.execution.oms.submit_envelope_mcp import (
    Acknowledgment,
    FailedSubmissionEntry,
    SubmissionResult,
)
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
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.execution.state_persistence.tables.orders import OrderRow
from alphamind.execution.state_persistence.tables.positions import PositionRow
from alphamind.execution.state_persistence.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.execution.state_persistence.tables.theses import ThesisRow
from alphamind.execution.state_persistence.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
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

_NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"
_PROCESS_ID = "proc-1"


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
    record = CashLedger.model_validate(
        {
            "current_cash_usd": current_cash_usd,
            "settled_cash_usd": current_cash_usd,
            "reserved_capital_usd": reserved_capital_usd,
            "available_buying_power_usd": current_cash_usd - reserved_capital_usd,
            "margin_held_usd": 0.0,
            "unsettled_proceeds": (),
            "cash_pct_of_portfolio": 0.0,
            "true_deployable_capital_usd": 0.0,
            "regt_excess_trailing_30d_usd": 0.0,
            "regt_excess_trailing_90d_usd": 0.0,
            "regt_excess_lifetime_usd": 0.0,
        }
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
    from alphamind.execution.state_persistence.tables.orders_codec import (
        record_to_row as order_record_to_row,
    )
    from tests.execution.state_persistence._fk_substrate import stub_order_row

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


def _open_command(underlying: str = "NVDA", sector: str = "semis") -> OpenCommand:
    return OpenCommand(
        command_type="open",
        instrument=OMSInstrument(asset_type="equity", direction="long", underlying=underlying),
        position_size=OMSPositionSize(sector=sector),  # type: ignore[arg-type]
    )


def _close_command(position_id: str = "POS-NVDA-001") -> CloseCommand:
    return CloseCommand(
        command_type="close",
        position_id=position_id,
        close_rationale_type="thesis_invalidated",
    )


def _adjust_command(position_id: str = "POS-NVDA-001") -> AdjustCommand:
    return AdjustCommand(command_type="adjust", position_id=position_id)


def _cancel_command(order_id: str = "ord-entry-1") -> CancelCommand:
    return CancelCommand(command_type="cancel", order_id=order_id)


def _add_command(
    position_id: str = "POS-NVDA-001",
    underlying: str = "NVDA",
    sector: str = "semis",
) -> AddCommand:
    return AddCommand(
        command_type="add",
        position_id=position_id,
        instrument=OMSInstrument(asset_type="equity", direction="long", underlying=underlying),
        position_size=OMSPositionSize(sector=sector),  # type: ignore[arg-type]
    )


def _make_analyst_envelope(
    envelope_id: str = "ENV-REC-1",
    commands: tuple[Any, ...] = (),
) -> PMEnvelope:
    if not commands:
        commands = (_open_command(),)
    return PMAnalystEnvelope(
        envelope_id=envelope_id,
        invocation_id=_INV_ID,
        source_provenance="pm_analyst",
        source_recommendation_id="REC-1",
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
        envelope_id=envelope_id,
        invocation_id=_INV_ID,
        source_provenance="pm_strategist",
        source_recommendation_id="SA-1",
        recommendation_type="position_assessment",
        position_id=position_id,
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
        ticker=ticker,
        share_count=10.0,
        average_cost_basis_per_share=150.0,
    )
    history = (
        PositionFill(
            fill_timestamp=_NOW - timedelta(hours=2),
            fill_price=150.0,
            fill_quantity=10.0,
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


def _active_thesis(
    thesis_id: str = "THE-NVDA-1", position_id: str = "POS-NVDA-001"
) -> ThesisRecord:
    components = tuple(
        ThesisComponent(
            component_id=f"{thesis_id}-{ct.value.lower()}",
            thesis_id=thesis_id,
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
        thesis_id=thesis_id,
        position_id=position_id,
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
        order_id=f"{bracket_id}-ord-stop",
        trigger=PriceTrigger(underlying_ticker="NVDA", threshold_usd=140.0, direction="LTE"),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=bracket_id,
        position_id=position_id,
        status=BracketStatus.ACTIVE,
        entry_order_id="ord-entry-1",
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
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


# ===========================================================================
# Tests — persist_envelope_parse_failure
# ===========================================================================


async def test_envelope_parse_failure_writes_one_log_entry(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A Layer-1 ValidationError appends one ENVELOPE_PARSE_FAILED activity
    log entry whose detail carries the raw args, the error string, and the
    synthetic command_id."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
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
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_rejection,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)

    envelope = _make_analyst_envelope(envelope_id="ENV-REC-2")
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
    from alphamind.execution.state_persistence.write_paths.phase2 import (
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

        envelope = _make_strategist_envelope(envelope_id="ENV-SA-99", position_id="POS-NONEXISTENT")
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
    from alphamind.execution.oms.submit_envelope_mcp import (
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
    response = await _handle_submit_envelope(
        envelope.model_dump(mode="json"),
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=bundle,
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=True,  # forces Layer-2/3 failure on the OPEN command
        sector_resolver=lambda _: "semis",
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
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    envelope = _make_analyst_envelope(commands=(_open_command(underlying="NVDA"),))
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


async def test_persist_envelope_outcome_stamps_phase2_completion_on_invocation_row(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An accepted envelope's writeback must set the bound invocation row's
    phase2_completed_at as the final step of the open transaction so observers
    can distinguish "Phase 2 in flight" from "Phase 2 committed".
    """
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)

    envelope = _make_analyst_envelope(commands=(_open_command(underlying="NVDA"),))
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
    from alphamind.execution.state_persistence.write_paths.phase2 import (
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
    from alphamind.execution.state_persistence.tables.orders_codec import (
        record_to_row,
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

    rec = OrderRecord(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker="NVDA"),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=140.0),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=f"alp-{order_id}",
        alpaca_order_id_chain=(f"alp-{order_id}",),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=thesis_id,
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
    from alphamind.execution.state_persistence.tables.orders_codec import (
        record_to_row,
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

    rec = OrderRecord(
        order_id=order_id,
        position_id=position_id,
        bracket_id=bracket_id,
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker="NVDA"),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id=f"alp-{order_id}",
        alpaca_order_id_chain=(f"alp-{order_id}",),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id=thesis_id,
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
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(commands=(_close_command(position_id="POS-NVDA-001"),))
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


async def test_adjust_command_cancels_old_protective_order_and_submits_new(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADJUST command writeback: existing PENDING protective order goes
    CANCELLED, a new PENDING protective order is inserted, and the bracket's
    modification history is appended to. Activity log carries order_cancelled
    + order_submitted + bracket_modified + pm_decision."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
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
        order_id="ord-old-stop",
        position_id="POS-NVDA-001",
        bracket_id="BRK-NVDA-1",
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker="NVDA"),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=140.0),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id="alp-ord-old-stop",
        alpaca_order_id_chain=("alp-ord-old-stop",),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id="THE-NVDA-1",
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    await _seed_position_cluster(
        factory, _open_position(), _active_thesis(), _active_bracket(), old_stop
    )

    envelope = _make_strategist_envelope(commands=(_adjust_command(position_id="POS-NVDA-001"),))
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


async def test_cancel_command_on_entry_dissolves_bracket_and_resolves_thesis(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """CANCEL of an entry order: target → CANCELLED, all bracket legs →
    CANCELLED, bracket → DISSOLVED, thesis → CANCELLED, capital released.
    Activity log carries order_cancelled + capital_released + thesis_resolved
    + bracket_dissolved + pm_decision."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
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
        order_id="ord-entry-1",
        position_id="POS-NVDA-001",
        bracket_id="BRK-NVDA-1",
        role=OrderRole.ENTRY,
        instrument_spec=EquityInstrumentSpec(ticker="NVDA"),
        direction=OrderDirection.BUY,
        order_type=OrderType.MARKET,
        order_class=OrderClass.BRACKET,
        price_parameters=PriceParameters(),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id="alp-ord-entry-1",
        alpaca_order_id_chain=("alp-ord-entry-1",),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id="THE-NVDA-1",
        originating_pm_command_id=None,
        age_hours=1.0,
    )
    old_stop_rec = OrderRecord(
        order_id="ord-old-stop",
        position_id="POS-NVDA-001",
        bracket_id="BRK-NVDA-1",
        role=OrderRole.PRICE_STOP,
        instrument_spec=EquityInstrumentSpec(ticker="NVDA"),
        direction=OrderDirection.SELL,
        order_type=OrderType.STOP,
        order_class=OrderClass.OTO,
        price_parameters=PriceParameters(stop_trigger_price=140.0),
        quantity=10.0,
        duration=OrderDuration.DAY,
        status=OrderStatus.PENDING,
        alpaca_order_id="alp-ord-old-stop",
        alpaca_order_id_chain=("alp-ord-old-stop",),
        submission_timestamp=_NOW - timedelta(hours=1),
        last_update_timestamp=_NOW - timedelta(hours=1),
        filled_quantity=0.0,
        avg_fill_price=None,
        remaining_quantity=10.0,
        modification_count=0,
        originating_thesis_id="THE-NVDA-1",
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

    envelope = _make_strategist_envelope(commands=(_cancel_command(order_id="ord-entry-1"),))
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


async def test_add_command_writes_add_entry_order_thesis_component_capital_reservation(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ADD command writeback: insert PENDING add-entry order, append a new
    thesis component, reserve capital. Activity log carries order_submitted
    + thesis_component_added + capital_reserved + pm_decision."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory, current_cash_usd=100_000.0)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(commands=(_add_command(position_id="POS-NVDA-001"),))
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
    from alphamind.execution.state_persistence.write_paths.phase2 import (
        persist_envelope_outcome,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    envelope = _make_strategist_envelope(
        commands=(
            _close_command(position_id="POS-NVDA-001"),
            _close_command(position_id="POS-NVDA-001"),
            _close_command(position_id="POS-NVDA-001"),
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


async def test_command_abandoned_emission_survives_per_command_rollback(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A failed Phase 2 transaction rolls back its state mutations but a
    follow-up ``persist_command_abandoned`` (in a fresh transaction) writes
    one COMMAND_ABANDONED activity log entry that survives the rollback."""
    from alphamind.execution.state_persistence.write_paths.phase2 import (
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
    from alphamind.execution.oms.submit_envelope_mcp import (
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
    response = await _handle_submit_envelope(
        bogus_args,
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=_minimal_bundle(),
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=False,
        sector_resolver=lambda _: "tech",
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
    from alphamind.execution.oms.submit_envelope_mcp import (
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
    response = await _handle_submit_envelope(
        bogus_args,
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=_minimal_bundle(),
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=False,
        sector_resolver=lambda _: "tech",
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
    from alphamind.execution.oms.submit_envelope_mcp import (
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
    response = await _handle_submit_envelope(
        envelope.model_dump(mode="json"),
        state=state,
        retrieval_store=_minimal_retrieval_store(),
        pre_processor_bundle=bundle,
        pm_view=_minimal_pm_view(),
        active_sectors=frozenset({"tech", "semis", "financials", "energy"}),
        halt_mode=False,
        sector_resolver=lambda _: "semis",
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
    assert EventType.CAPITAL_RESERVED.value in types


# ---------------------------------------------------------------------------
# Minimal fixtures to drive _handle_submit_envelope without the full MCP harness
# ---------------------------------------------------------------------------


def _minimal_validation_state() -> Any:
    from collections.abc import Mapping
    from types import MappingProxyType

    from alphamind.portfolio_state.records.capital import (
        ActiveRiskParameterSet,
        RegimeLabel,
        RegimeTransitionState,
        RiskBudgetConsumption,
    )
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
                    underlying="NVDA",
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
    from alphamind.portfolio_state.consumers.portfolio_manager import PortfolioManagerView

    return PortfolioManagerView.model_construct(
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
