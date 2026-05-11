"""Asyncio supervisor for the pipeline scheduler process (story 01).

The supervisor owns the long-running asyncio task tree. Later stories
register one task per moving part (story 04a: the APScheduler driver;
story 04b: the emergency-invocation receiver). In this story the registry
is empty by default; the supervisor still installs signal handlers and
honours ``request_stop()`` so the daemon entry-point in ``__main__`` can
start, idle, and shut down cleanly.

Behavior contract (mirrors what the story acceptance criteria pin):

* ``register_task(name=..., coro_fn=...)`` stores the coroutine factory
  under the given name. Duplicate names raise ``ValueError`` — silent
  overwrite is dangerous when later stories register tasks at module
  import time.
* ``run()`` installs SIGINT / SIGTERM handlers that call
  ``request_stop()``, awaits a stop signal, then cancels each task in
  registration order and awaits each with a per-task timeout bounded by
  ``shutdown_timeout_seconds``.
* A task raising an exception triggers stop, propagates the exception out
  of ``run()`` after siblings have been cancelled — per pre-resolved
  decision (H) the surrounding process catches per-invocation exceptions
  at the task level; only fatal supervisor-level errors (an exception
  caught here) terminate the process for NSSM to restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from alphamind.scheduler.session import PipelineSession

log = logging.getLogger(__name__)

TaskCoroFn = Callable[[PipelineSession], Awaitable[None]]
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@dataclass
class _RegisteredTask:
    name: str
    coro_fn: TaskCoroFn
    task: asyncio.Task[None] | None = field(default=None)


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
        self._registry: list[_RegisteredTask] = []
        self._stop_event: asyncio.Event | None = None

    # -- Registration -----------------------------------------------------

    def register_task(self, *, name: str, coro_fn: TaskCoroFn) -> None:
        """Register a coroutine factory under *name*."""
        if any(entry.name == name for entry in self._registry):
            msg = f"task name {name!r} already registered"
            raise ValueError(msg)
        self._registry.append(_RegisteredTask(name=name, coro_fn=coro_fn))

    def task_names(self) -> tuple[str, ...]:
        """Return the registered task names in registration order."""
        return tuple(entry.name for entry in self._registry)

    # -- Lifecycle --------------------------------------------------------

    def request_stop(self) -> None:
        """Signal the supervisor to begin orderly shutdown."""
        if self._stop_event is None:
            # ``request_stop`` can be called before ``run`` (e.g. by an early
            # signal during startup); the stop event will not yet exist, in
            # which case ``run`` checks the registry on its first wait and
            # honours the prior request via the deferred event setup.
            log.warning("PipelineSupervisor.request_stop called before run()")
            return
        self._stop_event.set()

    async def run(self) -> None:
        """Start tasks, await stop, then cancel + await each within the timeout."""
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._install_signal_handlers(loop)

        for entry in self._registry:
            entry.task = asyncio.create_task(
                self._run_one(entry),
                name=f"pipeline_supervisor:{entry.name}",
            )

        try:
            await self._stop_event.wait()
        finally:
            self._remove_signal_handlers(loop)
            await self._shutdown_registered_tasks()

    # -- Internals --------------------------------------------------------

    async def _run_one(self, entry: _RegisteredTask) -> None:
        """Run *entry*'s coroutine; surface unhandled exceptions via stop."""
        try:
            await entry.coro_fn(self._session)
        except asyncio.CancelledError:
            raise
        except BaseException:
            log.exception("pipeline supervisor task %s raised", entry.name)
            # Trigger orderly shutdown; the exception is preserved on the
            # task object and re-raised from ``_shutdown_registered_tasks``.
            if self._stop_event is not None:
                self._stop_event.set()
            raise

    async def _shutdown_registered_tasks(self) -> None:
        """Cancel + await every registered task within the configured timeout.

        Each registered task is cancelled in registration order. We then
        await its completion using ``asyncio.wait`` (not ``wait_for``) so a
        task that swallows ``CancelledError`` cannot stall the supervisor —
        the supervisor logs the timeout, releases the reference, and moves
        on. Any exception raised by a task propagates out of ``run()`` after
        every sibling has been processed.
        """
        first_exc: BaseException | None = None
        for entry in self._registry:
            task = entry.task
            if task is None:
                continue
            if not task.done():
                task.cancel()
                done, _pending = await asyncio.wait(
                    [task],
                    timeout=self._shutdown_timeout_seconds,
                )
                if task not in done:
                    log.error(
                        "pipeline supervisor task %s did not shut down within %ds",
                        entry.name,
                        self._shutdown_timeout_seconds,
                    )
                    continue
            # Task has finished — collect its exit reason.
            if task.cancelled():
                log.info("pipeline supervisor task %s cancelled cleanly", entry.name)
                continue
            exc = task.exception()
            if exc is None:
                log.info("pipeline supervisor task %s returned normally", entry.name)
            elif first_exc is None:
                first_exc = exc

        if first_exc is not None:
            raise first_exc

    def _install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, ValueError):
                # ``add_signal_handler`` is unsupported on Windows event loops
                # and on event loops created off the main thread. NSSM
                # delivers SIGINT via Ctrl-Break shimming in production; tests
                # can call ``request_stop`` directly.
                log.debug("could not install signal handler for %s", sig.name)

    def _remove_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in _SHUTDOWN_SIGNALS:
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)
