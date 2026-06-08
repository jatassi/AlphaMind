"""Activity-log emission for ``/api/control/*`` operator actions (story 04a / ALP-668).

One helper (:func:`write_operator_action_entry`) the proxy invokes
inside the open ``OperatorInvocationHandle`` to append a typed
:class:`ActivityLogEntry` row.  The row's ``source`` is always
:data:`EventSource.OPERATOR_CONSOLE`; the per-verb event-type mapping
follows the design doc's § Operator actions:

* ``pause`` / ``resume`` / ``set_halt_mode`` →
  :data:`EventType.RISK_PARAMETER_CHANGED` with the old / new parameter
  set JSON capturing the verb's before / after of the relevant flag.
* ``trigger_emergency_invocation`` →
  :data:`EventType.EMERGENCY_INVOCATION_REQUESTED` carrying the
  operator-supplied reason and a zero cooldown (the operator's request
  bypasses the cooldown check — the upstream verb's pre-flight already
  rejected when a cooldown is active).
* ``cancel_order`` → :data:`EventType.ORDER_CANCELLED` with
  ``cancel_reason="operator_cancel"`` and a sentinel
  ``filled_quantity_at_cancellation=0`` (the row is operator-attribution
  metadata; the broker fill-handler writes the authoritative
  ``ORDER_CANCELLED`` row with the actual fill state on the broker
  confirmation).
* ``force_close_position`` →
  :data:`EventType.RISK_PARAMETER_CHANGED` capturing the operator's
  request to flip the position from ``open`` to ``force_close_requested``
  with the synthesized engine envelope id under
  ``new_parameter_set_json``. The position-closed event itself is
  written by the OMS write path on broker confirmation.
* ``run_universe_validation`` → no row (read-only verb).
* ``switch_profile`` → split by outcome: the SUCCESS path delegates
  to
  :func:`alphamind.state.invocation_context.config_change.emit_profile_switch_entry`
  (typed :class:`ProfileSwitchedDetail` preserving previous / new
  profile names); the FAILURE path falls through to a generic
  :data:`EventType.RISK_PARAMETER_CHANGED` row from this helper so
  the rejection lands on the activity log alongside the other
  verbs (F8). Calling this helper for ``switch_profile`` with
  ``result.ok=True`` raises :class:`ValueError` so a future
  maintainer doesn't accidentally double-write.

The helper is synchronous because :func:`emit_activity_log_entry`
itself is synchronous (it ``session.add()``-s; the surrounding context
commits on clean exit).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from alphamind.command_center._kernel.control import (
    ControlResult,
    ControlVerb,
)
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.events.order_lifecycle import OrderCancelledDetail
from alphamind.portfolio_state.events.risk_guardrail import (
    EmergencyInvocationRequestedDetail,
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

__all__ = ["write_operator_action_entry"]

log = logging.getLogger(__name__)


_NO_AUDIT_VERBS: frozenset[ControlVerb] = frozenset(
    {
        # Read-only verb — no state change to audit beyond the invocation row.
        ControlVerb.RUN_UNIVERSE_VALIDATION,
        # Alert-surface verbs (story 05a / ALP-671) write their own
        # activity-log rows in the alert-routes module (typed
        # ``RiskParameterChangedDetail`` capturing the alert id + new
        # status). This helper is the proxy's verb path; the alert
        # routes don't reach it.
        ControlVerb.ACKNOWLEDGE_ALERT,
        ControlVerb.SNOOZE_ALERT,
    }
)
"""Verbs that do NOT produce an audit row from this helper.

* ``run_universe_validation`` is read-only — no state change to audit.
* ``acknowledge_alert`` / ``snooze_alert`` write directly from the
  alert-routes module (story 05a) — not via the proxy's audit helper.

``switch_profile`` is NOT in the set: its success path delegates to
:func:`emit_profile_switch_entry` (typed
:class:`ProfileSwitchedDetail`), enforced by the explicit
``result.ok``-guarded early-raise at the top of
:func:`write_operator_action_entry`; the failure path falls through
to the generic operator-action row (F8).

For verbs in this set, the proxy skips the helper entirely; calling
it anyway no-ops (returns ``None``).
"""


def write_operator_action_entry(
    *,
    handle: InvocationHandle,
    verb: ControlVerb,
    parameters: Mapping[str, Any],
    result: ControlResult,
    now: datetime | None = None,
) -> ActivityLogEntry | None:
    """Append a typed activity-log row for the operator action.

    Returns the persisted entry, or ``None`` when the verb's mapping
    suppresses a row (e.g. ``run_universe_validation``).

    Parameters
    ----------
    handle:
        The open :class:`InvocationHandle` from
        :func:`alphamind.command_center._kernel.operator_invocation.operator_invocation`.
        The row's ``invocation_id`` is bound to ``handle.invocation_id``;
        the underlying session is the binding scope (the row commits
        atomically with the surrounding context).
    verb:
        The dispatched :class:`ControlVerb`.
    parameters:
        The verb's request parameters as a plain mapping
        (``{"reason": "halt"}`` / ``{"order_id": "ord-1"}`` / etc.).
        Serialized into the detail payload's
        ``old_parameter_set_json`` / ``new_parameter_set_json`` /
        ``cancel_reason`` / etc. fields depending on the per-verb
        mapping below.
    result:
        The upstream call's :class:`ControlResult`. The detail payload
        captures whether the call succeeded (``result.ok``) plus the
        error envelope on the failure path.
    now:
        Optional tz-aware UTC datetime. Defaults to
        :func:`datetime.now(UTC)`.

    Raises
    ------
    ValueError:
        If ``verb`` is :data:`ControlVerb.SWITCH_PROFILE` AND
        ``result.ok`` is True. The success path is delegated to
        :func:`emit_profile_switch_entry` (which preserves the
        previous / new profile names in a typed detail); the failure
        path falls through to the generic operator-action row so the
        rejection lands on the activity log alongside the other
        verbs (F8).
    """
    if verb == ControlVerb.SWITCH_PROFILE and result.ok:
        msg = (
            "switch_profile success path is audited via emit_profile_switch_entry, "
            "not write_operator_action_entry"
        )
        raise ValueError(msg)
    if verb in _NO_AUDIT_VERBS:
        return None

    timestamp = now if now is not None else datetime.now(UTC)
    if timestamp.tzinfo is None:
        msg = "now must be tz-aware UTC"
        raise ValueError(msg)

    detail, event_type, position_id, order_id = _build_detail(
        verb=verb, parameters=parameters, result=result
    )
    return emit_activity_log_entry(
        handle,
        event_type=event_type,
        position_id=position_id,
        order_id=order_id,
        thesis_id=None,
        timestamp=timestamp,
        detail=detail,
        source=EventSource.OPERATOR_CONSOLE,
    )


def _build_detail(  # noqa: PLR0911 — one return per ControlVerb branch
    *,
    verb: ControlVerb,
    parameters: Mapping[str, Any],
    result: ControlResult,
) -> tuple[Any, EventType, str | None, str | None]:
    """Dispatch by verb to the matching detail dataclass + event type.

    Returns ``(detail, event_type, position_id, order_id)`` so the caller can
    populate the :class:`ActivityLogEntry` columns consistently. The
    ``event_group`` is no longer returned — :func:`build_activity_log_entry`
    derives it from the registry (every branch's group already equals the
    registry value, and the read path re-derives it regardless).
    """
    if verb == ControlVerb.PAUSE:
        detail = RiskParameterChangedDetail(
            old_parameter_set_json={"paused": False},
            new_parameter_set_json={
                "paused": True,
                "reason": parameters.get("reason"),
                "result_ok": result.ok,
                "applied_at": result.applied_at,
                "error_code": result.error_code.value if result.error_code else None,
                "error_detail": result.error_detail,
            },
            regime_label="operator_console_pause",
        )
        return detail, EventType.RISK_PARAMETER_CHANGED, None, None

    if verb == ControlVerb.RESUME:
        detail = RiskParameterChangedDetail(
            old_parameter_set_json={"paused": True},
            new_parameter_set_json={
                "paused": False,
                "result_ok": result.ok,
                "applied_at": result.applied_at,
                "error_code": result.error_code.value if result.error_code else None,
                "error_detail": result.error_detail,
            },
            regime_label="operator_console_resume",
        )
        return detail, EventType.RISK_PARAMETER_CHANGED, None, None

    if verb == ControlVerb.SET_HALT_MODE:
        enabled = bool(parameters.get("enabled"))
        detail = RiskParameterChangedDetail(
            old_parameter_set_json={"halt_mode_enabled": not enabled},
            new_parameter_set_json={
                "halt_mode_enabled": enabled,
                "reason": parameters.get("reason"),
                "result_ok": result.ok,
                "applied_at": result.applied_at,
                "error_code": result.error_code.value if result.error_code else None,
                "error_detail": result.error_detail,
            },
            regime_label="operator_console_set_halt_mode",
        )
        return detail, EventType.RISK_PARAMETER_CHANGED, None, None

    if verb == ControlVerb.TRIGGER_EMERGENCY_INVOCATION:
        reason = parameters.get("reason")
        # The operator-initiated trigger has a synthetic ``operator_console``
        # trigger source. The detail class's ``trigger_type`` is a closed
        # set keyed to the monitor's automatic triggers — we pick
        # ``regime_jump`` as the closest semantic fit when the operator
        # initiates the call (the operator's reason text carries the actual
        # rationale verbatim).
        emergency_detail = EmergencyInvocationRequestedDetail(
            trigger_type="regime_jump",
            trigger_reason=f"operator_console: {reason}",
            cooldown_remaining_seconds=0,
        )
        return (
            emergency_detail,
            EventType.EMERGENCY_INVOCATION_REQUESTED,
            None,
            None,
        )

    if verb == ControlVerb.CANCEL_ORDER:
        order_id = parameters.get("order_id")
        cancel_detail = OrderCancelledDetail(
            cancel_reason="operator_cancel",
            filled_quantity_at_cancellation=0,
        )
        return (
            cancel_detail,
            EventType.ORDER_CANCELLED,
            None,
            str(order_id) if order_id is not None else None,
        )

    if verb == ControlVerb.SWITCH_PROFILE:
        # Failure-path audit (F8). The success path is delegated to
        # emit_profile_switch_entry and never reaches here. We render
        # the rejection as a RISK_PARAMETER_CHANGED row so the operator
        # console + the activity log carry the attempted action even
        # when the upstream pipeline rejects it. The previous profile
        # is unknown at this layer (upstream returned a None outcome on
        # failure) so we omit it from the parameter-set deltas.
        switch_detail = RiskParameterChangedDetail(
            old_parameter_set_json={},
            new_parameter_set_json={
                "profile_name": parameters.get("profile_name"),
                "result_ok": result.ok,
                "applied_at": result.applied_at,
                "error_code": result.error_code.value if result.error_code else None,
                "error_detail": result.error_detail,
            },
            regime_label="operator_console_switch_profile",
        )
        return (
            switch_detail,
            EventType.RISK_PARAMETER_CHANGED,
            None,
            None,
        )

    if verb == ControlVerb.FORCE_CLOSE_POSITION:
        position_id = parameters.get("position_id")
        envelope_id = parameters.get("envelope_id")
        # The position-state change itself is written by the OMS write path
        # on broker confirmation (POSITION_CLOSED). This row captures the
        # operator-initiated request: the rationale + envelope id linkage.
        detail = RiskParameterChangedDetail(
            old_parameter_set_json={"position_state": "open"},
            new_parameter_set_json={
                "position_state": "force_close_requested",
                "position_id": position_id,
                "rationale": parameters.get("rationale"),
                "synthesized_envelope_id": envelope_id,
                "result_ok": result.ok,
                "applied_at": result.applied_at,
                "error_code": result.error_code.value if result.error_code else None,
                "error_detail": result.error_detail,
            },
            regime_label="operator_console_force_close_position",
        )
        return (
            detail,
            EventType.RISK_PARAMETER_CHANGED,
            str(position_id) if position_id is not None else None,
            None,
        )

    # Defensive — every ControlVerb except SWITCH_PROFILE +
    # RUN_UNIVERSE_VALIDATION (filtered above) is handled.
    msg = f"write_operator_action_entry: unhandled verb {verb!r}"
    raise ValueError(msg)
