"""APScheduler driver for the pipeline scheduler (story 04a — ALP-446).

Registers one ``AsyncIOScheduler`` cron job per ``scheduler.yaml`` trigger
key, gates each fire on the NYSE trading calendar (weekday triggers only)
and on the overlap-dedup window across rolling triggers, then dispatches
``run_invocation(trigger_type="scheduled", ...)`` from the story 03b
orchestrator. The driver runs as a ``PipelineSupervisor`` task from
story 01 so SIGINT / SIGTERM cleanly shut it down.

Per parent decision (H), an exception raised by ``run_invocation`` is
caught and logged via ``log.exception``; the daemon keeps running.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta

import exchange_calendars
import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alphamind.config.models.run_types import RunType
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.scheduler.control.events import SSEEventEmitter
from alphamind.scheduler.control.models import NextTriggerChangedEvent
from alphamind.scheduler.orchestrator import run_invocation
from alphamind.scheduler.run_context import RunInvocationContext
from alphamind.scheduler.session import PipelineSession
from alphamind.state.tables.invocations import InvocationRow

__all__ = ["register_pipeline_jobs", "run_pipeline_scheduler_task"]

log = logging.getLogger(__name__)


# Hard-coded set of weekday triggers that consult the market calendar. The
# two weekend triggers fire unconditionally on their named days per parent
# decision (E). ``emergency`` (story 04b) is never a scheduled trigger.
_MARKET_CALENDAR_GATED_TRIGGERS: frozenset[RunType] = frozenset(
    {
        RunType.pre_open,
        RunType.market_hours_rolling,
        RunType.pre_close,
        RunType.off_hours_rolling,
    }
)

# Dedup applies ONLY to rolling triggers — anchored triggers (pre_open,
# pre_close, weekend_*) bypass the dedup guard so the documented schedule
# is preserved even when a slow invocation finished moments before.
_DEDUP_GATED_TRIGGERS: frozenset[RunType] = frozenset(
    {
        RunType.market_hours_rolling,
        RunType.off_hours_rolling,
    }
)


def _now_utc() -> datetime:
    """Return ``datetime.now(UTC)``; lifted as a seam for monkey-patching."""
    return datetime.now(UTC)


def _is_trading_day(calendar_name: str, on_date: date) -> bool:
    """Return ``True`` if *on_date* is a trading session on the named exchange."""
    calendar = exchange_calendars.get_calendar(calendar_name)
    return bool(calendar.is_session(pd.Timestamp(on_date)))


async def _is_within_dedup_window(
    session: AsyncSession,
    *,
    now: datetime,
    lookback_minutes: int,
) -> bool:
    """Return ``True`` if any invocation completed within the lookback window.

    The dedup query treats ``phase2_completed_at IS NULL`` as "not completed"
    — aborted runs do not suppress later fires.
    """
    cutoff = now - timedelta(minutes=lookback_minutes)
    cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
    stmt = (
        select(InvocationRow.invocation_id)
        .where(InvocationRow.phase2_completed_at.is_not(None))
        .where(InvocationRow.phase2_completed_at >= cutoff_iso)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


def _make_scheduled_job(
    *,
    trigger_key: str,
    run_type: RunType,
    cron_expression: str,
    scheduler_config: SchedulerConfig,
    context: RunInvocationContext,
    sse_emitter: SSEEventEmitter | None = None,
    on_fire_complete: Callable[[], None] | None = None,
) -> Callable[[], Awaitable[None]]:
    """Build the async coroutine APScheduler fires for one trigger.

    The returned coroutine dispatches ``run_invocation(trigger_type="scheduled",
    ...)`` against the story 03b orchestrator. Per parent decision (H) any
    exception is caught and logged; the daemon keeps running.

    ``sse_emitter`` is threaded through to :func:`run_invocation` so the
    invocation emits its own ``invocation_started`` / ``phase_transition`` /
    ``invocation_ended`` events (ALP-720).  ``on_fire_complete`` is a hook
    the driver fires after the job settles so the next-trigger preview can
    be re-emitted (the just-fired slot drops out of the upcoming queue).
    """

    async def _job() -> None:
        now = _now_utc()
        if run_type in _MARKET_CALENDAR_GATED_TRIGGERS and not _is_trading_day(
            scheduler_config.market_calendar_exchange, now.date()
        ):
            log.info("scheduled trigger=%s skipped — non-trading day", trigger_key)
            return
        if run_type in _DEDUP_GATED_TRIGGERS:
            async with context.session_factory() as dedup_session:
                if await _is_within_dedup_window(
                    dedup_session,
                    now=now,
                    lookback_minutes=scheduler_config.overlap_dedup_lookback_minutes,
                ):
                    log.info("scheduled trigger=%s skipped — dedup window", trigger_key)
                    return
        start_perf = time.monotonic()
        try:
            summary = await run_invocation(
                context=context,
                trigger_type="scheduled",
                trigger_source=trigger_key,
                trigger_reason=cron_expression,
                firing_run_type=run_type,
                now=now,
                sse_emitter=sse_emitter,
            )
        except Exception:
            # Per-job supervisor per runtime §G1: catch any orchestrator
            # exception so the daemon keeps running per parent decision (H).
            # ``BaseException`` (``CancelledError``) propagates so daemon
            # shutdown is honored.
            log.exception("scheduled trigger=%s failed", trigger_key)
            if on_fire_complete is not None:
                on_fire_complete()
            return
        duration = time.monotonic() - start_perf
        log.info(
            "scheduled trigger=%s completed invocation=%s duration=%.3fs "
            "commands_submitted=%d commands_rejected=%d",
            trigger_key,
            summary.invocation_id,
            duration,
            summary.commands_submitted,
            summary.commands_rejected,
        )
        if on_fire_complete is not None:
            on_fire_complete()

    return _job


def register_pipeline_jobs(
    *,
    scheduler: AsyncIOScheduler,
    scheduler_config: SchedulerConfig,
    context: RunInvocationContext,
    sse_emitter: SSEEventEmitter | None = None,
    on_fire_complete: Callable[[], None] | None = None,
) -> None:
    """Register one cron job per ``scheduler_config.triggers`` entry.

    Each job's ID matches the trigger key. Raises ``ValueError`` if a
    yaml trigger key does not resolve to a :class:`RunType` member.

    ``sse_emitter`` and ``on_fire_complete`` are threaded into each job's
    closure (ALP-720) so the invocation emits schema events and the
    next-trigger preview re-publishes after each fire.
    """
    for trigger_key, cron_expression in scheduler_config.triggers.items():
        try:
            run_type = RunType[trigger_key]
        except KeyError as exc:
            msg = f"scheduler.yaml trigger {trigger_key!r} has no matching RunType member"
            raise ValueError(msg) from exc
        job = _make_scheduled_job(
            trigger_key=trigger_key,
            run_type=run_type,
            cron_expression=cron_expression,
            scheduler_config=scheduler_config,
            context=context,
            sse_emitter=sse_emitter,
            on_fire_complete=on_fire_complete,
        )
        scheduler.add_job(
            job,
            trigger=CronTrigger.from_crontab(cron_expression, timezone=scheduler_config.timezone),
            id=trigger_key,
            max_instances=scheduler_config.max_instances,
            coalesce=True,
            misfire_grace_time=60,
        )


# Trigger keys that match the schema's ``_RunType`` literal in
# ``scheduler.control.models``. Under the Tier B schedule (ALP-745)
# APScheduler registers four jobs (per ``config/scheduler.yaml``), of which
# the weekend trigger (``weekend_sunday``) is a valid AlphaMind run type the
# wire-format schema does not include in its closed enum, so a
# ``NextTriggerChangedEvent`` constructed with a weekend key raises
# ``ValidationError``. Filter at the emit boundary so weekend slots simply
# don't update the operator console rather than silently dropping all weekend
# emits to an error log. This set is keyed on the wire-format enum, not the
# scheduled triggers, so it still lists ``off_hours_rolling`` even though
# Tier B no longer schedules it (it remains a valid manual / emergency type).
_SCHEMA_TRIGGER_TYPES: frozenset[str] = frozenset(
    {
        "market_hours_rolling",
        "off_hours_rolling",
        "pre_open",
        "pre_close",
        "emergency",
    }
)


def emit_next_trigger_changed(
    *,
    emitter: SSEEventEmitter,
    scheduler: AsyncIOScheduler,
) -> None:
    """Publish the current next-trigger preview as an SSE event (ALP-720).

    Reads the soonest ``next_run_time`` across registered jobs whose ID is
    in the schema's :class:`_RunType` literal, and emits one
    :class:`NextTriggerChangedEvent`. Idempotent on no jobs — emits
    nothing. Errors are logged and swallowed so a bad emit never crashes
    the daemon.

    Weekend-only triggers (``weekend_saturday`` / ``weekend_sunday``) are
    skipped because the schema's ``next_trigger_type`` literal does not
    include them; if every upcoming job is a weekend trigger, no event
    fires until a schema-valid trigger is the soonest again (e.g., the
    Monday ``market_hours_rolling`` slot once the weekend is past).
    """
    soonest: tuple[datetime, str] | None = None
    for job in scheduler.get_jobs():
        if job.id not in _SCHEMA_TRIGGER_TYPES:
            continue
        nrt = job.next_run_time
        if nrt is None:
            continue
        nrt_utc = nrt.astimezone(UTC)
        if soonest is None or nrt_utc < soonest[0]:
            soonest = (nrt_utc, job.id)
    if soonest is None:
        return
    try:
        emitter.emit(
            NextTriggerChangedEvent(
                next_trigger_at=soonest[0],
                next_trigger_type=soonest[1],  # type: ignore[arg-type]
            )
        )
    except Exception:
        log.exception("SSE next_trigger_changed emit failed trigger_key=%s", soonest[1])


async def run_pipeline_scheduler_task(
    session: PipelineSession,
    *,
    scheduler_config: SchedulerConfig,
    context: RunInvocationContext,
    scheduler: AsyncIOScheduler | None = None,
    sse_emitter: SSEEventEmitter | None = None,
) -> None:
    """Register jobs on the (optionally-injected) scheduler; start, await cancellation.

    Designed to be registered as a :class:`PipelineSupervisor` task. The
    supervisor calls this coroutine with the per-process
    :class:`PipelineSession`; ``context`` is closed over by the caller via
    ``functools.partial`` in ``__main__``.

    ``scheduler`` may be provided so the :class:`AsyncIOSchedulerControl`
    adapter (ALP-720) shares the same instance the verbs pause / resume.
    When ``None`` is passed the driver mints its own instance — used by
    tests and the legacy non-control-surface path.

    ``sse_emitter`` is threaded into the per-job closures so each
    invocation can emit ``invocation_started`` / ``phase_transition`` /
    ``invocation_ended`` / ``agent_started`` / ``agent_succeeded`` events.
    On daemon startup and after each fire, the driver also publishes a
    fresh ``next_trigger_changed`` event so the operator console always
    sees the upcoming schedule.
    """
    log.info(
        "pipeline scheduler task start: process_lifetime_id=%s",
        session.process_lifetime_id,
    )
    owned_scheduler = scheduler is None
    if scheduler is None:
        scheduler = AsyncIOScheduler(timezone=scheduler_config.timezone)

    def _on_fire_complete() -> None:
        # After each job fires, the just-fired slot drops out of the
        # upcoming queue; re-emit the next-trigger preview so the
        # operator console sees the new soonest fire.
        if sse_emitter is not None:
            emit_next_trigger_changed(emitter=sse_emitter, scheduler=scheduler)

    register_pipeline_jobs(
        scheduler=scheduler,
        scheduler_config=scheduler_config,
        context=context,
        sse_emitter=sse_emitter,
        on_fire_complete=_on_fire_complete,
    )
    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("scheduled trigger=%s next fire at %s", job.id, job.next_run_time)
    if sse_emitter is not None:
        emit_next_trigger_changed(emitter=sse_emitter, scheduler=scheduler)
    try:
        await asyncio.Event().wait()
    finally:
        # Only shut down the scheduler if we created it; otherwise the
        # caller owns its lifecycle (e.g. the daemon's __main__ shares it
        # with the SchedulerControl adapter and disposes at process exit).
        if owned_scheduler:
            scheduler.shutdown(wait=True)
        log.info("pipeline scheduler stopped")
