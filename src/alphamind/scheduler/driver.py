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
from alphamind.execution.state_persistence.tables.invocations import InvocationRow
from alphamind.scheduler.orchestrator import run_invocation
from alphamind.scheduler.run_context import RunInvocationContext
from alphamind.scheduler.session import PipelineSession

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
) -> Callable[[], Awaitable[None]]:
    """Build the async coroutine APScheduler fires for one trigger.

    The returned coroutine dispatches ``run_invocation(trigger_type="scheduled",
    ...)`` against the story 03b orchestrator. Per parent decision (H) any
    exception is caught and logged; the daemon keeps running.
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
            )
        except Exception:
            log.exception("scheduled trigger=%s failed", trigger_key)
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

    return _job


def register_pipeline_jobs(
    *,
    scheduler: AsyncIOScheduler,
    scheduler_config: SchedulerConfig,
    context: RunInvocationContext,
) -> None:
    """Register one cron job per ``scheduler_config.triggers`` entry.

    Each job's ID matches the trigger key. Raises ``ValueError`` if a
    yaml trigger key does not resolve to a :class:`RunType` member.
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
        )
        scheduler.add_job(
            job,
            trigger=CronTrigger.from_crontab(cron_expression, timezone=scheduler_config.timezone),
            id=trigger_key,
            max_instances=scheduler_config.max_instances,
            coalesce=True,
            misfire_grace_time=60,
        )


async def run_pipeline_scheduler_task(
    session: PipelineSession,
    *,
    scheduler_config: SchedulerConfig,
    context: RunInvocationContext,
) -> None:
    """Build the ``AsyncIOScheduler``, register jobs, start, and await cancellation.

    Designed to be registered as a :class:`PipelineSupervisor` task. The
    supervisor calls this coroutine with the per-process
    :class:`PipelineSession`; ``context`` is closed over by the caller via
    ``functools.partial`` in ``__main__``.
    """
    log.info(
        "pipeline scheduler task start: process_lifetime_id=%s",
        session.process_lifetime_id,
    )
    scheduler = AsyncIOScheduler(timezone=scheduler_config.timezone)
    register_pipeline_jobs(
        scheduler=scheduler,
        scheduler_config=scheduler_config,
        context=context,
    )
    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("scheduled trigger=%s next fire at %s", job.id, job.next_run_time)
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=True)
        log.info("pipeline scheduler stopped")
