"""FastAPI views for ``GET /api/views/live`` and ``GET /api/views/schedule``.

Story 05b / ALP-672 — View A: live run watcher + schedule preview.

Endpoints
---------

``GET /api/views/live``
    One-shot snapshot assembling the three panes the live run watcher
    renders: pipeline status (current / most-recent invocation), monitor
    state, and active alerts.  All DB reads use the ``foreign_reader``
    session; alerts use the ``cc_writer`` session (alerts table is
    command-center-owned).

``GET /api/views/schedule``
    Cache-from-events snapshot: next 5 trigger fires + pause state,
    sourced from the most-recently-seen ``next_trigger_changed`` pipeline
    event.  The cache lives in the module-level :data:`_schedule_cache`
    dict; a subscriber registered on the :class:`EventMultiplexer` via
    :func:`update_schedule_cache` keeps it fresh.

Cache discipline
----------------

The schedule cache is intentionally lossy: a cold start returns
``{"paused": false, "triggers": []}`` until the first
``next_trigger_changed`` event arrives.  Connection drops / restarts
lose no data because the cache persists for the process lifetime
(it is screen state, not history — per ALP-128 pre-resolved G).

Pydantic at boundaries
----------------------

All FastAPI response models are Pydantic (this module).  Internal DB
rows and event payloads are kept as dicts / SQLAlchemy row objects until
the boundary conversion here.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from alphamind.command_center._kernel.events import PipelineEvent, PipelineEventType
from alphamind.command_center._kernel.ids import OperatorSessionId
from alphamind.command_center.auth.dependencies import current_session

__all__ = [
    "LiveViewResponse",
    "ScheduleViewResponse",
    "build_views_router",
    "update_schedule_cache",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schedule cache (module-level, process-lifetime)
# ---------------------------------------------------------------------------

_schedule_cache: dict[str, Any] = {
    "paused": False,
    "triggers": [],
    "cached_at": None,
}
"""Last-seen ``next_trigger_changed`` payload + derived pause state.

Updated by :func:`update_schedule_cache` whenever the multiplexer
publishes a ``pipeline:next_trigger_changed`` event.  The route handler
reads directly without locking — reads are atomic on CPython and the
worst case is a slightly stale snapshot, which is fine for screen state.
"""


def update_schedule_cache(event: PipelineEvent) -> None:
    """Update the schedule cache from a ``next_trigger_changed`` event.

    Called by the subscriber task registered in ``app.py``'s lifespan.
    Only processes :attr:`PipelineEventType.NEXT_TRIGGER_CHANGED` frames;
    silently ignores anything else (safe to register as a generic
    pipeline-events subscriber).
    """
    if event.event_type is not PipelineEventType.NEXT_TRIGGER_CHANGED:
        return
    payload: Mapping[str, Any] = event.payload
    next_at = payload.get("next_trigger_at")
    next_type = payload.get("next_trigger_type")
    paused = bool(payload.get("paused", False))

    triggers: list[dict[str, Any]] = []
    if next_at is not None:
        triggers.append(
            {
                "trigger_at": next_at,
                "trigger_type": next_type,
            }
        )
    # ``next_triggers`` carries a list of up to 5 upcoming fires when the
    # upstream populates it; fall back to the single-trigger shape when only
    # the legacy pair is present.
    extra: Any = payload.get("next_triggers")
    if isinstance(extra, list):
        triggers = [
            {"trigger_at": t.get("trigger_at"), "trigger_type": t.get("trigger_type")}
            for t in extra[:5]
            if isinstance(t, dict)
        ]
    elif not triggers:
        # No trigger data at all — leave triggers empty.
        pass

    _schedule_cache["paused"] = paused
    _schedule_cache["triggers"] = triggers[:5]
    _schedule_cache["cached_at"] = datetime.now(UTC).isoformat()
    log.debug(
        "schedule cache updated: paused=%s triggers=%d",
        paused,
        len(triggers),
    )


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class InvocationStatusModel(BaseModel):
    """Snapshot of the most-recent / in-flight invocation."""

    invocation_id: str | None = None
    run_type: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    status: str | None = None
    current_phase: str | None = None
    """Derived from phase_durations: the last phase key if status is running."""
    phase_durations: dict[str, Any] | None = None
    agent_metrics: dict[str, Any] | None = None
    retry_count: int | None = None
    error_summary: str | None = None


class MonitorStatusModel(BaseModel):
    """Snapshot of continuous-monitor state."""

    websocket_connected: bool = False
    time_since_connect_seconds: float | None = None
    last_fill_at: str | None = None
    breach_active: bool = False
    breach_rule: str | None = None


class AlertSummaryModel(BaseModel):
    """Compact alert row for the active-alerts banner."""

    alert_id: str
    rule_name: str
    severity: str
    fired_at: str
    context_json: str


class LiveViewResponse(BaseModel):
    """Response shape for ``GET /api/views/live``."""

    pipeline: InvocationStatusModel
    monitor: MonitorStatusModel
    active_alerts: list[AlertSummaryModel]
    assembled_at: str


class ScheduleTriggerModel(BaseModel):
    """One upcoming scheduled trigger."""

    trigger_at: str | None = None
    trigger_type: str | None = None


class ScheduleViewResponse(BaseModel):
    """Response shape for ``GET /api/views/schedule``."""

    paused: bool
    triggers: list[ScheduleTriggerModel]
    cached_at: str | None


# ---------------------------------------------------------------------------
# DB read helpers
# ---------------------------------------------------------------------------


async def _read_pipeline_status(request: Request) -> InvocationStatusModel:
    """Read the most-recent invocation from the foreign-reader session.

    Queries the ``invocations`` table which carries execution scaffolding
    (``start_at``, ``phase1_completed_at``, ``phase2_completed_at``),
    trigger metadata (``trigger_type``), and JSON summary columns
    (``fill_collection_summary_json``, ``command_execution_summary_json``).
    """
    foreign_reader: Any = getattr(request.app.state, "foreign_reader_session_factory", None)
    if foreign_reader is None:
        return InvocationStatusModel()
    try:
        import json

        from sqlalchemy import text

        async with foreign_reader() as session:
            row = await session.execute(
                text(
                    "SELECT invocation_id, trigger_type, start_at,"
                    " phase1_completed_at, phase2_completed_at,"
                    " fill_collection_summary_json,"
                    " command_execution_summary_json"
                    " FROM invocations"
                    " ORDER BY start_at DESC LIMIT 1"
                )
            )
            record = row.mappings().first()
        if record is None:
            return InvocationStatusModel()
        fill_summary: dict[str, Any] | None = None
        cmd_summary: dict[str, Any] | None = None
        if record.get("fill_collection_summary_json"):
            with contextlib.suppress(ValueError, TypeError):
                fill_summary = json.loads(record["fill_collection_summary_json"])
        if record.get("command_execution_summary_json"):
            with contextlib.suppress(ValueError, TypeError):
                cmd_summary = json.loads(record["command_execution_summary_json"])
        # Derive current_phase from completion timestamps.
        # Phase 2 completed → ended; Phase 1 completed → in phase 2;
        # neither → in phase 1 (or pre-phase).
        current_phase: str | None = None
        if not record.get("phase2_completed_at"):
            current_phase = "phase2" if record.get("phase1_completed_at") else "phase1"
        # status derived: has phase2_completed_at → completed, else running.
        status = "completed" if record.get("phase2_completed_at") else "running"
        return InvocationStatusModel(
            invocation_id=record.get("invocation_id"),
            run_type=record.get("trigger_type"),
            started_at=record.get("start_at"),
            ended_at=record.get("phase2_completed_at"),
            status=status,
            current_phase=current_phase,
            phase_durations={
                "phase1_completed_at": record.get("phase1_completed_at"),
                "phase2_completed_at": record.get("phase2_completed_at"),
            },
            agent_metrics={
                "fill_summary": fill_summary,
                "command_summary": cmd_summary,
            },
        )
    except Exception:
        log.exception("live view: failed to read invocations")
        return InvocationStatusModel()


async def _read_monitor_status(request: Request) -> MonitorStatusModel:
    """Return a placeholder monitor-status snapshot.

    The monitor pane was originally implemented to read
    ``websocket_connected`` / ``websocket_disconnected`` / ``fill_received``
    / ``breach_detected`` entries from ``activity_log``, but those names
    are SSE event labels emitted by the continuous-monitor sidecar
    (see ``alphamind.execution.continuous_monitor.control.events``) —
    they are not :class:`EventType` members and never land in
    ``activity_log``. The legacy raw-SQL query also referenced a
    non-existent ``timestamp`` column (the table's column is ``entry_at``)
    so the previous implementation always returned the empty snapshot
    via its blanket ``except Exception`` swallow.

    Until the live monitor surface is wired through a dedicated
    persistence path, we return ``MonitorStatusModel()`` with ``None``
    placeholders so the frontend can render "unwired" rather than be
    silently lied to. The ``request`` parameter remains in the
    signature so the future wiring slot drops in without touching
    callers.
    """
    del request
    # Stub — see docstring for context. The frontend uses ``None`` /
    # ``False`` placeholders to render the pane as "no live monitor
    # state available yet".
    return MonitorStatusModel(
        websocket_connected=False,
        time_since_connect_seconds=None,
        last_fill_at=None,
        breach_active=False,
        breach_rule=None,
    )


async def _read_active_alerts(request: Request) -> list[AlertSummaryModel]:
    """Read active (firing — not yet acknowledged) alerts from the cc_writer session.

    ``AlertStatus`` (see :mod:`alphamind.command_center.persistence.codecs`)
    is the enum ``firing`` / ``acknowledged`` / ``snoozed``; the legacy
    raw-SQL query filtered on ``status = 'active'`` which is not a member
    and silently matched zero rows. We use :class:`AlertStatus.FIRING`
    here so the enum's source of truth catches future renames at type-
    check time.
    """
    cc_writer: Any = getattr(request.app.state, "cc_writer_session_factory", None)
    if cc_writer is None:
        return []
    try:
        from sqlalchemy import text

        from alphamind.command_center.persistence.codecs import AlertStatus

        async with cc_writer() as session:
            rows = await session.execute(
                text(
                    "SELECT alert_id, rule_name, severity, fired_at, context_json"
                    " FROM alerts"
                    " WHERE status = :status"
                    " ORDER BY fired_at DESC"
                ),
                {"status": AlertStatus.FIRING.value},
            )
            records = rows.mappings().all()
        return [
            AlertSummaryModel(
                alert_id=r["alert_id"],
                rule_name=r["rule_name"],
                severity=r["severity"],
                fired_at=r["fired_at"],
                context_json=r["context_json"],
            )
            for r in records
        ]
    except Exception:
        log.exception("live view: failed to read active alerts")
        return []


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _handle_live(
    request: Request,
    _session: Annotated[OperatorSessionId, Depends(current_session)],
) -> LiveViewResponse:
    """``GET /api/views/live`` — assemble the live run watcher snapshot."""
    pipeline_status, monitor_status, active_alerts = (
        await _read_pipeline_status(request),
        await _read_monitor_status(request),
        await _read_active_alerts(request),
    )
    return LiveViewResponse(
        pipeline=pipeline_status,
        monitor=monitor_status,
        active_alerts=active_alerts,
        assembled_at=datetime.now(UTC).isoformat(),
    )


async def _handle_schedule(
    _session: Annotated[OperatorSessionId, Depends(current_session)],
) -> ScheduleViewResponse:
    """``GET /api/views/schedule`` — serve the event-driven schedule cache."""
    return ScheduleViewResponse(
        paused=_schedule_cache["paused"],
        triggers=[
            ScheduleTriggerModel(
                trigger_at=t.get("trigger_at"),
                trigger_type=t.get("trigger_type"),
            )
            for t in _schedule_cache["triggers"]
        ],
        cached_at=_schedule_cache.get("cached_at"),
    )


def build_views_router() -> APIRouter:
    """Return a fresh :class:`APIRouter` carrying the two view routes.

    Mirrors :func:`build_control_router` — a fresh router per call so
    multiple ``build_app`` instances do not share route state.
    """
    router = APIRouter()
    router.add_api_route(
        "/live",
        _handle_live,
        methods=["GET"],
        response_model=LiveViewResponse,
    )
    router.add_api_route(
        "/schedule",
        _handle_schedule,
        methods=["GET"],
        response_model=ScheduleViewResponse,
    )
    return router
