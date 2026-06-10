"""Asyncio supervisor for the continuous-monitor process (story 01).

Mirrors :class:`alphamind.scheduler.supervisor.PipelineSupervisor`: owns
the long-running task tree, installs SIGINT/SIGTERM handlers, supervises
every task under :class:`asyncio.TaskGroup`. A crash cancels its siblings
(fail-closed); a graceful stop cancels every task; the TaskGroup body
re-raises the first non-``CancelledError`` leaf so callers see the same
exception surface they did before the migration. Differences from the
pipeline supervisor: tasks receive ``(session, config)`` and the shutdown
timeout comes from the config record.

Liveness watchdog (ALP-768 / ALP-826). Every run-forever task drives its
loop through :meth:`MonitorSupervisor.supervised_loop`, which beats at the
top of each iteration and paces the loop. Each watched task is bounded by
its own declared heartbeat cadence (``cadence_seconds *
watchdog_cadence_multiplier``, or an explicit ``stall_timeout_seconds``
override) rather than one global timeout, so a 1s safety-critical loop is
detected far sooner than a 60s loop. A task registered ``watched=True`` (the
default) that never beats is loud at startup grace; the control-surface HTTP
server is the one task registered ``watched=False``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from alphamind._kernel.exception_group import first_non_cancelled
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.process_supervision import HeartbeatSink

log = logging.getLogger(__name__)

TaskCoroFn = Callable[[MonitorSession, ContinuousMonitorConfig], Coroutine[Any, Any, None]]
SleepFn = Callable[[float], Awaitable[None]]
MonotonicFn = Callable[[], float]
# A zero-arg factory returning the supervised heartbeat iterator with its name +
# cadence pre-bound. A run-forever leaf task drives it as
# ``async for _ in loop(): <body>`` so the supervisor owns the beat + the pacing.
# Wiring binds it to ``lambda: supervisor.supervised_loop(name, cadence)``; tests
# substitute a fake iterator factory that bounds the iteration count.
SupervisedLoop = Callable[[], AsyncIterator[None]]
_SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def derive_stall_bound(
    *, cadence_seconds: float, multiplier: float, override_seconds: float | None
) -> float:
    """Watchdog stall bound for a watched task (pure; functional core).

    The explicit ``override_seconds`` wins for an irregular task; otherwise
    the bound is the declared heartbeat ``cadence_seconds`` scaled by
    ``multiplier``. No I/O, no clock — the imperative watchdog loop and the
    watch-registration path both call this.
    """
    if override_seconds is not None:
        return override_seconds
    return cadence_seconds * multiplier


@dataclass(slots=True)
class _WatchEntry:
    """Per-task watchdog state: its stall bound + last-beat timestamp.

    ``last_beat`` is ``None`` until the task first beats — the startup-grace
    signal. ``watched=False`` opts a task out entirely (the control surface):
    it is never warned-about and never tripped. ``warned`` latches the
    never-beaten warning so it fires once, not on every check pass.
    """

    bound_seconds: float
    registered_at: float
    last_beat: float | None = None
    watched: bool = True
    warned: bool = False


class MonitorSupervisor:
    """Owns the asyncio task tree for one monitor-process lifetime."""

    def __init__(
        self,
        *,
        session: MonitorSession,
        config: ContinuousMonitorConfig,
        sleep: SleepFn = asyncio.sleep,
        monotonic: MonotonicFn = time.monotonic,
        heartbeat: HeartbeatSink | None = None,
    ) -> None:
        # ``heartbeat`` (ALP-941) is the off-hot-path file beat the dedicated
        # out-of-process monitor watchdog probes. ``None`` (the default) opts
        # out entirely — existing behavior is unchanged.
        self._session = session
        self._config = config
        self._sleep = sleep
        self._monotonic = monotonic
        self._heartbeat = heartbeat
        self._registry: list[tuple[str, TaskCoroFn]] = []
        self._stop_event: asyncio.Event | None = None
        # Per-task watchdog state (ALP-826). Seeded at task-registration when an
        # explicit ``watched`` intent is given, and at watch-registration (the
        # first ``supervised_loop`` iteration or an explicit ``register_watch``)
        # so the bound is known. ``beat`` updates the last-beat timestamp.
        self._watch: dict[str, _WatchEntry] = {}

    def register_task(self, *, name: str, coro_fn: TaskCoroFn, watched: bool = True) -> None:
        """Register a coroutine factory under *name*; reject duplicates.

        ``watched`` (default ``True``) is the liveness intent. A ``watched=True``
        task is expected to drive its loop through :meth:`supervised_loop` (or
        declare a watch + :meth:`beat`); one that never beats is named in a
        startup-grace warning. ``watched=False`` opts the task out of the
        watchdog entirely — used only for the control-surface HTTP server,
        which blocks in ``server.serve()`` with no natural per-iteration
        heartbeat (port-level liveness is a noted follow-up).
        """
        if any(existing == name for existing, _ in self._registry):
            msg = f"task name {name!r} already registered"
            raise ValueError(msg)
        self._registry.append((name, coro_fn))
        if not watched:
            # Seed an opted-out entry so the watchdog never warns about or trips
            # this task even if a stray beat ever arrives.
            self._watch[name] = _WatchEntry(
                bound_seconds=0.0,
                registered_at=self._monotonic(),
                watched=False,
            )

    def task_names(self) -> tuple[str, ...]:
        """Return the registered task names in registration order."""
        return tuple(name for name, _ in self._registry)

    def register_watch(
        self,
        name: str,
        cadence_seconds: float,
        *,
        stall_timeout_seconds: float | None = None,
    ) -> None:
        """Declare *name*'s heartbeat cadence so the watchdog can bound it.

        Idempotent: re-declaring keeps the existing last-beat timestamp (so a
        task that already beat is not reset) while refreshing the bound. The
        public seam both :meth:`supervised_loop` (internally) and a stream
        poll-loop that beats per slice via :meth:`beat` call to register the
        cadence used for the bound. A ``watched=False`` opt-out is never
        overwritten back to watched here.
        """
        existing = self._watch.get(name)
        if existing is not None and not existing.watched:
            return
        bound = derive_stall_bound(
            cadence_seconds=cadence_seconds,
            multiplier=self._config.watchdog_cadence_multiplier,
            override_seconds=stall_timeout_seconds,
        )
        if existing is None:
            self._watch[name] = _WatchEntry(
                bound_seconds=bound,
                registered_at=self._monotonic(),
            )
        else:
            existing.bound_seconds = bound

    async def supervised_loop(
        self,
        name: str,
        cadence_seconds: float,
        *,
        stall_timeout_seconds: float | None = None,
    ) -> AsyncIterator[None]:
        """Heartbeat seam: a run-forever loop drives this as its iterator.

        Usage::

            async for _ in supervisor.supervised_loop(name, cadence):
                <body>

        Records a beat for *name* at the top of every iteration, yields to run
        the body, then sleeps ``cadence_seconds`` before the next yield — so the
        loop body no longer owns its own trailing ``asyncio.sleep`` and beating
        is automatic (no hand-wired ``beat()``). ``cadence_seconds`` is the
        heartbeat interval, not the task's functional work interval: a task that
        acts on a daily wall-clock schedule still iterates on a short cadence and
        checks the wall clock inside the body. The watchdog bound is
        ``cadence_seconds * watchdog_cadence_multiplier`` unless
        ``stall_timeout_seconds`` overrides it.
        """
        self.register_watch(name, cadence_seconds, stall_timeout_seconds=stall_timeout_seconds)
        while True:
            self.beat(name)
            yield
            await self._sleep(cadence_seconds)

    def beat(self, task_name: str) -> None:
        """Record a liveness heartbeat for *task_name* (ALP-826 watchdog).

        Resets the task's stall timer. A name beaten before any
        :meth:`register_watch`/:meth:`supervised_loop` declaration is recorded
        with a zero bound until its cadence is declared — the watchdog skips a
        watched task until it has a positive bound, so a pre-declaration beat is
        harmless. A ``watched=False`` task is left opted-out.
        """
        existing = self._watch.get(task_name)
        now = self._monotonic()
        if existing is None:
            self._watch[task_name] = _WatchEntry(
                bound_seconds=0.0,
                registered_at=now,
                last_beat=now,
            )
            return
        if not existing.watched:
            return
        existing.last_beat = now

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
                    # Keep a reference so we can cancel the watchdog explicitly
                    # at shutdown. TaskGroup.__aexit__ only cancels tasks
                    # automatically when the body raises; on a normal body exit
                    # it waits for all group tasks — so an uncancelled
                    # ``while True`` watchdog would hang graceful shutdown.
                    watchdog_task = tg.create_task(
                        self._watchdog_loop(),
                        name="monitor_supervisor:watchdog",
                    )
                    await self._stop_event.wait()
                    # Graceful stop — cascade-cancel all tasks (registered +
                    # watchdog) and bound the wait so a task swallowing
                    # ``CancelledError`` cannot stall the process.
                    for task in tasks:
                        task.cancel()
                    watchdog_task.cancel()
                    stop_targets: list[asyncio.Task[None]] = [*tasks, watchdog_task]
                    timeout = self._config.supervisor_shutdown_timeout_seconds
                    await asyncio.wait(stop_targets, timeout=timeout)
            except BaseExceptionGroup as eg:
                first = first_non_cancelled(eg)
                if first is not None:
                    raise first from eg
        finally:
            _remove_signal_handlers(loop)

    async def _watchdog_loop(self) -> None:
        """Periodically check each watched task against its own stall bound.

        The check interval keys off the smallest active bound (checked at
        quarter-period so a stall is detected promptly relative to that task's
        cadence), not a single global. A watched task that has never beaten
        within its bound emits a one-shot startup-grace warning naming it
        (default-on visibility); a watched task that beat and then went silent
        past its bound trips ``os._exit(1)`` for NSSM to restart.
        """
        while True:
            await self._sleep(self._check_interval())
            # The sleep resumed, so the event loop is demonstrably turning —
            # beat the cross-process file heartbeat (ALP-941). A loop-thread
            # freeze stops this coroutine inside the sleep above, the file
            # goes stale, and the out-of-process watchdog restarts the
            # monitor — the wedge this in-loop watchdog structurally cannot
            # catch. A failing beat (disk full, permissions) must not kill
            # the process it reports on: log and keep checking — if the
            # failure persists, the stale file makes the external watchdog
            # restart the monitor, which is the correct recovery.
            if self._heartbeat is not None:
                try:
                    self._heartbeat.beat()
                except Exception:
                    log.warning("file heartbeat beat failed", exc_info=True)
            # Exit cleanly if the supervisor is shutting down — avoids a
            # spurious os._exit(1) while tasks are mid-cancellation.
            if self._stop_event is not None and self._stop_event.is_set():
                return
            now = self._monotonic()
            for name, entry in self._watch.items():
                self._check_watch_entry(name, entry, now)

    def _check_watch_entry(self, name: str, entry: _WatchEntry, now: float) -> None:
        """One watchdog check pass for one task: warn on net gaps, trip on stall."""
        if not entry.watched:
            return
        if entry.bound_seconds <= 0.0:
            # Watched but no cadence declared, so no stall bound applies
            # and the task cannot be tripped. A ``supervised_loop`` task
            # registers its bound (via ``register_watch``) before its
            # first beat, so a task that has *beaten* yet still carries no
            # bound called ``beat()`` directly without ``register_watch``
            # — it is silently outside the liveness net. Be loud once
            # (the default-on visibility guarantee) rather than skipping
            # it without a trace, so a forgotten ``register_watch`` in a
            # consumer wiring is caught at runtime, not in production.
            if not entry.warned and entry.last_beat is not None:
                log.warning(
                    "watchdog: task %r beats but declared no heartbeat "
                    "cadence (no register_watch / supervised_loop) — it is "
                    "outside the liveness net; declare its cadence so a "
                    "stall bound applies.",
                    name,
                )
                entry.warned = True
            return
        if entry.last_beat is None:
            # Never beaten — loud at startup grace (one bound elapsed
            # since registration), then latch so it does not spam.
            if not entry.warned and now - entry.registered_at > entry.bound_seconds:
                log.warning(
                    "watchdog: task %r is registered watched=True but has not "
                    "beaten within its %.0fs startup-grace bound — it is outside "
                    "the liveness net (drive it through supervised_loop or call "
                    "beat()).",
                    name,
                    entry.bound_seconds,
                )
                entry.warned = True
            return
        elapsed = now - entry.last_beat
        if elapsed > entry.bound_seconds:
            log.critical(
                "watchdog: task %r has not heartbeated in %.0fs "
                "(bound %.0fs) — forcing process exit for NSSM restart",
                name,
                elapsed,
                entry.bound_seconds,
            )
            # ``_exit`` (not ``sys.exit``) skips atexit handlers and
            # ``finally`` blocks so the process terminates immediately,
            # giving NSSM a clean exit code to restart on.
            os._exit(1)

    def _check_interval(self) -> float:
        """Quarter of the smallest active bound, floored at 1s.

        Keying off the smallest bound means a 1s-cadence task is checked far
        more often than the old global quarter-hour, while a process with only
        coarse tasks still checks cheaply. Falls back to 1s when no task has a
        positive bound yet (startup, before any cadence is declared).
        """
        bounds = [
            e.bound_seconds for e in self._watch.values() if e.watched and e.bound_seconds > 0.0
        ]
        if not bounds:
            return 1.0
        return max(1.0, min(bounds) / 4)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> None:
    # ``add_signal_handler`` is unsupported on Windows event loops / loops
    # off the main thread; NSSM shims SIGINT via Ctrl-Break in production.
    # CPython >= 3.13 surfaces the off-main-thread case as RuntimeError
    # (wrapping set_wakeup_fd's ValueError), so both are tolerated.
    for sig in _SHUTDOWN_SIGNALS:
        try:
            loop.add_signal_handler(sig, callback)
        except (NotImplementedError, ValueError, RuntimeError):
            log.debug("could not install signal handler for %s", sig.name)


def _remove_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in _SHUTDOWN_SIGNALS:
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            loop.remove_signal_handler(sig)
