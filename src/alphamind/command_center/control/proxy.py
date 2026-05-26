"""Verb-dispatch proxies for ``/api/control/*`` (story 04a / ALP-668).

One async function per verb. Common shape:

1. Open the :func:`operator_invocation` context (writes the
   ``invocations`` row in its own short transaction; yields the
   :class:`InvocationHandle`).
2. Call the corresponding :class:`PipelineClient` or
   :class:`MonitorClient` method.
3. Within the same handle, write the typed activity-log entry
   (:func:`write_operator_action_entry`) — or, for ``switch_profile``,
   call :func:`emit_profile_switch_entry` directly.
4. Return the verb-specific result record so the route layer can
   render the Pydantic boundary response.

Per the ALP-128 invariants:

* The proxy is the only module that opens an
  :class:`OperatorInvocationHandle`. The route layer threads the
  ``operator_session_id`` from :func:`current_session` + the
  process-lifetime id from ``app.state`` + the client + the production
  session factory into the proxy; the proxy does the rest.
* The audit-row write happens BEFORE the context manager exits, so the
  ``activity_log`` row commits atomically with the surrounding
  invocation row. On upstream failure the row still commits (carrying
  ``result_ok=False`` in the detail), reflecting the operator's
  attempted action.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.control import ControlResult, ControlVerb
from alphamind.command_center._kernel.ids import OperatorSessionId
from alphamind.command_center._kernel.operator_invocation import (
    operator_invocation,
)
from alphamind.command_center.control.audit import write_operator_action_entry
from alphamind.command_center.control.monitor_client import (
    MonitorClient,
    MonitorForceClosePositionResult,
)
from alphamind.command_center.control.pipeline_client import (
    PipelineClient,
    PipelineRunUniverseValidationResult,
    PipelineSwitchProfileResult,
    PipelineTriggerEmergencyResult,
    PipelineUniverseValidationReport,
)
from alphamind.state.invocation_context.config_change import (
    emit_profile_switch_entry,
)

__all__ = [
    "ProxyResult",
    "proxy_cancel_order",
    "proxy_force_close_position",
    "proxy_pause",
    "proxy_resume",
    "proxy_run_universe_validation",
    "proxy_set_halt_mode",
    "proxy_switch_profile",
    "proxy_trigger_emergency_invocation",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProxyResult:
    """Generic proxy outcome — extends :class:`ControlResult` with the invocation id.

    The invocation id is exposed so the route layer can reference it in
    structured response headers (e.g. ``X-AlphaMind-Invocation``) and so
    integration tests can correlate the audit row with the HTTP
    response. Verb-specific extra fields (invocation_id from
    trigger_emergency, envelope_id from force_close, report from
    run_universe_validation) come through dedicated wrappers below.
    """

    result: ControlResult
    invocation_id: str


@dataclass(frozen=True, slots=True)
class ProxyTriggerEmergencyResult:
    """Proxy outcome for ``trigger_emergency_invocation``."""

    result: ControlResult
    invocation_id: str
    pipeline_invocation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProxyForceClosePositionResult:
    """Proxy outcome for ``force_close_position``."""

    result: ControlResult
    invocation_id: str
    envelope_id: str | None = None


@dataclass(frozen=True, slots=True)
class ProxyRunUniverseValidationResult:
    """Proxy outcome for ``run_universe_validation`` — carries the upstream report."""

    result: ControlResult
    invocation_id: str
    report: PipelineUniverseValidationReport | None = None


# ---------------------------------------------------------------------------
# Helper to thread shared dependencies through.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProxyContext:
    """Bundle of dependencies common to every proxy call.

    Threaded once from the route layer into each ``proxy_*`` function;
    the alternative (per-call kwargs) inflates every signature with the
    same four fields. Frozen-dataclass so the request handler can build
    one per request without mutating shared state.
    """

    production_session_factory: async_sessionmaker[AsyncSession]
    process_lifetime_id: str
    operator_session_id: OperatorSessionId
    now_factory: Callable[[], datetime] | None = None

    def now(self) -> datetime:
        """Return the current UTC datetime, allowing test injection.

        Tests pass a frozen ``lambda: <fixed>`` to keep activity-log
        timestamps deterministic; production callers leave ``now_factory``
        as ``None`` and the helper falls through to :func:`datetime.now`.
        """
        if self.now_factory is not None:
            return self.now_factory()
        return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Pipeline verb proxies.
# ---------------------------------------------------------------------------


async def proxy_pause(*, ctx: ProxyContext, pipeline: PipelineClient, reason: str) -> ProxyResult:
    """``/api/control/pause`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.PAUSE,
        now=ctx.now(),
    ) as handle:
        result = await pipeline.pause(reason=reason)
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.PAUSE,
            parameters={"reason": reason},
            result=result,
            now=ctx.now(),
        )
        return ProxyResult(result=result, invocation_id=handle.invocation_id)


async def proxy_resume(*, ctx: ProxyContext, pipeline: PipelineClient) -> ProxyResult:
    """``/api/control/resume`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.RESUME,
        now=ctx.now(),
    ) as handle:
        result = await pipeline.resume()
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.RESUME,
            parameters={},
            result=result,
            now=ctx.now(),
        )
        return ProxyResult(result=result, invocation_id=handle.invocation_id)


async def proxy_trigger_emergency_invocation(
    *, ctx: ProxyContext, pipeline: PipelineClient, reason: str
) -> ProxyTriggerEmergencyResult:
    """``/api/control/trigger_emergency_invocation`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.TRIGGER_EMERGENCY_INVOCATION,
        now=ctx.now(),
    ) as handle:
        upstream: PipelineTriggerEmergencyResult = await pipeline.trigger_emergency_invocation(
            reason=reason
        )
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.TRIGGER_EMERGENCY_INVOCATION,
            parameters={"reason": reason},
            result=upstream.result,
            now=ctx.now(),
        )
        return ProxyTriggerEmergencyResult(
            result=upstream.result,
            invocation_id=handle.invocation_id,
            pipeline_invocation_id=upstream.invocation_id,
        )


async def proxy_switch_profile(
    *, ctx: ProxyContext, pipeline: PipelineClient, profile_name: str
) -> ProxyResult:
    """``/api/control/switch_profile`` proxy.

    Unlike the other proxies, this one routes its audit-row emission
    through :func:`emit_profile_switch_entry` rather than
    :func:`write_operator_action_entry`. The profile-switch event has
    its own typed detail class (:class:`ProfileSwitchedDetail`) and the
    helper consumes the upstream's :class:`ProfileSwitchOutcome` so the
    activity-log row preserves the previous / new profile names.

    On upstream success the helper is invoked; on upstream failure no
    profile-switch row is written (the invocation row alone records the
    attempted operator action — the failure is reflected by the absence
    of the ``PROFILE_SWITCHED`` row).
    """
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.SWITCH_PROFILE,
        now=ctx.now(),
    ) as handle:
        upstream: PipelineSwitchProfileResult = await pipeline.switch_profile(
            profile_name=profile_name
        )
        if upstream.result.ok and upstream.outcome is not None:
            emit_profile_switch_entry(
                handle=handle,
                outcome=upstream.outcome,
                now=ctx.now(),
            )
        return ProxyResult(result=upstream.result, invocation_id=handle.invocation_id)


async def proxy_run_universe_validation(
    *, ctx: ProxyContext, pipeline: PipelineClient
) -> ProxyRunUniverseValidationResult:
    """``/api/control/run_universe_validation`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.RUN_UNIVERSE_VALIDATION,
        now=ctx.now(),
    ) as handle:
        upstream: PipelineRunUniverseValidationResult = await pipeline.run_universe_validation()
        # No audit row — read-only verb.
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.RUN_UNIVERSE_VALIDATION,
            parameters={},
            result=upstream.result,
            now=ctx.now(),
        )
        return ProxyRunUniverseValidationResult(
            result=upstream.result,
            invocation_id=handle.invocation_id,
            report=upstream.report,
        )


# ---------------------------------------------------------------------------
# Monitor verb proxies.
# ---------------------------------------------------------------------------


async def proxy_cancel_order(
    *, ctx: ProxyContext, monitor: MonitorClient, order_id: str
) -> ProxyResult:
    """``/api/control/cancel_order`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.CANCEL_ORDER,
        now=ctx.now(),
    ) as handle:
        result = await monitor.cancel_order(order_id=order_id)
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.CANCEL_ORDER,
            parameters={"order_id": order_id},
            result=result,
            now=ctx.now(),
        )
        return ProxyResult(result=result, invocation_id=handle.invocation_id)


async def proxy_force_close_position(
    *, ctx: ProxyContext, monitor: MonitorClient, position_id: str, rationale: str
) -> ProxyForceClosePositionResult:
    """``/api/control/force_close_position`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.FORCE_CLOSE_POSITION,
        now=ctx.now(),
    ) as handle:
        upstream: MonitorForceClosePositionResult = await monitor.force_close_position(
            position_id=position_id, rationale=rationale
        )
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.FORCE_CLOSE_POSITION,
            parameters={
                "position_id": position_id,
                "rationale": rationale,
                "envelope_id": upstream.envelope_id,
            },
            result=upstream.result,
            now=ctx.now(),
        )
        return ProxyForceClosePositionResult(
            result=upstream.result,
            invocation_id=handle.invocation_id,
            envelope_id=upstream.envelope_id,
        )


async def proxy_set_halt_mode(
    *, ctx: ProxyContext, monitor: MonitorClient, enabled: bool, reason: str
) -> ProxyResult:
    """``/api/control/set_halt_mode`` proxy."""
    async with operator_invocation(
        production_session_factory=ctx.production_session_factory,
        process_lifetime_id=ctx.process_lifetime_id,
        operator_session_id_=ctx.operator_session_id,
        verb=ControlVerb.SET_HALT_MODE,
        now=ctx.now(),
    ) as handle:
        result = await monitor.set_halt_mode(enabled=enabled, reason=reason)
        write_operator_action_entry(
            handle=handle,
            verb=ControlVerb.SET_HALT_MODE,
            parameters={"enabled": enabled, "reason": reason},
            result=result,
            now=ctx.now(),
        )
        return ProxyResult(result=result, invocation_id=handle.invocation_id)
