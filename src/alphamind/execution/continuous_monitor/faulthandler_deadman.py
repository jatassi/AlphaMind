"""Faulthandler deadman: self-capture the blocking frame of a loop freeze (ALP-941).

A frozen monitor event loop is restarted by the out-of-process watchdog, but a
restart destroys the evidence — the frozen stack is gone, and CPython 3.14
cannot be introspected by py-spy. The deadman closes that gap with
:mod:`faulthandler`'s **C-thread** timer: each iteration of a supervised loop
it cancels the prior pending dump and re-arms ``faulthandler.
dump_traceback_later(stall_bound, repeat=False, file=monitor_faulthandler.log)``.
While the event loop turns, every armed timer is replaced before expiry —
nothing is written. The moment the loop freezes, the re-arm stops with a timer
still pending, faulthandler's dedicated watchdog thread (which does not run on
the frozen loop) fires at expiry, and the frozen main-thread stack — the exact
blocking frame — lands in the file before the external watchdog's restart
destroys it.

The arm/cancel callables are injected so the re-arm contract is unit-testable
against a fake timer; ``__main__`` binds the real ``faulthandler`` calls.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import (
    MonitorSupervisor,
    SupervisedLoop,
)

log = logging.getLogger(__name__)

_TASK_NAME = "faulthandler_deadman"


async def run_faulthandler_deadman(
    *,
    loop: SupervisedLoop,
    arm: Callable[[], None],
    cancel: Callable[[], None],
) -> None:
    """Run-forever re-arm loop: cancel the pending dump, arm a fresh one.

    Cancel-then-arm each iteration keeps exactly one pending dump alive at all
    times: the only way it reaches expiry is this coroutine never running again
    — which on a single event loop means the loop itself froze (the wedge the
    dump exists to explain).
    """
    async for _ in loop():
        cancel()
        arm()


def register_faulthandler_deadman_task(
    supervisor: MonitorSupervisor,
    *,
    tick_seconds: float,
    arm: Callable[[], None],
    cancel: Callable[[], None],
) -> None:
    """Register the deadman on the supervisor (watched, cadence ``tick_seconds``).

    The supervised loop's stall bound (``tick_seconds *
    watchdog_cadence_multiplier``) matches the dump timer the caller binds into
    ``arm`` — the deadman, the in-loop watchdog, and the out-of-process watchdog
    all key off the same cadence x multiplier relationship (scope D), so the
    dump is armed to fire within the same window the external watchdog declares
    a wedge.
    """

    async def _deadman_task(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
        del session, config
        await run_faulthandler_deadman(
            loop=lambda: supervisor.supervised_loop(_TASK_NAME, tick_seconds),
            arm=arm,
            cancel=cancel,
        )

    supervisor.register_task(name=_TASK_NAME, coro_fn=_deadman_task)
