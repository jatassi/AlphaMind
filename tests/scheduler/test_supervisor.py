"""Tests for ``PipelineSupervisor`` (story 01).

The supervisor's job is narrow: own an asyncio event loop, hold a task
registry that later stories (04a APScheduler driver, 04b emergency
receiver) plug into, install signal handlers that trigger graceful
shutdown, and cancel + await its registered tasks within a bounded
shutdown window. Exceptions from a registered task propagate out of
``run()`` after sibling tasks have been cancelled — per pre-resolved
decision (H) the surrounding process keeps running and NSSM restarts on
fatal supervisor errors only.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import UTC, datetime

import pytest

from alphamind.scheduler.session import PipelineSession
from alphamind.scheduler.supervisor import PipelineSupervisor


def _session() -> PipelineSession:
    return PipelineSession(
        process_lifetime_id="plt-pipeline-test",
        started_at=datetime.now(UTC),
        mode="paper",
    )


def _supervisor(shutdown_timeout_seconds: int = 5) -> PipelineSupervisor:
    return PipelineSupervisor(
        session=_session(),
        shutdown_timeout_seconds=shutdown_timeout_seconds,
    )


class TestRegisterTask:
    def test_register_task_stores_under_given_name(self) -> None:
        supervisor = _supervisor()

        async def _task(session: PipelineSession) -> None:
            del session

        supervisor.register_task(name="apscheduler", coro_fn=_task)
        assert "apscheduler" in supervisor.task_names()

    def test_register_task_rejects_duplicate_name(self) -> None:
        supervisor = _supervisor()

        async def _task(session: PipelineSession) -> None:
            del session

        supervisor.register_task(name="apscheduler", coro_fn=_task)
        with pytest.raises(ValueError, match="already registered"):
            supervisor.register_task(name="apscheduler", coro_fn=_task)


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

        async def long_running(session: PipelineSession) -> None:
            del session
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

        async def bad_task(session: PipelineSession) -> None:
            del session
            bad_started.set()
            await asyncio.sleep(0)  # ensure sibling is awaiting
            raise RuntimeError("kaboom")

        async def sibling(session: PipelineSession) -> None:
            del session
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

        # Sibling was cancelled cleanly before the exception propagated.
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
        """``run()`` must register SIGINT/SIGTERM handlers that call ``request_stop()``.

        We assert by inspecting the loop's signal-handler registry while the
        supervisor is awaiting its stop event. Firing real signals from a
        pytest worker is flaky; verifying the loop state is sufficient and
        stable across platforms that support ``add_signal_handler``.
        """
        loop = asyncio.get_running_loop()
        seen: dict[str, bool] = {"sigint": False, "sigterm": False}

        supervisor = _supervisor()

        async def probe_and_stop() -> None:
            # Yield once so ``run`` has installed handlers before we look.
            await asyncio.sleep(0)
            handlers = getattr(loop, "_signal_handlers", {})
            seen["sigint"] = signal.SIGINT in handlers
            seen["sigterm"] = signal.SIGTERM in handlers
            supervisor.request_stop()

        await asyncio.gather(supervisor.run(), probe_and_stop())
        assert seen["sigint"] is True, "SIGINT handler was not installed during run"
        assert seen["sigterm"] is True, "SIGTERM handler was not installed during run"


class TestSupervisorShutdownTimeoutValidation:
    def test_supervisor_rejects_zero_shutdown_timeout(self) -> None:
        with pytest.raises(ValueError, match="shutdown_timeout_seconds"):
            PipelineSupervisor(session=_session(), shutdown_timeout_seconds=0)
