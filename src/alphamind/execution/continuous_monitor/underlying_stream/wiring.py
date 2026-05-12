"""Supervisor-side wiring for the underlying-stream task (story 02b / ALP-434).

``__main__.py`` imports :func:`register_underlying_stream_task` and calls it
during daemon setup. Keeping the closure construction here means the entry
point's edit stays narrow (one import + one call) — important because stories
02a / 02c register sibling tasks against the same supervisor.

The wiring constructs the shared :class:`UnderlyingPriceCache` and the
production :class:`DefaultAlpacaStreamFactory`, then registers a closure on
the supervisor that adapts the run-forever coroutine's
``(session, config, *, repository, cache, factory)`` shape to the
:type:`TaskCoroFn` ``(session, config)`` signature the supervisor enforces.

The cache instance the wiring constructs is returned so the entry point can
hand the same instance to subsequent stories' task wiring (03a greeks
refresh, 03b breach loop, 04c options bracket-stop) — every reader observes
the same single-writer cache.
"""

from __future__ import annotations

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor
from alphamind.execution.continuous_monitor.underlying_stream.cache import (
    UnderlyingPriceCache,
)
from alphamind.execution.continuous_monitor.underlying_stream.subscriptions import (
    OpenPositionsReader,
)
from alphamind.execution.continuous_monitor.underlying_stream.task import (
    AlpacaStreamFactory,
    DefaultAlpacaStreamFactory,
    run_underlying_stream,
)


def register_underlying_stream_task(
    supervisor: MonitorSupervisor,
    *,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache | None = None,
    factory: AlpacaStreamFactory | None = None,
) -> UnderlyingPriceCache:
    """Register the ``underlying_stream`` task on *supervisor*.

    Returns the :class:`UnderlyingPriceCache` instance so the entry point can
    inject the same writer into reader-side tasks (greeks refresh, breach
    loop, options bracket-stop).
    """
    resolved_cache = cache if cache is not None else UnderlyingPriceCache()
    resolved_factory = factory if factory is not None else DefaultAlpacaStreamFactory()

    async def _coro(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        await run_underlying_stream(
            session,
            config,
            repository=repository,
            cache=resolved_cache,
            factory=resolved_factory,
        )

    supervisor.register_task(name="underlying_stream", coro_fn=_coro)
    return resolved_cache
