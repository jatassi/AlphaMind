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
from collections.abc import Awaitable, Callable
from typing import Any

from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopHealthSignal,
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

    async def _composed(result: BreachLoopResult, evaluation: RuleEvaluation) -> None:
        try:
            emitter.emit_breach_detected(
                rule=evaluation.rule_id,
                current_value=float(evaluation.current_value),
                limit=float(evaluation.limit_value),
                response_classification="immediate",
            )
        except Exception:
            log.exception("SSE emit_breach_detected failed; continuing")
        await inner(result, evaluation)

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
        except Exception:
            log.exception("SSE emit_breach_detected (deferred) failed; continuing")

    return _emit


def make_breach_loop_health_emit(
    *,
    emitter: SSEEventEmitter,
) -> Callable[[BreachLoopHealthSignal], Awaitable[None]]:
    """Return the breach loop's ``on_health_signal`` sink (ALP-732 Gap 2).

    Maps each :class:`BreachLoopHealthSignal` transition onto the SSE
    ``breach_loop_degraded`` / ``breach_loop_recovered`` events (and flips the
    emitter's health flag), so a silently-failing breach loop becomes visible
    on the monitor's ``/events`` stream. The emit is guarded: an SSE failure
    must not propagate back into the loop's per-tick supervisor.
    """

    async def _emit(signal: BreachLoopHealthSignal) -> None:
        try:
            if signal.degraded:
                emitter.emit_breach_loop_degraded(
                    consecutive_failures=signal.consecutive_failures,
                    last_error=signal.last_error or "unknown",
                )
            else:
                emitter.emit_breach_loop_recovered(consecutive_failures=signal.consecutive_failures)
        except Exception:
            log.exception("SSE emit breach_loop health signal failed; continuing")

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
            except Exception:
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
            # position_id is supplied by the monitor's resolution path
            # before this seam (the schema's fill_received invariant
            # requires resolved IDs at emission time).
            order_id = getattr(record, "order_id", None)
            position_id = getattr(record, "position_id", None)
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
        except Exception:
            log.exception("SSE emit_fill_received failed; persistence unaffected")
        if inner is None:
            return record
        return await inner(record)

    return _emit_and_delegate


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
            emitter.emit_greeks_refreshed(underlying=underlying, refreshed_at=datetime.now(UTC))
        except Exception:
            log.exception("SSE emit_greeks_refreshed failed; refresh unaffected")

    return _emit


__all__ = [
    "make_breach_loop_health_emit",
    "make_deferred_breach_emit_for_breach_loop",
    "make_greeks_refresh_emit",
    "wrap_emergency_activity_log_writer",
    "wrap_fill_enrichment_with_emit",
    "wrap_on_immediate_breach",
]
