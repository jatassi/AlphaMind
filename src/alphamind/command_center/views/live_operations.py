"""FastAPI view router for the live-operations pane (story 05b / ALP-672).

Ships two read-only snapshot endpoints consumed by the frontend's live
route on connect and reconnect.  Live updates between snapshots arrive
via the SSE multiplexer (``/api/events``).

* ``GET /api/views/live`` — assembles the three-pane snapshot: current
  invocation (or most-recent if none is running), active alerts, and a
  monitor-session summary.  All reads via :func:`foreign_reader_session`
  except alerts which uses ``cc_writer_session`` (the alerts table is
  cc-owned).

* ``GET /api/views/schedule`` — returns the most-recently-seen
  ``next_trigger_changed`` payload cached by the SSE subscriber below,
  plus a ``paused`` flag.  The cache is updated in-process by the
  :class:`EventMultiplexer` subscriber registered by
  :func:`make_schedule_cache_subscriber`.

Architectural notes:

* No cross-process APScheduler reads.  The command center and the
  pipeline process communicate only via the loopback SSE stream and the
  ``/control/*`` surface.  The schedule-preview data comes from the
  ``next_trigger_changed`` SSE event the pipeline emits whenever the
  scheduler reschedules a trigger.

* Module-level cache dict.  The schedule cache is an intentionally thin
  module-level ``dict`` — it carries transient screen state (ALP-128
  pre-resolved G: "transient screen state is never historical"), not
  persistent data.  Tests replace it via :func:`set_schedule_cache_for_test`.

* Pydantic at boundaries only.  The response models are the only Pydantic
  shapes in this module; internal helpers pass plain ``dict``/frozen-DC
  values.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from alphamind.command_center._kernel.events import PipelineEvent, PipelineEventType
from alphamind.command_center.auth.dependencies import current_session
from alphamind.command_center.events.multiplexer import CombinedEvent

__all__ = [
    "LiveSnapshot",
    "ScheduleSnapshot",
    "build_live_operations_router",
    "make_schedule_cache_subscriber",
    "set_schedule_cache_for_test",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level schedule cache
# ---------------------------------------------------------------------------


@dataclass
class _ScheduleCache:
    """Mutable container for the most-recently-seen schedule state.

    Updated by the SSE subscriber task registered during app startup.
    Reads are non-atomic but the data is transient screen state so
    tearing is acceptable — the frontend always cross-checks with the
    live SSE stream.
    """

    next_trigger_at: str | None = None
    next_trigger_type: str | None = None
    paused: bool = False
    raw_payloads: list[dict[str, Any]] = field(default_factory=list)


# Module-level singleton; replaced in tests via set_schedule_cache_for_test.
_schedule_cache: _ScheduleCache = _ScheduleCache()


def set_schedule_cache_for_test(cache: _ScheduleCache) -> None:
    """Inject a replacement cache for isolated unit tests.

    Not safe for concurrent use — intended for single-threaded Vitest
    /  pytest setups only.
    """
    global _schedule_cache  # global rebind for test isolation
    _schedule_cache = cache


# ---------------------------------------------------------------------------
# Pydantic boundary models
# ---------------------------------------------------------------------------


class InvocationSummary(BaseModel):
    """Subset of ``invocations`` columns surfaced in the live pane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str
    start_at: str
    phase1_completed_at: str | None
    phase2_completed_at: str | None
    trigger_type: str
    trigger_source: str
    active_profile: str
    active_mode: str


class AlertSummary(BaseModel):
    """Compact alert record for the active-alerts banner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert_id: str
    rule_name: str
    severity: str
    status: str
    fired_at: str
    context_json: str


class LiveSnapshot(BaseModel):
    """Response body for ``GET /api/views/live``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation: InvocationSummary | None
    active_alerts: list[AlertSummary]
    next_trigger_at: str | None
    next_trigger_type: str | None


class TriggerEntry(BaseModel):
    """One upcoming trigger entry in the schedule preview."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    next_trigger_at: str | None
    next_trigger_type: str | None


class ScheduleSnapshot(BaseModel):
    """Response body for ``GET /api/views/schedule``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: list[TriggerEntry]
    paused: bool


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _fetch_current_invocation(
    request: Request,
) -> InvocationSummary | None:
    """Return the running (or most-recent) invocation row via foreign_reader."""
    factory = getattr(request.app.state, "foreign_reader_session_factory", None)
    if factory is None:
        return None
    from sqlalchemy import text

    try:
        async with factory() as session:
            # Try to find an in-flight invocation first (phase2 not yet complete).
            result = await session.execute(
                text(
                    "SELECT invocation_id, start_at, phase1_completed_at,"
                    " phase2_completed_at, trigger_type, trigger_source,"
                    " active_profile, active_mode"
                    " FROM invocations"
                    " WHERE phase2_completed_at IS NULL"
                    " ORDER BY start_at DESC"
                    " LIMIT 1"
                )
            )
            row = result.mappings().first()
            if row is None:
                # Fall back to most-recent completed invocation.
                result = await session.execute(
                    text(
                        "SELECT invocation_id, start_at, phase1_completed_at,"
                        " phase2_completed_at, trigger_type, trigger_source,"
                        " active_profile, active_mode"
                        " FROM invocations"
                        " ORDER BY start_at DESC"
                        " LIMIT 1"
                    )
                )
                row = result.mappings().first()
            if row is None:
                return None
            return InvocationSummary(
                invocation_id=str(row["invocation_id"]),
                start_at=str(row["start_at"]),
                phase1_completed_at=(
                    str(row["phase1_completed_at"])
                    if row["phase1_completed_at"] is not None
                    else None
                ),
                phase2_completed_at=(
                    str(row["phase2_completed_at"])
                    if row["phase2_completed_at"] is not None
                    else None
                ),
                trigger_type=str(row["trigger_type"]),
                trigger_source=str(row["trigger_source"]),
                active_profile=str(row["active_profile"]),
                active_mode=str(row["active_mode"]),
            )
    except Exception as exc:
        log.warning("live_operations: invocation fetch failed: %s", exc)
        return None


async def _fetch_active_alerts(request: Request) -> list[AlertSummary]:
    """Return active (firing/snoozed) alerts via cc_writer_session."""
    factory = getattr(request.app.state, "cc_writer_session_factory", None)
    if factory is None:
        return []
    from sqlalchemy import text

    try:
        async with factory() as session:
            result = await session.execute(
                text(
                    "SELECT alert_id, rule_name, severity, status,"
                    " fired_at, context_json"
                    " FROM alerts"
                    " WHERE status IN ('firing', 'snoozed')"
                    " ORDER BY fired_at DESC"
                )
            )
            rows = result.mappings().all()
            return [
                AlertSummary(
                    alert_id=str(r["alert_id"]),
                    rule_name=str(r["rule_name"]),
                    severity=str(r["severity"]),
                    status=str(r["status"]),
                    fired_at=str(r["fired_at"]),
                    context_json=str(r["context_json"]),
                )
                for r in rows
            ]
    except Exception as exc:
        log.warning("live_operations: alerts fetch failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Schedule-cache SSE subscriber
# ---------------------------------------------------------------------------


def make_schedule_cache_subscriber(
    cache: _ScheduleCache | None = None,
) -> asyncio.Queue[CombinedEvent]:
    """Return a subscriber queue whose drain task updates the module cache.

    Intended to be called once during app startup and registered on the
    :class:`EventMultiplexer` as a long-lived subscriber.  The caller
    is responsible for draining the returned queue.

    In production ``cache`` is ``None`` and the module-level
    ``_schedule_cache`` is updated.  Tests pass an explicit
    ``_ScheduleCache`` to avoid global state contamination.
    """
    target = cache if cache is not None else _schedule_cache
    queue: asyncio.Queue[CombinedEvent] = asyncio.Queue(maxsize=256)

    async def _drain() -> None:
        while True:
            event = await queue.get()
            if (
                isinstance(event, PipelineEvent)
                and event.event_type == PipelineEventType.NEXT_TRIGGER_CHANGED
            ):
                payload = dict(event.payload)
                target.next_trigger_at = payload.get("next_trigger_at")
                target.next_trigger_type = payload.get("next_trigger_type")
                target.raw_payloads.append(payload)
            queue.task_done()

    # The drain coroutine is returned as-is; the caller is responsible for
    # wrapping it in a supervised task (story 05b § Wiring note).
    _drain_coroutine = _drain  # expose for supervised-task registration
    queue._drain_coroutine = _drain_coroutine  # type: ignore[attr-defined]
    return queue


# ---------------------------------------------------------------------------
# Route builder
# ---------------------------------------------------------------------------


def build_live_operations_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` carrying the two view endpoints."""
    router = APIRouter(prefix="/api/views", tags=["views"])

    @router.get("/live")
    async def get_live(
        request: Request,
        _session_id: object = Depends(current_session),
    ) -> LiveSnapshot:
        """One-shot snapshot for the live-operations pane.

        Assembles:
        * Current (or most-recent) invocation from ``invocations`` table.
        * Active alerts from the ``alerts`` cc-owned table.
        * Most-recently-seen ``next_trigger_at`` from the schedule cache.

        Used on connect and after SSE reconnect to bootstrap pane state.
        Live updates thereafter arrive via the SSE multiplexer.
        """
        del _session_id

        invocation, alerts = await asyncio.gather(
            _fetch_current_invocation(request),
            _fetch_active_alerts(request),
        )
        return LiveSnapshot(
            invocation=invocation,
            active_alerts=alerts,
            next_trigger_at=_schedule_cache.next_trigger_at,
            next_trigger_type=_schedule_cache.next_trigger_type,
        )

    @router.get("/schedule")
    async def get_schedule(
        _request: Request,
        _session_id: object = Depends(current_session),
    ) -> ScheduleSnapshot:
        """Next trigger preview from the event-driven schedule cache.

        Serves up to 5 ``next_trigger_changed`` payloads seen since
        last restart.  When the cache is empty (daemon just started and
        no ``next_trigger_changed`` event has arrived yet) the list is
        empty; the frontend should fall back to the SSE stream.
        """
        del _session_id

        payloads = _schedule_cache.raw_payloads[-5:] if _schedule_cache.raw_payloads else []
        entries = [
            TriggerEntry(
                next_trigger_at=p.get("next_trigger_at"),
                next_trigger_type=p.get("next_trigger_type"),
            )
            for p in payloads
        ]
        # If no history but current value is set, include it as the sole entry.
        if not entries and _schedule_cache.next_trigger_at is not None:
            entries = [
                TriggerEntry(
                    next_trigger_at=_schedule_cache.next_trigger_at,
                    next_trigger_type=_schedule_cache.next_trigger_type,
                )
            ]
        return ScheduleSnapshot(entries=entries, paused=_schedule_cache.paused)

    return router
