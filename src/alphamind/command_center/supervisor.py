"""``asyncio.TaskGroup``-rooted supervisor for the command center.

Mirrors :class:`alphamind.scheduler.supervisor.PipelineSupervisor`
shape: a per-process supervisor that holds a registry of named
coroutine factories, opens an outer
:class:`asyncio.TaskGroup` on :meth:`run`, schedules every registered
task as a child, and supervises them until graceful shutdown (a stop
signal) or until any task raises (in which case the TaskGroup cancels
the siblings and the first non-``CancelledError`` leaf re-raises).

Story 02 ships the supervisor scaffold; no concurrent tasks are
registered yet. The FastAPI / Uvicorn server itself is the supervisor's
first task — it runs inside the lifespan as the Uvicorn task once
:mod:`alphamind.command_center.__main__` wires it up. Story 04b
(``/api/events`` SSE multiplexer) will register the per-upstream SSE
consumer tasks; story 05a (alert engine) will register the alert-
evaluation task.

The supervisor's design contract:

* Every long-running async task lives inside the TaskGroup — no bare
  ``asyncio.create_task`` calls anywhere downstream. Parent issue
  pre-resolved TaskGroup discipline.
* A graceful stop (``SIGINT`` / ``SIGTERM`` via the installed signal
  handlers; or :meth:`request_stop` from inside the process) cancels
  every supervised task and bounds the cancellation wait by
  ``shutdown_timeout_seconds`` so a task that swallows
  ``CancelledError`` cannot stall the daemon.
* On task crash, the TaskGroup re-raises; the supervisor extracts the
  first non-``CancelledError`` leaf and propagates it so the outer
  ``__main__`` handler can log + exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Coroutine
from typing import Any

from alphamind._kernel.exception_group import first_non_cancelled
from alphamind.command_center.session import ProcessSession

log = logging.getLogger(__name__)

TaskCoroFn = Callable[[ProcessSession], Coroutine[Any, Any, None]]
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class CommandCenterSupervisor:
    """Owns the asyncio task tree for one command-center-process lifetime.

    Mirrors :class:`alphamind.scheduler.supervisor.PipelineSupervisor`
    semantics: the supervisor's :meth:`run` opens an outer
    :class:`asyncio.TaskGroup`, schedules every registered task as a
    child, and supervises them until graceful shutdown or until any
    task raises.
    """

    def __init__(
        self,
        *,
        session: ProcessSession,
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
            log.warning("CommandCenterSupervisor.request_stop called before run()")
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
                            coro_fn(self._session),
                            name=f"command_center_supervisor:{name}",
                        )
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
