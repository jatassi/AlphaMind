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
            handle: Any, envelope: Any, results: Any, *, config: Any
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

        async def _marker_persist(handle: Any, envelope: Any, results: Any, *, config: Any) -> None:
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
