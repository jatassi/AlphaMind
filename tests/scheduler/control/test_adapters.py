"""Tests for ``alphamind.scheduler.control.adapters`` production verb adapters.

``ActivityLogEmergencyTrigger.trigger()`` is the operator-console emergency
path (``POST /control/trigger_emergency_invocation``). These tests drive it
against a real in-memory SQLite session — no fake substituted for the ORM
write — which is the exact seam the original ``TypeError`` (wrong ORM field
``timestamp``) and ``IntegrityError`` (NOT-NULL ``invocation_id=None``) hid
behind. The verb/route tests fake the ``EmergencyTrigger`` Protocol, so this
file is the only coverage of the live activity-log write path (ALP-869).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.portfolio_state.events.activity_log import (
    EmergencyInvocationRequestedDetail,
    decode_detail,
)
from alphamind.scheduler.control.adapters import (
    ActivityLogEmergencyTrigger,
    AsyncIOSchedulerControl,
    DeferredUniverseValidator,
)
from alphamind.scheduler.control.verbs import UniverseValidationFailedError
from alphamind.state.invocation_context.records import (
    InvocationRecord,
    TriggerType,
    invocation_record_to_row,
)
from alphamind.state.tables.activity_log import ActivityLogRow

_PROCESS_LIFETIME_ID = "proc-driver-1"


def _make_invocation_record(
    *,
    invocation_id: str,
    start_at: str,
    trigger_type: TriggerType = "scheduled",
    phase2_completed_at: str | None = None,
) -> InvocationRecord:
    """Build a minimal invocation record (FK target for activity_log).

    Defaults to a not-yet-completed ``scheduled`` invocation; callers seeding a
    completed-emergency for the cooldown read override ``trigger_type`` and
    ``phase2_completed_at``.
    """
    return InvocationRecord(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=start_at,
        phase1_completed_at=None,
        phase2_completed_at=phase2_completed_at,
        trigger_type=trigger_type,
        trigger_source="test",
        trigger_reason="seed",
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


async def _seed_invocations(
    factory: async_sessionmaker[AsyncSession],
    *,
    records: list[InvocationRecord],
) -> None:
    async with factory() as session:
        for record in records:
            session.add(invocation_record_to_row(record))
        await session.commit()


class TestTriggerPersistsRow:
    async def test_trigger_writes_one_row_bound_to_latest_invocation(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``trigger`` persists exactly one activity-log row mirroring the
        monitor's emergency-emit shape, bound to the most-recently-started
        invocation, and returns a ``pending-emerg-opcon-inv-…`` handle."""
        # Two invocations with distinct start_at — the row must bind to the LATER.
        await _seed_invocations(
            async_factory,
            records=[
                _make_invocation_record(invocation_id="inv-older", start_at="2026-05-07T13:00:00Z"),
                _make_invocation_record(
                    invocation_id="inv-latest", start_at="2026-05-07T14:30:00Z"
                ),
            ],
        )
        trigger = ActivityLogEmergencyTrigger(session_factory=async_factory, cooldown_minutes=30)
        now = datetime(2026, 5, 7, 15, 0, 0, tzinfo=UTC)

        handle = await trigger.trigger(
            reason="manual operator escalation", source="operator_console", now=now
        )

        assert handle.startswith("pending-emerg-opcon-inv-")

        async with async_factory() as session:
            rows = (await session.execute(select(ActivityLogRow))).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == "EMERGENCY_INVOCATION_REQUESTED"
        assert row.event_group == "RISK_AND_GUARDRAIL"
        assert row.source == "GUARDRAIL_LAYER"
        assert row.entry_at is not None
        assert row.invocation_id == "inv-latest"

        detail = decode_detail(row.detail_json, EmergencyInvocationRequestedDetail)
        assert detail.trigger_type == "multi_rule_breach"
        assert detail.trigger_reason == "operator_console:manual operator escalation"

    async def test_trigger_raises_when_no_invocations_exist(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """With no ``invocations`` row to satisfy the NOT-NULL FK, ``trigger``
        raises rather than writing an FK-violating row."""
        trigger = ActivityLogEmergencyTrigger(session_factory=async_factory, cooldown_minutes=30)
        now = datetime(2026, 5, 7, 15, 0, 0, tzinfo=UTC)

        with pytest.raises(RuntimeError):
            await trigger.trigger(
                reason="manual operator escalation",
                source="operator_console",
                now=now,
            )

        async with async_factory() as session:
            rows = (await session.execute(select(ActivityLogRow))).scalars().all()
        assert rows == []


# ---------------------------------------------------------------------------
# AsyncIOSchedulerControl over a fake APScheduler (sanctioned infra seam).
# ---------------------------------------------------------------------------


class _FakeJob:
    """Minimal stand-in for an APScheduler ``Job`` (id + next_run_time only)."""

    def __init__(self, *, job_id: str, next_run_time: datetime | None) -> None:
        self.id = job_id
        self.next_run_time = next_run_time


class _FakeScheduler:
    """Fake ``AsyncIOScheduler`` recording pause/resume calls + canned jobs."""

    def __init__(self, *, jobs: list[_FakeJob] | None = None) -> None:
        self._jobs = jobs or []
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self) -> None:
        self.pause_calls += 1

    def resume(self) -> None:
        self.resume_calls += 1

    def get_jobs(self) -> list[_FakeJob]:
        return list(self._jobs)


class TestAsyncIOSchedulerControlPauseResume:
    def test_pause_is_idempotent(self) -> None:
        scheduler = _FakeScheduler()
        control = AsyncIOSchedulerControl(scheduler)
        first = datetime(2026, 5, 7, 14, 0, 0, tzinfo=UTC)
        second = datetime(2026, 5, 7, 14, 5, 0, tzinfo=UTC)

        applied = control.pause(reason="operator pause", now=first)

        assert applied == first
        assert control.is_paused() is True
        assert scheduler.pause_calls == 1

        # Second pause while paused returns the ORIGINAL applied time, no re-pause.
        applied_again = control.pause(reason="operator pause again", now=second)

        assert applied_again == first
        assert scheduler.pause_calls == 1

    def test_resume_clears_flag_and_is_idempotent(self) -> None:
        scheduler = _FakeScheduler()
        control = AsyncIOSchedulerControl(scheduler)
        paused_at = datetime(2026, 5, 7, 14, 0, 0, tzinfo=UTC)
        resumed_at = datetime(2026, 5, 7, 15, 0, 0, tzinfo=UTC)
        control.pause(reason="operator pause", now=paused_at)

        applied = control.resume(now=resumed_at)

        assert applied == resumed_at
        assert control.is_paused() is False
        assert scheduler.resume_calls == 1

        # Resume while already running returns ``now`` without calling resume again.
        later = datetime(2026, 5, 7, 16, 0, 0, tzinfo=UTC)
        applied_again = control.resume(now=later)

        assert applied_again == later
        assert scheduler.resume_calls == 1


class TestAsyncIOSchedulerControlNextRunPreview:
    def test_returns_soonest_in_utc_and_skips_none(self) -> None:
        # job_b's wall-clock (15:00) is LATER than job_a's (14:00) but its
        # +02:00 zone makes it 13:00Z — soonest only after UTC conversion.
        plus_two = timezone(timedelta(hours=2))
        scheduler = _FakeScheduler(
            jobs=[
                _FakeJob(
                    job_id="market_open", next_run_time=datetime(2026, 5, 7, 14, 0, tzinfo=UTC)
                ),
                _FakeJob(
                    job_id="midday", next_run_time=datetime(2026, 5, 7, 15, 0, tzinfo=plus_two)
                ),
                _FakeJob(job_id="paused_job", next_run_time=None),
            ]
        )
        control = AsyncIOSchedulerControl(scheduler)

        preview = control.next_run_preview()

        assert preview is not None  # narrow for the indexed asserts below
        # Aware-datetime equality compares instants, so an equality check alone would
        # also pass for an un-converted +02:00 value; assert the offset directly so
        # the .astimezone(UTC) conversion is what the test actually proves.
        assert preview[1] == "midday"
        assert preview[0] == datetime(2026, 5, 7, 13, 0, tzinfo=UTC)
        assert preview[0].utcoffset() == timedelta(0)
        assert preview[0].tzinfo == UTC

    def test_returns_none_when_no_jobs(self) -> None:
        control = AsyncIOSchedulerControl(_FakeScheduler(jobs=[]))
        assert control.next_run_preview() is None

    def test_returns_none_when_every_job_next_run_is_none(self) -> None:
        scheduler = _FakeScheduler(
            jobs=[
                _FakeJob(job_id="a", next_run_time=None),
                _FakeJob(job_id="b", next_run_time=None),
            ]
        )
        control = AsyncIOSchedulerControl(scheduler)
        assert control.next_run_preview() is None


# ---------------------------------------------------------------------------
# Cooldown informational read (the value the receiver later re-checks), driven
# through the real trigger() path and decoded off the written row.
# ---------------------------------------------------------------------------


async def _trigger_and_read_detail(
    factory: async_sessionmaker[AsyncSession],
    *,
    cooldown_minutes: int,
    now: datetime,
) -> EmergencyInvocationRequestedDetail:
    trigger = ActivityLogEmergencyTrigger(
        session_factory=factory, cooldown_minutes=cooldown_minutes
    )
    await trigger.trigger(reason="manual escalation", source="operator_console", now=now)
    async with factory() as session:
        row = (await session.execute(select(ActivityLogRow))).scalars().one()
    return decode_detail(row.detail_json, EmergencyInvocationRequestedDetail)


class TestCooldownRemainingSeconds:
    _NOW = datetime(2026, 5, 7, 15, 0, 0, tzinfo=UTC)

    async def test_zero_when_no_prior_completed_emergency(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # A scheduled invocation satisfies the FK bind; no completed emergency exists.
        await _seed_invocations(
            async_factory,
            records=[
                _make_invocation_record(invocation_id="inv-1", start_at="2026-05-07T14:00:00Z")
            ],
        )
        detail = await _trigger_and_read_detail(async_factory, cooldown_minutes=30, now=self._NOW)
        assert detail.cooldown_remaining_seconds == 0

    async def test_positive_when_inside_window(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Completed emergency 10 min ago; 30-min cooldown → 1800 - 600 = 1200 remaining.
        # A *more-recently-completed* scheduled invocation is also seeded (and is the
        # latest-started, so it wins the FK bind): the cooldown read must still anchor
        # on the emergency row. If the query's trigger_type=='emergency' filter were
        # dropped it would pick the scheduled completion (14:58) → 1680, not 1200, so
        # this assertion proves the filter is load-bearing.
        await _seed_invocations(
            async_factory,
            records=[
                _make_invocation_record(
                    invocation_id="inv-emerg",
                    start_at="2026-05-07T14:40:00Z",
                    trigger_type="emergency",
                    phase2_completed_at="2026-05-07T14:50:00Z",
                ),
                _make_invocation_record(
                    invocation_id="inv-sched-recent",
                    start_at="2026-05-07T14:55:00Z",
                    trigger_type="scheduled",
                    phase2_completed_at="2026-05-07T14:58:00Z",
                ),
            ],
        )
        detail = await _trigger_and_read_detail(async_factory, cooldown_minutes=30, now=self._NOW)
        assert detail.cooldown_remaining_seconds == 1200

    async def test_zero_when_outside_window(
        self, async_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        # Completed emergency 40 min ago; 30-min cooldown elapsed → clamped to 0.
        await _seed_invocations(
            async_factory,
            records=[
                _make_invocation_record(
                    invocation_id="inv-emerg",
                    start_at="2026-05-07T14:15:00Z",
                    trigger_type="emergency",
                    phase2_completed_at="2026-05-07T14:20:00Z",
                )
            ],
        )
        detail = await _trigger_and_read_detail(async_factory, cooldown_minutes=30, now=self._NOW)
        assert detail.cooldown_remaining_seconds == 0


# ---------------------------------------------------------------------------
# DeferredUniverseValidator — placeholder always raises.
# ---------------------------------------------------------------------------


class TestDeferredUniverseValidator:
    def test_validate_raises_universe_validation_failed(self) -> None:
        with pytest.raises(UniverseValidationFailedError):
            DeferredUniverseValidator().validate(as_of=date(2026, 5, 7))
