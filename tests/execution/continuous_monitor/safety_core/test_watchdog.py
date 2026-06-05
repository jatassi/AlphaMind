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
