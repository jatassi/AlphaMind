"""Tests for the dedicated out-of-process safety-core watchdog (ALP-857).

AC: the dedicated out-of-process watchdog restarts a wedged safety core on
heartbeat staleness. The watchdog is a *separate process* that probes the
safety core's file heartbeat and restarts it when stale — NOT a loop-resident
probe (a loop-resident watchdog cannot catch a freeze of its own loop, the
ALP-841 failure this story makes unrepresentable).

These tests drive ``run_watchdog`` against a fake heartbeat probe and a fake
process controller (the broker/clock/DB boundaries are not involved; the
controller is the one OS-process seam, faked the way the broker would be).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from alphamind.execution.continuous_monitor.safety_core.watchdog import (
    WatchdogLoop,
    run_watchdog,
)


class _FakeProbe:
    """Heartbeat probe returning a scripted age per tick."""

    def __init__(self, ages: list[float | None]) -> None:
        self._ages = list(ages)
        self.calls = 0

    def age(self, *, now: float) -> float | None:
        del now
        self.calls += 1
        return self._ages.pop(0) if self._ages else None


class _FakeController:
    """Records restart calls in place of a real NSSM/subprocess restart."""

    def __init__(self) -> None:
        self.restart_calls = 0

    def restart(self) -> None:
        self.restart_calls += 1


def _bounded_loop(n_ticks: int) -> WatchdogLoop:
    """A watchdog loop iterator that ticks exactly *n_ticks* times then stops."""

    async def _loop() -> AsyncIterator[None]:
        for _ in range(n_ticks):
            yield

    return _loop


@pytest.mark.asyncio
async def test_restarts_core_when_heartbeat_stale() -> None:
    """Heartbeat age past the stall bound → the watchdog restarts the core."""
    probe = _FakeProbe(ages=[120.0])  # 120s old, bound below
    controller = _FakeController()

    await run_watchdog(
        probe=probe,
        controller=controller,
        stall_bound_seconds=60.0,
        loop=_bounded_loop(1),
        now=lambda: 1000.0,
    )

    assert controller.restart_calls == 1


@pytest.mark.asyncio
async def test_does_not_restart_when_heartbeat_fresh() -> None:
    """Heartbeat within the stall bound → no restart."""
    probe = _FakeProbe(ages=[5.0])
    controller = _FakeController()

    await run_watchdog(
        probe=probe,
        controller=controller,
        stall_bound_seconds=60.0,
        loop=_bounded_loop(1),
        now=lambda: 1000.0,
    )

    assert controller.restart_calls == 0


@pytest.mark.asyncio
async def test_missing_heartbeat_is_startup_grace_not_restart() -> None:
    """A never-written heartbeat (core still starting) does not trip a restart."""
    probe = _FakeProbe(ages=[None])
    controller = _FakeController()

    await run_watchdog(
        probe=probe,
        controller=controller,
        stall_bound_seconds=60.0,
        loop=_bounded_loop(1),
        now=lambda: 1000.0,
    )

    assert controller.restart_calls == 0


class _SteppingClock:
    """Wall clock that advances a fixed step on each read (one read per tick)."""

    def __init__(self, *, start: float, step: float) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        now = self._t
        self._t += self._step
        return now


@pytest.mark.asyncio
async def test_restart_storm_suppressed_within_cooldown_window() -> None:
    """A heartbeat that stays stale across several ticks fires EXACTLY ONE restart.

    After a restart, NSSM must stop/relaunch the core and the core must write its
    first fresh heartbeat. During that whole window the file still holds the OLD
    stale timestamp, so a naive watchdog re-issues ``restart`` every tick — a
    storm that can interrupt the in-progress restart. The cooldown suppresses
    further restarts for ~``stall_bound`` seconds after one fires.
    """
    probe = _FakeProbe(ages=[120.0, 120.0, 120.0, 120.0])  # still stale every tick
    controller = _FakeController()
    # 5s between ticks; stall bound 60s → all four ticks fall inside the cooldown
    # window that opens at the first restart.
    clock = _SteppingClock(start=1000.0, step=5.0)

    await run_watchdog(
        probe=probe,
        controller=controller,
        stall_bound_seconds=60.0,
        loop=_bounded_loop(4),
        now=clock,
    )

    assert controller.restart_calls == 1


@pytest.mark.asyncio
async def test_second_restart_allowed_after_cooldown_elapses() -> None:
    """Still stale after the cooldown window → a second restart is allowed.

    The cooldown is a grace window for the core to relaunch + beat, not a
    permanent mute: if the heartbeat is still stale once ~``stall_bound`` seconds
    have passed, the core failed to recover and the watchdog restarts again.
    """
    probe = _FakeProbe(ages=[120.0, 120.0, 120.0])
    controller = _FakeController()
    # 40s between ticks: tick0 restarts at t=1000 (cooldown until 1060); tick1 at
    # t=1040 is suppressed (inside cooldown); tick2 at t=1080 is past the cooldown
    # and still stale → a second restart.
    clock = _SteppingClock(start=1000.0, step=40.0)

    await run_watchdog(
        probe=probe,
        controller=controller,
        stall_bound_seconds=60.0,
        loop=_bounded_loop(3),
        now=clock,
    )

    assert controller.restart_calls == 2
