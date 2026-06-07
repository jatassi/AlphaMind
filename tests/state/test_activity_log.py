"""Tests for the activity-log SQL table + persistence helpers (story 03 / ALP-357).

Covers:
- ``ActivityLogRow`` SQLAlchemy model: column shape, indexes, CHECK constraints,
  FK to ``invocations``.
- ``append_activity_log_entry``: invocation-id mismatch rejection, joins the
  open ``InvocationContext`` transaction (commits with the parent row;
  rolls back when an exception escapes the context).
- Activity-log codec round-trip: every ``EventGroup`` represented by at least
  one detail class (faithful Pydantic round-trip through ``detail_json``).
- Read APIs: intra-invocation changelog, recent PM-decision sliding window,
  position modification trail, most-recent ``distillation_config_change``
  ``new_hash`` lookup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import Session

from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol, ThesisId
from alphamind._kernel.money import money, price, signed_money
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.events import (
    ActivityLogEntry,
    BracketActivatedDetail,
    CashDebitedDetail,
    CashDebitReason,
    CommandAbandonedDetail,
    CorporateActionAppliedDetail,
    CorporateActionType,
    DistillationConfigChange,
    DistillationConfigChangeDetail,
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
    GreeksRefreshFailedDetail,
    GuardrailRejectionDetail,
    HaltActivatedDetail,
    HaltLiftedDetail,
    OrderFilledDetail,
    PMDecisionDetail,
    PMVerdict,
    PositionOpenedDetail,
    PositionOpenMechanism,
    ReconciliationAlertDetail,
    ThesisCreatedDetail,
)
from alphamind.state.invocation_context import (
    InvocationContext,
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.invocation_context.activity_log import (
    append_activity_log_entry,
)
from alphamind.state.repository.activity_log_queries import (
    read_intra_invocation_changelog,
    read_most_recent_config_change_new_hash,
    read_position_modification_trail,
    read_recent_pm_decision_log,
)
from alphamind.state.tables.activity_log import ActivityLogRow

# ---------------------------------------------------------------------------
# Sync fixture (table-shape and CHECK-constraint tests)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with the full schema + state-persistence tables."""
    import alphamind.state.tables  # noqa: F401

    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


# ---------------------------------------------------------------------------
# Async fixture (transactional InvocationContext + queries)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def async_engine_and_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Async engine + factory backed by a fresh on-disk SQLite DB.

    Tables are materialized via the sync engine, then the parent
    ``process_lifetimes`` row is seeded so any test that opens an
    ``InvocationContext`` has an FK target ready.  Stub positions, orders, and
    theses covering the position_ids / order_ids / thesis_ids used across the
    activity-log tests are seeded once here so every FK on ActivityLogRow
    resolves at COMMIT.
    """
    from tests.state._fk_substrate import (
        stub_bracket_row,
        stub_order_row,
        stub_position_row,
        stub_thesis_row,
    )

    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    sync_engine = make_engine(str(db_path))
    with make_session_factory(sync_engine)() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
        for pid in ("pos-1", "pos-2", "pos-3", "pos-A", "pos-B", "pos-C"):
            sess.add(stub_position_row(pid))
        sess.add(stub_thesis_row("thesis-1", "pos-1"))
        sess.add(stub_order_row("ord-1", "brk-stub-1"))
        sess.add(stub_order_row("brk-stub-1-entry", "brk-stub-1"))
        sess.add(stub_bracket_row("brk-stub-1", "pos-1", "brk-stub-1-entry"))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


def _make_invocation_record(
    invocation_id: str = "inv-2026-05-07T14:30:00Z-abcd",
    start_at: str = "2026-05-07T14:30:00Z",
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at=start_at,
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json='["pre-event"]',
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/resolved_config.json"
        ),
        feature_flags_snapshot_json='{"foo": true}',
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration_state.json"
        ),
        data_source_freshness_json='{"polygon": "2026-05-07T14:00:00Z"}',
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _entry(
    entry_id: str,
    invocation_id: str,
    timestamp: datetime,
    event_type: EventType,
    event_group: EventGroup,
    detail: Any,
    *,
    source: EventSource = EventSource.COMMAND_EXECUTOR,
    position_id: str | None = None,
    order_id: str | None = None,
    thesis_id: str | None = None,
) -> ActivityLogEntry:
    return ActivityLogEntry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=event_type,
        event_group=event_group,
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail=detail,
    )


def _position_opened_entry(
    *,
    entry_id: str,
    invocation_id: str,
    timestamp: datetime,
    position_id: str,
    thesis_id: str = "thesis-1",
    bracket_id: str = "bracket-1",
) -> ActivityLogEntry:
    detail = PositionOpenedDetail(
        ticker=Symbol("AAPL"),
        direction="long",
        fill_price=price("150.25"),
        quantity=100.0,
        thesis_id=thesis_id,
        bracket_id=bracket_id,
        mechanism=PositionOpenMechanism.ORDER_FILL,
        parent_position_id=None,
    )
    return _entry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.POSITION_OPENED,
        event_group=EventGroup.POSITION_LIFECYCLE,
        detail=detail,
        position_id=position_id,
        thesis_id=thesis_id,
        source=EventSource.FILL_PROCESSOR,
    )


def _pm_decision_entry(
    *,
    entry_id: str,
    invocation_id: str,
    timestamp: datetime,
    envelope_id: str = "env-1",
) -> ActivityLogEntry:
    detail = PMDecisionDetail(
        envelope_id=envelope_id,
        source_provenance_json={"agent": "analyst", "recommendation_id": "rec-1"},
        evaluation_json={"thesis_quality": "strong"},
        modifications_json=[],
        resulting_command_ids=("cmd-1",),
        verdict=PMVerdict.APPROVE,
        originating_proposal_json={},
    )
    return _entry(
        entry_id=entry_id,
        invocation_id=invocation_id,
        timestamp=timestamp,
        event_type=EventType.PM_DECISION,
        event_group=EventGroup.PM_DECISION,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Schema-shape tests (sync engine)
# ---------------------------------------------------------------------------


class TestActivityLogTable:
    def test_table_primary_key(self, engine: Engine) -> None:
        insp = inspect(engine)
        pk = insp.get_pk_constraint("activity_log")
        assert pk["constrained_columns"] == ["entry_id"]

    def test_table_has_expected_indexes(self, engine: Engine) -> None:
        insp = inspect(engine)
        indexes = {idx["name"]: idx for idx in insp.get_indexes("activity_log")}
        assert "ix_activity_log_invocation_id" in indexes
        assert indexes["ix_activity_log_invocation_id"]["column_names"] == ["invocation_id"]
        assert "ix_activity_log_event_type_invocation_id" in indexes
        assert indexes["ix_activity_log_event_type_invocation_id"]["column_names"] == [
            "event_type",
            "invocation_id",
        ]
        assert "ix_activity_log_position_id_entry_at" in indexes
        assert indexes["ix_activity_log_position_id_entry_at"]["column_names"] == [
            "position_id",
            "entry_at",
        ]

    def test_fk_blocks_orphan_invocation_id(self, session: Session) -> None:
        # No invocations row exists, so the FK must reject this insert.
        session.add(
            ActivityLogRow(
                entry_id="entry-1",
                invocation_id="missing",
                entry_at="2026-05-07T14:30:00Z",
                event_type=EventType.POSITION_OPENED.value,
                event_group=EventGroup.POSITION_LIFECYCLE.value,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR.value,
                detail_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_event_type(self, session: Session) -> None:
        session.add(
            ActivityLogRow(
                entry_id="entry-1",
                invocation_id="inv-1",
                entry_at="2026-05-07T14:30:00Z",
                event_type="UNKNOWN_EVENT_TYPE",
                event_group=EventGroup.POSITION_LIFECYCLE.value,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR.value,
                detail_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_event_group(self, session: Session) -> None:
        session.add(
            ActivityLogRow(
                entry_id="entry-1",
                invocation_id="inv-1",
                entry_at="2026-05-07T14:30:00Z",
                event_type=EventType.POSITION_OPENED.value,
                event_group="UNKNOWN_GROUP",
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.FILL_PROCESSOR.value,
                detail_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()

    def test_check_rejects_unknown_source(self, session: Session) -> None:
        session.add(
            ActivityLogRow(
                entry_id="entry-1",
                invocation_id="inv-1",
                entry_at="2026-05-07T14:30:00Z",
                event_type=EventType.POSITION_OPENED.value,
                event_group=EventGroup.POSITION_LIFECYCLE.value,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source="UNKNOWN_SOURCE",
                detail_json="{}",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# Round-trip codec — one detail class from each EventGroup
# ---------------------------------------------------------------------------


_T0 = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


def _all_event_group_entries(invocation_id: str) -> list[ActivityLogEntry]:
    """One entry per EventGroup, exercising at least one detail class each."""
    return [
        # POSITION_LIFECYCLE
        _position_opened_entry(
            entry_id="entry-pos",
            invocation_id=invocation_id,
            timestamp=_T0,
            position_id=PositionId("pos-1"),
        ),
        # ORDER_LIFECYCLE
        _entry(
            entry_id="entry-order",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.ORDER_FILLED,
            event_group=EventGroup.ORDER_LIFECYCLE,
            detail=OrderFilledDetail(
                fill_price=price("150.25"),
                fill_quantity=100.0,
                slippage=signed_money("0.05"),
                fees=money("1.25"),
            ),
            order_id=OrderId("ord-1"),
            source=EventSource.FILL_PROCESSOR,
        ),
        # BRACKET
        _entry(
            entry_id="entry-bracket",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.BRACKET_ACTIVATED,
            event_group=EventGroup.BRACKET,
            detail=BracketActivatedDetail(
                bracket_id=BracketId("bracket-1"),
                protective_leg_order_ids=("ord-stop", "ord-target"),
            ),
            source=EventSource.BRACKET_MANAGER,
        ),
        # THESIS
        _entry(
            entry_id="entry-thesis",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.THESIS_CREATED,
            event_group=EventGroup.THESIS,
            detail=ThesisCreatedDetail(
                thesis_id=ThesisId("thesis-1"), summary="Reversal at support"
            ),
            thesis_id=ThesisId("thesis-1"),
            source=EventSource.COMMAND_EXECUTOR,
        ),
        # CASH_AND_MARGIN
        _entry(
            entry_id="entry-cash",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.CASH_DEBITED,
            event_group=EventGroup.CASH_AND_MARGIN,
            detail=CashDebitedDetail(
                amount_usd=money("15025.0"),
                reason=CashDebitReason.ENTRY_FILL,
                new_balance_usd=signed_money("84975.0"),
            ),
            source=EventSource.FILL_PROCESSOR,
        ),
        # RISK_AND_GUARDRAIL
        _entry(
            entry_id="entry-guardrail",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.GUARDRAIL_REJECTION,
            event_group=EventGroup.RISK_AND_GUARDRAIL,
            detail=GuardrailRejectionDetail(
                command_summary="OPEN AAPL +200",
                blocking_rule_ids=("max-position-size",),
                current_limit_values_json={"max_position_size_usd": 10000.0},
                headroom_json={"max_position_size_usd": 0.0},
                suggested_modification="Reduce quantity to 50",
            ),
            source=EventSource.GUARDRAIL_LAYER,
        ),
        # PM_DECISION
        _pm_decision_entry(entry_id="entry-pm", invocation_id=invocation_id, timestamp=_T0),
        # PM_DECISION (command_abandoned, separate detail class)
        _entry(
            entry_id="entry-cmd-abandoned",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.COMMAND_ABANDONED,
            event_group=EventGroup.PM_DECISION,
            detail=CommandAbandonedDetail(
                envelope_id="env-1",
                command_id="cmd-1",
                originating_agent="analyst",
                command_type="OPEN",
                failure_reason="Alpaca unreachable after 3 retries",
                retry_attempt_count=3,
            ),
            source=EventSource.COMMAND_EXECUTOR,
        ),
        # CORPORATE_ACTION
        _entry(
            entry_id="entry-ca",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.CORPORATE_ACTION_APPLIED,
            event_group=EventGroup.CORPORATE_ACTION,
            detail=CorporateActionAppliedDetail(
                action_type=CorporateActionType.SPLIT,
                alpaca_activity_id="ca-1",
                ticker=Symbol("AAPL"),
                new_ticker=None,
                ratio_or_amount=2.0,
                pre_action_quantity=100.0,
                post_action_quantity=200.0,
                pre_action_cost_basis=money("150.0"),
                post_action_cost_basis=money("75.0"),
                signed_cash_impact_usd=signed_money("0.0"),
                parent_position_id=None,
                resulting_position_status="open",
            ),
            source=EventSource.CORPORATE_ACTION_PROCESSOR,
        ),
        # CONFIGURATION
        _entry(
            entry_id="entry-config",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.DISTILLATION_CONFIG_CHANGE,
            event_group=EventGroup.CONFIGURATION,
            detail=DistillationConfigChangeDetail(
                config_file="config/distillation.yaml",
                prior_hash="b" * 64,
                new_hash="c" * 64,
                changes=(
                    DistillationConfigChange(
                        key_path="anomaly_detection.volume_anomaly_sigma",
                        old_value=2.5,
                        new_value=2.75,
                    ),
                ),
                git_sha="a" * 40,
            ),
            source=EventSource.CONFIG_RELOAD,
        ),
        # RECONCILIATION
        _entry(
            entry_id="entry-recon",
            invocation_id=invocation_id,
            timestamp=_T0,
            event_type=EventType.RECONCILIATION_ALERT,
            event_group=EventGroup.RECONCILIATION,
            detail=ReconciliationAlertDetail(
                domain="position",
                field_name="share_count",
                local_value=10.0,
                alpaca_value=9.5,
                delta_description="AAPL: local share_count=10.0 vs Alpaca qty=9.5",
            ),
            position_id=PositionId("pos-1"),
            source=EventSource.CORPORATE_ACTION_PROCESSOR,
        ),
    ]


class TestActivityLogCodecRoundTrip:
    async def test_round_trip_each_event_group(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Every EventGroup has a representative entry that round-trips losslessly."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record()

        entries = _all_event_group_entries(invocation_id=record.invocation_id)
        # Cover every EventGroup the catalog defines.
        groups_covered = {e.event_group for e in entries}
        assert groups_covered == set(EventGroup)

        async with InvocationContext(session_factory=factory, record=record) as handle:
            for entry in entries:
                append_activity_log_entry(handle, entry)

        # Read back every entry and verify field-for-field equivalence.
        async with factory() as sess:
            rehydrated = await read_intra_invocation_changelog(sess, record.invocation_id)

        rehydrated_by_id = {e.entry_id: e for e in rehydrated}
        assert set(rehydrated_by_id) == {e.entry_id for e in entries}
        for original in entries:
            roundtripped = rehydrated_by_id[original.entry_id]
            assert roundtripped == original

    async def test_round_trip_continuous_monitor_event_types(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """ALP-123 continuous-monitor event types round-trip losslessly.

        Covers the four new ``RISK_AND_GUARDRAIL`` event types added in
        ALP-123: ``HALT_ACTIVATED``, ``HALT_LIFTED``, ``GREEKS_REFRESH_FAILED``,
        and ``EMERGENCY_INVOCATION_REQUESTED``. The schema CHECK constraint
        admits these via the migration ``b2c4f7a3d9e8``; this test guards both
        the migration acceptance and the per-detail-class JSON codec.
        """
        _, factory = async_engine_and_factory
        record = _make_invocation_record()

        entries = [
            _entry(
                entry_id="entry-halt-activated",
                invocation_id=record.invocation_id,
                timestamp=_T0,
                event_type=EventType.HALT_ACTIVATED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                detail=HaltActivatedDetail(
                    halt_type="daily_drawdown",
                    current_drawdown_pct=4.5,
                    limit_pct=4.0,
                    detected_at=_T0,
                ),
                source=EventSource.GUARDRAIL_LAYER,
            ),
            _entry(
                entry_id="entry-halt-lifted",
                invocation_id=record.invocation_id,
                timestamp=_T0,
                event_type=EventType.HALT_LIFTED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                detail=HaltLiftedDetail(
                    halt_type="cumulative_drawdown_tier3",
                    current_drawdown_pct=8.5,
                    lifted_at=_T0,
                ),
                source=EventSource.GUARDRAIL_LAYER,
            ),
            _entry(
                entry_id="entry-greeks-failed",
                invocation_id=record.invocation_id,
                timestamp=_T0,
                event_type=EventType.GREEKS_REFRESH_FAILED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                detail=GreeksRefreshFailedDetail(
                    underlying_ticker=Symbol("AAPL"),
                    occ_symbol="O:AAPL260619C00150000",
                    failure_reason="iv_fetch_no_row",
                    prior_as_of=_T0,
                ),
                position_id=PositionId("pos-1"),
                source=EventSource.GUARDRAIL_LAYER,
            ),
            _entry(
                entry_id="entry-emergency",
                invocation_id=record.invocation_id,
                timestamp=_T0,
                event_type=EventType.EMERGENCY_INVOCATION_REQUESTED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                detail=EmergencyInvocationRequestedDetail(
                    trigger_type="regime_jump",
                    trigger_reason="NORMAL → CRISIS",
                    cooldown_remaining_seconds=0,
                ),
                source=EventSource.GUARDRAIL_LAYER,
            ),
        ]

        async with InvocationContext(session_factory=factory, record=record) as handle:
            for entry in entries:
                append_activity_log_entry(handle, entry)

        async with factory() as sess:
            rehydrated = await read_intra_invocation_changelog(sess, record.invocation_id)

        rehydrated_by_id = {e.entry_id: e for e in rehydrated}
        for original in entries:
            roundtripped = rehydrated_by_id[original.entry_id]
            assert roundtripped == original, (
                f"round-trip mismatch for {original.event_type.value}: "
                f"got {roundtripped!r}, expected {original!r}"
            )


# ---------------------------------------------------------------------------
# append_activity_log_entry behavior
# ---------------------------------------------------------------------------


class TestAppendActivityLogEntry:
    async def test_rejects_invocation_id_mismatch(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        record = _make_invocation_record()

        async with InvocationContext(session_factory=factory, record=record) as handle:
            wrong_entry = _position_opened_entry(
                entry_id="entry-mismatch",
                invocation_id="inv-other",
                timestamp=_T0,
                position_id=PositionId("pos-1"),
            )
            with pytest.raises(ValueError, match="invocation_id"):
                append_activity_log_entry(handle, wrong_entry)

    async def test_emission_rolls_back_when_context_raises(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """An exception escaping ``InvocationContext`` rolls back appended entries."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record(invocation_id="inv-rollback")

        class _BoomError(RuntimeError):
            pass

        with pytest.raises(_BoomError):
            async with InvocationContext(session_factory=factory, record=record) as handle:
                entry = _position_opened_entry(
                    entry_id="entry-rolled-back",
                    invocation_id="inv-rollback",
                    timestamp=_T0,
                    position_id=PositionId("pos-1"),
                )
                append_activity_log_entry(handle, entry)
                raise _BoomError("simulated downstream failure")

        # Neither the parent invocation row nor the activity_log entry persisted.
        async with factory() as sess:
            result = await sess.execute(
                select(ActivityLogRow).where(ActivityLogRow.entry_id == "entry-rolled-back")
            )
            assert result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Read APIs
# ---------------------------------------------------------------------------


class TestReadIntraInvocationChangelog:
    async def test_returns_only_matching_invocation_ordered_ascending(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        rec_a = _make_invocation_record(invocation_id="inv-a", start_at="2026-05-07T14:30:00Z")
        rec_b = _make_invocation_record(invocation_id="inv-b", start_at="2026-05-07T15:00:00Z")

        # Two entries on inv-a (out-of-order by timestamp) and one on inv-b.
        e_a_late = _position_opened_entry(
            entry_id="entry-a-late",
            invocation_id="inv-a",
            timestamp=datetime(2026, 5, 7, 14, 32, 0, tzinfo=UTC),
            position_id=PositionId("pos-1"),
        )
        e_a_early = _position_opened_entry(
            entry_id="entry-a-early",
            invocation_id="inv-a",
            timestamp=datetime(2026, 5, 7, 14, 31, 0, tzinfo=UTC),
            position_id=PositionId("pos-2"),
        )
        e_b = _position_opened_entry(
            entry_id="entry-b",
            invocation_id="inv-b",
            timestamp=datetime(2026, 5, 7, 15, 1, 0, tzinfo=UTC),
            position_id=PositionId("pos-3"),
        )

        async with InvocationContext(session_factory=factory, record=rec_a) as h:
            # Insert late then early — the read API must order by entry_at.
            append_activity_log_entry(h, e_a_late)
            append_activity_log_entry(h, e_a_early)
        async with InvocationContext(session_factory=factory, record=rec_b) as h:
            append_activity_log_entry(h, e_b)

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-a")

        assert [r.entry_id for r in rows] == ["entry-a-early", "entry-a-late"]
        assert all(r.invocation_id == "inv-a" for r in rows)

    async def test_empty_when_no_entries(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-empty")
        assert rows == ()


class TestReadRecentPmDecisionLog:
    async def test_returns_only_pm_decision_entries_within_sliding_window(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        # Three invocations, one PM decision in each + a non-PM entry in inv-1.
        records = [
            _make_invocation_record(
                invocation_id=f"inv-{i}",
                start_at=f"2026-05-07T14:{30 + i}:00Z",
            )
            for i in range(3)
        ]
        for i, rec in enumerate(records):
            async with InvocationContext(session_factory=factory, record=rec) as h:
                append_activity_log_entry(
                    h,
                    _pm_decision_entry(
                        entry_id=f"pm-{i}",
                        invocation_id=rec.invocation_id,
                        timestamp=datetime(2026, 5, 7, 14, 30 + i, 0, tzinfo=UTC),
                        envelope_id=f"env-{i}",
                    ),
                )
                if i == 1:
                    # Non-PM entry mingled in — must NOT appear in the result.
                    append_activity_log_entry(
                        h,
                        _position_opened_entry(
                            entry_id="entry-non-pm",
                            invocation_id=rec.invocation_id,
                            timestamp=datetime(2026, 5, 7, 14, 31, 30, tzinfo=UTC),
                            position_id=PositionId("pos-1"),
                        ),
                    )

        async with factory() as sess:
            # Sliding window of 2 — should include the most recent two
            # invocations' PM decisions.
            rows = await read_recent_pm_decision_log(sess, sliding_window_invocations=2)

        ids = sorted(r.entry_id for r in rows)
        assert ids == ["pm-1", "pm-2"]
        assert all(r.event_type == EventType.PM_DECISION for r in rows)

    async def test_window_larger_than_history_returns_all(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        rec = _make_invocation_record()
        async with InvocationContext(session_factory=factory, record=rec) as h:
            append_activity_log_entry(
                h,
                _pm_decision_entry(
                    entry_id="pm-only",
                    invocation_id=rec.invocation_id,
                    timestamp=_T0,
                ),
            )

        async with factory() as sess:
            rows = await read_recent_pm_decision_log(sess, sliding_window_invocations=10)

        assert [r.entry_id for r in rows] == ["pm-only"]


class TestReadPositionModificationTrail:
    async def test_groups_by_position_in_chronological_order(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        rec = _make_invocation_record()

        e1 = _position_opened_entry(
            entry_id="e1",
            invocation_id=rec.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC),
            position_id=PositionId("pos-A"),
        )
        e2 = _position_opened_entry(
            entry_id="e2",
            invocation_id=rec.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 32, 0, tzinfo=UTC),
            position_id=PositionId("pos-A"),
        )
        e3 = _position_opened_entry(
            entry_id="e3",
            invocation_id=rec.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 31, 0, tzinfo=UTC),
            position_id=PositionId("pos-A"),
        )
        e_b = _position_opened_entry(
            entry_id="e_b",
            invocation_id=rec.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 30, 30, tzinfo=UTC),
            position_id=PositionId("pos-B"),
        )
        e_other = _position_opened_entry(
            entry_id="e_other",
            invocation_id=rec.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC),
            position_id=PositionId("pos-C"),
        )

        async with InvocationContext(session_factory=factory, record=rec) as h:
            for entry in (e1, e2, e3, e_b, e_other):
                append_activity_log_entry(h, entry)

        async with factory() as sess:
            trail = await read_position_modification_trail(sess, ["pos-A", "pos-B"])

        assert set(trail) == {"pos-A", "pos-B"}
        assert [e.entry_id for e in trail["pos-A"]] == ["e1", "e3", "e2"]
        assert [e.entry_id for e in trail["pos-B"]] == ["e_b"]

    async def test_unknown_position_id_returns_empty_tuple(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        async with factory() as sess:
            trail = await read_position_modification_trail(sess, ["pos-missing"])
        assert trail == {"pos-missing": ()}


class TestReadMostRecentConfigChangeNewHash:
    async def test_returns_most_recent_matching_hash(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        rec0 = _make_invocation_record(invocation_id="inv-0", start_at="2026-05-07T13:00:00Z")
        rec1 = _make_invocation_record(invocation_id="inv-1", start_at="2026-05-07T14:00:00Z")

        first = _entry(
            entry_id="cfg-first",
            invocation_id=rec0.invocation_id,
            timestamp=datetime(2026, 5, 7, 13, 0, 0, tzinfo=UTC),
            event_type=EventType.DISTILLATION_CONFIG_CHANGE,
            event_group=EventGroup.CONFIGURATION,
            detail=DistillationConfigChangeDetail(
                config_file="config/distillation.yaml",
                prior_hash=None,
                new_hash="hash-v1",
                changes=(),
                git_sha="a" * 40,
            ),
            source=EventSource.CONFIG_RELOAD,
        )
        second = _entry(
            entry_id="cfg-second",
            invocation_id=rec1.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 0, 0, tzinfo=UTC),
            event_type=EventType.DISTILLATION_CONFIG_CHANGE,
            event_group=EventGroup.CONFIGURATION,
            detail=DistillationConfigChangeDetail(
                config_file="config/distillation.yaml",
                prior_hash="hash-v1",
                new_hash="hash-v2",
                changes=(
                    DistillationConfigChange(
                        key_path="anomaly_detection.volume_anomaly_sigma",
                        old_value=2.5,
                        new_value=3.0,
                    ),
                ),
                git_sha="b" * 40,
            ),
            source=EventSource.CONFIG_RELOAD,
        )
        # An unrelated config_file event must NOT shadow the queried one.
        unrelated = _entry(
            entry_id="cfg-other",
            invocation_id=rec1.invocation_id,
            timestamp=datetime(2026, 5, 7, 14, 5, 0, tzinfo=UTC),
            event_type=EventType.DISTILLATION_CONFIG_CHANGE,
            event_group=EventGroup.CONFIGURATION,
            detail=DistillationConfigChangeDetail(
                config_file="config/other.yaml",
                prior_hash=None,
                new_hash="hash-other",
                changes=(),
                git_sha="c" * 40,
            ),
            source=EventSource.CONFIG_RELOAD,
        )

        async with InvocationContext(session_factory=factory, record=rec0) as h:
            append_activity_log_entry(h, first)
        async with InvocationContext(session_factory=factory, record=rec1) as h:
            append_activity_log_entry(h, second)
            append_activity_log_entry(h, unrelated)

        async with factory() as sess:
            new_hash = await read_most_recent_config_change_new_hash(
                sess, "config/distillation.yaml"
            )
        assert new_hash == "hash-v2"

    async def test_returns_none_when_no_matching_entry(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        _, factory = async_engine_and_factory
        async with factory() as sess:
            new_hash = await read_most_recent_config_change_new_hash(
                sess, "config/distillation.yaml"
            )
        assert new_hash is None

    async def test_sql_side_filter_skips_unrelated_config_files(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """The query must filter by ``config_file`` at the SQL layer rather
        than decoding every history row in Python.

        Builds a 100-entry history dominated by one unrelated ``config_file``
        with a single matching row near the head and one near the tail; the
        query must surface the most-recent matching entry without scanning
        and decoding every other row.
        """
        _, factory = async_engine_and_factory
        rec = _make_invocation_record(invocation_id="inv-bulk", start_at="2026-05-07T13:00:00Z")

        async with InvocationContext(session_factory=factory, record=rec) as h:
            # Earliest matching entry (will be shadowed by the later one).
            append_activity_log_entry(
                h,
                _entry(
                    entry_id="cfg-target-early",
                    invocation_id=rec.invocation_id,
                    timestamp=datetime(2026, 5, 7, 13, 0, 0, tzinfo=UTC),
                    event_type=EventType.DISTILLATION_CONFIG_CHANGE,
                    event_group=EventGroup.CONFIGURATION,
                    detail=DistillationConfigChangeDetail(
                        config_file="config/distillation.yaml",
                        prior_hash=None,
                        new_hash="target-early",
                        changes=(),
                        git_sha="a" * 40,
                    ),
                    source=EventSource.CONFIG_RELOAD,
                ),
            )
            # 100 unrelated entries with a different config_file.
            for i in range(100):
                append_activity_log_entry(
                    h,
                    _entry(
                        entry_id=f"cfg-other-{i:03d}",
                        invocation_id=rec.invocation_id,
                        timestamp=datetime(2026, 5, 7, 13, 1 + i // 60, i % 60, tzinfo=UTC),
                        event_type=EventType.DISTILLATION_CONFIG_CHANGE,
                        event_group=EventGroup.CONFIGURATION,
                        detail=DistillationConfigChangeDetail(
                            config_file="config/other.yaml",
                            prior_hash=None,
                            new_hash=f"other-{i:03d}",
                            changes=(),
                            git_sha="b" * 40,
                        ),
                        source=EventSource.CONFIG_RELOAD,
                    ),
                )
            # Most-recent matching entry — the one the query should return.
            append_activity_log_entry(
                h,
                _entry(
                    entry_id="cfg-target-late",
                    invocation_id=rec.invocation_id,
                    timestamp=datetime(2026, 5, 7, 14, 0, 0, tzinfo=UTC),
                    event_type=EventType.DISTILLATION_CONFIG_CHANGE,
                    event_group=EventGroup.CONFIGURATION,
                    detail=DistillationConfigChangeDetail(
                        config_file="config/distillation.yaml",
                        prior_hash="target-early",
                        new_hash="target-late",
                        changes=(),
                        git_sha="c" * 40,
                    ),
                    source=EventSource.CONFIG_RELOAD,
                ),
            )

        async with factory() as sess:
            new_hash = await read_most_recent_config_change_new_hash(
                sess, "config/distillation.yaml"
            )
        assert new_hash == "target-late"
