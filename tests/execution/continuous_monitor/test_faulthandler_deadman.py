"""Tests for the faulthandler deadman task (ALP-941 scope E).

The deadman makes the *next* loop freeze self-introspecting: each iteration of
its supervised loop cancels the prior pending ``faulthandler`` dump and re-arms
a fresh one at ``stall_bound`` out. While the event loop turns, every armed
timer is replaced before it expires — nothing is written. If the loop freezes,
the re-arm stops, the last armed timer survives to expiry, and faulthandler's
dedicated C-thread dumps the frozen main-thread stack (the exact blocking
frame) to ``monitor_faulthandler.log``.

The arm/cancel callables are injected, so a fake timer that models
faulthandler's arm/replace/expire semantics against the test's virtual clock
proves both halves of the AC without real waiting or a real freeze: no dump
fires while the deadman keeps re-arming; the dump does fire once re-arming
stops.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.faulthandler_deadman import (
    register_faulthandler_deadman_task,
    run_faulthandler_deadman,
)
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor

_TICK = 15.0
_STALL_BOUND = 150.0  # tick * watchdog_cadence_multiplier (15 x 10)


class _FakeClock:
    """Virtual monotonic clock + cooperative sleep (the sanctioned clock seam)."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, delay: float) -> None:
        await asyncio.sleep(0)
        self._now += delay

    def advance(self, delay: float) -> None:
        self._now += delay


class _FakeDumpTimer:
    """Models faulthandler's dump_traceback_later / cancel semantics.

    ``arm`` schedules a dump at ``now + stall_bound`` (replacing any pending
    one, as ``dump_traceback_later`` does); ``cancel`` unschedules. ``fired``
    reports whether a scheduled dump's expiry has passed while still armed —
    i.e. whether faulthandler's C-thread would have written the stack dump.
    """

    def __init__(self, clock: _FakeClock, stall_bound: float) -> None:
        self._clock = clock
        self._stall_bound = stall_bound
        self.fire_at: float | None = None
        self.expired_while_armed: bool = False
        self.calls: list[str] = []

    def arm(self) -> None:
        self._observe()
        self.calls.append("arm")
        self.fire_at = self._clock.monotonic() + self._stall_bound

    def cancel(self) -> None:
        self._observe()
        self.calls.append("cancel")
        self.fire_at = None

    def fired(self) -> bool:
        self._observe()
        return self.expired_while_armed

    def _observe(self) -> None:
        if self.fire_at is not None and self._clock.monotonic() >= self.fire_at:
            self.expired_while_armed = True


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260609T120000Z-deadbeef",
        started_at=datetime.now(UTC),
        mode="paper",
    )


def _config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
    )


def _bounded_loop(n: int) -> object:
    async def _loop() -> AsyncIterator[None]:
        for _ in range(n):
            yield
            await asyncio.sleep(0)

    return _loop


async def test_no_dump_while_the_loop_keeps_rearming() -> None:
    """A turning loop replaces every armed timer before expiry — nothing fires."""
    clock = _FakeClock()
    timer = _FakeDumpTimer(clock, _STALL_BOUND)

    async def _loop() -> AsyncIterator[None]:
        for _ in range(5):
            yield
            await clock.sleep(_TICK)  # the supervised loop's pacing sleep

    await run_faulthandler_deadman(loop=_loop, arm=timer.arm, cancel=timer.cancel)

    # Five iterations turned; the tick (15s) is far inside the bound (150s), so
    # no armed timer ever reached expiry — faulthandler wrote nothing.
    assert timer.fired() is False
    # Each iteration cancels the prior pending dump, then re-arms.
    assert timer.calls == ["cancel", "arm"] * 5


async def test_dump_fires_when_rearming_stops() -> None:
    """A frozen loop stops re-arming; the last armed timer survives to expiry.

    The bounded iterator ending models the freeze: the deadman coroutine never
    runs again (exactly what a blocked loop thread means for it). The pending
    timer is then past its expiry → faulthandler's C-thread dumps the stack.
    """
    clock = _FakeClock()
    timer = _FakeDumpTimer(clock, _STALL_BOUND)

    async def _loop() -> AsyncIterator[None]:
        for _ in range(2):
            yield
            await clock.sleep(_TICK)

    await run_faulthandler_deadman(loop=_loop, arm=timer.arm, cancel=timer.cancel)

    # The deadman left a pending dump armed (last call is arm, not cancel) ...
    assert timer.calls[-1] == "arm"
    assert timer.fire_at is not None
    assert timer.fired() is False
    # ... and once the freeze outlasts the stall bound, it fires.
    clock.advance(_STALL_BOUND + 1.0)
    assert timer.fired() is True


async def test_register_wires_a_watched_supervised_task() -> None:
    """The deadman registers under its own name and beats the in-loop watchdog.

    Driving the registered coroutine through the supervisor proves the task
    runs via ``supervised_loop`` (watched, bound = tick x multiplier) and that
    the injected arm/cancel callables are the ones invoked.
    """
    clock = _FakeClock()
    supervisor = MonitorSupervisor(
        session=_session(),
        config=_config(),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    timer = _FakeDumpTimer(clock, _STALL_BOUND)
    started = asyncio.Event()

    def _arm_and_signal() -> None:
        timer.arm()
        started.set()

    register_faulthandler_deadman_task(
        supervisor, tick_seconds=_TICK, arm=_arm_and_signal, cancel=timer.cancel
    )

    assert "faulthandler_deadman" in supervisor.task_names()

    async def _stop_after_first_arm() -> None:
        await started.wait()
        supervisor.request_stop()

    await asyncio.gather(supervisor.run(), _stop_after_first_arm())

    assert timer.calls[:2] == ["cancel", "arm"]
    watch = supervisor._watch["faulthandler_deadman"]
    assert watch.watched is True
    assert watch.bound_seconds == _STALL_BOUND
