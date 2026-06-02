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
import time
import unittest.mock as mock
from datetime import UTC, datetime

import pytest

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.session import MonitorSession
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor


def _session() -> MonitorSession:
    return MonitorSession(
        session_id="mon-20260511T120000Z-deadbeef",
        started_at=datetime.now(UTC),
        mode="paper",
    )


def _config(*, shutdown_timeout: int = 5, watchdog_timeout: int = 3600) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=shutdown_timeout,
        watchdog_stall_timeout_seconds=watchdog_timeout,
    )


def _supervisor(*, shutdown_timeout: int = 5, watchdog_timeout: int = 3600) -> MonitorSupervisor:
    return MonitorSupervisor(
        session=_session(),
        config=_config(shutdown_timeout=shutdown_timeout, watchdog_timeout=watchdog_timeout),
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
# ALP-768 — in-process watchdog (beat + _watchdog_loop)
# ---------------------------------------------------------------------------


class TestBeat:
    def test_beat_registers_task_on_first_call(self) -> None:
        supervisor = _supervisor()
        assert "fill_stream_consumer" not in supervisor._heartbeats
        supervisor.beat("fill_stream_consumer")
        assert "fill_stream_consumer" in supervisor._heartbeats

    def test_beat_refreshes_timestamp_on_subsequent_calls(self) -> None:
        supervisor = _supervisor()
        supervisor.beat("task")
        first_ts = supervisor._heartbeats["task"]
        time.sleep(0.01)  # ensure monotonic advances
        supervisor.beat("task")
        assert supervisor._heartbeats["task"] > first_ts

    def test_beat_stores_monotonic_timestamp(self) -> None:
        before = time.monotonic()
        supervisor = _supervisor()
        supervisor.beat("task")
        after = time.monotonic()
        ts = supervisor._heartbeats["task"]
        assert before <= ts <= after


class TestWatchdogLoop:
    async def test_watchdog_does_not_fire_for_fresh_heartbeat(self) -> None:
        supervisor = _supervisor()
        supervisor._stop_event = asyncio.Event()
        supervisor.beat("task")  # fresh — elapsed ~ 0

        # Allow one sleep-then-check iteration, then stop via CancelledError.
        _call = 0

        async def _one_shot_sleep(_: float) -> None:
            nonlocal _call
            _call += 1
            if _call > 1:
                raise asyncio.CancelledError()

        exit_calls: list[int] = []
        with (
            mock.patch("asyncio.sleep", new=_one_shot_sleep),
            mock.patch(
                "alphamind.execution.continuous_monitor.supervisor.os._exit",
                side_effect=exit_calls.append,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            # Awaiting directly (not via create_task) so BaseException propagates normally.
            await supervisor._watchdog_loop()

        assert exit_calls == []

    async def test_watchdog_fires_os_exit_for_stale_heartbeat(self) -> None:
        supervisor = _supervisor()
        supervisor._stop_event = asyncio.Event()
        timeout = supervisor._config.watchdog_stall_timeout_seconds
        # Simulate a heartbeat that was last recorded far past the timeout.
        supervisor._heartbeats["stale_task"] = time.monotonic() - (timeout + 1)

        with (
            mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)),
            mock.patch(
                "alphamind.execution.continuous_monitor.supervisor.os._exit",
                side_effect=SystemExit(1),
            ),
            # Await directly so SystemExit propagates to pytest.raises.
            pytest.raises(SystemExit) as exc_info,
        ):
            await supervisor._watchdog_loop()
        assert exc_info.value.code == 1

    async def test_watchdog_exits_cleanly_when_stop_event_is_set(self) -> None:
        """Shutdown guard: no os._exit during graceful stop even if heartbeats are stale."""
        supervisor = _supervisor()
        supervisor._stop_event = asyncio.Event()
        supervisor._stop_event.set()  # shutdown already in progress
        timeout = supervisor._config.watchdog_stall_timeout_seconds
        supervisor._heartbeats["stale_task"] = time.monotonic() - (timeout + 1)

        exit_calls: list[int] = []
        with (
            mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None)),
            mock.patch(
                "alphamind.execution.continuous_monitor.supervisor.os._exit",
                side_effect=exit_calls.append,
            ),
        ):
            # Should return without raising or appending because stop_event is set.
            await supervisor._watchdog_loop()

        assert exit_calls == []


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
