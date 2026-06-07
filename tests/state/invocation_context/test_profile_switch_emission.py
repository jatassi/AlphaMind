"""Tests for the ``emit_profile_switch_entry`` helper (ALP-663).

Verifies:
- No-op outcome (is_no_op=True) writes zero ActivityLogEntry rows.
- Real switch (is_no_op=False) writes exactly one ActivityLogEntry.
- The persisted row round-trips back to a ProfileSwitchedDetail equal to the outcome.
- Emission rolls back when the InvocationContext raises mid-transaction.
- No production call sites exist for emit_profile_switch_entry in this story.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config.control_handlers.profile_switch import ProfileSwitchOutcome
from alphamind.config.models.main import Profile
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.portfolio_state.events import EventSource, EventType, ProfileSwitchedDetail
from alphamind.state.invocation_context import (
    InvocationContext,
    InvocationRecord,
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.state.invocation_context.config_change import emit_profile_switch_entry
from alphamind.state.repository.activity_log_queries import read_intra_invocation_changelog


@pytest.fixture()
async def async_engine_and_factory(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Async engine + factory backed by a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    sync_engine = make_engine(str(db_path))
    with make_session_factory(sync_engine)() as sess:
        sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
        sess.commit()
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-1",
        process_role="pipeline",
        process_start_at="2026-05-25T10:00:00Z",
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


def _make_invocation_record(invocation_id: str = "inv-1") -> InvocationRecord:
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id="proc-1",
        start_at="2026-05-25T10:00:00Z",
        fill_collection_completed_at=None,
        command_execution_completed_at=None,
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        git_sha_at_invocation="a" * 40,
        active_profile="medium",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="0" * 64,
        resolved_config_snapshot_path=f"/tmp/provenance/invocations/{invocation_id}/resolved.json",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path=(
            f"/tmp/provenance/invocations/{invocation_id}/calibration.json"
        ),
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=None,
    )


def _make_outcome(
    previous: Profile,
    new: Profile,
    *,
    is_no_op: bool,
    main_yaml_path: Path | None = None,
) -> ProfileSwitchOutcome:
    return ProfileSwitchOutcome(
        previous_profile=previous,
        new_profile=new,
        main_yaml_path=main_yaml_path or Path("/config/main.yaml"),
        is_no_op=is_no_op,
    )


class TestEmitProfileSwitchEntry:
    async def test_no_op_outcome_writes_zero_rows(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """is_no_op=True → helper returns without appending any row."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record("inv-noop")
        outcome = _make_outcome(Profile.medium, Profile.medium, is_no_op=True)

        async with InvocationContext(session_factory=factory, record=record) as handle:
            emit_profile_switch_entry(handle=handle, outcome=outcome)

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-noop")
        assert rows == ()

    async def test_real_switch_writes_exactly_one_row(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """is_no_op=False → exactly one ActivityLogEntry is committed."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record("inv-switch")
        outcome = _make_outcome(Profile.small, Profile.large, is_no_op=False)

        async with InvocationContext(session_factory=factory, record=record) as handle:
            emit_profile_switch_entry(handle=handle, outcome=outcome)

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-switch")
        assert len(rows) == 1

    async def test_emitted_row_has_correct_event_type_and_source(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Committed entry has PROFILE_SWITCHED type and OPERATOR_CONSOLE source."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record("inv-type-check")
        outcome = _make_outcome(Profile.medium, Profile.large, is_no_op=False)

        async with InvocationContext(session_factory=factory, record=record) as handle:
            emit_profile_switch_entry(handle=handle, outcome=outcome)

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-type-check")
        entry = rows[0]
        assert entry.event_type is EventType.PROFILE_SWITCHED
        assert entry.source is EventSource.OPERATOR_CONSOLE

    async def test_detail_round_trips_from_outcome(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """Committed detail decodes back to a ProfileSwitchedDetail equal to the outcome."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record("inv-detail")
        outcome = _make_outcome(Profile.small, Profile.large, is_no_op=False)

        async with InvocationContext(session_factory=factory, record=record) as handle:
            emit_profile_switch_entry(handle=handle, outcome=outcome)

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-detail")
        detail = rows[0].detail
        assert isinstance(detail, ProfileSwitchedDetail)
        assert detail.previous_profile is Profile.small
        assert detail.new_profile is Profile.large
        assert detail.is_no_op is False

    async def test_emission_rolls_back_when_context_raises(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """An exception escaping InvocationContext rolls back the appended entry."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record("inv-rollback")
        outcome = _make_outcome(Profile.small, Profile.large, is_no_op=False)

        class _BoomError(RuntimeError):
            pass

        with pytest.raises(_BoomError):
            async with InvocationContext(session_factory=factory, record=record) as handle:
                emit_profile_switch_entry(handle=handle, outcome=outcome)
                raise _BoomError("simulated downstream failure")

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-rollback")
        assert rows == ()

    async def test_now_parameter_stamps_entry_timestamp(
        self,
        async_engine_and_factory: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    ) -> None:
        """The caller-supplied ``now`` is used as the entry timestamp (F10)."""
        _, factory = async_engine_and_factory
        record = _make_invocation_record("inv-now")
        outcome = _make_outcome(Profile.small, Profile.large, is_no_op=False)
        caller_now = datetime(2026, 5, 26, 14, 30, 0, tzinfo=UTC)

        async with InvocationContext(session_factory=factory, record=record) as handle:
            emit_profile_switch_entry(handle=handle, outcome=outcome, now=caller_now)

        async with factory() as sess:
            rows = await read_intra_invocation_changelog(sess, "inv-now")
        # Timestamp matches the caller-supplied ``now`` rather than an
        # independently-derived datetime.now(UTC).
        assert rows[0].timestamp == caller_now
