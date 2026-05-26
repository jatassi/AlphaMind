"""FastAPI route handlers for ``/api/control/*`` (story 04a / ALP-668).

Eight POST endpoints, one per :class:`ControlVerb`. Each handler:

1. ``Depends(current_session)`` → :class:`OperatorSessionId` (401 on
   any rejection path).
2. ``Depends(csrf_required)`` → double-submit CSRF check (403 on
   rejection).
3. Validates the Pydantic request body (the ``models.py`` boundary
   type with ``extra='forbid'``).
4. Builds a :class:`ProxyContext` from ``request.app.state`` (production
   session factory, process_lifetime_id, the validated session id) and
   the proxy-local clock factory.
5. Dispatches to the matching ``proxy_*`` function.
6. Renders the result envelope (or maps the failure ``ControlResult``
   onto the upstream HTTP status per the schema's per-verb table).

Per the ALP-128 invariants, this module is the only place the
``/api/control/*`` Pydantic models cross the proxy seam. The proxy
itself operates on frozen-dataclass results; the conversion happens
here.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
)
from alphamind.command_center._kernel.ids import OperatorSessionId
from alphamind.command_center.auth.dependencies import (
    csrf_required,
    current_session,
)
from alphamind.command_center.control.models import (
    CancelOrderRequest,
    ControlError,
    ControlErrorEnvelope,
    ControlResponseEnvelope,
    ForceClosePositionRequest,
    ForceClosePositionResponse,
    PauseRequest,
    ResumeRequest,
    RunUniverseValidationRequest,
    RunUniverseValidationResponse,
    SetHaltModeRequest,
    SwitchProfileRequest,
    TriggerEmergencyInvocationRequest,
    TriggerEmergencyInvocationResponse,
    UniverseValidationCriterionRow,
    UniverseValidationReport,
    UniverseValidationTickerRow,
)
from alphamind.command_center.control.pipeline_client import (
    PipelineUniverseValidationReport,
)
from alphamind.command_center.control.proxy import (
    ProxyContext,
    proxy_cancel_order,
    proxy_force_close_position,
    proxy_pause,
    proxy_resume,
    proxy_run_universe_validation,
    proxy_set_halt_mode,
    proxy_switch_profile,
    proxy_trigger_emergency_invocation,
)

__all__ = ["build_control_router"]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error-code → HTTP status (mirrors the upstream schemas' per-verb tables).
# ---------------------------------------------------------------------------


_ERROR_CODE_TO_HTTP: dict[ControlErrorCode, int] = {
    ControlErrorCode.VALIDATION_FAILED: status.HTTP_400_BAD_REQUEST,
    ControlErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ControlErrorCode.PRECONDITION_FAILED: status.HTTP_409_CONFLICT,
    ControlErrorCode.COOLDOWN_ACTIVE: status.HTTP_409_CONFLICT,
    ControlErrorCode.BROKER_ERROR: status.HTTP_502_BAD_GATEWAY,
    ControlErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def _build_error_envelope(result: ControlResult) -> ControlErrorEnvelope:
    """Build the response envelope for a failure :class:`ControlResult`."""
    if result.error_code is None or result.error_detail is None:
        # Defensive — every failure result has both fields populated by
        # ControlResult.failure(). Fall through to internal_error if
        # the invariant is somehow broken.
        code: ControlErrorCode = ControlErrorCode.INTERNAL_ERROR
        detail: str = "upstream returned a failure result without code/detail"
    else:
        code = result.error_code
        detail = result.error_detail
    return ControlErrorEnvelope(
        error=ControlError(code=code, detail=detail, details=None)
    )


def _raise_for_failure(result: ControlResult) -> None:
    """Raise :class:`HTTPException` for a failed :class:`ControlResult`.

    The HTTP body is the :class:`ControlErrorEnvelope` shape; the
    status code maps from ``result.error_code`` per the per-verb error
    tables in the upstream schemas.
    """
    code = result.error_code or ControlErrorCode.INTERNAL_ERROR
    http_status = _ERROR_CODE_TO_HTTP.get(code, status.HTTP_500_INTERNAL_SERVER_ERROR)
    envelope = _build_error_envelope(result)
    raise HTTPException(
        status_code=http_status,
        detail=envelope.model_dump(mode="json"),
    )


def _make_ctx(request: Request, session_id: OperatorSessionId) -> ProxyContext:
    """Build a :class:`ProxyContext` from the FastAPI request + validated session.

    Pulls the production session factory + process_lifetime_id off
    ``request.app.state``; the route layer is the only place that
    reaches into ``app.state`` so the proxy stays decoupled from
    FastAPI.
    """
    return ProxyContext(
        production_session_factory=request.app.state.production_session_factory,
        process_lifetime_id=request.app.state.process_lifetime_id,
        operator_session_id=session_id,
        now_factory=getattr(request.app.state, "clock", None),
    )


def _pipeline_client(request: Request) -> Any:
    """Pull the :class:`PipelineClient` off ``app.state``."""
    return request.app.state.pipeline_client


def _monitor_client(request: Request) -> Any:
    """Pull the :class:`MonitorClient` off ``app.state``."""
    return request.app.state.monitor_client


# ---------------------------------------------------------------------------
# Universe-validation report conversion (proxy mirror → Pydantic boundary).
# ---------------------------------------------------------------------------


def _report_to_pydantic(
    record: PipelineUniverseValidationReport,
) -> UniverseValidationReport:
    return UniverseValidationReport(
        validated_at=record.validated_at,
        tickers=[
            UniverseValidationTickerRow(
                ticker=t.ticker,
                verdict=t.verdict,  # type: ignore[arg-type]
                criteria=[
                    UniverseValidationCriterionRow(
                        criterion=c.criterion,  # type: ignore[arg-type]
                        verdict=c.verdict,  # type: ignore[arg-type]
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
# Router builder
# ---------------------------------------------------------------------------


def build_control_router() -> APIRouter:
    """Construct the ``/api/control/*`` router.

    The router is mounted under ``/api/control`` so each registered
    route's path is the verb's path segment without further prefixing.
    """
    router = APIRouter()

    @router.post("/pause", response_model=ControlResponseEnvelope)
    async def post_pause(
        body: PauseRequest,
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ControlResponseEnvelope:
        out = await proxy_pause(
            ctx=_make_ctx(request, session_id),
            pipeline=_pipeline_client(request),
            reason=body.reason,
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        return ControlResponseEnvelope(
            status="accepted", applied_at=_parse_applied_at(out.result)
        )

    @router.post("/resume", response_model=ControlResponseEnvelope)
    async def post_resume(
        body: ResumeRequest,  # noqa: ARG001 — empty body validated by Pydantic
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ControlResponseEnvelope:
        out = await proxy_resume(
            ctx=_make_ctx(request, session_id), pipeline=_pipeline_client(request)
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        return ControlResponseEnvelope(
            status="accepted", applied_at=_parse_applied_at(out.result)
        )

    @router.post(
        "/trigger_emergency_invocation",
        response_model=TriggerEmergencyInvocationResponse,
    )
    async def post_trigger_emergency(
        body: TriggerEmergencyInvocationRequest,
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> TriggerEmergencyInvocationResponse:
        out = await proxy_trigger_emergency_invocation(
            ctx=_make_ctx(request, session_id),
            pipeline=_pipeline_client(request),
            reason=body.reason,
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        if out.pipeline_invocation_id is None:
            # Defensive — happy path always populates the pipeline id.
            raise HTTPException(
                status_code=500,
                detail=_build_error_envelope(
                    ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail="upstream omitted invocation_id",
                    )
                ).model_dump(mode="json"),
            )
        return TriggerEmergencyInvocationResponse(
            status="accepted",
            applied_at=_parse_applied_at(out.result),
            invocation_id=out.pipeline_invocation_id,
        )

    @router.post("/switch_profile", response_model=ControlResponseEnvelope)
    async def post_switch_profile(
        body: SwitchProfileRequest,
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ControlResponseEnvelope:
        out = await proxy_switch_profile(
            ctx=_make_ctx(request, session_id),
            pipeline=_pipeline_client(request),
            profile_name=body.profile_name,
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        return ControlResponseEnvelope(
            status="accepted", applied_at=_parse_applied_at(out.result)
        )

    @router.post(
        "/run_universe_validation",
        response_model=RunUniverseValidationResponse,
    )
    async def post_run_universe_validation(
        body: RunUniverseValidationRequest,  # noqa: ARG001
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> RunUniverseValidationResponse:
        out = await proxy_run_universe_validation(
            ctx=_make_ctx(request, session_id), pipeline=_pipeline_client(request)
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        if out.report is None or not isinstance(
            out.report, PipelineUniverseValidationReport
        ):
            raise HTTPException(
                status_code=500,
                detail=_build_error_envelope(
                    ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail="upstream omitted report",
                    )
                ).model_dump(mode="json"),
            )
        return RunUniverseValidationResponse(
            status="accepted",
            applied_at=_parse_applied_at(out.result),
            report=_report_to_pydantic(out.report),
        )

    @router.post("/cancel_order", response_model=ControlResponseEnvelope)
    async def post_cancel_order(
        body: CancelOrderRequest,
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ControlResponseEnvelope:
        out = await proxy_cancel_order(
            ctx=_make_ctx(request, session_id),
            monitor=_monitor_client(request),
            order_id=body.order_id,
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        return ControlResponseEnvelope(
            status="accepted", applied_at=_parse_applied_at(out.result)
        )

    @router.post(
        "/force_close_position", response_model=ForceClosePositionResponse
    )
    async def post_force_close_position(
        body: ForceClosePositionRequest,
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ForceClosePositionResponse:
        out = await proxy_force_close_position(
            ctx=_make_ctx(request, session_id),
            monitor=_monitor_client(request),
            position_id=body.position_id,
            rationale=body.rationale,
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        if out.envelope_id is None:
            raise HTTPException(
                status_code=500,
                detail=_build_error_envelope(
                    ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail="upstream omitted envelope_id",
                    )
                ).model_dump(mode="json"),
            )
        return ForceClosePositionResponse(
            status="accepted",
            applied_at=_parse_applied_at(out.result),
            envelope_id=out.envelope_id,
        )

    @router.post("/set_halt_mode", response_model=ControlResponseEnvelope)
    async def post_set_halt_mode(
        body: SetHaltModeRequest,
        request: Request,
        session_id: Annotated[OperatorSessionId, Depends(current_session)],
        _csrf: Annotated[None, Depends(csrf_required)],
    ) -> ControlResponseEnvelope:
        out = await proxy_set_halt_mode(
            ctx=_make_ctx(request, session_id),
            monitor=_monitor_client(request),
            enabled=body.enabled,
            reason=body.reason,
        )
        if not out.result.ok:
            _raise_for_failure(out.result)
        return ControlResponseEnvelope(
            status="accepted", applied_at=_parse_applied_at(out.result)
        )

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_applied_at(result: ControlResult) -> Any:
    """Render the success ``ControlResult.applied_at`` as a datetime.

    The proxy carries ``applied_at`` as the raw ISO string the upstream
    returned (the wire shape); the Pydantic response model takes an
    :class:`AwareDatetime` so it can validate the timezone is present.
    Pydantic v2 accepts ISO-8601 strings directly for ``AwareDatetime``
    fields, so we forward the string and let the model normalize.
    """
    if result.applied_at is None:
        # Caller-side guard: the route layer never invokes this on a
        # failure path. Defensive default keeps mypy happy on the union.
        from datetime import UTC, datetime

        return datetime.now(UTC)
    return result.applied_at
