"""Production wiring for the emergency-invocation trigger (story 04b / ALP-439).

Builds the activity-log writer + on_emergency_input callback the
``__main__.py`` daemon plugs into the breach loop (story 03b's
``register_breach_loop_task`` ``on_emergency_input`` parameter).

The activity-log writer opens a fresh ``AsyncSession`` per emit and
commits — same shape as the greeks-refresh task's
``make_activity_log_emitter``. This keeps the writer transaction
narrow (one row, one commit) and avoids holding the DB lock across the
broker-state probe or the trigger-evaluation logic.

The wiring helper threads two configs:

* :class:`BreachBehaviorConfig` — cooldown source (the canonical knob).
* :class:`ContinuousMonitorConfig` — carried for symmetry with other
  monitor tasks; the evaluator itself reads no fields from it.

The invocation-id provider mirrors greeks-refresh's
``make_invocation_id_provider``: resolves the most-recently-started
invocation per emit so the activity-log FK to ``invocations`` holds.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.config.models.guardrails import BreachResponse
from alphamind.execution.continuous_monitor.breach_loop.result import BreachLoopResult
from alphamind.execution.continuous_monitor.emergency_trigger.cooldown import (
    CooldownTracker,
)
from alphamind.execution.continuous_monitor.emergency_trigger.evaluator import (
    ActivityLogWriter,
    EmergencyTriggerEvaluator,
    MarginCallObserver,
    NoMarginCallObserver,
    TriggerIdGenerator,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.risk_guardrails.breach_behavior import BreachBehaviorConfig

log = logging.getLogger(__name__)


def make_emergency_invocation_writer(
    session_factory: async_sessionmaker[AsyncSession],
) -> ActivityLogWriter:
    """Build an :class:`ActivityLogWriter` backed by per-emit transactions.

    Re-uses the activity-log row codec via a thin per-emit session +
    commit. Mirrors :func:`make_activity_log_emitter` (greeks-refresh).
    """
    from alphamind.execution.state_persistence.invocation_context.activity_log import (
        activity_log_entry_to_row,
    )

    async def _emit(entry: ActivityLogEntry) -> None:
        async with session_factory() as sess:
            sess.add(activity_log_entry_to_row(entry))
            await sess.commit()

    return _emit


def make_invocation_id_provider(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], str]:
    """Return a callable that resolves the latest invocation_id to anchor entries.

    The monitor runs across invocations; the ``activity_log.invocation_id``
    FK requires the row to reference a real invocation. Mirrors
    ``greeks_refresh.wiring.make_invocation_id_provider`` — the same
    convention, the same fallback sentinel.
    """
    import asyncio

    from sqlalchemy import select

    from alphamind.execution.state_persistence.tables.invocations import InvocationRow

    async def _read() -> str:
        async with session_factory() as sess:
            stmt = (
                select(InvocationRow.invocation_id).order_by(InvocationRow.start_at.desc()).limit(1)
            )
            result = await sess.execute(stmt)
            value = result.scalar_one_or_none()
            if value is None:
                return "monitor-bootstrap"
            return str(value)

    def _provider() -> str:
        try:
            return asyncio.run(_read())
        except RuntimeError:
            return "monitor-bootstrap"

    return _provider


def make_emergency_callback(
    *,
    session: MonitorSession,
    breach_behavior_config: BreachBehaviorConfig,
    breach_response_lookup: Mapping[str, BreachResponse],
    session_factory: async_sessionmaker[AsyncSession],
    margin_call_observer: MarginCallObserver | None = None,
    trigger_ids: TriggerIdGenerator | None = None,
    invocation_id_provider: Callable[[], str] | None = None,
) -> Callable[[BreachLoopResult], Awaitable[None]]:
    """Build the ``on_emergency_input`` callback for the breach loop.

    Constructs the :class:`EmergencyTriggerEvaluator` from the production
    substrate and returns its ``handle_emergency_input`` method as the
    coroutine the breach loop awaits.

    ``trigger_ids`` is optional; when omitted, a fresh generator scoped
    to the monitor session is created. The cascade dispatcher (story 04a)
    is expected to share the same instance via the wiring path that
    follows; until then each callback gets its own generator and the
    sequences are session-local but not cross-task.

    ``margin_call_observer`` defaults to :class:`NoMarginCallObserver`
    until the broker adapter exposes a live margin-call surface
    (story 04b's "margin-call event sourcing" scope item).
    """
    writer = make_emergency_invocation_writer(session_factory)
    cooldown = CooldownTracker(
        cooldown_minutes=breach_behavior_config.emergency_invocation_cooldown_minutes
    )
    evaluator = EmergencyTriggerEvaluator(
        session=session,
        breach_behavior_config=breach_behavior_config,
        cooldown=cooldown,
        trigger_ids=trigger_ids or TriggerIdGenerator(monitor_session_id=session.session_id),
        margin_call_observer=margin_call_observer or NoMarginCallObserver(),
        activity_log_writer=writer,
        breach_response_lookup=breach_response_lookup,
        invocation_id_provider=invocation_id_provider
        or make_invocation_id_provider(session_factory),
    )
    return evaluator.handle_emergency_input


__all__ = [
    "make_emergency_callback",
    "make_emergency_invocation_writer",
    "make_invocation_id_provider",
]
