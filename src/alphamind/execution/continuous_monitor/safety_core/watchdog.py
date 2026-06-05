"""Dedicated out-of-process watchdog for the safety core (ALP-857 / ADR-0004).

A **separate process** that probes the safety core's file heartbeat and restarts
the core when the heartbeat goes stale. It is NOT a loop-resident probe inside
the safety core's own event loop: a loop-resident watchdog cannot catch a freeze
of its own loop — that is the structural ALP-841 failure (a data hang froze the
safety loop *and* the in-loop watchdog together). Running the watchdog in its own
process, reading the core's heartbeat across the file boundary, makes that
mechanism unrepresentable.

The shell is thin: each tick reads the heartbeat age and, if it exceeds the
stall bound, asks the injected :class:`ProcessController` to restart the core.
In production the controller shells out to NSSM (``nssm restart
alphamind-safety-core``); in tests a fake controller records the calls, so the
restart-on-staleness behaviour is unit-testable without a real process or NSSM.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Protocol

log = logging.getLogger(__name__)


class HeartbeatProbe(Protocol):
    """Read-only heartbeat age source the watchdog polls."""

    def age(self, *, now: float) -> float | None: ...


class ProcessController(Protocol):
    """Restarts the supervised safety-core process (the one OS-process seam).

    Production implementations shell out to the service manager (NSSM) or
    ``subprocess``; tests substitute a fake that records ``restart`` calls. The
    watchdog depends only on this Protocol, never on a concrete process API, so
    the restart-on-staleness logic is testable without a real process.
    """

    def restart(self) -> None: ...


# A zero-arg factory returning the watchdog's tick iterator. Production binds it
# to a ``supervised_loop``-style sleeper; tests bind a bounded iterator so the
# loop terminates. Mirrors the supervisor's ``SupervisedLoop`` seam without
# importing it (the safety core imports no monitor internals).
WatchdogLoop = Callable[[], AsyncIterator[None]]


async def run_watchdog(
    *,
    probe: HeartbeatProbe,
    controller: ProcessController,
    stall_bound_seconds: float,
    loop: WatchdogLoop,
    now: Callable[[], float] | None = None,
) -> None:
    """Run-forever watchdog loop: probe the heartbeat, restart on staleness.

    On each tick, read the heartbeat age. ``None`` (file absent) is startup
    grace — the core has not yet beaten — so no restart. An age strictly greater
    than ``stall_bound_seconds`` means the core has wedged: log loudly and ask
    the controller to restart it.

    ``now`` defaults to a wall-clock epoch (``datetime.now(UTC).timestamp()``) so
    it compares against the heartbeat's wall-clock timestamp across the process
    boundary; tests inject a deterministic source.
    """
    clock = now or (lambda: datetime.now(UTC).timestamp())
    async for _ in loop():
        age = probe.age(now=clock())
        if age is None:
            continue
        if age > stall_bound_seconds:
            log.critical(
                "safety-core watchdog: heartbeat stale (%.0fs > bound %.0fs) — "
                "restarting the safety-core process",
                age,
                stall_bound_seconds,
            )
            controller.restart()


def supervised_watchdog_loop(cadence_seconds: float) -> WatchdogLoop:
    """Production watchdog tick iterator: yield, then sleep ``cadence_seconds``.

    The watchdog's own liveness is the service manager's concern (NSSM restarts
    *it* on exit); it needs no internal heartbeat because it is the thing that
    catches freezes, and a frozen watchdog simply stops restarting — fail-safe,
    since the broker floor still protects positions.
    """

    async def _loop() -> AsyncIterator[None]:
        while True:
            yield
            await asyncio.sleep(cadence_seconds)

    return _loop
