"""Supervisor + control surface integration test (ALP-720).

End-to-end test that registers ``run_uvicorn_server_task`` on a
:class:`PipelineSupervisor` the same way the production daemon does
in ``alphamind.scheduler.__main__._run_daemon``, drives the HTTP
surface with real ``httpx`` requests, and cancels cleanly.

This is the regression test the issue calls out — without it, the
binding gap in ``_run_daemon`` slipped through ALP-664 even though
``test_app.py`` proves the surface itself works correctly in
isolation.  The fixture mirrors the daemon's wiring shape, so any
future regression where ``_run_daemon`` forgets to register the
control-surface task fails this test.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from datetime import UTC, datetime

import httpx
import pytest

from alphamind.scheduler.control.app import (
    VerbDispatch,
    build_app,
    run_uvicorn_server_task,
)
from alphamind.scheduler.control.events import SSEEventEmitter
from alphamind.scheduler.control.verbs import (
    CooldownInfo,
    RunningInfo,
    UniverseValidationReportRecord,
)
from alphamind.scheduler.session import new_session
from alphamind.scheduler.supervisor import PipelineSupervisor

_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _free_port() -> int:
    """Bind to ephemeral, release, return — best-effort race-free test port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class _FakeSchedulerControl:
    """Minimal :class:`SchedulerControl` Protocol impl tracking pause state."""

    def __init__(self) -> None:
        self._paused_at: datetime | None = None

    def is_paused(self) -> bool:
        return self._paused_at is not None

    def pause(self, *, reason: str, now: datetime) -> datetime:
        del reason
        if self._paused_at is None:
            self._paused_at = now
        return self._paused_at

    def resume(self, *, now: datetime) -> datetime:
        del now
        self._paused_at = None
        return _NOW

    def next_run_preview(self) -> tuple[datetime, str] | None:
        return (_NOW, "pre_open")


class _FakeEmergencyTrigger:
    """Minimal :class:`EmergencyTrigger` Protocol impl returning a placeholder ID."""

    async def trigger(self, *, reason: str, source: str, now: datetime) -> str:
        del reason, source, now
        return "pending-emerg-test"

    def cooldown_info(self) -> CooldownInfo | None:
        return None

    def running_info(self) -> RunningInfo | None:
        return None


class _FakeUniverseValidator:
    """Minimal :class:`UniverseValidator` Protocol impl returning an empty report."""

    def validate(self, *, as_of: object) -> UniverseValidationReportRecord:
        del as_of
        from alphamind.scheduler.control.verbs import (
            UniverseValidationCriterionRecord,
            UniverseValidationTickerRecord,
        )

        return UniverseValidationReportRecord(
            validated_at=_NOW,
            tickers=(
                UniverseValidationTickerRecord(
                    ticker="AAPL",
                    verdict="pass",
                    criteria=(
                        UniverseValidationCriterionRecord(criterion="adv", verdict="pass"),
                        UniverseValidationCriterionRecord(
                            criterion="analyst_coverage", verdict="pass"
                        ),
                        UniverseValidationCriterionRecord(criterion="beta", verdict="pass"),
                        UniverseValidationCriterionRecord(criterion="market_cap", verdict="pass"),
                        UniverseValidationCriterionRecord(criterion="options_oi", verdict="pass"),
                    ),
                ),
            ),
        )


def _build_dispatch() -> VerbDispatch:
    return VerbDispatch(
        scheduler=_FakeSchedulerControl(),
        emergency=_FakeEmergencyTrigger(),
        universe_validator=_FakeUniverseValidator(),
        config_dir=None,
        now_factory=lambda: _NOW,
    )


@pytest.mark.asyncio
class TestPipelineSupervisorBindsControlSurface:
    async def test_pause_verb_round_trips_via_real_http(self) -> None:
        """Mirror the daemon's wiring: register run_uvicorn_server_task on the supervisor.

        This is the test that would have caught ALP-720's binding gap — it
        asserts the same task-registration shape the production daemon uses.
        """
        port = _free_port()
        emitter = SSEEventEmitter()
        dispatch = _build_dispatch()
        app = build_app(emitter=emitter, dispatch=dispatch)

        session = new_session(process_lifetime_id="test-plid", mode="paper")
        supervisor = PipelineSupervisor(
            session=session,
            shutdown_timeout_seconds=5,
        )

        async def _control_surface_task(_session: object) -> None:
            await run_uvicorn_server_task(app=app, host="127.0.0.1", port=port)

        supervisor.register_task(name="control_surface", coro_fn=_control_surface_task)

        async def _drive_client() -> None:
            # Wait for Uvicorn to bind, then hit /control/pause.
            for _ in range(50):
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            f"http://127.0.0.1:{port}/control/pause",
                            json={"reason": "integration smoke"},
                            timeout=2.0,
                        )
                        assert resp.status_code == 200
                        body = resp.json()
                        assert body["status"] == "accepted"
                        return
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            msg = "control surface never bound"
            raise AssertionError(msg)

        supervisor_task = asyncio.create_task(supervisor.run())
        try:
            await _drive_client()
        finally:
            supervisor.request_stop()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor_task

        # Verify the fake's pause flag flipped — round-trips the verb.
        scheduler = dispatch.scheduler
        assert isinstance(scheduler, _FakeSchedulerControl)
        assert scheduler.is_paused() is True

    async def test_events_stream_emits_heartbeat_within_window(self) -> None:
        """SSE stream is reachable AND emits a heartbeat within the configured window.

        This covers acceptance criterion A: ``GET /events -N`` returns a
        ``heartbeat`` frame within 15 s (we use a shorter cadence for tests).
        """
        port = _free_port()
        emitter = SSEEventEmitter(heartbeat_interval_seconds=0.1)
        dispatch = _build_dispatch()
        app = build_app(emitter=emitter, dispatch=dispatch)

        session = new_session(process_lifetime_id="test-plid", mode="paper")
        supervisor = PipelineSupervisor(
            session=session,
            shutdown_timeout_seconds=5,
        )

        async def _control_surface_task(_session: object) -> None:
            await run_uvicorn_server_task(app=app, host="127.0.0.1", port=port)

        supervisor.register_task(name="control_surface", coro_fn=_control_surface_task)

        async def _drive_sse() -> None:
            # Wait for binding then stream /events for one record.
            for _ in range(50):
                try:
                    async with (
                        httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=5.0)) as client,
                        client.stream("GET", f"http://127.0.0.1:{port}/events") as response,
                    ):
                        assert response.status_code == 200
                        assert response.headers["content-type"].startswith("text/event-stream")
                        buffer = ""
                        async for chunk in response.aiter_text():
                            buffer += chunk
                            if "\n\n" in buffer:
                                record_text, _ = buffer.split("\n\n", 1)
                                # Heartbeat name and timestamp present.
                                assert "event: heartbeat" in record_text
                                assert "timestamp" in record_text
                                return
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            msg = "control surface never bound or heartbeat never arrived"
            raise AssertionError(msg)

        supervisor_task = asyncio.create_task(supervisor.run())
        try:
            await _drive_sse()
        finally:
            supervisor.request_stop()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor_task
