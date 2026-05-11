"""Tests for ``alphamind.scheduler.phase2_dispatch.dispatch_phase2`` (story 03b).

The dispatcher iterates the PM result's ``submission_log``, persists each
envelope's outcome via the Phase 2 write path, and aggregates accepted /
rejected counts into the :class:`Phase2Summary` the orchestrator records.
Per the parent issue's fail-closed invariant, any submission exception
propagates so the surrounding :class:`InvocationContext` rolls back.
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
from alphamind.execution.oms.submit_envelope_mcp import (
    Acknowledgment,
    SubmissionLogEntry,
    SubmissionResult,
)
from alphamind.execution.state_persistence.config import StatePersistenceConfig
from alphamind.execution.state_persistence.invocation_context.context import (
    InvocationHandle,
)
from alphamind.execution.state_persistence.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
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

    import alphamind.execution.state_persistence.tables  # noqa: F401

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


def _make_handle(session: AsyncSession, invocation_id: str = "inv-x") -> InvocationHandle:
    return InvocationHandle(session=session, invocation_id=invocation_id)


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

        async with async_factory() as session:
            handle = _make_handle(session)
            summary = await dispatch_phase2(
                handle=handle,
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

        async with async_factory() as session:
            handle = _make_handle(session)
            summary = await dispatch_phase2(
                handle=handle,
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

        async with async_factory() as session:
            handle = _make_handle(session)
            with pytest.raises(RuntimeError, match="phase 2 write failed"):
                await dispatch_phase2(
                    handle=handle,
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

        async with async_factory() as session:
            handle = _make_handle(session)
            summary = await dispatch_phase2(
                handle=handle,
                pm_result=_make_pm_result(submission_log=entries),
                state_persistence_config=_make_state_persistence_config(),
            )

        assert persist_mock.await_count == 2
        assert summary.commands_submitted == 1
        assert summary.commands_rejected == 1
