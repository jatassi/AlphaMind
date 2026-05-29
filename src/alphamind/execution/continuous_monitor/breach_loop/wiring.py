"""Supervisor-side wiring for the breach-evaluation loop (story 03b / ALP-437).

``__main__.py`` imports :func:`register_breach_loop_task` and calls it during
daemon setup. The wiring constructs the no-op stub callbacks consumed by
stories 04a / 04b (which replace them with real handlers once those land),
adapts the loop coroutine's keyword signature to the supervisor's uniform
``(session, config)`` task contract, and registers it as ``breach_loop``.

The wiring's narrow scope mirrors story 02b's
:func:`register_underlying_stream_task` — one closure that pre-binds every
non-uniform dependency.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import datetime

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.config.models.guardrails import BreachResponse, ProgressiveTier
from alphamind.execution.continuous_monitor.breach_loop.result import (
    BreachLoopHealthSignal,
    BreachLoopResult,
    RuleEvaluation,
)
from alphamind.execution.continuous_monitor.breach_loop.task import (
    MarketHoursClock,
    run_breach_loop,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.portfolio_state.events.activity_log import ActivityLogEntry
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.risk_guardrails.guardrail_evaluation import (
    IvProvider,
    LibraryConfig,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    PortfolioStateSnapshot as LibrarySnapshot,
)
from alphamind.risk_guardrails.regime_adaptation import RegimeAdaptationOutput


async def _no_op_immediate_breach(_result: BreachLoopResult, _evaluation: RuleEvaluation) -> None:
    """Fallback default for ``on_immediate_breach`` — used when the caller does not
    wire the cascade-dispatcher callback (e.g., tests)."""


async def _no_op_emergency_input(_result: BreachLoopResult) -> None:
    """Fallback default for ``on_emergency_input`` — used when the caller does not
    wire the emergency-invocation trigger (e.g., tests)."""


async def _no_op_activity_log_sink(_entries: Iterable[ActivityLogEntry]) -> None:
    """Fallback default for ``activity_log_sink`` — used when the caller does not
    wire the persistence-side activity-log writer."""


def register_breach_loop_task(  # noqa: PLR0913
    supervisor: MonitorSupervisor,
    *,
    repository: PortfolioStateRepository,
    cache: UnderlyingPriceCache,
    snapshot_provider: Callable[[], Awaitable[LibrarySnapshot]],
    regime_provider: Callable[[], Awaitable[RegimeAdaptationOutput]],
    progressive_tiers: tuple[ProgressiveTier, ...],
    library_config_factory: Callable[[ActiveRiskParameterSet], LibraryConfig],
    iv_provider: IvProvider,
    risk_free_rate: float,
    breach_response_lookup: Mapping[str, BreachResponse],
    market_hours: MarketHoursClock,
    on_immediate_breach: (
        Callable[[BreachLoopResult, RuleEvaluation], Awaitable[None]] | None
    ) = None,
    on_emergency_input: Callable[[BreachLoopResult], Awaitable[None]] | None = None,
    activity_log_sink: (Callable[[Iterable[ActivityLogEntry]], Awaitable[None]] | None) = None,
    on_health_signal: (Callable[[BreachLoopHealthSignal], None] | None) = None,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Register the ``breach_loop`` task on *supervisor*.

    The supervisor wires the cascade-dispatcher and emergency-trigger
    callbacks; the no-op defaults above keep the registration valid in tests
    or any caller that omits them. The activity-log sink defaults to a no-op
    stub so the breach loop runs even if the caller does not wire the writer.
    ``on_health_signal`` (ALP-732) carries the sustained-failure escalation
    sink; when omitted the loop's own no-op default applies so an unwired
    caller still runs (the loop just logs degraded/recovered transitions).
    """
    resolved_immediate = on_immediate_breach or _no_op_immediate_breach
    resolved_emergency = on_emergency_input or _no_op_emergency_input
    resolved_sink = activity_log_sink or _no_op_activity_log_sink

    async def _coro(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        kwargs: dict[str, object] = {
            "repository": repository,
            "cache": cache,
            "snapshot_provider": snapshot_provider,
            "regime_provider": regime_provider,
            "progressive_tiers": progressive_tiers,
            "library_config_factory": library_config_factory,
            "iv_provider": iv_provider,
            "risk_free_rate": risk_free_rate,
            "breach_response_lookup": breach_response_lookup,
            "market_hours": market_hours,
            "activity_log_sink": resolved_sink,
            "on_immediate_breach": resolved_immediate,
            "on_emergency_input": resolved_emergency,
        }
        if on_health_signal is not None:
            kwargs["on_health_signal"] = on_health_signal
        if now is not None:
            kwargs["now"] = now
        await run_breach_loop(session, config, **kwargs)  # type: ignore[arg-type]

    supervisor.register_task(name="breach_loop", coro_fn=_coro)
