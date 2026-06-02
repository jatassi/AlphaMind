"""Supervisor-side wiring for the underlying-stream task (story 02b / ALP-434; ALP-832).

``__main__.py`` imports :func:`register_underlying_stream_task` and calls it
during daemon setup. Keeping the closure construction here means the entry
point's edit stays narrow (one import + one call) — important because stories
02a / 02c register sibling tasks against the same supervisor.

The wiring constructs the shared :class:`UnderlyingPriceCache` and the
production :class:`DefaultAlpacaStreamFactory`, then registers a closure on
the supervisor that adapts the run-forever coroutine's
``(session, config, *, repository, cache, factory, ...)`` shape to the
:type:`TaskCoroFn` ``(session, config)`` signature the supervisor enforces.

Stall resilience (ALP-832). The reconnect-driven loop is not a fixed-cadence
``supervised_loop``; its liveness comes from the 02a kernel's per-slice
``beat``. So the task declares its poll cadence to the watchdog via
``supervisor.register_watch`` so a positive stall bound derives. Without this
declaration a bare ``beat`` would leave the task watched-but-unbounded — the
watchdog would warn but could never trip it.

The cache instance the wiring constructs is returned so the entry point can
hand the same instance to subsequent stories' task wiring (03a greeks
refresh, 03b breach loop, 04c options bracket-stop) — every reader observes
the same single-writer cache.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

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

# Default stream poll interval for production. This is how often the staleness-
# watch loop wakes to beat the watchdog + check for RTH silence. 5s is tight
# enough to detect a wedge within one watchdog check interval while cheap enough
# that there is no meaningful CPU overhead from the async sleep cycle.
_DEFAULT_STREAM_POLL_INTERVAL: float = 5.0


def register_underlying_stream_task(
    supervisor: MonitorSupervisor,
    *,
    repository: OpenPositionsReader,
    cache: UnderlyingPriceCache | None = None,
    factory: AlpacaStreamFactory | None = None,
    is_market_open: Callable[[datetime], bool] | None = None,
    stream_poll_interval: float = _DEFAULT_STREAM_POLL_INTERVAL,
) -> UnderlyingPriceCache:
    """Register the ``underlying_stream`` task on *supervisor*.

    Returns the :class:`UnderlyingPriceCache` instance so the entry point can
    inject the same writer into reader-side tasks (greeks refresh, breach
    loop, options bracket-stop).

    ``is_market_open`` gates the connected-but-silent staleness check to RTH
    (IEX quotes are legitimately sparse off-hours). The production entry point
    wires ``calendar_cache.is_market_open``; pass ``None`` to disable the
    staleness check (the monitor beats but is frame-timeout-inert).

    ``stream_poll_interval`` is the slice width for the staleness-watch loop —
    how often the task beats the watchdog and inspects for RTH silence. It is
    also declared to the watchdog via ``supervisor.register_watch`` so a
    positive stall bound (``interval * watchdog_cadence_multiplier``) derives.
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
            is_market_open=is_market_open,
            beat=lambda: supervisor.beat("underlying_stream"),
            register_watch=lambda cadence: supervisor.register_watch("underlying_stream", cadence),
            stream_poll_interval=stream_poll_interval,
        )

    supervisor.register_task(name="underlying_stream", coro_fn=_coro)
    return resolved_cache
