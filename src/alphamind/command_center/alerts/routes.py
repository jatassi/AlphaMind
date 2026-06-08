"""FastAPI router for the alert APIs (story 05a / ALP-671).

Three endpoints:

* ``GET /api/alerts`` — list active alerts. Gated by
  ``Depends(current_session)``.
* ``POST /api/alerts/{id}/acknowledge`` — mark an alert acknowledged.
  Gated by both ``current_session`` + ``csrf_required`` + writes an
  activity-log row inside ``operator_invocation()`` so the audit trail
  carries the operator action.
* ``POST /api/alerts/{id}/snooze`` — mark an alert snoozed until the
  requested timestamp. Same gating + audit shape as acknowledge.

The route layer is the single place where the Pydantic request /
response models cross the persistence + audit seam — internal
persistence + engine code operates on frozen dataclasses.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from alphamind.command_center._kernel.control import ControlVerb
from alphamind.command_center._kernel.ids import (
    AlertId,
    OperatorSessionId,
    alert_id,
)
from alphamind.command_center._kernel.operator_invocation import (
    operator_invocation,
)
from alphamind.command_center.alerts.persistence import (
    list_active,
    load_alert,
    update_acknowledged,
    update_snoozed,
)
from alphamind.command_center.auth.dependencies import (
    csrf_required,
    current_session,
)
from alphamind.command_center.persistence.codecs import AlertRecord, AlertStatus
from alphamind.portfolio_state.events.risk_guardrail import (
    RiskParameterChangedDetail,
)
from alphamind.portfolio_state.events.types import (
    EventSource,
    EventType,
)
from alphamind.state.invocation_context.activity_log import (
    emit_activity_log_entry,
)
from alphamind.state.invocation_context.context import InvocationHandle

__all__ = [
    "ActiveAlertsResponse",
    "AlertResponse",
    "SnoozeAlertRequest",
    "build_alerts_router",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic boundary models (Pydantic-at-boundaries-only invariant).
# ---------------------------------------------------------------------------


class AlertResponse(BaseModel):
    """One row in :class:`ActiveAlertsResponse`.

    Mirrors :class:`AlertRecord` field-for-field; the route layer is the
    only Pydantic surface, and the model carries the JSON-API shape the
    browser consumes via ``GET /api/alerts``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str
    rule_name: str
    severity: str
    status: str
    fired_at: str
    acknowledged_at: str | None
    snoozed_until: str | None
    context_json: str


class ActiveAlertsResponse(BaseModel):
    """Response shape for ``GET /api/alerts``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alerts: list[AlertResponse] = Field(default_factory=list)


class SnoozeAlertRequest(BaseModel):
    """Request body for ``POST /api/alerts/{id}/snooze``.

    ``snoozed_until`` MUST be a tz-aware ISO-8601 timestamp; the
    Pydantic ``AwareDatetime`` field validates that. The route layer
    rejects naive datetimes at the boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snoozed_until: AwareDatetime


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _record_to_response(record: AlertRecord) -> AlertResponse:
    return AlertResponse(
        alert_id=str(record.alert_id),
        rule_name=str(record.rule_name),
        severity=record.severity.value,
        status=record.status.value,
        fired_at=record.fired_at,
        acknowledged_at=record.acknowledged_at,
        snoozed_until=record.snoozed_until,
        context_json=record.context_json,
    )


def _parse_alert_id_param(raw: str) -> AlertId:
    """Parse + validate the path-parameter alert id; raise 404 on invalid."""
    try:
        return alert_id(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="alert not found"
        ) from exc


def _write_audit_entry(
    handle: InvocationHandle,
    *,
    verb: ControlVerb,
    alert_id_: AlertId,
    record: AlertRecord,
    now: datetime,
) -> None:
    """Append a typed activity-log row inside the operator-invocation handle.

    The row's ``source`` is :data:`EventSource.OPERATOR_CONSOLE`, the
    event type is :data:`EventType.RISK_PARAMETER_CHANGED` (matching the
    convention in :func:`alphamind.command_center.control.audit.write_operator_action_entry`),
    and the detail payload carries ``alert_id`` + the new status + the
    rule name so the activity-log explorer can filter by rule.
    """
    if record.status == AlertStatus.SNOOZED:
        regime_label = "operator_console_snooze_alert"
        old_state = "firing"
        new_state = "snoozed"
        extra_fields: dict[str, object] = {"snoozed_until": record.snoozed_until or ""}
    else:
        regime_label = "operator_console_acknowledge_alert"
        old_state = "firing"
        new_state = "acknowledged"
        extra_fields = {}
    detail = RiskParameterChangedDetail(
        old_parameter_set_json={"alert_status": old_state},
        new_parameter_set_json={
            "alert_status": new_state,
            "alert_id": str(alert_id_),
            "rule_name": str(record.rule_name),
            "severity": record.severity.value,
            "verb": verb.value,
            **extra_fields,
        },
        regime_label=regime_label,
    )
    entry_id = f"{handle.invocation_id}-{verb.value}-{uuid.uuid4().hex}"
    emit_activity_log_entry(
        handle,
        event_type=EventType.RISK_PARAMETER_CHANGED,
        position_id=None,
        order_id=None,
        thesis_id=None,
        timestamp=now,
        detail=detail,
        source=EventSource.OPERATOR_CONSOLE,
        entry_id=entry_id,
    )


# ---------------------------------------------------------------------------
# Route handlers.
# ---------------------------------------------------------------------------


async def _handle_list_active(
    request: Request,
    _session_id: Annotated[OperatorSessionId, Depends(current_session)],
) -> ActiveAlertsResponse:
    """GET ``/api/alerts`` — return active alerts."""
    del _session_id
    factory = request.app.state.cc_writer_session_factory
    clock = getattr(request.app.state, "clock", None)
    now = clock() if clock is not None else datetime.now(UTC)
    records = await list_active(factory, now=now)
    return ActiveAlertsResponse(
        alerts=[_record_to_response(r) for r in records],
    )


async def _handle_acknowledge(
    alert_id_raw: str,
    request: Request,
    session_id: Annotated[OperatorSessionId, Depends(current_session)],
    _csrf: Annotated[None, Depends(csrf_required)],
) -> AlertResponse:
    """POST ``/api/alerts/{id}/acknowledge`` — mark acknowledged.

    Mutates the row + writes an activity-log entry via
    :func:`operator_invocation`. Returns the post-update alert state.
    """
    del _csrf
    alert_id_ = _parse_alert_id_param(alert_id_raw)
    factory = request.app.state.cc_writer_session_factory
    clock = getattr(request.app.state, "clock", None)
    now = clock() if clock is not None else datetime.now(UTC)
    # Idempotent ack: if the row is already acknowledged the update is a
    # no-op; we still want to return the current row state.
    updated = await update_acknowledged(factory, alert_id_=alert_id_, acknowledged_at=now)
    record = await load_alert(factory, alert_id_=alert_id_)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
    if updated:
        await _emit_operator_audit(
            request=request,
            session_id=session_id,
            verb=ControlVerb.ACKNOWLEDGE_ALERT,
            alert_id_=alert_id_,
            record=record,
            now=now,
        )
    return _record_to_response(record)


async def _handle_snooze(
    alert_id_raw: str,
    body: SnoozeAlertRequest,
    request: Request,
    session_id: Annotated[OperatorSessionId, Depends(current_session)],
    _csrf: Annotated[None, Depends(csrf_required)],
) -> AlertResponse:
    """POST ``/api/alerts/{id}/snooze`` — mark snoozed.

    Mutates the row + writes an activity-log entry via
    :func:`operator_invocation`. Returns the post-update alert state.
    """
    del _csrf
    alert_id_ = _parse_alert_id_param(alert_id_raw)
    factory = request.app.state.cc_writer_session_factory
    clock = getattr(request.app.state, "clock", None)
    now = clock() if clock is not None else datetime.now(UTC)
    if body.snoozed_until <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="snoozed_until must be in the future",
        )
    updated = await update_snoozed(factory, alert_id_=alert_id_, snoozed_until=body.snoozed_until)
    record = await load_alert(factory, alert_id_=alert_id_)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="alert is not in firing/snoozed state",
        )
    await _emit_operator_audit(
        request=request,
        session_id=session_id,
        verb=ControlVerb.SNOOZE_ALERT,
        alert_id_=alert_id_,
        record=record,
        now=now,
    )
    return _record_to_response(record)


async def _emit_operator_audit(
    *,
    request: Request,
    session_id: OperatorSessionId,
    verb: ControlVerb,
    alert_id_: AlertId,
    record: AlertRecord,
    now: datetime,
) -> None:
    """Open an :func:`operator_invocation` handle + append the audit row.

    Pulls the production session factory + process_lifetime_id off
    ``request.app.state`` (the lifespan + composition root wire both).
    Misses surface as ``500 Internal Server Error`` so a wiring miss
    fails loud rather than silently dropping the audit row.
    """
    production_factory = getattr(request.app.state, "production_session_factory", None)
    if production_factory is None:
        log.error("alerts surface not wired: production_session_factory unset")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="alerts surface not wired: production_session_factory unset",
        )
    process_lifetime_id = getattr(request.app.state, "process_lifetime_id", None)
    if process_lifetime_id is None:
        log.error("alerts surface not wired: process_lifetime_id unset")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="alerts surface not wired: process_lifetime_id unset",
        )
    async with operator_invocation(
        production_session_factory=production_factory,
        process_lifetime_id=process_lifetime_id,
        operator_session_id_=session_id,
        verb=verb,
        now=now,
    ) as handle:
        _write_audit_entry(
            handle,
            verb=verb,
            alert_id_=alert_id_,
            record=record,
            now=now,
        )


# ---------------------------------------------------------------------------
# Build router.
# ---------------------------------------------------------------------------


def build_alerts_router() -> APIRouter:
    """Construct a fresh :class:`APIRouter` carrying the three alert endpoints.

    Mirrors the events / control router pattern (one fresh router per
    ``build_app`` call so independent apps don't share router state).
    """
    router = APIRouter(prefix="/api/alerts", tags=["alerts"])
    router.add_api_route(
        "",
        _handle_list_active,
        methods=["GET"],
        response_model=ActiveAlertsResponse,
    )
    router.add_api_route(
        "/{alert_id_raw}/acknowledge",
        _handle_acknowledge,
        methods=["POST"],
        response_model=AlertResponse,
    )
    router.add_api_route(
        "/{alert_id_raw}/snooze",
        _handle_snooze,
        methods=["POST"],
        response_model=AlertResponse,
    )
    return router
