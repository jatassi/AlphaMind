"""Asyncio supervisor for the continuous-monitor process (story 01).

Mirrors :class:`alphamind.scheduler.supervisor.PipelineSupervisor`: owns
the long-running task tree, installs SIGINT/SIGTERM handlers, supervises
every task under :class:`asyncio.TaskGroup`. A crash cancels its siblings
(fail-closed); a graceful stop cancels every task; the TaskGroup body
re-raises the first non-``CancelledError`` leaf so callers see the same
exception surface they did before the migration. Differences from the
pipeline supervisor: tasks receive ``(session, config)`` and the shutdown
timeout comes from the config record.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import Callable, Coroutine
from typing import Any

from alphamind._kernel.exception_group import first_non_cancelled
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession

log = logging.getLogger(__name__)

TaskCoroFn = Callable[[MonitorSession, ContinuousMonitorConfig], Coroutine[Any, Any, None]]
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class MonitorSupervisor:
    """Owns the asyncio task tree for one monitor-process lifetime."""

    def __init__(
        self,
        *,
        session: MonitorSession,
        config: ContinuousMonitorConfig,
    ) -> None:
        self._session = session
        self._config = config
        self._registry: list[tuple[str, TaskCoroFn]] = []
        self._stop_event: asyncio.Event | None = None
        # Per-task heartbeat timestamps for the in-process watchdog (ALP-768).
        # Populated by tasks that call beat(); the watchdog loop only watches
        # names present in this dict (opt-in, not all registered tasks).
        self._heartbeats: dict[str, float] = {}

    def register_task(self, *, name: str, coro_fn: TaskCoroFn) -> None:
        """Register a coroutine factory under *name*; reject duplicates."""
        if any(existing == name for existing, _ in self._registry):
            msg = f"task name {name!r} already registered"
            raise ValueError(msg)
        self._registry.append((name, coro_fn))

    def task_names(self) -> tuple[str, ...]:
        """Return the registered task names in registration order."""
        return tuple(name for name, _ in self._registry)

    def beat(self, task_name: str) -> None:
        """Record a liveness heartbeat for *task_name* (ALP-768 watchdog).

        The first call registers *task_name* with the watchdog; subsequent
        calls reset its timer. The watchdog fires ``os._exit(1)`` if no beat
        arrives within ``watchdog_stall_timeout_seconds`` of the previous one.
        Only tasks that call this method are watched — tasks that never beat
        are ignored, so existing tasks opt in incrementally.
        """
        self._heartbeats[task_name] = time.monotonic()

    def request_stop(self) -> None:
        """Signal the supervisor to begin orderly shutdown."""
        if self._stop_event is None:
            # Pre-``run()`` signal — warn so it is not lost silently.
            log.warning("MonitorSupervisor.request_stop called before run()")
            return
        self._stop_event.set()

    async def run(self) -> None:
        """Supervise registered tasks; return cleanly on stop, raise on crash."""
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        _install_signal_handlers(loop, self.request_stop)
        try:
            try:
                async with asyncio.TaskGroup() as tg:
                    tasks: list[asyncio.Task[None]] = [
                        tg.create_task(
                            fn(self._session, self._config), name=f"monitor_supervisor:{n}"
                        )
                        for n, fn in self._registry
                    ]
                    tg.create_task(
                        self._watchdog_loop(),
                        name="monitor_supervisor:watchdog",
                    )
                    await self._stop_event.wait()
                    # Graceful stop — cascade-cancel and bound the wait
                    # so a task swallowing ``CancelledError`` cannot stall.
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        timeout = self._config.supervisor_shutdown_timeout_seconds
                        await asyncio.wait(tasks, timeout=timeout)
            except BaseExceptionGroup as eg:
                first = first_non_cancelled(eg)
                if first is not None:
                    raise first from eg
        finally:
            _remove_signal_handlers(loop)

    async def _watchdog_loop(self) -> None:
        """Periodically check heartbeats; force exit if any watched task stalls."""
        timeout = self._config.watchdog_stall_timeout_seconds
        # Check at quarter-period so a stall is detected promptly but the
        # check overhead is negligible relative to the timeout interval.
        check_interval = max(1, timeout // 4)
        while True:
            await asyncio.sleep(check_interval)
            now = time.monotonic()
            for name, last in list(self._heartbeats.items()):
                elapsed = now - last
                if elapsed > timeout:
                    log.critical(
                        "watchdog: task %r has not heartbeated in %.0fs "
                        "(limit %ds) — forcing process exit for NSSM restart",
                        name,
                        elapsed,
                        timeout,
                    )
                    os._exit(1)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> None:
    # ``add_signal_handler`` is unsupported on Windows event loops / loops
    # off the main thread; NSSM shims SIGINT via Ctrl-Break in production.
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, ValueError):
            log.debug("could not install signal handler for %s", sig.name)


def _remove_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in _SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.remove_signal_handler(sig)
