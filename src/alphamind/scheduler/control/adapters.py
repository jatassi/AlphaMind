"""Production adapters wiring the verb Protocols onto live process state (ALP-720).

The verb layer in :mod:`alphamind.scheduler.control.verbs` defines three
Protocols (:class:`SchedulerControl`, :class:`EmergencyTrigger`,
:class:`UniverseValidator`) that the route handlers depend on.  Tests
substitute in-memory fakes; this module supplies the production
implementations the daemon's ``_run_daemon`` constructs at startup.

* :class:`AsyncIOSchedulerControl` — wraps an APScheduler
  :class:`AsyncIOScheduler` instance; tracks the operator-pause flag
  in-memory (live state, not history per ALP-128 pre-resolved decision G)
  and surfaces the soonest next-fire across registered jobs.
* :class:`ActivityLogEmergencyTrigger` — writes an
  ``EMERGENCY_INVOCATION_REQUESTED`` row to ``activity_log`` the same way
  the monitor's evaluator does; the existing emergency-receiver task
  picks the row up via its poll loop and dispatches ``run_invocation``.
  Cooldown info is sourced from the most-recent completed emergency
  invocation, matching ``alphamind.scheduler.emergency``.
* :class:`DeferredUniverseValidator` — placeholder that raises
  ``UniverseValidationFailedError`` until the wiring lands in a
  follow-up (the validation script is non-trivial and the
  command-center has an out-of-band path).  Wired so the verb's
  ``internal_error`` (HTTP 500) envelope renders gracefully.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from alphamind.portfolio_state.events import (
    ActivityLogEntry,
    EmergencyInvocationRequestedDetail,
    EventGroup,
    EventSource,
    EventType,
)
from alphamind.scheduler.control.verbs import (
    CooldownInfo,
    RunningInfo,
    UniverseValidationFailedError,
    UniverseValidationReportRecord,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.invocation_id import mint_invocation_id
from alphamind.state.tables.invocations import InvocationRow

if TYPE_CHECKING:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "ActivityLogEmergencyTrigger",
    "AsyncIOSchedulerControl",
    "DeferredUniverseValidator",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SchedulerControl over APScheduler.
# ---------------------------------------------------------------------------


class AsyncIOSchedulerControl:
    """Wrap a live :class:`AsyncIOScheduler` to satisfy :class:`SchedulerControl`.

    The pause flag lives both on the APScheduler instance (so no new jobs
    fire) and in this adapter's :attr:`_paused_at` (so the verb returns a
    stable ``applied_at`` on idempotent re-pauses).  ``next_run_preview``
    reads the soonest ``next_run_time`` across the scheduler's registered
    jobs; jobs with ``next_run_time is None`` (paused jobs in some
    APScheduler versions) are skipped.
    """

    def __init__(self, scheduler: AsyncIOScheduler) -> None:
        self._scheduler = scheduler
        self._paused_at: datetime | None = None

    def is_paused(self) -> bool:
        return self._paused_at is not None

    def pause(self, *, reason: str, now: datetime) -> datetime:
        # Idempotent: a pause-while-paused returns the ORIGINAL applied_at.
        if self._paused_at is not None:
            return self._paused_at
        log.info("scheduler paused via /control/pause reason=%s", reason)
        self._scheduler.pause()
        self._paused_at = now
        return now

    def resume(self, *, now: datetime) -> datetime:
        # Idempotent: a resume-while-running returns ``accepted`` with ``now``.
        if self._paused_at is None:
            return now
        log.info("scheduler resumed via /control/resume")
        self._scheduler.resume()
        self._paused_at = None
        return now

    def next_run_preview(self) -> tuple[datetime, str] | None:
        """Return ``(next_trigger_at, next_trigger_type)`` or ``None``.

        ``next_trigger_type`` is the APScheduler job ID, which matches the
        ``scheduler.yaml`` trigger key (the driver registers each job under
        its trigger key).  Returns ``None`` when no jobs are scheduled OR
        every job's ``next_run_time`` is ``None`` (the scheduler is paused
        in some configurations).
        """
        soonest: tuple[datetime, str] | None = None
        for job in self._scheduler.get_jobs():
            nrt = job.next_run_time
            if nrt is None:
                continue
            # Make sure we have a timezone-aware datetime — APScheduler returns
            # the scheduler's timezone; convert to UTC for the schema's
            # ``AwareDatetime`` field.
            nrt_utc = nrt.astimezone(UTC)
            if soonest is None or nrt_utc < soonest[0]:
                soonest = (nrt_utc, job.id)
        return soonest


# ---------------------------------------------------------------------------
# EmergencyTrigger over the activity-log write path.
# ---------------------------------------------------------------------------


_PENDING_INVOCATION_PREFIX = "pending-emerg-"


class ActivityLogEmergencyTrigger:
    """Write ``EMERGENCY_INVOCATION_REQUESTED`` rows; the receiver dispatches.

    The verb's contract returns an ``invocation_id`` the operator console
    correlates with subsequent ``invocation_started`` / ``invocation_ended``
    events.  Because :func:`alphamind.scheduler.invocation.insert_invocation_record`
    mints the canonical ID only when the receiver task picks up the activity-log
    row and calls ``run_invocation``, this adapter returns the row's
    ``entry_id`` prefixed with ``pending-emerg-`` so the operator console can
    poll for the eventually-assigned ID via the activity log + invocations
    join.  Tighter correlation (verb-time invocation_id pre-allocation) is
    deferred to a follow-up.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cooldown_minutes: int,
    ) -> None:
        self._session_factory = session_factory
        self._cooldown_minutes = cooldown_minutes

    async def trigger(self, *, reason: str, source: str, now: datetime) -> str:
        """Write the activity-log row and return a correlation handle.

        Mirrors the monitor's
        :class:`alphamind.execution.continuous_monitor.emergency_trigger.evaluator.EmergencyTriggerEvaluator._emit`
        shape — same ``EMERGENCY_INVOCATION_REQUESTED`` event type, same
        ``EventGroup.RISK_AND_GUARDRAIL``, same detail payload.  The verb's
        ``source`` argument is recorded in the detail's ``trigger_reason``
        prefix so operators can distinguish operator-console emergencies
        from autonomous monitor-triggered ones in the activity log.
        """
        annotated_reason = f"{source}:{reason}"
        entry_id = _build_operator_console_entry_id(now=now)
        # One session for both reads and the write: resolve invocation_id first
        # so a cold-start trigger fails fast without the informational cooldown
        # query, and so the cooldown read and the invocation binding share a
        # single consistent snapshot.
        async with self._session_factory() as session:
            # The activity_log row is a context/correlation row: the receiver
            # mints the requested invocation independently when it picks the
            # row up. ``invocation_id`` is NOT-NULL with an FK to invocations,
            # so bind it to the most-recently-started invocation — mirroring
            # the monitor's emergency emit (wiring.make_invocation_id_provider).
            # Cold-start (no invocation row yet) deliberately DIVERGES from the
            # monitor: we raise below rather than fall back to its
            # ``"monitor-bootstrap"`` sentinel, which is not a real invocations
            # row and would itself violate this FK. Do not "align" the two by
            # returning a sentinel — that re-introduces the crash this fixes.
            invocation_id = (
                await session.execute(
                    select(InvocationRow.invocation_id)
                    .order_by(InvocationRow.start_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if invocation_id is None:
                msg = (
                    "cannot queue an operator-console emergency request: no "
                    "invocations row exists yet to satisfy the activity_log "
                    "NOT-NULL invocation_id FK (the daemon has not run an "
                    "invocation since boot)"
                )
                raise RuntimeError(msg)
            # The receiver maps ``EmergencyInvocationRequestedDetail.trigger_type``
            # against ``_TRIGGER_TYPE_BY_PRIMITIVE`` to enforce the cooldown.
            # Operator-console emergencies route through ``multi_rule_breach``
            # which respects the cooldown like any other non-margin trigger.
            # The cooldown_remaining_seconds field is the writer-side observation
            # (informational); the receiver re-checks against the live DB.
            cooldown_remaining = await self._compute_cooldown_remaining_seconds(session, now=now)
            entry = ActivityLogEntry(
                entry_id=entry_id,
                invocation_id=invocation_id,
                timestamp=now,
                event_type=EventType.EMERGENCY_INVOCATION_REQUESTED,
                event_group=EventGroup.RISK_AND_GUARDRAIL,
                position_id=None,
                order_id=None,
                thesis_id=None,
                source=EventSource.GUARDRAIL_LAYER,
                detail=EmergencyInvocationRequestedDetail(
                    trigger_type="multi_rule_breach",
                    trigger_reason=annotated_reason,
                    cooldown_remaining_seconds=cooldown_remaining,
                ),
            )
            session.add(activity_log_entry_to_row(entry))
            await session.commit()
        log.info(
            "operator-console emergency request queued entry_id=%s reason=%r",
            entry_id,
            annotated_reason,
        )
        return f"{_PENDING_INVOCATION_PREFIX}{entry_id}"

    def cooldown_info(self) -> CooldownInfo | None:
        """Synchronous cooldown probe is unavailable; receiver enforces.

        The verb's pre-flight check calls this synchronously; we can't run an
        async DB query from here.  Return ``None`` and let the receiver
        re-check the cooldown against the live ``invocations`` table when it
        dispatches.  This means a verb call during cooldown is accepted with
        a pending-emerg ID but the receiver will suppress the dispatch — the
        operator-console UX is correct because the LiveRun dashboard observes
        no follow-up ``invocation_started`` event.

        Follow-up: refactor the verb-flow to allow async cooldown probes so
        the verb itself returns ``cooldown_active`` HTTP 409 cleanly.
        """
        return None

    def running_info(self) -> RunningInfo | None:
        """Synchronous running probe is unavailable; APScheduler max_instances enforces.

        APScheduler's ``max_instances=1`` (per ``scheduler.yaml``) prevents
        concurrent invocations at the driver level.  The receiver task will
        re-check before dispatching.  Same follow-up applies as
        :meth:`cooldown_info`.
        """
        return None

    async def _compute_cooldown_remaining_seconds(
        self, session: AsyncSession, *, now: datetime
    ) -> int:
        """Inspect the most-recent completed emergency for an informational read.

        Runs on the caller's session so the cooldown read shares one snapshot
        with the invocation-id binding.
        """
        stmt = (
            select(InvocationRow.command_execution_completed_at)
            .where(
                InvocationRow.trigger_type == "emergency",
                InvocationRow.command_execution_completed_at.is_not(None),
            )
            .order_by(InvocationRow.command_execution_completed_at.desc())
            .limit(1)
        )
        text = (await session.execute(stmt)).scalar_one_or_none()
        if text is None:
            return 0
        last_completed = datetime.fromisoformat(text)
        if last_completed.tzinfo is None:
            last_completed = last_completed.replace(tzinfo=UTC)
        elapsed = (now - last_completed).total_seconds()
        cooldown_seconds = self._cooldown_minutes * 60
        remaining = int(cooldown_seconds - elapsed)
        return max(0, remaining)


def _build_operator_console_entry_id(*, now: datetime) -> str:
    """Build an ``entry_id`` for an operator-console-initiated emergency request.

    The monitor's evaluator uses ``MON.{session_id}.{trigger_id}`` per
    ``oms-command-ids.md``.  Operator-console emergencies have no monitor
    session; we mint a fresh invocation-id-shaped ID via :func:`mint_invocation_id`
    so the entry_id is monotonic + collision-free across operator-console
    issuances.
    """
    return f"opcon-{mint_invocation_id(now)}"


# ---------------------------------------------------------------------------
# UniverseValidator — placeholder.
# ---------------------------------------------------------------------------


class DeferredUniverseValidator:
    """Universe-validation runtime is not wired through the verb yet.

    The verb's ``UniverseValidationFailedError`` maps to HTTP 500
    ``internal_error``; this placeholder fires it with a descriptive
    cause so the command-center surfaces a useful operator message.
    Follow-up: wire ``scripts/ops/validate_universe.py`` through this adapter.
    """

    def validate(self, *, as_of: date) -> UniverseValidationReportRecord:
        del as_of
        raise UniverseValidationFailedError(
            cause=RuntimeError(
                "Universe validation via /control/run_universe_validation is "
                "not yet wired in production. Run scripts/ops/validate_universe.py "
                "directly or file a follow-up to ALP-720."
            )
        )
