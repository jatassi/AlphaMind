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


def _config(*, shutdown_timeout: int = 5) -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=shutdown_timeout,
    )


def _supervisor(*, shutdown_timeout: int = 5) -> MonitorSupervisor:
    return MonitorSupervisor(session=_session(), config=_config(shutdown_timeout=shutdown_timeout))


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
