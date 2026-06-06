"""Emergency-invocation receiver task.

Polls ``activity_log`` for ``EMERGENCY_INVOCATION_REQUESTED`` entries written
by the continuous monitor per ``docs/design/06-risk-guardrails/breach-behavior.md``
§ Emergency invocation trigger. Each accepted entry results in one
``run_invocation(trigger_type="emergency", firing_run_type=RunType.emergency, ...)``
dispatch.

The 30-minute cooldown (``breach_behavior.yaml`` §
``emergency_invocation_cooldown_minutes``) suppresses repeat dispatches; a
``trigger_type='margin_call'`` entry bypasses the cooldown because the broker's
deadline is external and non-negotiable.

Per parent issue ``ALP-431`` § Notes for the orchestrator, an exception from
``run_invocation`` is caught and logged via ``log.exception`` — the receiver
keeps polling so a single failed dispatch does not kill the long-running task.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.run_types import RunType
from alphamind.portfolio_state.events.activity_log import (
    EmergencyInvocationRequestedDetail,
    EventType,
    decode_detail,
)
from alphamind.scheduler.control.events import SSEEventEmitter
from alphamind.scheduler.orchestrator import run_invocation
from alphamind.scheduler.run_context import RunInvocationContext
from alphamind.scheduler.session import PipelineSession
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow

__all__ = ["run_emergency_receiver_task"]

log = logging.getLogger(__name__)


async def _read_initial_high_water_mark(
    session_factory: async_sessionmaker[AsyncSession],
) -> int | None:
    """Return the max ``event_seq`` of any ``EMERGENCY_INVOCATION_REQUESTED`` row.

    The cursor keys on ``event_seq`` (the table's producer-independent insertion
    order, SQLite ``rowid``) rather than the producer-formatted ``entry_id`` —
    ``entry_id`` formats differ per producer (``opcon-…``, ``mon-emt-…``) and do
    not sort across producers, so a lexicographic ``entry_id`` cursor silently
    drops a later row whose prefix sorts below an already-processed one (ALP-870).

    The receiver initializes its high-water mark to this value at startup so
    pre-existing emergency requests written before the process came up are
    NOT replayed (per parent decision (G) — at-most-once semantics for
    requests already past the cooldown of any prior emergency dispatch).

    The ``event_type`` WHERE references a real column, anchoring the ``FROM`` for
    ``func.max(event_seq)`` (``event_seq`` is a ``literal_column`` over ``rowid``
    with no table of its own — see ``BrokerEventLogRow.event_seq``).
    """
    async with session_factory() as session:
        stmt = select(func.max(ActivityLogRow.event_seq)).where(
            ActivityLogRow.event_type == EventType.EMERGENCY_INVOCATION_REQUESTED.value
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def _read_new_emergency_entries(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    last_seen_seq: int | None,
) -> list[ActivityLogRow]:
    """Return new ``EMERGENCY_INVOCATION_REQUESTED`` rows with event_seq > last_seen."""
    async with session_factory() as session:
        stmt = select(ActivityLogRow).where(
            ActivityLogRow.event_type == EventType.EMERGENCY_INVOCATION_REQUESTED.value
        )
        if last_seen_seq is not None:
            stmt = stmt.where(ActivityLogRow.event_seq > last_seen_seq)
        stmt = stmt.order_by(ActivityLogRow.event_seq.asc())
        result = await session.execute(stmt)
        return list(result.scalars())


async def _most_recent_completed_emergency_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> datetime | None:
    """Return ``phase2_completed_at`` of the most recent completed emergency invocation."""
    async with session_factory() as session:
        stmt = (
            select(InvocationRow.phase2_completed_at)
            .where(
                InvocationRow.trigger_type == "emergency",
                InvocationRow.phase2_completed_at.is_not(None),
            )
            .order_by(InvocationRow.phase2_completed_at.desc())
            .limit(1)
        )
        text = (await session.execute(stmt)).scalar_one_or_none()
        if text is None:
            return None
        # Stored as Z-suffixed ISO 8601. ``datetime.fromisoformat`` accepts
        # the ``Z`` suffix directly on Python 3.11+.
        return datetime.fromisoformat(text)


def _is_cooldown_active(
    *,
    most_recent_completed_at: datetime | None,
    now: datetime,
    cooldown_minutes: int,
) -> bool:
    """Return True when the previous emergency completed within the cooldown window."""
    if most_recent_completed_at is None:
        return False
    return most_recent_completed_at >= now - timedelta(minutes=cooldown_minutes)


async def run_emergency_receiver_task(
    session: PipelineSession,
    *,
    poll_interval_seconds: float,
    cooldown_minutes: int,
    context: RunInvocationContext,
    sse_emitter: SSEEventEmitter | None = None,
) -> None:
    """Poll ``activity_log`` for emergency requests and dispatch ``run_invocation``.

    Loops until cancellation:

    1. Sleep ``poll_interval_seconds``.
    2. Query new ``EMERGENCY_INVOCATION_REQUESTED`` rows (event_seq > last-seen).
    3. For each new row:
       a. Parse :class:`EmergencyInvocationRequestedDetail` from ``detail_json``.
       b. **Cooldown check** — bypass for ``trigger_type='margin_call'``; otherwise
          query the most-recent completed ``trigger_type='emergency'`` invocation
          and suppress dispatch when its ``phase2_completed_at`` falls within the
          cooldown window.
       c. **Dispatch** ``run_invocation(...)``; log + swallow any exception so
          the receiver continues polling.
       d. Advance ``last_seen_seq``.

    Tests monkey-patch the module-level :func:`run_invocation` import to
    observe dispatch kwargs without running the full orchestrator.
    """
    last_seen_seq = await _read_initial_high_water_mark(context.session_factory)
    log.info(
        "emergency receiver task start: process_lifetime_id=%s last_seen_seq=%s",
        session.process_lifetime_id,
        last_seen_seq,
    )

    while True:
        try:
            await asyncio.sleep(poll_interval_seconds)
        except asyncio.CancelledError:
            log.info("emergency receiver task cancelled")
            raise

        new_rows = await _read_new_emergency_entries(
            context.session_factory, last_seen_seq=last_seen_seq
        )
        for row in new_rows:
            last_seen_seq = row.event_seq
            await _process_one_entry(
                row,
                cooldown_minutes=cooldown_minutes,
                context=context,
                sse_emitter=sse_emitter,
            )


async def _process_one_entry(
    row: ActivityLogRow,
    *,
    cooldown_minutes: int,
    context: RunInvocationContext,
    sse_emitter: SSEEventEmitter | None = None,
) -> None:
    """Parse, cooldown-check, and dispatch one emergency-request row.

    Any exception from :func:`run_invocation` is logged and swallowed so the
    surrounding poll loop continues per parent decision (H).
    """
    try:
        decoded = decode_detail(row.detail_json, EmergencyInvocationRequestedDetail)
    except (ValueError, TypeError) as exc:
        # ``decode_detail`` raises ``json.JSONDecodeError`` (a ``ValueError``)
        # for malformed JSON and ``TypeError`` for shape mismatches. Other
        # exceptions surface naturally — they would indicate a bug in the
        # decoder rather than a malformed payload.
        log.warning(
            "emergency entry_id=%s detail parse failed",
            row.entry_id,
            exc_info=exc,
        )
        return
    if not isinstance(decoded, EmergencyInvocationRequestedDetail):
        log.error(
            "emergency entry_id=%s decoded to unexpected type %s",
            row.entry_id,
            type(decoded).__name__,
        )
        return
    detail = decoded

    if detail.trigger_type != "margin_call":
        most_recent = await _most_recent_completed_emergency_at(context.session_factory)
        if _is_cooldown_active(
            most_recent_completed_at=most_recent,
            now=datetime.now(UTC),
            cooldown_minutes=cooldown_minutes,
        ):
            log.info(
                "emergency request entry_id=%s suppressed -- cooldown "
                "(most_recent_completed=%s, cooldown_minutes=%d)",
                row.entry_id,
                most_recent,
                cooldown_minutes,
            )
            return

    try:
        summary = await run_invocation(
            context=context,
            trigger_type="emergency",
            trigger_source="continuous_monitor",
            trigger_reason=detail.trigger_reason,
            firing_run_type=RunType.emergency,
            now=datetime.now(UTC),
            sse_emitter=sse_emitter,
        )
    except Exception:
        # Per-iteration supervisor per runtime §G1: any unhandled error from
        # ``run_invocation`` is logged so the receiver task keeps polling per
        # parent decision (H). ``BaseException`` (``CancelledError``) propagates
        # so external interruption bubbles to the supervisor.
        log.exception("emergency invocation entry_id=%s failed", row.entry_id)
        return

    log.info(
        "emergency invocation completed entry_id=%s invocation_id=%s duration=%.3fs",
        row.entry_id,
        summary.invocation_id,
        summary.duration_seconds,
    )
