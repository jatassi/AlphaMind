"""Tests for ``alphamind.scheduler.phase2_dispatch.dispatch_phase2`` (ALP-449).

The dispatcher iterates the PM result's ``submission_log`` and persists each
envelope's outcome via the Phase 2 write path — **each envelope in its own
transaction** per the design's "each command's mutations commit atomically"
guarantee
(``docs/design/05-execution-layer/state-persistence.md`` § Phase 2 write
path). Aggregates accepted / rejected counts into the
:class:`Phase2Summary` the orchestrator records. Per the parent issue's
fail-closed invariant, any submission exception propagates so the
in-flight envelope's transaction rolls back — earlier envelopes' commits
stand.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# Side-effect import to break the submit_envelope_mcp ↔ portfolio_manager
# cycle: importing PMEnvelope first ensures portfolio_manager's runner and
# harness load before submit_envelope_mcp re-enters them.
from alphamind.decision.portfolio_manager.models import PMEnvelope  # noqa: F401
from alphamind.decision.portfolio_manager.submit_envelope import (
    Acknowledgment,
    SubmissionLogEntry,
    SubmissionResult,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-p2-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-p2-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


@pytest.fixture
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory bound to an initialized SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _make_state_persistence_config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/pip-freeze",
            "invocation_provenance_root": "/tmp/provenance",
        }
    )


_INVOCATION_ID = "inv-x"


def _make_acknowledgment() -> Acknowledgment:
    return Acknowledgment()


def _make_submission_result(
    *, command_ordinal: int, command_id: str, status: str
) -> SubmissionResult:
    if status == "accepted":
        return SubmissionResult(
            command_ordinal=command_ordinal,
            status="accepted",
            command_id=command_id,
            acknowledgment=_make_acknowledgment(),
        )
    return SubmissionResult(
        command_ordinal=command_ordinal,
        status="rejected",
        command_id=command_id,
    )


def _make_pm_result(submission_log: tuple[SubmissionLogEntry, ...]) -> Any:
    """Lightweight stand-in carrying ``submission_log``; the dispatcher only
    reads the one attribute so we avoid the full PMResult construction
    boilerplate."""
    from types import SimpleNamespace

    return SimpleNamespace(submission_log=submission_log)


class _StubEnvelope:
    """Minimal envelope shape for the test — only needs identity."""

    def __init__(self, envelope_id: str = "ENV-REC-1") -> None:
        self.envelope_id = envelope_id


class TestDispatchPhase2:
    async def test_empty_submission_log_returns_zero_counts(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No PM envelopes submitted → ``Phase2Summary(0, 0)``; persistence not invoked."""
        from alphamind.scheduler.phase2_dispatch import (
            Phase2Summary,
            dispatch_phase2,
        )

        summary = await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=()),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert summary == Phase2Summary(commands_submitted=0, commands_rejected=0)

    async def test_aggregates_accepted_and_rejected_counts(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One envelope with two accepted + one rejected commands → 2 submitted, 1 rejected."""
        from alphamind.scheduler import phase2_dispatch as module

        persist_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(module, "persist_envelope_outcome", persist_mock)

        envelope = _StubEnvelope("ENV-REC-1")
        submission_results = (
            _make_submission_result(command_ordinal=0, command_id="cmd-a", status="accepted"),
            _make_submission_result(command_ordinal=1, command_id="cmd-b", status="accepted"),
            _make_submission_result(command_ordinal=2, command_id="cmd-c", status="rejected"),
        )
        entry = SubmissionLogEntry(
            envelope=cast(Any, envelope), submission_results=submission_results
        )
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        summary = await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=(entry,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert summary.commands_submitted == 2
        assert summary.commands_rejected == 1
        persist_mock.assert_awaited_once()

    async def test_persistence_exception_propagates(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An exception from ``persist_envelope_outcome`` propagates."""
        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        async def _raising_persist(*args: object, **kwargs: object) -> None:
            msg = "phase 2 write failed"
            raise RuntimeError(msg)

        monkeypatch.setattr(module, "persist_envelope_outcome", _raising_persist)

        envelope = _StubEnvelope("ENV-REC-1")
        results = (
            _make_submission_result(command_ordinal=0, command_id="cmd-a", status="accepted"),
        )
        entry = SubmissionLogEntry(envelope=cast(Any, envelope), submission_results=results)

        with pytest.raises(RuntimeError, match="phase 2 write failed"):
            await dispatch_phase2(
                session_factory=async_factory,
                invocation_id=_INVOCATION_ID,
                pm_result=_make_pm_result(submission_log=(entry,)),
                state_persistence_config=_make_state_persistence_config(),
            )

    async def test_multiple_envelopes_each_persisted(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each ``SubmissionLogEntry`` triggers one ``persist_envelope_outcome``."""
        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        persist_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(module, "persist_envelope_outcome", persist_mock)

        entries = (
            SubmissionLogEntry(
                envelope=cast(Any, _StubEnvelope("ENV-REC-1")),
                submission_results=(
                    _make_submission_result(
                        command_ordinal=0, command_id="cmd-a", status="accepted"
                    ),
                ),
            ),
            SubmissionLogEntry(
                envelope=cast(Any, _StubEnvelope("ENV-REC-2")),
                submission_results=(
                    _make_submission_result(
                        command_ordinal=0, command_id="cmd-b", status="rejected"
                    ),
                ),
            ),
        )

        summary = await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=entries),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert persist_mock.await_count == 2
        assert summary.commands_submitted == 1
        assert summary.commands_rejected == 1

    async def test_dispatch_results_forwarded_to_persist_envelope_outcome(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per ALP-711 scope (C): each ``SubmissionLogEntry``'s
        ``dispatch_results`` flows verbatim into ``persist_envelope_outcome``
        so the writeback persists the broker's real ``alpaca_order_id``
        instead of falling back to ``alp-{order_id}`` synthetic placeholders.
        """
        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        captured_dispatch_results: list[Any] = []

        async def _capture_persist(
            handle: Any,
            envelope: Any,
            results: Any,
            *,
            config: Any,
            dispatch_results: Any = None,
        ) -> None:
            captured_dispatch_results.append(dispatch_results)

        monkeypatch.setattr(module, "persist_envelope_outcome", _capture_persist)

        # Sentinel "BrokerDispatchResult" payload — the dispatcher's only
        # contract with downstream callers is its ``alpaca_order_id``
        # attribute; the type annotation in ``SubmissionLogEntry`` is
        # ``Any`` so a sentinel suffices to verify forwarding.
        class _Sentinel:
            alpaca_order_id = "real-alpaca-uuid-abc"

        sentinel = _Sentinel()
        envelope = _StubEnvelope("ENV-REC-1")
        submission_results = (
            _make_submission_result(command_ordinal=0, command_id="cmd-a", status="accepted"),
        )
        entry = SubmissionLogEntry(
            envelope=cast(Any, envelope),
            submission_results=submission_results,
            dispatch_results=(sentinel,),
        )

        await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=(entry,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert captured_dispatch_results == [(sentinel,)]

    async def test_dispatch_results_default_none_for_legacy_entries(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``SubmissionLogEntry.dispatch_results`` defaults to ``None`` for
        callers that don't run broker routing (debug-e2e / non-prod log-only
        path). ``dispatch_phase2`` forwards ``None`` so the synthetic-ID
        fallback in ``persist_envelope_outcome`` still fires for those runs.
        """
        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        captured_dispatch_results: list[Any] = []

        async def _capture_persist(
            handle: Any,
            envelope: Any,
            results: Any,
            *,
            config: Any,
            dispatch_results: Any = None,
        ) -> None:
            captured_dispatch_results.append(dispatch_results)

        monkeypatch.setattr(module, "persist_envelope_outcome", _capture_persist)

        envelope = _StubEnvelope("ENV-REC-1")
        submission_results = (
            _make_submission_result(command_ordinal=0, command_id="cmd-a", status="accepted"),
        )
        entry = SubmissionLogEntry(
            envelope=cast(Any, envelope),
            submission_results=submission_results,
        )

        await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=(entry,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert captured_dispatch_results == [None]

    async def test_abandoned_entries_emit_command_abandoned_rows(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ALP-711 — abandoned entries on the log entry trigger one
        ``COMMAND_ABANDONED`` activity-log row per entry. The orchestrator's
        PM-submit path uses ``defer_writeback=True`` so the submit_envelope
        wrapper's in-tool emit is suppressed; dispatch_phase2 takes over and
        emits the abandoned-command audit trail from the data carried on
        ``SubmissionLogEntry.abandoned_entries``.
        """
        from types import SimpleNamespace

        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        persist_envelope_mock = AsyncMock(return_value=None)
        abandoned_calls: list[dict[str, Any]] = []

        async def _capture_abandoned(handle: Any, **kwargs: Any) -> None:
            abandoned_calls.append(kwargs)

        monkeypatch.setattr(module, "persist_envelope_outcome", persist_envelope_mock)
        monkeypatch.setattr(module, "persist_command_abandoned", _capture_abandoned)

        envelope = SimpleNamespace(
            envelope_id="ENV-REC-1",
            source_provenance="strategist",
        )
        submission_results = (
            _make_submission_result(command_ordinal=0, command_id="cmd-a", status="rejected"),
        )
        abandoned_entry = SimpleNamespace(
            command_id="cmd-a",
            command_type="OPEN",
            failure_reason="gateway_submission_failed: timeout",
            retry_attempt_count=3,
        )
        entry = SubmissionLogEntry(
            envelope=cast(Any, envelope),
            submission_results=submission_results,
            abandoned_entries=(abandoned_entry,),
        )

        await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=(entry,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert len(abandoned_calls) == 1
        call = abandoned_calls[0]
        assert call["envelope_id"] == "ENV-REC-1"
        assert call["command_id"] == "cmd-a"
        assert call["command_type"] == "OPEN"
        assert call["failure_reason"] == "gateway_submission_failed: timeout"
        assert call["retry_attempt_count"] == 3

    async def test_each_envelope_runs_in_its_own_transaction(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each envelope's persist call receives a distinct session.

        Per ALP-449 Slice 3, ``dispatch_phase2`` opens a fresh session per
        envelope so that envelope-N abort cannot roll back envelope
        0..N-1's writes (matches the design's "each command's mutations
        commit atomically" guarantee).
        """
        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        captured_sessions: list[AsyncSession] = []

        async def _capture_session_persist(
            handle: Any,
            envelope: Any,
            results: Any,
            *,
            config: Any,
            dispatch_results: Any = None,
        ) -> None:
            captured_sessions.append(handle.session)

        monkeypatch.setattr(module, "persist_envelope_outcome", _capture_session_persist)

        entries = tuple(
            SubmissionLogEntry(
                envelope=cast(Any, _StubEnvelope(f"ENV-REC-{i}")),
                submission_results=(
                    _make_submission_result(
                        command_ordinal=0, command_id=f"cmd-{i}", status="accepted"
                    ),
                ),
            )
            for i in range(3)
        )

        await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INVOCATION_ID,
            pm_result=_make_pm_result(submission_log=entries),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert len(captured_sessions) == 3
        # Each envelope sees its own session — three distinct identities.
        assert len({id(s) for s in captured_sessions}) == 3

    async def test_mid_batch_failure_leaves_earlier_envelopes_committed(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Per-envelope commits: envelope-N failure does not unwind 0..N-1.

        Drives the stub to write a marker row via each envelope's session;
        on the third envelope the stub writes then raises. The first two
        markers must persist (visible to a fresh session post-run); the
        third's write rolls back with its session.
        """
        from sqlalchemy import select, text

        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2
        from alphamind.state.invocation_context.context import (
            insert_invocation_row,
        )
        from alphamind.state.invocation_context.records import (
            InvocationRecord,
        )
        from alphamind.state.tables.invocations import InvocationRow

        # Seed an invocation row that the per-envelope stubs can update as
        # their "marker" — the stub writes ``staleness_flag = <envelope_idx>``
        # to prove the per-envelope session reached commit.
        record = InvocationRecord(
            invocation_id=_INVOCATION_ID,
            process_lifetime_id="proc-p2-1",
            start_at="2026-05-07T14:30:00Z",
            phase1_completed_at="2026-05-07T14:30:01Z",
            phase2_completed_at=None,
            trigger_type="manual",
            trigger_source="cli",
            trigger_reason="test",
            git_sha_at_invocation="a" * 40,
            active_profile="medium",
            active_regime="normal",
            active_mode="normal",
            active_overlays_json="[]",
            resolved_config_hash="0" * 64,
            resolved_config_snapshot_path="/tmp/r.json",
            feature_flags_snapshot_json="{}",
            data_calibration_state_snapshot_path="/tmp/c.json",
            data_source_freshness_json="{}",
            fill_collection_summary_json=None,
            command_execution_summary_json=None,
            staleness_flag=None,
            snapshot_metadata_json=None,
        )
        await insert_invocation_row(async_factory, record)

        async def _marker_persist(
            handle: Any,
            envelope: Any,
            results: Any,
            *,
            config: Any,
            dispatch_results: Any = None,
        ) -> None:
            # Stamp the row's command_execution_summary_json with the
            # current envelope_id as a marker for "this envelope's session
            # reached persist". Each envelope sees a fresh session; on
            # commit, the value lands.
            envelope_idx = int(envelope.envelope_id.rsplit("-", 1)[-1])
            await handle.session.execute(
                text(
                    "UPDATE invocations SET command_execution_summary_json = :v "
                    "WHERE invocation_id = :iid"
                ),
                {"v": str(envelope_idx), "iid": _INVOCATION_ID},
            )
            if envelope_idx == 2:
                raise RuntimeError("simulated failure on envelope 2")

        monkeypatch.setattr(module, "persist_envelope_outcome", _marker_persist)

        entries = tuple(
            SubmissionLogEntry(
                envelope=cast(Any, _StubEnvelope(f"ENV-REC-{i}")),
                submission_results=(
                    _make_submission_result(
                        command_ordinal=0, command_id=f"cmd-{i}", status="accepted"
                    ),
                ),
            )
            for i in range(4)
        )

        with pytest.raises(RuntimeError, match="simulated failure on envelope 2"):
            await dispatch_phase2(
                session_factory=async_factory,
                invocation_id=_INVOCATION_ID,
                pm_result=_make_pm_result(submission_log=entries),
                state_persistence_config=_make_state_persistence_config(),
            )

        async with async_factory() as session:
            row = (
                await session.execute(
                    select(InvocationRow).where(InvocationRow.invocation_id == _INVOCATION_ID)
                )
            ).scalar_one()

        # Envelope 1's update is the latest commit before envelope 2 raised.
        # Envelope 2's update rolled back (was in its own transaction).
        # Envelope 3 never ran.
        assert row.command_execution_summary_json == "1"


_INV_IDEMPOTENT = "inv-2026-05-05"


async def _seed_for_real_writeback(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed cash ledger + invocation row so a real ``persist_envelope_outcome``
    (capital reservation + pm_decision) can commit against ``factory``."""
    from datetime import UTC, datetime

    from alphamind.portfolio_state.records.cash import CashLedger
    from alphamind.state.invocation_context.records import (
        InvocationRecord,
        invocation_record_to_row,
    )
    from alphamind.state.tables.cash_ledger_codec import cash_ledger_record_to_row

    now = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC)
    cash = CashLedger(
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
    inv = InvocationRecord(
        invocation_id=_INV_IDEMPOTENT,
        process_lifetime_id="proc-p2-1",
        start_at=now.isoformat().replace("+00:00", "Z"),
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
        resolved_config_snapshot_path="/tmp/r.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/tmp/c.json",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )
    async with factory() as sess:
        sess.add(cash_ledger_record_to_row(cash, last_updated_at=now))
        sess.add(invocation_record_to_row(inv))
        await sess.commit()


async def _persist_one_envelope_in_turn(
    factory: async_sessionmaker[AsyncSession],
) -> SubmissionLogEntry:
    """Run the in-turn (broker-active) writeback once — mirrors what the PM turn
    does in production — and return the resulting ``SubmissionLogEntry`` for
    ``dispatch_phase2`` to re-process. The envelope is fully persisted +
    committed by the time this returns."""
    import uuid
    from unittest.mock import MagicMock

    from alphamind._kernel.ids import AlpacaOrderId, ClientOrderId
    from alphamind._kernel.money import price
    from alphamind.commands.command_models import EntryOrder, OMSCommand
    from alphamind.decision.portfolio_manager.submit_envelope import (
        _handle_submit_envelope,
        build_initial_submit_envelope_state,
    )
    from alphamind.state.invocation_context.context import InvocationHandle
    from tests.execution.oms.test_engine_stub_broker_routing import _default_execution_config
    from tests.execution.oms.test_submit_envelope_mcp import (
        _DEFAULT_ACTIVE_SECTORS,
        _make_analyst_envelope,
        _make_bundle,
        _make_pm_view,
        _make_validation_state,
        _open_command,
        _recommendation_stub,
        _retrieval_store,
        _sector_resolver,
        _state_persistence_config,
    )

    class _Dispatch:
        async def __call__(
            self, command: OMSCommand, *, client_order_id: str, **context: Any
        ) -> Any:
            from alphamind.execution.broker_adapter import EquitySubmission, Submitted
            from alphamind.execution.oms.broker_dispatch import BrokerDispatchResult

            oid = AlpacaOrderId(str(uuid.uuid4()))
            return Submitted(
                payload=BrokerDispatchResult(
                    alpaca_order_id=oid,
                    client_order_id=ClientOrderId(client_order_id),
                    status="accepted",
                    order_class="simple",
                    payload_kind="equity",
                    raw_submission=EquitySubmission(
                        alpaca_order_id=oid,
                        client_order_id=ClientOrderId(client_order_id),
                        status="accepted",
                        order_class="simple",
                    ),
                ),
                attempt_count=1,
            )

    envelope = _make_analyst_envelope(
        commands=(
            _open_command(
                entry_order=EntryOrder(type="limit", limit_price=price(1000.0), stop_price=None)
            ),
        )
    )
    validation_state = _make_validation_state()
    state = build_initial_submit_envelope_state(
        invocation_id=validation_state.invocation_id,
        starting_validation_state=validation_state,
    )
    async with factory() as turn_session:
        handle = InvocationHandle(session=turn_session, invocation_id=_INV_IDEMPOTENT)
        _response, new_state = await _handle_submit_envelope(
            envelope.model_dump(mode="json"),
            state=state,
            retrieval_store=_retrieval_store(),
            pre_processor_bundle=_make_bundle(recommendations=(_recommendation_stub("REC-1"),)),
            pm_view=_make_pm_view(),
            active_sectors=_DEFAULT_ACTIVE_SECTORS,
            halt_mode=False,
            sector_resolver=_sector_resolver,
            state_persistence_config=_state_persistence_config(),
            invocation_handle=handle,
            client=MagicMock(),
            queries=MagicMock(),
            execution_config=_default_execution_config(),
            broker_dispatch=_Dispatch(),
            defer_writeback=True,
        )
    (log_entry,) = new_state.submission_log
    return log_entry


class TestDispatchPhase2Idempotency:
    """ALP-763 — ``dispatch_phase2`` skips an envelope already persisted by the
    in-turn (broker-active) writeback: no double-write, no double capital
    reservation, no duplicate ``pm_decision``/audit emits — while still counting
    the summary and emitting ``command_abandoned`` for abandoned entries."""

    async def test_already_persisted_envelope_is_not_written_twice(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from sqlalchemy import func, select

        from alphamind.scheduler.phase2_dispatch import dispatch_phase2
        from alphamind.state.tables.activity_log import ActivityLogRow
        from alphamind.state.tables.cash_ledger import (
            CASH_LEDGER_SINGLETON_ID,
            CashLedgerRow,
        )
        from alphamind.state.tables.orders import OrderRow

        await _seed_for_real_writeback(async_factory)
        log_entry = await _persist_one_envelope_in_turn(async_factory)

        async def _snapshot() -> tuple[int, int, float]:
            async with async_factory() as sess:
                orders = (
                    await sess.execute(select(func.count()).select_from(OrderRow))
                ).scalar_one()
                logs = (
                    await sess.execute(select(func.count()).select_from(ActivityLogRow))
                ).scalar_one()
                cash = await sess.get(CashLedgerRow, CASH_LEDGER_SINGLETON_ID)
                assert cash is not None
                return int(orders), int(logs), float(cash.reserved_capital_usd)

        before = await _snapshot()
        assert before[0] > 0  # the in-turn writeback wrote order rows
        assert before[2] > 0.0  # and reserved capital once

        summary = await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INV_IDEMPOTENT,
            pm_result=_make_pm_result(submission_log=(log_entry,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        after = await _snapshot()
        # No second INSERT, no second reservation, no duplicate audit emits.
        assert after == before
        # Summary counts still reflect the envelope's accepted command.
        assert summary.commands_submitted == 1
        assert summary.commands_rejected == 0

    async def test_skip_does_not_re_emit_command_abandoned(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A skipped (already-persisted) envelope emits NO ``command_abandoned``.

        On the broker-active in-turn path, ``submit_envelope`` Step 6 already
        emitted one ``COMMAND_ABANDONED`` row per abandoned entry (and committed
        it) alongside the order/pm_decision graph. ``dispatch_phase2`` detects the
        in-turn writeback (``already_persisted``) and must NOT re-emit those rows,
        else every broker-dispatch failure on the production path would produce two
        identical audit rows (ALP-763 review finding)."""
        from types import SimpleNamespace

        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        await _seed_for_real_writeback(async_factory)
        log_entry = await _persist_one_envelope_in_turn(async_factory)

        abandoned_calls: list[dict[str, Any]] = []

        async def _capture_abandoned(handle: Any, **kwargs: Any) -> None:
            abandoned_calls.append(kwargs)

        monkeypatch.setattr(module, "persist_command_abandoned", _capture_abandoned)

        abandoned_entry = SimpleNamespace(
            command_id="cmd-abandoned",
            command_type="OPEN",
            failure_reason="gateway_submission_failed: timeout",
            retry_attempt_count=2,
        )
        # Re-wrap the persisted log entry with an abandoned entry attached.
        entry_with_abandon = SubmissionLogEntry(
            envelope=log_entry.envelope,
            submission_results=log_entry.submission_results,
            dispatch_results=log_entry.dispatch_results,
            abandoned_entries=(abandoned_entry,),
        )

        await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INV_IDEMPOTENT,
            pm_result=_make_pm_result(submission_log=(entry_with_abandon,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        # The in-turn writeback already emitted the abandoned audit; this stage
        # must not double it.
        assert abandoned_calls == []

    async def test_deferred_path_emits_command_abandoned_exactly_once(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The non-broker deferred path (no in-turn writeback) still emits exactly
        one ``COMMAND_ABANDONED`` row per abandoned entry via ``dispatch_phase2``.

        Here ``submit_envelope`` Step 6 never ran (no broker triple), so the
        envelope is NOT already-persisted and ``dispatch_phase2`` is the sole
        emitter of both the writeback and the abandoned audit (ALP-763)."""
        from types import SimpleNamespace

        from alphamind.scheduler import phase2_dispatch as module
        from alphamind.scheduler.phase2_dispatch import dispatch_phase2

        persist_envelope_mock = AsyncMock(return_value=None)
        abandoned_calls: list[dict[str, Any]] = []

        async def _capture_abandoned(handle: Any, **kwargs: Any) -> None:
            abandoned_calls.append(kwargs)

        monkeypatch.setattr(module, "persist_envelope_outcome", persist_envelope_mock)
        monkeypatch.setattr(module, "persist_command_abandoned", _capture_abandoned)

        envelope = SimpleNamespace(envelope_id="ENV-DEFERRED-1", source_provenance="strategist")
        submission_results = (
            _make_submission_result(command_ordinal=0, command_id="cmd-a", status="rejected"),
        )
        abandoned_entry = SimpleNamespace(
            command_id="cmd-a",
            command_type="OPEN",
            failure_reason="gateway_submission_failed: timeout",
            retry_attempt_count=3,
        )
        entry = SubmissionLogEntry(
            envelope=cast(Any, envelope),
            submission_results=submission_results,
            abandoned_entries=(abandoned_entry,),
        )

        await dispatch_phase2(
            session_factory=async_factory,
            invocation_id=_INV_IDEMPOTENT,
            pm_result=_make_pm_result(submission_log=(entry,)),
            state_persistence_config=_make_state_persistence_config(),
        )

        assert len(abandoned_calls) == 1
        assert abandoned_calls[0]["command_id"] == "cmd-a"
