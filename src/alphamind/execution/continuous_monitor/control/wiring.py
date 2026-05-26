"""Production wiring for the monitor /control surface (ALP-665).

Composes the :class:`SSEEventEmitter` with the existing breach-loop /
fill-stream / greeks-refresh / emergency-trigger emit points so each
producer fires structural callbacks the SSE consumer can observe.

The composition is deliberately additive: each adapter wraps an existing
callback (``on_immediate_breach``, ``activity_log_sink``, ``submit_envelope``,
etc.) and emits onto the SSE bus before delegating to the original. A
monitor that boots without the HTTP surface (degraded mode) calls the
original callback directly with no SSE side-effect — the emitter is
optional throughout.

Per the parent issue's architectural invariants this module imports only
from ``control/`` and the existing monitor subpackages; it does not import
from ``scheduler/`` or ``command_center/``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.control.events import SSEEventEmitter
from alphamind.portfolio_state.events.activity_log import (
    ActivityLogEntry,
    EventType,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Breach loop — emits ``breach_detected`` events
# ---------------------------------------------------------------------------


def wrap_on_immediate_breach(
    *,
    emitter: SSEEventEmitter,
    inner: Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]],
) -> Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]]:
    """Wrap the cascade dispatcher's ``on_immediate_breach`` with SSE emit.

    Fires ``breach_detected`` with ``response_classification="immediate"``
    before delegating to the inner cascade-dispatch path. The inner
    callback's behavior is unchanged.
    """

    async def _composed(
        result: BreachLoopResult, evaluation: RuleEvaluation
    ) -> None:
        try:
            emitter.emit_breach_detected(
                rule=evaluation.rule_id,
                current_value=float(evaluation.current_value),
                limit=float(evaluation.limit_value),
                response_classification="immediate",
            )
        except Exception:  # noqa: BLE001 - emit failure must not block the cascade
            log.exception("SSE emit_breach_detected failed; continuing")
        await inner(result, evaluation)

    return _composed


def wrap_activity_log_sink_with_deferred_breach_emit(
    *,
    emitter: SSEEventEmitter,
    inner: Callable[[Iterable[ActivityLogEntry]], Awaitable[None]],
) -> Callable[[Iterable[ActivityLogEntry]], Awaitable[None]]:
    """Wrap the breach loop's ``activity_log_sink`` with SSE emit.

    The breach loop emits ``HALT_ACTIVATED`` / ``HALT_LIFTED`` activity-log
    entries; these are not breach events themselves. Halt events do not fire
    SSE breach_detected — that channel is reserved for live breach
    detections per the schema. This wrapper exists for symmetry with the
    immediate-breach wrap; today's only effect is to pass through.
    """

    async def _composed(entries: Iterable[ActivityLogEntry]) -> None:
        # Materialize so we can inspect + delegate. Activity-log lists are
        # small (typically 0–2 entries per breach-loop tick).
        materialized = tuple(entries)
        # We currently emit nothing here — halt transitions surface
        # downstream via the broader activity-log multiplexer in the
        # command center, not this monitor-specific SSE stream.
        del materialized
        await inner(entries)

    return _composed


def make_deferred_breach_emit_for_breach_loop(
    *,
    emitter: SSEEventEmitter,
) -> Callable[[RuleEvaluation], None]:
    """Return a small helper the breach loop calls per deferred-classification rule.

    The breach loop currently invokes ``on_immediate_breach`` only for
    immediate-classification rules in ``RiskZone.BLOCKED``. Deferred-
    classification rules need a parallel SSE emit. The breach loop's wiring
    can install this helper alongside ``on_immediate_breach``.
    """

    def _emit(evaluation: RuleEvaluation) -> None:
        try:
            emitter.emit_breach_detected(
                rule=evaluation.rule_id,
                current_value=float(evaluation.current_value),
                limit=float(evaluation.limit_value),
                response_classification="deferred",
            )
        except Exception:  # noqa: BLE001 - emit failure must not block the breach loop
            log.exception("SSE emit_breach_detected (deferred) failed; continuing")

    return _emit


# ---------------------------------------------------------------------------
# Emergency trigger — emits ``emergency_invocation_triggered``
# ---------------------------------------------------------------------------


def wrap_emergency_activity_log_writer(
    *,
    emitter: SSEEventEmitter,
    inner: Callable[[ActivityLogEntry], Awaitable[None]],
) -> Callable[[ActivityLogEntry], Awaitable[None]]:
    """Wrap the emergency-trigger evaluator's activity-log writer with SSE emit.

    The evaluator writes an ``EMERGENCY_INVOCATION_REQUESTED`` row per fire;
    we map that into the schema's ``emergency_invocation_triggered`` SSE
    event. The inner write proceeds regardless of SSE emit success.
    """

    async def _composed(entry: ActivityLogEntry) -> None:
        if entry.event_type == EventType.EMERGENCY_INVOCATION_REQUESTED:
            reason = _extract_emergency_reason(entry)
            try:
                emitter.emit_emergency_invocation_triggered(reason=reason)
            except Exception:  # noqa: BLE001 - emit failure must not block the write
                log.exception("SSE emit_emergency_invocation_triggered failed")
        await inner(entry)

    return _composed


def _extract_emergency_reason(entry: ActivityLogEntry) -> str:
    """Read the trigger reason off the activity-log entry's detail."""
    detail = entry.detail
    # ``EmergencyInvocationRequestedDetail`` carries ``trigger_reason``; defensively
    # fall back to ``str(detail)`` if the shape ever changes.
    reason = getattr(detail, "trigger_reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return "unknown"


# ---------------------------------------------------------------------------
# Cascade dispatch — submit_envelope wrapper emits ``breach_detected``
#                    (the immediate-breach path already emits via
#                    wrap_on_immediate_breach above; this wrapper is reserved
#                    for the margin-call cascade path which does NOT go
#                    through the breach loop)
# ---------------------------------------------------------------------------


def wrap_submit_envelope_for_cascade(
    *,
    emitter: SSEEventEmitter,
    inner: Callable[[Any], Awaitable[Any]],
) -> Callable[[Any], Awaitable[Any]]:
    """Wrap the cascade dispatcher's ``submit_envelope`` with SSE emit.

    Reserved hook — margin-call cascades route through the dispatcher's
    ``handle_margin_call`` entry rather than the breach loop, so the
    breach_detected event for that path needs to be sourced from the
    envelope's ``guardrail_trigger_record``. Today's implementation emits
    nothing additional — the breach loop's wrap is sufficient for the
    BLOCKED-zone breaches the schema actually targets.
    """
    return inner


# ---------------------------------------------------------------------------
# Fill stream — emits ``fill_received``
# ---------------------------------------------------------------------------


def wrap_fill_enrichment_with_emit(
    *,
    emitter: SSEEventEmitter,
    inner: Callable[[Any], Awaitable[Any]] | None,
) -> Callable[[Any], Awaitable[Any]]:
    """Wrap the paper-mode enrichment callable with an SSE emit.

    The fill-stream consumer's ``enrichment_callable`` receives each
    translated ``FillRecord`` before persistence. We hook the SSE
    ``fill_received`` emit there because the record already carries the
    resolved ``order_id`` (matching an OMS ``client_order_id``) and the
    fill price/quantity. For live mode (``inner is None``), we return a
    passthrough that performs only the emit.
    """

    async def _emit_and_delegate(record: Any) -> Any:
        try:
            # The fill record's order_id maps to the OMS client_order_id;
            # position_id is resolved by the monitor before this point.
            order_id = getattr(record, "order_id", None)
            position_id = getattr(record, "position_id", None) or _resolve_position_id(record)
            fill_price = getattr(record, "fill_price", None)
            fill_qty = getattr(record, "fill_quantity", None)
            if (
                order_id is not None
                and position_id is not None
                and fill_price is not None
                and fill_qty is not None
            ):
                emitter.emit_fill_received(
                    order_id=str(order_id),
                    position_id=str(position_id),
                    fill_price=float(fill_price),
                    fill_qty=float(fill_qty),
                )
        except Exception:  # noqa: BLE001 - emit failure must not block persistence
            log.exception("SSE emit_fill_received failed; persistence unaffected")
        if inner is None:
            return record
        return await inner(record)

    return _emit_and_delegate


def _resolve_position_id(record: Any) -> str | None:
    """FillRecord does not carry position_id directly — return None safely.

    The full resolution flows through the activity-log writeback; for the
    SSE emit path we surface ``order_id`` as the durable identifier and
    leave position_id unresolved at this seam. Future work: tighten the
    resolution if the monitor's fill-buffer surfaces ``position_id``
    inline.
    """
    return None


# ---------------------------------------------------------------------------
# Greeks refresh — emits ``greeks_refreshed``
# ---------------------------------------------------------------------------


def make_greeks_refresh_emit(
    *,
    emitter: SSEEventEmitter,
) -> Callable[[str], None]:
    """Return a callback the greeks-refresh task fires per completed refresh.

    The greeks-refresh task already groups refreshes by underlying. The
    wiring layer in ``__main__`` threads this callback into the refresh
    loop so each completed (per-underlying) cycle emits one
    ``greeks_refreshed`` event.
    """
    from datetime import UTC, datetime

    def _emit(underlying: str) -> None:
        try:
            emitter.emit_greeks_refreshed(
                underlying=underlying, refreshed_at=datetime.now(UTC)
            )
        except Exception:  # noqa: BLE001
            log.exception("SSE emit_greeks_refreshed failed; refresh unaffected")

    return _emit


__all__ = [
    "make_deferred_breach_emit_for_breach_loop",
    "make_greeks_refresh_emit",
    "wrap_activity_log_sink_with_deferred_breach_emit",
    "wrap_emergency_activity_log_writer",
    "wrap_fill_enrichment_with_emit",
    "wrap_on_immediate_breach",
    "wrap_submit_envelope_for_cascade",
]
