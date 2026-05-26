"""FastAPI route handlers for /control + /events (ALP-665).

The handlers are thin shells: parse Pydantic at the boundary, call the verb,
translate the typed ``VerbResult`` / ``VerbError`` into HTTP per the schema's
per-verb table.

Per the parent issue's architectural invariants, internal logic operates on
plain Python values; Pydantic lives only at the HTTP boundary. The verb
seams in ``verbs.py`` are :class:`Protocol`-typed so production substitutes
real implementations and tests substitute in-memory fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import APIRouter, Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from alphamind.execution.continuous_monitor.control.events import (
    EmittedEvent,
    SSEEventEmitter,
)
from alphamind.execution.continuous_monitor.control.models import (
    CancelOrderRequest,
    ControlErrorEnvelope,
    ControlResponseEnvelope,
    ErrorBody,
    ForceClosePositionRequest,
    ForceClosePositionResponse,
    HeartbeatEvent,
    SetHaltModeRequest,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    CancelEmitterProtocol,
    CloseSubmitterProtocol,
    HaltModeRepoProtocol,
    OrderLookup,
    PositionLookup,
    VerbError,
    VerbResult,
    cancel_order as _cancel_order_verb,
    force_close_position as _force_close_position_verb,
    set_halt_mode as _set_halt_mode_verb,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ControlSurfaceDependencies:
    """Wired-up collaborators the routes inject into the verbs.

    Built once by ``build_app`` from production wiring; tests substitute
    in-memory fakes for each Protocol field.
    """

    order_lookup: OrderLookup
    position_lookup: PositionLookup
    cancel_emitter: CancelEmitterProtocol
    close_submitter: CloseSubmitterProtocol
    halt_mode_repo: HaltModeRepoProtocol
    event_emitter: SSEEventEmitter


# ---------------------------------------------------------------------------
# Per-error-code → HTTP status (per schema's per-verb tables)
# ---------------------------------------------------------------------------


_ERROR_CODE_TO_HTTP = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "precondition_failed": status.HTTP_409_CONFLICT,
    "broker_error": status.HTTP_502_BAD_GATEWAY,
    "validation_failed": status.HTTP_400_BAD_REQUEST,
    "internal_error": status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _error_response(error: VerbError) -> JSONResponse:
    envelope = ControlErrorEnvelope(
        error=ErrorBody(code=error.code, detail=error.detail, details=error.details)
    )
    http_status = _ERROR_CODE_TO_HTTP.get(
        error.code, status.HTTP_500_INTERNAL_SERVER_ERROR
    )
    return JSONResponse(
        status_code=http_status, content=envelope.model_dump(mode="json")
    )


# ---------------------------------------------------------------------------
# Router builder
# ---------------------------------------------------------------------------


def build_router(
    deps: ControlSurfaceDependencies,
    *,
    heartbeat_interval_seconds: float = 15.0,
) -> APIRouter:
    """Construct an APIRouter wired to *deps*.

    Returned router is mounted at the app root by :func:`build_app`.
    """
    router = APIRouter()

    def _get_deps() -> ControlSurfaceDependencies:
        return deps

    # ----------------------- POST /control/cancel_order ------------------

    @router.post("/control/cancel_order", response_model=None)
    async def cancel_order_route(
        body: CancelOrderRequest,
        d: ControlSurfaceDependencies = Depends(_get_deps),
    ) -> JSONResponse:
        outcome = await _cancel_order_verb(
            order_id=body.order_id,
            order_lookup=d.order_lookup,
            cancel_emitter=d.cancel_emitter,
        )
        if isinstance(outcome, VerbError):
            return _error_response(outcome)
        # Render the success envelope.
        envelope = ControlResponseEnvelope(
            status="accepted", applied_at=outcome.applied_at
        )
        return JSONResponse(
            status_code=200, content=envelope.model_dump(mode="json")
        )

    # ----------------------- POST /control/force_close_position ----------

    @router.post("/control/force_close_position", response_model=None)
    async def force_close_position_route(
        body: ForceClosePositionRequest,
        d: ControlSurfaceDependencies = Depends(_get_deps),
    ) -> JSONResponse:
        outcome = await _force_close_position_verb(
            position_id=body.position_id,
            rationale=body.rationale,
            position_lookup=d.position_lookup,
            close_submitter=d.close_submitter,
        )
        if isinstance(outcome, VerbError):
            return _error_response(outcome)
        # ``envelope_id`` is always populated on the force_close happy path.
        if outcome.envelope_id is None:  # pragma: no cover - defensive
            msg = "force_close_position verb returned VerbResult without envelope_id"
            raise RuntimeError(msg)
        envelope = ForceClosePositionResponse(
            status="accepted",
            applied_at=outcome.applied_at,
            envelope_id=outcome.envelope_id,
        )
        return JSONResponse(
            status_code=200, content=envelope.model_dump(mode="json")
        )

    # ----------------------- POST /control/set_halt_mode -----------------

    @router.post("/control/set_halt_mode", response_model=None)
    async def set_halt_mode_route(
        body: SetHaltModeRequest,
        d: ControlSurfaceDependencies = Depends(_get_deps),
    ) -> JSONResponse:
        outcome = await _set_halt_mode_verb(
            enabled=body.enabled,
            reason=body.reason,
            repo=d.halt_mode_repo,
        )
        if isinstance(outcome, VerbError):
            return _error_response(outcome)
        envelope = ControlResponseEnvelope(
            status="accepted", applied_at=outcome.applied_at
        )
        return JSONResponse(
            status_code=200, content=envelope.model_dump(mode="json")
        )

    # ----------------------- GET /events ---------------------------------

    @router.get("/events")
    async def events_route(
        request: Request, d: ControlSurfaceDependencies = Depends(_get_deps)
    ) -> StreamingResponse:
        return StreamingResponse(
            _sse_iterator(
                d.event_emitter,
                request,
                heartbeat_interval_seconds=heartbeat_interval_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    return router


# ---------------------------------------------------------------------------
# SSE iterator
# ---------------------------------------------------------------------------


async def _sse_iterator(
    emitter: SSEEventEmitter,
    request: Request,
    *,
    heartbeat_interval_seconds: float,
) -> AsyncIterator[bytes]:
    """Pull events from a fresh subscriber queue and yield SSE frames.

    Per the schema's framing spec, each event is one record:

        event: <event_name>
        data: <json>
        \n

    The ``heartbeat`` event fires every ``heartbeat_interval_seconds`` when
    the queue is idle so a healthy connection always has a record within
    that window.
    """
    async with emitter.subscribe() as queue:
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_interval_seconds
                )
            except asyncio.TimeoutError:
                # Idle cadence — emit a heartbeat directly into the stream
                # (not via the broadcaster, so it only reaches this subscriber).
                heartbeat = HeartbeatEvent.model_validate({"timestamp": _now_iso_z()})
                event = EmittedEvent(
                    name="heartbeat", payload=heartbeat.model_dump(mode="json")
                )
            yield _render_sse_frame(event)


def _render_sse_frame(event: EmittedEvent) -> bytes:
    """Render one :class:`EmittedEvent` as an SSE record per the schema."""
    data = json.dumps(event.payload, separators=(",", ":"))
    frame = f"event: {event.name}\ndata: {data}\n\n"
    return frame.encode("utf-8")


def _now_iso_z() -> str:
    """ISO 8601 with ``Z`` suffix — the schema's documented timestamp shape."""
    from datetime import UTC, datetime  # local import keeps the module load light

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Validation-error → 400 mapper (FastAPI default returns 422; we map to 400)
# ---------------------------------------------------------------------------


def install_validation_error_handler(app: FastAPI) -> None:
    """Override FastAPI's 422 → 400 with the documented error envelope."""
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        del request
        envelope = ControlErrorEnvelope(
            error=ErrorBody(
                code="validation_failed",
                detail=_summarize_validation_error(exc),
            )
        )
        return JSONResponse(status_code=400, content=envelope.model_dump(mode="json"))


def _summarize_validation_error(exc: object) -> str:
    """One-line summary of the first validation error."""
    errors = getattr(exc, "errors", lambda: [])()
    if not errors:
        return "validation failed"
    first = errors[0]
    loc = ".".join(str(p) for p in first.get("loc", ()))
    msg = first.get("msg", "validation failed")
    return f"{loc}: {msg}"


__all__ = [
    "ControlSurfaceDependencies",
    "build_router",
    "install_validation_error_handler",
]
