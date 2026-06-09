"""Tests for the monitor-facing engine envelope submission path — ALP-375 / story 04.

Drives ``submit_engine_envelope`` — the direct (non-MCP) write function the
continuous monitor calls between invocations to persist a protective CLOSE
issued from a guardrail trigger. Mirrors the PM-side
``submit_envelope_mcp`` envelope-level validation + per-command writeback
shape but operates on engine-originated envelopes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Import portfolio_manager.models first to break the latent cycle between
# alphamind.execution.oms (engine-stub MCP) and alphamind.decision.portfolio_manager.
import alphamind.decision.portfolio_manager.models  # noqa: F401
from alphamind._kernel.ids import (
    BracketId,
    CommandId,
    EnvelopeId,
    OrderId,
    PositionId,
    Symbol,
    ThesisId,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind.commands.command_models import CloseCommand
from alphamind.commands.engine_envelope import (
    BreachDetails,
    EngineEnvelope,
    GuardrailTriggerRecord,
    SecondaryBreachCheckResult,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.events.activity_log import EventSource, EventType
from alphamind.portfolio_state.records.cash import CashLedger
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    EnforcementBinding,
    OrderRole,
    OrderStatus,
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
from alphamind.state.tables.brackets_codec import (
    record_to_rows as bracket_record_to_rows,
)
from alphamind.state.tables.cash_ledger_codec import (
    cash_ledger_record_to_row,
)
from alphamind.state.tables.orders import OrderRow
from alphamind.state.tables.positions_codec import (
    record_to_row as position_record_to_row,
)
from alphamind.state.tables.theses_codec import (
    record_to_rows as thesis_record_to_rows,
)

_NOW = datetime(2026, 5, 9, 14, 30, 0, tzinfo=UTC)
_TRIGGER_TS = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
_INV_ID = "inv-2026-05-09T14:30:00Z-aaaa"
_PROCESS_ID = "proc-engine-1"
_MONITOR_SESSION = "session-abc"


# ---------------------------------------------------------------------------
# Engine + session fixtures (mirrors test_command_execution_write_path.py)
# ---------------------------------------------------------------------------


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
        pip_freeze_snapshot_path="/tmp/pip-freeze/proc-engine-1.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0",
    )


def _make_invocation_record(invocation_id: str = _INV_ID) -> InvocationRecord:
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


# ---------------------------------------------------------------------------
# Builders — position cluster (mirrors command_execution helpers)
# ---------------------------------------------------------------------------


def _open_position(
    position_id: str = "POS-NVDA-001",
    *,
    thesis_id: str = "THE-NVDA-0123456789abcdef0123456789abcdef",
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


def _active_thesis(
    thesis_id: str = "THE-NVDA-0123456789abcdef0123456789abcdef", position_id: str = "POS-NVDA-001"
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
        entry_order_id=OrderId(f"{bracket_id}-ord-entry"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


async def _seed_position_cluster(
    factory: async_sessionmaker[AsyncSession],
    position: PositionRecord,
    thesis: ThesisRecord,
    bracket: BracketRecord,
) -> None:
    """Seed position + thesis + bracket in a single deferred-FK transaction."""
    from tests.state._fk_substrate import stub_order_row

    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_parent, leg_rows = bracket_record_to_rows(bracket)

    stub_ids_needed: list[str] = [bracket_parent.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        for oid in stub_ids_needed:
            sess.add(stub_order_row(oid, bracket.bracket_id))
        sess.add(bracket_parent)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _seed_native_bracket_equity(
    factory: async_sessionmaker[AsyncSession],
    *,
    broker_legs: tuple[tuple[str, str, str], ...],
    monitor_leg: tuple[str, str] | None = None,
) -> None:
    """Seed a native-bracket equity position whose PENDING protective legs reserve
    shares (ALP-939 reproduction substrate).

    ``broker_legs`` are ``(order_id, order_role, alpaca_order_id)`` triples
    inserted as PENDING, ``BROKER_ENFORCED`` legs carrying a real broker id (the
    OCO legs an equity CLOSE must cancel). ``monitor_leg`` is an optional
    ``(order_id, order_role)`` inserted as a PENDING ``MONITOR_ENFORCED`` leg with
    NO broker id (e.g. a TIME_STOP) — the resolver must exclude it from the broker
    cancel set. Reuses ``_open_position`` / ``_active_thesis`` (NVDA, POS-NVDA-001,
    bracket BRK-NVDA-1).
    """
    from tests.state._fk_substrate import stub_bracket_row, stub_order_row

    position = _open_position()
    thesis = _active_thesis()
    thesis_row, component_rows = thesis_record_to_rows(thesis)
    bracket_id = str(position.bracket_id)
    entry_order_id = f"{bracket_id}-ord-entry"

    leg_specs: list[tuple[str, str, str | None, EnforcementBinding]] = [
        (order_id, role, alpaca_id, EnforcementBinding.BROKER_ENFORCED)
        for order_id, role, alpaca_id in broker_legs
    ]
    if monitor_leg is not None:
        leg_specs.append(
            (monitor_leg[0], monitor_leg[1], None, EnforcementBinding.MONITOR_ENFORCED)
        )

    async with factory() as sess:
        sess.add(position_record_to_row(position))
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        sess.add(stub_order_row(entry_order_id, bracket_id, position_id=str(position.position_id)))
        sess.add(
            stub_bracket_row(
                bracket_id,
                str(position.position_id),
                entry_order_id,
                status=BracketStatus.ACTIVE.value,
            )
        )
        for order_id, role, alpaca_id, _binding in leg_specs:
            row = stub_order_row(
                order_id,
                bracket_id,
                position_id=str(position.position_id),
                role=role,
                status=OrderStatus.PENDING.value,
            )
            row.alpaca_order_id = alpaca_id
            row.alpaca_order_id_chain_json = "[]" if alpaca_id is None else f'["{alpaca_id}"]'
            sess.add(row)
        await sess.flush()
        for leg_index, (order_id, _role, _alpaca_id, binding) in enumerate(leg_specs):
            sess.add(
                BracketLegRow(
                    bracket_leg_id=f"{bracket_id}-leg-{leg_index}",
                    bracket_id=bracket_id,
                    leg_index=leg_index,
                    leg_type=BracketLegType.PRICE_STOP.value,
                    order_id=order_id,
                    trigger_kind="PRICE",
                    trigger_payload_json=json.dumps(
                        {
                            "trigger_type": "price",
                            "underlying_ticker": "NVDA",
                            "threshold_usd": 140.0,
                            "direction": "LTE",
                        }
                    ),
                    pl_anchor_json=None,
                    enforcement=BracketLegEnforcement.MECHANICAL.value,
                    enforcement_binding=binding.value,
                    leg_status=BracketLegStatus.ACTIVE.value,
                    trigger_signal=None,
                )
            )
        await sess.commit()


async def _open_handle(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str = _INV_ID + "-engine",
) -> tuple[InvocationContext, InvocationHandle]:
    ctx = InvocationContext(
        session_factory=factory,
        record=_make_invocation_record(invocation_id=invocation_id),
    )
    handle = await ctx.__aenter__()
    return ctx, handle


# ---------------------------------------------------------------------------
# Builders — engine envelope variants
# ---------------------------------------------------------------------------


def _engine_close_command(
    *,
    position_id: str = "POS-NVDA-001",
    command_id: str | None = None,
) -> CloseCommand:
    return CloseCommand(
        command_id=CommandId(command_id) if command_id is not None else None,
        command_type="close",
        position_id=PositionId(position_id),
        quantity="all",
        order_type="market",
        close_rationale_type="risk_management",
        risk_management_subtype="engine_guardrail",
    )


def _breach_details() -> BreachDetails:
    return BreachDetails(
        current_value=12_500.0,
        limit_value=10_000.0,
        overage=2_500.0,
        unit="usd",
        regime_at_breach="risk_off",
    )


def _trigger_record(
    *,
    cascade_id: str | None = None,
    secondary_breach_check_result: SecondaryBreachCheckResult | None = None,
    position_selection_rationale: str = "position triggering the position-level max loss limit",
    rule_breached: str = "per_position_max_loss",
) -> GuardrailTriggerRecord:
    return GuardrailTriggerRecord(
        rule_breached=rule_breached,
        trigger_timestamp=_TRIGGER_TS,
        breach_details=_breach_details(),
        position_selection_rationale=position_selection_rationale,
        cascade_id=cascade_id,
        secondary_breach_check_result=secondary_breach_check_result,
    )


def _engine_envelope(
    *,
    envelope_id: str = "MON.session-abc.42",
    trigger_record: GuardrailTriggerRecord | None = None,
    commands: tuple[CloseCommand, ...] | None = None,
) -> EngineEnvelope:
    if commands is None:
        commands = (_engine_close_command(),)
    if trigger_record is None:
        trigger_record = _trigger_record()
    return EngineEnvelope(
        envelope_id=EnvelopeId(envelope_id),
        invocation_id=None,
        trigger_timestamp=_TRIGGER_TS,
        source_provenance="engine_guardrail",
        guardrail_trigger_record=trigger_record,
        commands=commands,
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
# Tests
# ===========================================================================


async def test_happy_path_persists_close_order_and_emits_activity_log(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A valid engine envelope persists a CLOSE order and emits an
    ``order_submitted`` activity-log entry tagged with engine provenance."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)

    ctx, handle = await _open_handle(factory)
    result, _state = await submit_engine_envelope(
        _engine_envelope(),
        handle=handle,
        state=state,
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    # Result-shape assertions. The embedded CLOSE carries command_id=None, so
    # the OMS derives it carrying the broker-carried link (ALP-844): the closed
    # position's thesis + this invocation, woven into the canonical engine id.
    from alphamind.execution.oms.command_ids import parse_engine_command_id

    assert result.command_ordinal == 0
    assert result.status == "accepted"
    derived = parse_engine_command_id(result.command_id)
    assert derived.monitor_session_id == "session-abc"
    assert derived.trigger_id == 42
    assert derived.thesis_id == "THE-NVDA-0123456789abcdef0123456789abcdef"
    # The parsed invocation_id is the FULL inv-prefixed form (FK-valid against
    # invocations.invocation_id) — it equals the handle's invocation id verbatim.
    assert derived.invocation_id == handle.invocation_id
    assert result.acknowledgment is not None
    assert result.acknowledgment.position_id == "POS-NVDA-001"

    # State assertions
    async with factory() as sess:
        orders = (await sess.execute(select(OrderRow))).scalars().all()
        close_orders = [o for o in orders if o.order_role == "CLOSE"]
        assert len(close_orders) == 1
        assert close_orders[0].position_id == "POS-NVDA-001"
        assert close_orders[0].status == "PENDING"

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    submitted_rows = [r for r in rows if r.event_type == EventType.ORDER_SUBMITTED.value]
    assert len(submitted_rows) == 1
    # Engine envelopes tag activity-log entries with BRACKET_MANAGER per
    # ``oms-commands.md § Command origins`` — guard against a regression that
    # silently flips this to COMMAND_EXECUTOR (the PM-side default).
    assert submitted_rows[0].source == EventSource.BRACKET_MANAGER.value
    detail = json.loads(submitted_rows[0].detail_json)
    params = detail["order_parameters_json"]
    assert params["risk_management_subtype"] == "engine_guardrail"


async def test_derives_command_id_when_embedded_close_lacks_one(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When the embedded CLOSE has command_id=None, ``submit_engine_envelope``
    derives it via ``derive_engine_command_id`` carrying the broker-carried link
    (ALP-844) — the closed position's thesis (*why*) + this invocation (*when*).
    """
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.command_ids import (
        is_engine_originated,
        parse_engine_command_id,
    )
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    envelope = _engine_envelope(commands=(_engine_close_command(command_id=None),))

    ctx, handle = await _open_handle(factory)
    result, _state = await submit_engine_envelope(
        envelope,
        handle=handle,
        state=state,
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert is_engine_originated(result.command_id)
    derived = parse_engine_command_id(result.command_id)
    assert derived.monitor_session_id == "session-abc"
    assert derived.trigger_id == 42
    assert derived.thesis_id == "THE-NVDA-0123456789abcdef0123456789abcdef"
    # The parsed invocation_id is the FULL inv-prefixed form (FK-valid against
    # invocations.invocation_id) — it equals the handle's invocation id verbatim.
    assert derived.invocation_id == handle.invocation_id


async def test_engine_equity_close_cancels_broker_enforced_legs_before_sell(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """ALP-939 — the engine-envelope equity CLOSE threads the native bracket's
    broker-enforced protective legs into the dispatcher, so ``_close_equity``
    cancels them BEFORE the SIMPLE close sell.

    Against the ``held_for_orders`` fake (the resting OCO legs reserve 100% of the
    shares) the close sell is ``available: 0`` pre-fix and accepted post-fix. A
    monitor-enforced TIME_STOP leg (no broker id) is excluded from the cancel set —
    only the two broker-enforced legs are sent to the broker.
    """
    from typing import Any, cast
    from unittest.mock import MagicMock

    from alphamind.execution.broker_adapter import AccountStateQueries
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope
    from tests.execution.oms.test_broker_dispatch import (
        _execution_config,
        _HeldForOrdersClient,
    )

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_native_bracket_equity(
        factory,
        broker_legs=(
            ("BRK-NVDA-1-ord-tp", OrderRole.TAKE_PROFIT.value, "alp-tp-1"),
            ("BRK-NVDA-1-ord-stop", OrderRole.PRICE_STOP.value, "alp-stop-1"),
        ),
        monitor_leg=("BRK-NVDA-1-ord-time", OrderRole.TIME_STOP.value),
    )

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    client = _HeldForOrdersClient(protective_leg_ids=("alp-tp-1", "alp-stop-1"))
    queries = MagicMock(spec=AccountStateQueries)

    ctx, handle = await _open_handle(factory)
    result, _state = await submit_engine_envelope(
        _engine_envelope(),
        handle=handle,
        state=state,
        config=_make_state_persistence_config(),
        client=cast(Any, client),
        queries=queries,
        execution_config=_execution_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert result.status == "accepted"
    # Both broker-enforced legs are cancelled before the close sell; the
    # monitor-enforced TIME_STOP (no broker id) is never sent to the broker.
    assert [op for op, _ in client.calls] == ["cancel", "cancel", "submit"]
    assert {arg for op, arg in client.calls if op == "cancel"} == {"alp-tp-1", "alp-stop-1"}


async def test_validates_command_id_consistency_with_envelope(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """When the embedded CLOSE has a command_id whose session/trigger components
    don't match the envelope's, ``submit_engine_envelope`` raises."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    # Envelope's session/trigger encoded as `session-abc.42`; embedded CLOSE
    # carries a well-formed command_id with a *different* session, so the
    # bijection is violated (exercises the session-mismatch branch, not a parse
    # failure).
    mismatched = CommandId(
        "MON.session-OTHER.42.0~the-THE-NVDA-0123456789abcdef0123456789abcdef~inv-X"
    )
    bad_envelope = _engine_envelope(
        envelope_id=EnvelopeId("MON.session-abc.42"),
        commands=(_engine_close_command(command_id=mismatched),),
    )

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError):
        await submit_engine_envelope(
            bad_envelope,
            handle=handle,
            state=state,
            config=_make_state_persistence_config(),
        )
    await ctx.__aexit__(None, None, None)


async def test_rejects_secondary_breach_deferred_to_pm(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An envelope whose secondary-breach check returns ``deferred_to_pm`` is
    rejected — the monitor must defer to the PM at the next invocation."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    deferred_envelope = _engine_envelope(
        trigger_record=_trigger_record(
            secondary_breach_check_result=SecondaryBreachCheckResult(result="deferred_to_pm"),
        ),
    )

    ctx, handle = await _open_handle(factory)
    result, _state = await submit_engine_envelope(
        deferred_envelope,
        handle=handle,
        state=state,
        config=_make_state_persistence_config(),
    )
    await ctx.__aexit__(None, None, None)

    assert result.status == "rejected"
    assert result.rejection_payload is not None
    assert result.rejection_payload.feature_disabled is None
    # No CLOSE order should have been persisted.
    async with factory() as sess:
        orders = (await sess.execute(select(OrderRow))).scalars().all()
        assert [o for o in orders if o.order_role == "CLOSE"] == []


async def test_cascade_id_threads_through_to_activity_log(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An envelope with ``cascade_id`` set produces an activity-log entry whose
    detail carries the same cascade_id; multiple envelopes sharing the cascade_id
    yield activity-log entries that share it."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    # Seed two open positions so two envelopes can each close one.
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())
    await _seed_position_cluster(
        factory,
        _open_position(
            position_id=PositionId("POS-NVDA-002"),
            thesis_id=ThesisId("THE-NVDA-fedcba9876543210fedcba9876543210"),
            bracket_id=BracketId("BRK-NVDA-2"),
        ),
        _active_thesis(
            thesis_id=ThesisId("THE-NVDA-fedcba9876543210fedcba9876543210"),
            position_id=PositionId("POS-NVDA-002"),
        ),
        _active_bracket(bracket_id=BracketId("BRK-NVDA-2"), position_id=PositionId("POS-NVDA-002")),
    )

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    cascade_id = "cascade-margin-001"
    env1 = _engine_envelope(
        envelope_id=EnvelopeId("MON.session-abc.42"),
        trigger_record=_trigger_record(cascade_id=cascade_id),
    )
    env2 = _engine_envelope(
        envelope_id=EnvelopeId("MON.session-abc.43"),
        trigger_record=_trigger_record(cascade_id=cascade_id),
        commands=(_engine_close_command(position_id=PositionId("POS-NVDA-002")),),
    )

    ctx, handle = await _open_handle(factory)
    _result1, state = await submit_engine_envelope(
        env1, handle=handle, state=state, config=_make_state_persistence_config()
    )
    _result2, state = await submit_engine_envelope(
        env2, handle=handle, state=state, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    submitted_rows = [r for r in rows if r.event_type == EventType.ORDER_SUBMITTED.value]
    assert len(submitted_rows) == 2
    cascade_ids = {
        json.loads(r.detail_json)["order_parameters_json"].get("cascade_id") for r in submitted_rows
    }
    assert cascade_ids == {cascade_id}


async def test_position_selection_rationale_threads_to_activity_log(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The activity-log entry surfaces the trigger record's
    ``position_selection_rationale`` so feedback-loop queries can correlate."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    rationale = "smallest position in breaching sector"
    envelope = _engine_envelope(
        trigger_record=_trigger_record(position_selection_rationale=rationale),
    )

    ctx, handle = await _open_handle(factory)
    _result, _state = await submit_engine_envelope(
        envelope, handle=handle, state=state, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    submitted_rows = [r for r in rows if r.event_type == EventType.ORDER_SUBMITTED.value]
    assert len(submitted_rows) == 1
    params = json.loads(submitted_rows[0].detail_json)["order_parameters_json"]
    assert params["position_selection_rationale"] == rationale


async def test_duplicate_trigger_id_within_session_raises(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Submitting an envelope with a ``(monitor_session_id, trigger_id)``
    pair previously seen in this session raises a structural error per
    oms-command-ids.md duplicate handling."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    envelope = _engine_envelope()

    ctx, handle = await _open_handle(factory)
    _result, state = await submit_engine_envelope(
        envelope, handle=handle, state=state, config=_make_state_persistence_config()
    )
    # Second submission with the same envelope_id must raise.
    with pytest.raises(ValueError):
        await submit_engine_envelope(
            envelope, handle=handle, state=state, config=_make_state_persistence_config()
        )
    await ctx.__aexit__(None, None, None)


async def test_state_session_id_must_match_envelope_session(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """Submitting an envelope from a different monitor session than the state's
    bound session is a structural error — engine command IDs include
    ``monitor_session_id`` and a session change creates a new state cell."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id="session-1")
    envelope_other_session = _engine_envelope(envelope_id=EnvelopeId("MON.session-2.42"))

    ctx, handle = await _open_handle(factory)
    with pytest.raises(ValueError):
        await submit_engine_envelope(
            envelope_other_session,
            handle=handle,
            state=state,
            config=_make_state_persistence_config(),
        )
    await ctx.__aexit__(None, None, None)


async def test_module_re_exports_submit_engine_envelope_symbols(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The package re-exports the new monitor-facing helper types.

    The :func:`submit_engine_envelope` function shares a name with its
    containing submodule, so importing it via ``from alphamind.execution.oms
    import submit_engine_envelope`` resolves to the module (Python's import
    system finds the submodule before consulting ``__getattr__``). Callers
    import the function directly from
    :mod:`alphamind.execution.oms.submit_engine_envelope`; only the helper
    types are re-exported at the package level.
    """
    from alphamind.execution.oms import (
        SubmitEngineEnvelopeState as PkgState,
    )
    from alphamind.execution.oms import (
        build_initial_submit_engine_envelope_state as pkg_build_state,
    )
    from alphamind.execution.oms.submit_engine_envelope import (
        SubmitEngineEnvelopeState,
        build_initial_submit_engine_envelope_state,
    )

    assert PkgState is SubmitEngineEnvelopeState
    assert pkg_build_state is build_initial_submit_engine_envelope_state


async def test_engine_envelope_does_not_emit_pm_decision(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """The engine envelope path emits ``order_submitted`` but no ``pm_decision``
    — there is no PM verdict for an engine-originated CLOSE."""
    from alphamind.execution.oms import build_initial_submit_engine_envelope_state
    from alphamind.execution.oms.submit_engine_envelope import submit_engine_envelope

    _, factory = db
    await _seed_invocation_substrate(factory)
    await _seed_cash_ledger(factory)
    await _seed_position_cluster(factory, _open_position(), _active_thesis(), _active_bracket())

    state = build_initial_submit_engine_envelope_state(monitor_session_id=_MONITOR_SESSION)
    envelope = _engine_envelope()

    ctx, handle = await _open_handle(factory)
    _result, _state = await submit_engine_envelope(
        envelope, handle=handle, state=state, config=_make_state_persistence_config()
    )
    await ctx.__aexit__(None, None, None)

    rows = await _read_activity_log_for(factory, handle.invocation_id)
    types = {r.event_type for r in rows}
    assert EventType.ORDER_SUBMITTED.value in types
    assert EventType.PM_DECISION.value not in types


# ---------------------------------------------------------------------------
# ALP-476 — frozen-dataclass invariants on the engine-envelope state cell
# ---------------------------------------------------------------------------


def test_submit_engine_envelope_state_is_frozen() -> None:
    """``SubmitEngineEnvelopeState`` is a frozen dataclass; attribute assignment
    raises (ALP-476 — Group C L8 mutable-dataclass conversion)."""
    import dataclasses

    from alphamind.execution.oms import build_initial_submit_engine_envelope_state

    state = build_initial_submit_engine_envelope_state(monitor_session_id="session-frozen")
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.seen_trigger_ids = frozenset({1})  # type: ignore[misc]


def test_submit_engine_envelope_state_replace_extends_seen_trigger_ids() -> None:
    """``dataclasses.replace(state, seen_trigger_ids=...)`` yields a new instance
    with the updated dedup set; the input state is unchanged."""
    import dataclasses

    from alphamind.execution.oms import build_initial_submit_engine_envelope_state

    state = build_initial_submit_engine_envelope_state(monitor_session_id="session-replace")
    new_state = dataclasses.replace(state, seen_trigger_ids=frozenset({42}))

    assert new_state is not state
    assert new_state.seen_trigger_ids == frozenset({42})
    assert state.seen_trigger_ids == frozenset()
    assert new_state.monitor_session_id == state.monitor_session_id
