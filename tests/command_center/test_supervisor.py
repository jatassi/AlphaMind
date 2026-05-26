"""Tests for ``command_center.supervisor`` (story 02 / ALP-666).

Mirrors :mod:`tests.scheduler.test_supervisor` in shape — covers the
TaskGroup scaffold, the register / task_names API, the request_stop
shutdown path, and the bounded-cancellation-wait property.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from alphamind.command_center.session import ProcessSession
from alphamind.command_center.supervisor import CommandCenterSupervisor


def _session() -> ProcessSession:
    return ProcessSession(
        process_lifetime_id="plt-test",
        started_at=datetime.now(UTC),
    )


class TestRegisterTask:
    def test_records_name_in_order(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=5)

        async def task_a(_: ProcessSession) -> None:
            return None

        async def task_b(_: ProcessSession) -> None:
            return None

        sup.register_task(name="a", coro_fn=task_a)
        sup.register_task(name="b", coro_fn=task_b)
        assert sup.task_names() == ("a", "b")

    def test_rejects_duplicate_names(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=5)

        async def task(_: ProcessSession) -> None:
            return None

        sup.register_task(name="a", coro_fn=task)
        with pytest.raises(ValueError, match="already registered"):
            sup.register_task(name="a", coro_fn=task)


class TestShutdownTimeoutValidation:
    def test_negative_timeout_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=0)


class TestRequestStop:
    async def test_no_tasks_returns_immediately(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=5)
        # Pre-arm the stop event so run() doesn't block.
        run_task = asyncio.create_task(sup.run())
        # Yield once so the supervisor enters its TaskGroup body.
        await asyncio.sleep(0)
        sup.request_stop()
        await asyncio.wait_for(run_task, timeout=2)

    async def test_running_task_gets_cancelled_on_stop(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=5)
        was_cancelled = asyncio.Event()

        async def long_task(_: ProcessSession) -> None:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                was_cancelled.set()
                raise

        sup.register_task(name="long", coro_fn=long_task)
        run_task = asyncio.create_task(sup.run())
        await asyncio.sleep(0.1)  # let the task start
        sup.request_stop()
        await asyncio.wait_for(run_task, timeout=2)
        assert was_cancelled.is_set()

    def test_request_stop_before_run_warns_but_does_not_raise(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=5)
        # Should log a warning, not raise.
        sup.request_stop()


class TestCrashReRaises:
    async def test_first_non_cancelled_exception_propagates(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=5)

        class _SentinelError(RuntimeError):
            pass

        async def crashing_task(_: ProcessSession) -> None:
            await asyncio.sleep(0.05)
            raise _SentinelError("crash")

        sup.register_task(name="crash", coro_fn=crashing_task)
        with pytest.raises(_SentinelError, match="crash"):
            await sup.run()


class TestSwallowedCancellationBoundedByTimeout:
    """F4: a task swallowing CancelledError cannot stall the supervisor.

    Uses a finite swallow count rather than an infinite loop so the
    pytest event loop can collect the task cleanly after the
    supervisor abandons it. The supervisor's discipline does not change
    — it still issues two cancels and gives up after the bounded wait;
    we just don't want to leak a task into pytest's teardown.
    """

    async def test_swallowed_cancel_does_not_stall_run(self) -> None:
        sup = CommandCenterSupervisor(session=_session(), shutdown_timeout_seconds=1)
        swallows = 0
        # Tracked so the test can confirm the supervisor actually issued
        # a cancellation that the task swallowed at least once.

        async def swallows_then_exits(_: ProcessSession) -> None:
            nonlocal swallows
            for _attempt in range(3):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    swallows += 1
                    # Eventually honor — keeps pytest's event-loop
                    # teardown clean while still proving the supervisor
                    # returns before the task does.
                    continue
            # Final yield to give the supervisor's wait a chance to
            # observe completion.
            await asyncio.sleep(0)

        sup.register_task(name="swallow", coro_fn=swallows_then_exits)
        run_task = asyncio.create_task(sup.run())
        # Let the task start.
        await asyncio.sleep(0.1)
        sup.request_stop()
        # The supervisor must return within the timeout window even if
        # the task swallows the first cancellation — the supervisor's
        # bounded-wait gives it 1s, then issues a second cancel + 0.5s
        # re-wait, then abandons. Cap the outer wait generously.
        await asyncio.wait_for(run_task, timeout=5)
        assert swallows >= 1, "supervisor never cancelled the task"
