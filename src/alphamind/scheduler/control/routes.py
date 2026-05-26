"""FastAPI route handlers for the pipeline ``/control`` + ``/events`` surface (ALP-664).

Each route handler:

1. Receives a Pydantic request body (FastAPI validates inbound).
2. Dispatches to :mod:`alphamind.scheduler.control.verbs`, threading
   through the per-process :class:`VerbDispatch` held on ``app.state``.
3. Converts the frozen-dataclass result back into a Pydantic response
   model at the boundary (Pydantic at boundaries only — P5).
4. Maps typed :class:`~alphamind.scheduler.control.verbs.VerbError`
   subclasses to HTTP status codes + :class:`ControlErrorEnvelope`
   bodies per the schema's per-verb error table.

The ``GET /events`` handler subscribes to the shared
:class:`SSEEventEmitter` and returns a :class:`StreamingResponse` of
SSE-framed records; heartbeats are injected by
:meth:`SSEEventEmitter.iter_events` on idle connections.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from alphamind.config.control_handlers.profile_switch import ProfileNotFoundError
from alphamind.scheduler.control import events as events_module
from alphamind.scheduler.control import models, verbs

if TYPE_CHECKING:
    from alphamind.scheduler.control.app import VerbDispatch

__all__ = ["router"]

log = logging.getLogger(__name__)

router = APIRouter()


def _get_dispatch(request: Request) -> VerbDispatch:
    """Pull the per-process :class:`VerbDispatch` off ``app.state``."""
    return request.app.state.verb_dispatch  # type: ignore[no-any-return]


def _get_emitter(request: Request) -> events_module.SSEEventEmitter:
    """Pull the shared :class:`SSEEventEmitter` off ``app.state``."""
    return request.app.state.event_emitter  # type: ignore[no-any-return]


def _error_envelope(
    *,
    code: models._ErrorCode,
    detail: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    """Construct the canonical error envelope as a JSON-safe dict.

    Returned through ``HTTPException(detail=...)`` — FastAPI serializes
    the dict body verbatim, matching the schema's wire envelope.
    """
    inner: dict[str, object] = {"code": code, "detail": detail}
    if details is not None:
        inner["details"] = details
    return {"error": inner}


# ---------------------------------------------------------------------------
# Control verbs.
# ---------------------------------------------------------------------------


@router.post(
    "/control/pause",
    response_model=models.ControlResponseEnvelope,
)
async def post_pause(
    body: models.PauseRequest,
    request: Request,
) -> models.ControlResponseEnvelope:
    """``POST /control/pause`` — set the scheduler-pause flag."""
    dispatch = _get_dispatch(request)
    result = verbs.pause(
        scheduler=dispatch.scheduler,
        reason=body.reason,
        now=dispatch.now_factory(),
    )
    return models.ControlResponseEnvelope(status="accepted", applied_at=result.applied_at)


@router.post(
    "/control/resume",
    response_model=models.ControlResponseEnvelope,
)
async def post_resume(
    body: models.ResumeRequest,  # noqa: ARG001 — empty body validated by Pydantic
    request: Request,
) -> models.ControlResponseEnvelope:
    """``POST /control/resume`` — clear the scheduler-pause flag."""
    dispatch = _get_dispatch(request)
    result = verbs.resume(
        scheduler=dispatch.scheduler,
        now=dispatch.now_factory(),
    )
    return models.ControlResponseEnvelope(status="accepted", applied_at=result.applied_at)


@router.post(
    "/control/trigger_emergency_invocation",
    response_model=models.TriggerEmergencyInvocationResponse,
)
async def post_trigger_emergency_invocation(
    body: models.TriggerEmergencyInvocationRequest,
    request: Request,
) -> models.TriggerEmergencyInvocationResponse:
    """``POST /control/trigger_emergency_invocation`` — fire out-of-schedule invocation."""
    dispatch = _get_dispatch(request)
    try:
        result = await verbs.trigger_emergency_invocation(
            emergency=dispatch.emergency,
            reason=body.reason,
            now=dispatch.now_factory(),
        )
    except verbs.CooldownActiveError as exc:
        raise HTTPException(
            status_code=409,
            detail=_error_envelope(
                code="cooldown_active",
                detail=str(exc),
                details={
                    "cooldown_remaining_seconds": exc.cooldown_remaining_seconds,
                    "cooldown_started_at": exc.cooldown_started_at.isoformat(),
                },
            ),
        ) from exc
    except verbs.PreconditionFailedError as exc:
        raise HTTPException(
            status_code=409,
            detail=_error_envelope(
                code="precondition_failed",
                detail=str(exc),
                details={"running_invocation_id": exc.running_invocation_id},
            ),
        ) from exc
    return models.TriggerEmergencyInvocationResponse(
        status="accepted",
        applied_at=result.applied_at,
        invocation_id=result.invocation_id,
    )


@router.post(
    "/control/switch_profile",
    response_model=models.ControlResponseEnvelope,
)
async def post_switch_profile(
    body: models.SwitchProfileRequest,
    request: Request,
) -> models.ControlResponseEnvelope:
    """``POST /control/switch_profile`` — rewrite ``main.yaml``'s ``active_profile``."""
    dispatch = _get_dispatch(request)
    if dispatch.config_dir is None:
        # Composition error — should never occur in production.
        raise HTTPException(
            status_code=500,
            detail=_error_envelope(
                code="internal_error",
                detail="VerbDispatch.config_dir is unset; cannot serve switch_profile",
            ),
        )
    try:
        result = verbs.switch_profile(
            profile_name=body.profile_name,
            config_dir=dispatch.config_dir,
            now=dispatch.now_factory(),
        )
    except verbs.ValidationFailedError as exc:
        raise HTTPException(
            status_code=400,
            detail=_error_envelope(code="validation_failed", detail=str(exc)),
        ) from exc
    except ProfileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=_error_envelope(code="not_found", detail=str(exc)),
        ) from exc
    return models.ControlResponseEnvelope(status="accepted", applied_at=result.applied_at)


@router.post(
    "/control/run_universe_validation",
    response_model=models.RunUniverseValidationResponse,
)
async def post_run_universe_validation(
    body: models.RunUniverseValidationRequest,  # noqa: ARG001 — empty body validated by Pydantic
    request: Request,
) -> models.RunUniverseValidationResponse:
    """``POST /control/run_universe_validation`` — synchronous validation run."""
    dispatch = _get_dispatch(request)
    now = dispatch.now_factory()
    try:
        result = verbs.run_universe_validation(
            validator=dispatch.universe_validator,
            now=now,
            as_of=now.date(),
        )
    except verbs.UniverseValidationFailedError as exc:
        raise HTTPException(
            status_code=500,
            detail=_error_envelope(code="internal_error", detail=str(exc)),
        ) from exc
    return models.RunUniverseValidationResponse(
        status="accepted",
        applied_at=result.applied_at,
        report=_report_to_model(result.report),
    )


def _report_to_model(
    record: verbs.UniverseValidationReportRecord,
) -> models.UniverseValidationReport:
    """Convert the verb's frozen-dataclass report into the Pydantic boundary type."""
    return models.UniverseValidationReport(
        validated_at=record.validated_at,
        tickers=[
            models.UniverseValidationTickerRow(
                ticker=t.ticker,
                verdict=t.verdict,
                criteria=[
                    models.UniverseValidationCriterionRow(
                        criterion=c.criterion,
                        verdict=c.verdict,
                        computed_value=c.computed_value,
                        threshold=c.threshold,
                        note=c.note,
                    )
                    for c in t.criteria
                ],
            )
            for t in record.tickers
        ],
    )


# ---------------------------------------------------------------------------
# SSE event stream.
# ---------------------------------------------------------------------------


@router.get("/events")
async def get_events(request: Request) -> StreamingResponse:
    """``GET /events`` — SSE stream of pipeline state.

    Subscribes to the shared :class:`SSEEventEmitter`, framing each
    enqueued event into the SSE record shape and yielding bytes to the
    underlying ASGI server.  The stream terminates only when the
    connection drops.
    """
    emitter = _get_emitter(request)

    async def streamer() -> AsyncIterator[str]:
        async with emitter.subscribe() as queue:
            async for record in emitter.iter_events(queue=queue):
                if await request.is_disconnected():
                    return
                yield events_module.format_sse_record(record)

    return StreamingResponse(
        streamer(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Default ``now_factory``.
# ---------------------------------------------------------------------------


def default_now_factory() -> datetime:
    """Return ``datetime.now(UTC)``; lifted as a seam for tests/monkey-patching."""
    return datetime.now(UTC)
