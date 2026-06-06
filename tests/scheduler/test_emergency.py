"""Tests for ``alphamind.scheduler.emergency`` — emergency-invocation receiver (story 04b).

The receiver polls ``activity_log`` for ``EMERGENCY_INVOCATION_REQUESTED`` entries
written by the continuous monitor (ALP-123 / ALP-439), enforces the 30-minute
cooldown (with margin-call override), and dispatches one ``run_invocation`` per
accepted request with ``trigger_type='emergency'`` and
``firing_run_type=RunType.emergency``.

Tests use an on-disk SQLite database (mirroring the runtime tests) and
monkey-patch ``run_invocation`` so the dispatch surface is exercised without
hitting the LLM-dependent inner stages.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.run_types import RunType
from alphamind.config.models.venue import VenueConfig
from alphamind.execution.write_paths.phase1 import Phase1Summary
from alphamind.portfolio_state.events.activity_log import (
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
    encode_detail,
)
from alphamind.scheduler.emergency import run_emergency_receiver_task
from alphamind.scheduler.orchestrator import InvocationSummary
from alphamind.scheduler.session import PipelineSession, new_session
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    invocation_record_to_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow

# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------


_PROCESS_LIFETIME_ID = "proc-driver-1"
_BOOTSTRAP_INV_ID = "inv-bootstrap-1"


def _make_invocation_record(
    *,
    invocation_id: str,
    start_at: str,
    trigger_type: str,
    phase2_completed_at: str | None,
) -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=phase2_completed_at,
        trigger_type=trigger_type,  # type: ignore[arg-type]
        trigger_source="test",
        trigger_reason="seed",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=f"/tmp/provenance/invocations/{invocation_id}/resolved.json",
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


def _make_emergency_activity_log_row(
    *,
    entry_at: datetime,
    invocation_id: str,
    trigger_type: str,
    trigger_reason: str = "Regime jump: normal -> crisis",
    cooldown_remaining_seconds: int = 0,
    entry_id: str | None = None,
) -> ActivityLogRow:
    """Build an ``EMERGENCY_INVOCATION_REQUESTED`` row for direct SQL insert.

    ``entry_id`` defaults to a time-sortable ``em-…`` id; pass it explicitly to
    exercise producer-prefix collisions (e.g. ``opcon-…`` vs ``mon-emt-…``).
    """
    detail = EmergencyInvocationRequestedDetail(
        trigger_type=trigger_type,  # type: ignore[arg-type]
        trigger_reason=trigger_reason,
        cooldown_remaining_seconds=cooldown_remaining_seconds,
    )
    return ActivityLogRow(
        entry_id=entry_id or f"em-{entry_at.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}",
        invocation_id=invocation_id,
        entry_at=entry_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        event_type=EventType.EMERGENCY_INVOCATION_REQUESTED.value,
        event_group=EventGroup.RISK_AND_GUARDRAIL.value,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=EventSource.GUARDRAIL_LAYER.value,
        detail_json=encode_detail(detail),
    )


@pytest.fixture()
def pipeline_session() -> PipelineSession:
    return new_session(process_lifetime_id=_PROCESS_LIFETIME_ID, mode="paper")


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def venue_config() -> VenueConfig:
    """Load shipped ``venue.yaml``; values are never consumed (tests stub ``run_invocation``)."""
    raw = yaml.safe_load((_REPO_ROOT / "config" / "venue.yaml").read_text())
    return VenueConfig.model_validate(raw)


def _make_invocation_summary(
    *,
    invocation_id: str,
) -> InvocationSummary:
    """Fabricate a minimal ``InvocationSummary`` for the stubbed ``run_invocation``."""
    return InvocationSummary(
        invocation_id=invocation_id,
        trigger_type="emergency",
        trigger_source="continuous_monitor",
        firing_run_type=RunType.emergency,
        phase1_summary=Phase1Summary(
            fills_processed=0,
            fills_quarantined=0,
            ca_activities_processed=0,
            reconciliation_alerts=0,
        ),
        commands_submitted=0,
        commands_rejected=0,
        staleness_flag=False,
        duration_seconds=0.0,
    )


class _RunInvocationRecorder:
    """Records every ``run_invocation`` call and returns a canned summary."""

    def __init__(self, *, exc: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._exc = exc

    async def __call__(self, **kwargs: Any) -> InvocationSummary:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return _make_invocation_summary(invocation_id=f"inv-stub-{len(self.calls)}")


async def _seed_emergency_entry(
    factory: async_sessionmaker[AsyncSession],
    *,
    entry_at: datetime,
    trigger_type: str = "regime_jump",
    trigger_reason: str = "Regime jump: normal -> crisis",
    entry_id: str | None = None,
) -> None:
    """Seed the bootstrap invocation (FK target) + an EMERGENCY entry.

    The bootstrap inv + process row (driver ID) are now supplied by the
    hoisted async_factory; we still ensure the inv row here for tests that
    call this helper directly on a factory. ``merge`` keeps the bootstrap
    invocation idempotent so a test may seed several entries in sequence.
    """
    async with factory() as session:
        # Ensure bootstrap invocation row (target for activity_log FK).
        # (process_lifetime row is pre-seeded by async_factory.) ``merge`` is
        # idempotent, so repeated calls in one test do not collide on the PK.
        await session.merge(
            invocation_record_to_row(
                _make_invocation_record(
                    invocation_id=_BOOTSTRAP_INV_ID,
                    start_at="2026-05-07T14:00:00Z",
                    trigger_type="manual",
                    phase2_completed_at=None,
                )
            )
        )
        await session.flush()
        session.add(
            _make_emergency_activity_log_row(
                entry_at=entry_at,
                invocation_id=_BOOTSTRAP_INV_ID,
                trigger_type=trigger_type,
                trigger_reason=trigger_reason,
                entry_id=entry_id,
            )
        )
        await session.commit()


async def _seed_completed_emergency_invocation(
    factory: async_sessionmaker[AsyncSession],
    *,
    invocation_id: str,
    phase2_completed_at: datetime,
) -> None:
    """Insert a prior emergency invocation that completed at the given UTC time."""
    iso = phase2_completed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    record = InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=iso,
        phase1_completed_at=iso,
        phase2_completed_at=iso,
        trigger_type="emergency",
        trigger_source="continuous_monitor",
        trigger_reason="earlier emergency",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=f"/tmp/provenance/invocations/{invocation_id}/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/data_calibration.json"
        ),
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )
    async with factory() as session:
        session.add(invocation_record_to_row(record))
        await session.commit()


@contextlib.asynccontextmanager
async def _running_receiver(
    *,
    pipeline_session: PipelineSession,
    factory: async_sessionmaker[AsyncSession],
    venue_config: VenueConfig,
    tmp_path: Path,
    recorder: _RunInvocationRecorder,
    monkeypatch: pytest.MonkeyPatch,
    cooldown_minutes: int = 30,
    poll_interval_seconds: float = 0.01,
) -> AsyncIterator[asyncio.Task[None]]:
    """Spawn the receiver as an asyncio task, yield it, then cancel cleanly.

    Stubs the module-level ``run_invocation`` import on
    :mod:`alphamind.scheduler.emergency` so the orchestrator is never invoked;
    the supplied ``recorder`` captures every dispatch.
    """
    from alphamind.scheduler import emergency as emergency_module

    monkeypatch.setattr(emergency_module, "run_invocation", recorder)

    from alphamind.scheduler.run_context import RunInvocationContext

    context = RunInvocationContext(
        session_factory=factory,
        sync_session_factory="sync-factory-sentinel",  # type: ignore[arg-type]
        process_lifetime_id=pipeline_session.process_lifetime_id,
        archive_root=tmp_path / "archive",
        config_dir=tmp_path / "config",
        env_path=tmp_path / ".env",
        venue_config=venue_config,
        execution_mode=ExecutionMode.paper,
    )

    async def _runner() -> None:
        await run_emergency_receiver_task(
            pipeline_session,
            poll_interval_seconds=poll_interval_seconds,
            cooldown_minutes=cooldown_minutes,
            context=context,
        )

    task = asyncio.create_task(_runner())
    # Let the receiver capture its initial high-water mark before tests seed
    # post-startup entries; one short sleep is enough at 10ms poll cadence.
    await asyncio.sleep(0.05)
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_until_dispatched(
    recorder: _RunInvocationRecorder,
    *,
    expected: int,
    timeout_seconds: float = 2.0,
    poll_interval_seconds: float = 0.01,
) -> None:
    """Poll ``recorder.calls`` until it reaches ``expected`` or the timeout expires."""
    steps = int(timeout_seconds / poll_interval_seconds)
    for _ in range(steps):
        if len(recorder.calls) >= expected:
            return
        await asyncio.sleep(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStartupHighWaterMark:
    async def test_pre_startup_entries_are_not_replayed(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        pipeline_session: PipelineSession,
        venue_config: VenueConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ``EMERGENCY_INVOCATION_REQUESTED`` row already present when the
        task starts must NOT be dispatched — the receiver initializes its
        high-water mark from the current ``activity_log`` state."""
        # Seed an entry BEFORE the task starts.
        await _seed_emergency_entry(
            async_factory,
            entry_at=datetime(2026, 5, 7, 14, 0, 0, tzinfo=UTC),
            trigger_type="regime_jump",
        )

        recorder = _RunInvocationRecorder()
        async with _running_receiver(
            pipeline_session=pipeline_session,
            factory=async_factory,
            venue_config=venue_config,
            tmp_path=tmp_path,
            recorder=recorder,
            monkeypatch=monkeypatch,
        ):
            # Allow ample time for several poll cycles; no dispatch should occur.
            await asyncio.sleep(0.1)

        assert recorder.calls == []


class TestDispatchAfterStartup:
    async def test_fresh_entry_after_startup_is_dispatched_as_emergency(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        pipeline_session: PipelineSession,
        venue_config: VenueConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ``EMERGENCY_INVOCATION_REQUESTED`` entry written after the task
        starts must be dispatched as ``run_invocation(trigger_type='emergency',
        firing_run_type=RunType.emergency, ...)``."""
        recorder = _RunInvocationRecorder()
        async with _running_receiver(
            pipeline_session=pipeline_session,
            factory=async_factory,
            venue_config=venue_config,
            tmp_path=tmp_path,
            recorder=recorder,
            monkeypatch=monkeypatch,
        ):
            await _seed_emergency_entry(
                async_factory,
                entry_at=datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC),
                trigger_type="regime_jump",
                trigger_reason="Regime jump: normal -> crisis",
            )
            await _wait_until_dispatched(recorder, expected=1)

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["trigger_type"] == "emergency"
        assert call["firing_run_type"] is RunType.emergency
        assert call["trigger_source"] == "continuous_monitor"
        assert call["trigger_reason"] == "Regime jump: normal -> crisis"
        assert call["context"].process_lifetime_id == pipeline_session.process_lifetime_id


class TestProducerIndependentCursor:
    async def test_later_row_with_lower_sorting_entry_id_is_still_dispatched(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        pipeline_session: PipelineSession,
        venue_config: VenueConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for ALP-870 — the dispatch cursor must key on the table's
        producer-independent insertion order, not the producer-formatted
        ``entry_id``.

        Row X (``opcon-…``) is dispatched first, advancing the cursor. Row Y is
        inserted *after* X but its ``entry_id`` (``mon-emt-…``) sorts
        lexicographically *below* X. With the pre-fix ``entry_id`` cursor Y is
        silently filtered out (``mon-emt-…`` < ``opcon-…``); with the
        ``event_seq`` cursor Y has the higher rowid and dispatches. Both rows
        must dispatch exactly once.
        """
        recorder = _RunInvocationRecorder()
        async with _running_receiver(
            pipeline_session=pipeline_session,
            factory=async_factory,
            venue_config=venue_config,
            tmp_path=tmp_path,
            recorder=recorder,
            monkeypatch=monkeypatch,
        ):
            # Row X — high-sorting producer prefix; processed first so the
            # cursor advances past it before Y is written.
            await _seed_emergency_entry(
                async_factory,
                entry_at=datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC),
                trigger_type="regime_jump",
                entry_id="opcon-inv-20260507T143000Z-aaaaaaaa",
            )
            await _wait_until_dispatched(recorder, expected=1)

            # Row Y — inserted after X (higher rowid) but its entry_id sorts
            # below X's. This is the cross-producer inversion the receiver must
            # tolerate.
            await _seed_emergency_entry(
                async_factory,
                entry_at=datetime(2026, 5, 7, 14, 31, 0, tzinfo=UTC),
                trigger_type="regime_jump",
                entry_id="mon-emt-sess1-000000000001-bbbbbbbb",
            )
            await _wait_until_dispatched(recorder, expected=2)

        assert len(recorder.calls) == 2


class TestCooldown:
    async def test_second_entry_within_cooldown_is_suppressed(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        pipeline_session: PipelineSession,
        venue_config: VenueConfig,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When a prior emergency invocation completed within the cooldown
        window, a fresh non-``margin_call`` entry must be logged as suppressed
        and NOT dispatched."""
        await _seed_completed_emergency_invocation(
            async_factory,
            invocation_id="inv-prev-emergency",
            phase2_completed_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        recorder = _RunInvocationRecorder()
        with caplog.at_level(logging.INFO, logger="alphamind.scheduler.emergency"):
            async with _running_receiver(
                pipeline_session=pipeline_session,
                factory=async_factory,
                venue_config=venue_config,
                tmp_path=tmp_path,
                recorder=recorder,
                monkeypatch=monkeypatch,
            ):
                await _seed_emergency_entry(
                    async_factory,
                    entry_at=datetime.now(UTC),
                    trigger_type="regime_jump",
                )
                await asyncio.sleep(0.2)  # several poll cycles

        assert recorder.calls == []
        assert any("suppressed" in r.message and "cooldown" in r.message for r in caplog.records)

    async def test_margin_call_within_cooldown_bypasses_and_dispatches(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        pipeline_session: PipelineSession,
        venue_config: VenueConfig,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``trigger_type='margin_call'`` entry MUST bypass the cooldown — the
        broker's deadline is external and non-negotiable per breach-behavior.md
        § Cooldown."""
        await _seed_completed_emergency_invocation(
            async_factory,
            invocation_id="inv-prev-emergency",
            phase2_completed_at=datetime.now(UTC) - timedelta(minutes=5),
        )

        recorder = _RunInvocationRecorder()
        async with _running_receiver(
            pipeline_session=pipeline_session,
            factory=async_factory,
            venue_config=venue_config,
            tmp_path=tmp_path,
            recorder=recorder,
            monkeypatch=monkeypatch,
        ):
            await _seed_emergency_entry(
                async_factory,
                entry_at=datetime.now(UTC),
                trigger_type="margin_call",
                trigger_reason="Broker margin call -- liquidation imminent",
            )
            await _wait_until_dispatched(recorder, expected=1)

        assert len(recorder.calls) == 1
        assert recorder.calls[0]["trigger_reason"] == "Broker margin call -- liquidation imminent"


class TestDispatchFailureIsolation:
    async def test_run_invocation_exception_is_logged_and_does_not_propagate(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        pipeline_session: PipelineSession,
        venue_config: VenueConfig,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``run_invocation`` raises inside the receiver loop, the
        exception must be caught and logged via ``log.exception``; the
        receiver task continues polling rather than dying."""

        class _BoomError(RuntimeError):
            pass

        recorder = _RunInvocationRecorder(exc=_BoomError("simulated invocation failure"))
        with caplog.at_level(logging.ERROR, logger="alphamind.scheduler.emergency"):
            async with _running_receiver(
                pipeline_session=pipeline_session,
                factory=async_factory,
                venue_config=venue_config,
                tmp_path=tmp_path,
                recorder=recorder,
                monkeypatch=monkeypatch,
            ) as task:
                await _seed_emergency_entry(
                    async_factory,
                    entry_at=datetime.now(UTC),
                    trigger_type="regime_jump",
                )
                await _wait_until_dispatched(recorder, expected=1)
                # The receiver must still be polling after the failed dispatch.
                await asyncio.sleep(0.05)
                assert not task.done(), "receiver task must keep polling after a dispatch exception"

        assert len(recorder.calls) == 1
        assert any("failed" in r.message and r.levelno >= logging.ERROR for r in caplog.records)
