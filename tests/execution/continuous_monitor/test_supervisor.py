"""Tests for ``MonitorSupervisor`` (story 01).

The supervisor's job is narrow: own an asyncio event loop, hold a task
registry that later stories (02b underlying-price stream, 02c fill consumer,
03a greeks refresh, 03b breach loop, 04a engine-envelope cascade dispatcher,
04b emergency-invocation trigger, 04c options bracket-stop firing) plug into,
install signal handlers that trigger graceful shutdown, and cancel + await
its registered tasks within a bounded shutdown window. Exceptions from a
registered task propagate out of ``run()`` after sibling tasks have been
cancelled — the surrounding NSSM service catches the propagation and restarts.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import unittest.mock as mock
from datetime import UTC, datetime

import pytest

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import (
    MonitorSupervisor,
    derive_stall_bound,
)


class _FakeClock:
    """Injectable monotonic clock + sleep for deterministic watchdog timing.

    ``monotonic()`` returns the accumulated virtual time; ``sleep(d)`` yields
    control to the event loop (so concurrent tasks make progress, like real
    ``asyncio.sleep``) and advances virtual time by ``d``. The system clock is
    one of the four sanctioned mock boundaries — faking it here lets a
    watchdog-bound test trip at an exact virtual elapsed time without any
    real-wall-clock waiting.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    async def sleep(self, delay: float) -> None:
        # Cooperative yield so sibling tasks (the registered task, the stop
        # trigger) run, then advance virtual time as a real sleep would.
        await asyncio.sleep(0)
        self._now += delay

    def advance(self, delay: float) -> None:
        self._now += delay


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T120000Z-deadbeef",
        started_at=datetime.now(UTC),
        mode="paper",
    )


def _config(
    *, shutdown_timeout: int = 5, watchdog_multiplier: float = 10.0
) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=shutdown_timeout,
        watchdog_cadence_multiplier=watchdog_multiplier,
    )


def _supervisor(
    *,
    shutdown_timeout: int = 5,
    watchdog_multiplier: float = 10.0,
    clock: _FakeClock | None = None,
) -> MonitorSupervisor:
    fake = clock or _FakeClock()
    return MonitorSupervisor(
        session=_session(),
        config=_config(shutdown_timeout=shutdown_timeout, watchdog_multiplier=watchdog_multiplier),
        sleep=fake.sleep,
        monotonic=fake.monotonic,
    )


class TestRegisterTask:
    def test_register_task_stores_under_given_name(self) -> None:
        supervisor = _supervisor()

        async def _task(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session
            del config

        supervisor.register_task(name="fill_consumer", coro_fn=_task)
        assert "fill_consumer" in supervisor.task_names()

    def test_register_task_rejects_duplicate_name(self) -> None:
        supervisor = _supervisor()

        async def _task(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session
            del config

        supervisor.register_task(name="fill_consumer", coro_fn=_task)
        with pytest.raises(ValueError, match="already registered"):
            supervisor.register_task(name="fill_consumer", coro_fn=_task)


class TestSupervisorRunNoTasks:
    async def test_run_returns_cleanly_when_request_stop_called(self) -> None:
        supervisor = _supervisor()

        async def _stop_after_yield() -> None:
            # Yield once so ``run`` reaches its wait point before stop fires.
            await asyncio.sleep(0)
            supervisor.request_stop()

        await asyncio.gather(supervisor.run(), _stop_after_yield())


class TestSupervisorRunWithOneTask:
    async def test_task_starts_and_is_cancelled_on_stop(self) -> None:
        cancelled = asyncio.Event()
        started = asyncio.Event()

        async def long_running(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session
            del config
            started.set()
            try:
                await asyncio.Future()  # never resolves on its own
            except asyncio.CancelledError:
                cancelled.set()
                raise

        supervisor = _supervisor()
        supervisor.register_task(name="long_running", coro_fn=long_running)

        async def trigger_stop() -> None:
            await started.wait()
            supervisor.request_stop()

        await asyncio.gather(supervisor.run(), trigger_stop())
        assert cancelled.is_set()


class TestSupervisorRunPropagatesTaskException:
    async def test_task_exception_propagates_after_siblings_are_cancelled(self) -> None:
        sibling_cancelled = asyncio.Event()
        bad_started = asyncio.Event()

        async def bad_task(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session
            del config
            bad_started.set()
            await asyncio.sleep(0)  # ensure sibling is awaiting
            raise RuntimeError("kaboom")

        async def sibling(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session
            del config
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

        supervisor = _supervisor()
        supervisor.register_task(name="sibling", coro_fn=sibling)
        supervisor.register_task(name="bad", coro_fn=bad_task)

        with pytest.raises(RuntimeError, match="kaboom"):
            await supervisor.run()

        assert sibling_cancelled.is_set()
        assert bad_started.is_set()


class TestSupervisorSignalHandlers:
    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "add_signal_handler is POSIX-only; Windows ProactorEventLoop uses "
            "a different shutdown path"
        ),
    )
    async def test_run_installs_sigint_and_sigterm_handlers_during_run(self) -> None:
        loop = asyncio.get_running_loop()
        seen: dict[str, bool] = {"sigint": False, "sigterm": False}

        supervisor = _supervisor()

        async def probe_and_stop() -> None:
            await asyncio.sleep(0)
            handlers = getattr(loop, "_signal_handlers", {})
            seen["sigint"] = signal.SIGINT in handlers
            seen["sigterm"] = signal.SIGTERM in handlers
            supervisor.request_stop()

        await asyncio.gather(supervisor.run(), probe_and_stop())
        assert seen["sigint"] is True
        assert seen["sigterm"] is True


class TestSupervisorTaskCoroSignature:
    """Tasks receive ``(session, config)`` so the registry surface is uniform."""

    async def test_task_receives_session_and_config(self) -> None:
        captured: dict[str, object] = {}
        inspected = asyncio.Event()

        async def inspect(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            captured["session"] = session
            captured["config"] = config
            inspected.set()

        supervisor = _supervisor()
        supervisor.register_task(name="inspect", coro_fn=inspect)

        async def trigger_stop_after_first_pass() -> None:
            await inspected.wait()
            supervisor.request_stop()

        await asyncio.gather(supervisor.run(), trigger_stop_after_first_pass())
        assert isinstance(captured["session"], MonitorSession)
        assert isinstance(captured["config"], ContinuousMonitorConfig)


# ---------------------------------------------------------------------------
# ALP-826 — per-cadence default-on stall watchdog
# ---------------------------------------------------------------------------


_EXIT_PATH = "alphamind.execution.continuous_monitor.supervisor.os._exit"


class _StopAfter:
    """Injectable sleep that drives a fake clock and stops the watchdog loop.

    Advances *clock* by each requested delay (as a real sleep would) and raises
    ``CancelledError`` after *passes* sleeps so the run-forever watchdog loop
    terminates deterministically — mirroring how the supervisor cancels the
    watchdog at shutdown.
    """

    def __init__(self, clock: _FakeClock, *, passes: int) -> None:
        self._clock = clock
        self._passes = passes
        self.calls = 0

    async def __call__(self, delay: float) -> None:
        self.calls += 1
        if self.calls > self._passes:
            raise asyncio.CancelledError
        await asyncio.sleep(0)
        self._clock.advance(delay)


class TestDeriveStallBound:
    def test_bound_is_cadence_times_multiplier(self) -> None:
        bound = derive_stall_bound(cadence_seconds=1.0, multiplier=10.0, override_seconds=None)
        assert bound == 10.0

    def test_override_takes_precedence_over_cadence(self) -> None:
        bound = derive_stall_bound(cadence_seconds=1.0, multiplier=10.0, override_seconds=120.0)
        assert bound == 120.0


class TestSupervisedLoop:
    async def test_beats_at_top_of_each_iteration_and_paces_at_cadence(self) -> None:
        """A loop driven by supervised_loop is watched with no hand-wired beat()."""
        clock = _FakeClock()
        supervisor = _supervisor(clock=clock)
        beats_at: list[float] = []
        iterations = 0

        async for _ in supervisor.supervised_loop("entry_window", 60.0):
            # The beat fired at the TOP of this iteration → last_beat is now().
            beats_at.append(supervisor._watch["entry_window"].last_beat)  # type: ignore[arg-type]
            iterations += 1
            if iterations >= 3:
                break

        # Registered + watched purely by driving the loop (no beat() call site).
        entry = supervisor._watch["entry_window"]
        assert entry.watched is True
        assert entry.bound_seconds == 60.0 * 10.0
        # Three beats, each at the clock value after the preceding 60s sleep.
        assert beats_at == [1000.0, 1060.0, 1120.0]


class TestPerTaskBounds:
    async def test_fast_and_slow_tasks_trip_at_independent_bounds(self) -> None:
        """1s-cadence and 60s-cadence tasks trip os._exit at different bounds.

        No shared global: the fast task (bound 10s) is stale once 10s elapse,
        while the slow task (bound 600s) is still healthy at the same instant.
        """
        clock = _FakeClock()
        supervisor = _supervisor(clock=clock)
        supervisor._stop_event = asyncio.Event()
        supervisor.register_watch("bracket_stops", 1.0)  # bound 10s
        supervisor.register_watch("breach_loop", 60.0)  # bound 600s
        supervisor.beat("bracket_stops")
        supervisor.beat("breach_loop")
        # Advance past the fast bound but well within the slow bound.
        clock.advance(20.0)

        exits: list[int] = []
        stopper = _StopAfter(clock, passes=1)  # one check pass, then cancel
        with (
            mock.patch(_EXIT_PATH, side_effect=exits.append),
            pytest.raises(asyncio.CancelledError),
        ):
            supervisor._sleep = stopper  # type: ignore[method-assign]
            await supervisor._watchdog_loop()

        # Only the fast task (bound 10s) was stale at +20s; the slow task
        # (bound 600s) is still healthy — no shared global timeout.
        assert exits == [1]

    async def test_slow_task_not_tripped_before_its_bound(self) -> None:
        clock = _FakeClock()
        supervisor = _supervisor(clock=clock)
        supervisor._stop_event = asyncio.Event()
        supervisor.register_watch("breach_loop", 60.0)  # bound 600s
        supervisor.beat("breach_loop")
        clock.advance(20.0)  # < 600s

        exits: list[int] = []
        stopper = _StopAfter(clock, passes=1)
        with (
            mock.patch(_EXIT_PATH, side_effect=exits.append),
            pytest.raises(asyncio.CancelledError),
        ):
            supervisor._sleep = stopper  # type: ignore[method-assign]
            await supervisor._watchdog_loop()

        assert exits == []

    async def test_explicit_override_supersedes_cadence_bound(self) -> None:
        """An irregular task's stall_timeout_seconds override sets its bound."""
        clock = _FakeClock()
        supervisor = _supervisor(clock=clock)
        supervisor._stop_event = asyncio.Event()
        # cadence 1s would imply bound 10s, but the override pins it to 120s.
        supervisor.register_watch("irregular", 1.0, stall_timeout_seconds=120.0)
        supervisor.beat("irregular")
        clock.advance(60.0)  # past the 10s cadence bound, within the 120s override

        exits: list[int] = []
        stopper = _StopAfter(clock, passes=1)
        with (
            mock.patch(_EXIT_PATH, side_effect=exits.append),
            pytest.raises(asyncio.CancelledError),
        ):
            supervisor._sleep = stopper  # type: ignore[method-assign]
            await supervisor._watchdog_loop()

        assert exits == []


class TestWatchedOptOut:
    async def test_unwatched_task_never_trips_and_never_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The control surface (watched=False) is never tripped, never warned."""
        clock = _FakeClock()
        supervisor = _supervisor(clock=clock)
        supervisor._stop_event = asyncio.Event()

        async def _server(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session, config

        supervisor.register_task(name="control_surface", coro_fn=_server, watched=False)
        # Far past any conceivable bound; an unwatched task must be ignored.
        clock.advance(100_000.0)

        exits: list[int] = []
        stopper = _StopAfter(clock, passes=2)
        with (
            mock.patch(_EXIT_PATH, side_effect=exits.append),
            caplog.at_level("WARNING"),
            pytest.raises(asyncio.CancelledError),
        ):
            supervisor._sleep = stopper  # type: ignore[method-assign]
            await supervisor._watchdog_loop()

        assert exits == []
        assert "control_surface" not in caplog.text


class TestStartupGraceWarning:
    async def test_never_beating_watched_task_warns_once_at_startup_grace(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A watched=True task that never beats is named in a startup warning."""
        clock = _FakeClock()
        supervisor = _supervisor(clock=clock)
        supervisor._stop_event = asyncio.Event()
        supervisor.register_watch("forgot_to_beat", 1.0)  # bound 10s; never beats
        clock.advance(20.0)  # past the startup-grace bound

        stopper = _StopAfter(clock, passes=2)
        with (
            mock.patch(_EXIT_PATH, side_effect=AssertionError("must not exit")),
            caplog.at_level("WARNING"),
            pytest.raises(asyncio.CancelledError),
        ):
            supervisor._sleep = stopper  # type: ignore[method-assign]
            await supervisor._watchdog_loop()

        warnings = [r for r in caplog.records if "forgot_to_beat" in r.getMessage()]
        assert len(warnings) == 1  # latched: one warning, not one per check pass


class TestWatchdogShutdown:
    async def test_graceful_stop_cancels_watchdog_without_hanging(self) -> None:
        """run() must return within the shutdown timeout — the watchdog must not block __aexit__."""
        started = asyncio.Event()

        async def _blocking_task(session: MonitorSession, config: ContinuousMonitorConfig) -> None:
            del session, config
            started.set()
            await asyncio.Future()  # blocks until cancelled

        supervisor = _supervisor(shutdown_timeout=3)
        supervisor.register_task(name="blocking", coro_fn=_blocking_task)

        async def _stop_after_start() -> None:
            await started.wait()
            supervisor.request_stop()

        # asyncio.wait_for with a generous ceiling catches a hang if it occurs.
        await asyncio.wait_for(
            asyncio.gather(supervisor.run(), _stop_after_start()),
            timeout=5.0,
        )
