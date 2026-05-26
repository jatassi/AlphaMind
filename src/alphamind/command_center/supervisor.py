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
        """Supervise registered tasks; return cleanly on stop, raise on crash.

        Uses a manually-managed task set rather than
        :class:`asyncio.TaskGroup` so the shutdown timeout actually bounds
        the supervisor's exit — a TaskGroup's ``__aexit__`` awaits every
        child to completion regardless of the supervisor's
        ``asyncio.wait(timeout=N)``, which means a task that swallows
        ``CancelledError`` (the very class of bug the bounded wait was
        supposed to defend against) still stalls the daemon forever (F4).

        Crash semantics still match :class:`asyncio.TaskGroup` (per
        pre-resolved decision H): any registered task raising a non-
        ``CancelledError`` exception cancels its siblings, bounded-waits
        for their cancellation, then re-raises the original exception
        directly (which preserves the same exception type callers saw
        under the previous TaskGroup-based implementation, where
        ``first_non_cancelled`` unwrapped the group leaf).
        """
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        _install_signal_handlers(loop, self.request_stop)
        try:
            tasks: list[asyncio.Task[None]] = [
                asyncio.create_task(
                    coro_fn(self._session),
                    name=f"command_center_supervisor:{name}",
                )
                for name, coro_fn in self._registry
            ]
            stop_task = asyncio.create_task(self._stop_event.wait(), name="cc_stop_wait")
            # Wait until either stop is requested or any registered
            # task finishes (clean return or crash).
            pending_for_first: set[asyncio.Task[Any]] = {*tasks, stop_task}
            done, _ = await asyncio.wait(
                pending_for_first,
                return_when=asyncio.FIRST_COMPLETED,
            )
            del done
            # Was it a crash? Find the first task that returned a
            # non-CancelledError exception.
            crash_exc: BaseException | None = None
            for task in tasks:
                if not task.done():
                    continue
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    crash_exc = exc
                    break
            # Cancel everything still running and wait, bounded.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if not stop_task.done():
                stop_task.cancel()
            await _bounded_wait(tasks, self._shutdown_timeout_seconds)
            # Drain stop_task so the cancellation propagates and we
            # don't leave a dangling task reference.
            if not stop_task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
            if crash_exc is not None:
                # Surface the originating exception directly — preserves
                # the same exception type callers saw under the
                # TaskGroup-based implementation (``first_non_cancelled``
                # unwrapped the group; surfacing the leaf directly is the
                # equivalent end state).
                raise crash_exc
        finally:
            _remove_signal_handlers(loop)


async def _bounded_wait(tasks: list[asyncio.Task[None]], timeout_seconds: int) -> None:
    """Wait up to *timeout_seconds* for *tasks* to finish; log + abandon stragglers.

    A task swallowing ``CancelledError`` (the bug class F4 defends against)
    cannot stall the supervisor: after the timeout, the helper logs the
    straggler names and returns. The event loop will collect them when
    the process exits; in production NSSM bounds the process lifetime
    anyway. This is the deliberate trade-off — refusing to honor the
    timeout would mean a hostile task could pin the daemon indefinitely.
    """
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
    if pending:
        # Issue a second cancel (a task that suppressed once may honor
        # the second), then briefly re-wait. If still pending, abandon.
        for task in pending:
            with contextlib.suppress(RuntimeError):
                task.cancel()
        _, still_pending = await asyncio.wait(pending, timeout=0.5)
        if still_pending:
            names = sorted(t.get_name() for t in still_pending)
            log.warning(
                "supervisor shutdown timeout: abandoning unfinished tasks %s "
                "(they swallowed CancelledError); event loop will reap on exit",
                names,
            )
    del done


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
