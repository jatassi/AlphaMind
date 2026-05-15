"""Asyncio supervisor for the pipeline scheduler process (story 01).

Owns the long-running task tree. Registers a coroutine factory per moving
part, installs SIGINT/SIGTERM handlers, and supervises every task under
:class:`asyncio.TaskGroup` so a crash cancels its siblings (fail-closed
per pre-resolved decision H) and a graceful stop cancels every supervised
task. The TaskGroup body re-raises the first non-``CancelledError`` leaf
so callers see the same exception surface they did before the
structured-concurrency migration.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Coroutine
from typing import Any

from alphamind._kernel.exception_group import first_non_cancelled
from alphamind.scheduler.session import PipelineSession

log = logging.getLogger(__name__)

TaskCoroFn = Callable[[PipelineSession], Coroutine[Any, Any, None]]
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class PipelineSupervisor:
    """Owns the asyncio task tree for one pipeline-process lifetime."""

    def __init__(
        self,
        *,
        session: PipelineSession,
        shutdown_timeout_seconds: int,
    ) -> None:
        if shutdown_timeout_seconds < 1:
            msg = "shutdown_timeout_seconds must be >= 1"
            raise ValueError(msg)
        self._session = session
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._registry: list[tuple[str, TaskCoroFn]] = []
        self._stop_event: asyncio.Event | None = None

    def register_task(self, *, name: str, coro_fn: TaskCoroFn) -> None:
        """Register a coroutine factory under *name*; reject duplicates."""
        if any(existing == name for existing, _ in self._registry):
            msg = f"task name {name!r} already registered"
            raise ValueError(msg)
        self._registry.append((name, coro_fn))

    def task_names(self) -> tuple[str, ...]:
        """Return the registered task names in registration order."""
        return tuple(name for name, _ in self._registry)

    def request_stop(self) -> None:
        """Signal the supervisor to begin orderly shutdown."""
        if self._stop_event is None:
            # Pre-``run()`` signal — warn so it is not lost silently.
            log.warning("PipelineSupervisor.request_stop called before run()")
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
                        tg.create_task(coro_fn(self._session), name=f"pipeline_supervisor:{name}")
                        for name, coro_fn in self._registry
                    ]
                    await self._stop_event.wait()
                    # Graceful stop — cascade-cancel and bound the wait so
                    # a task swallowing ``CancelledError`` cannot stall.
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.wait(tasks, timeout=self._shutdown_timeout_seconds)
            except BaseExceptionGroup as eg:
                first = first_non_cancelled(eg)
                if first is not None:
                    raise first from eg
        finally:
            _remove_signal_handlers(loop)


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
