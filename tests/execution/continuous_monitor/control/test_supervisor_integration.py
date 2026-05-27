"""Supervisor + control surface integration test (ALP-665).

End-to-end test: register the control-surface task on a
:class:`MonitorSupervisor`, run it, hit the HTTP surface with a real httpx
request, then cancel cleanly. Confirms:

* The Uvicorn server binds inside the supervisor's ``asyncio.TaskGroup``
  (no bare ``create_task``).
* ``POST /control/set_halt_mode`` reaches the in-process verb via real HTTP.
* Shutdown cancels the bound port cleanly.
"""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig
from alphamind.execution.continuous_monitor.control.app import (
    ControlSurfaceDependencies,
    make_control_surface_task,
)
from alphamind.execution.continuous_monitor.control.events import SSEEventEmitter
from alphamind.execution.continuous_monitor.control.halt_mode_repo import (
    HaltModeRecord,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    CancelOrderOutcome,
    ForceCloseOutcome,
    OrderState,
    PositionState,
)
from alphamind.execution.continuous_monitor.session import (
    MonitorSession,
    new_session,
)
from alphamind.execution.continuous_monitor.supervisor import MonitorSupervisor


def _free_port() -> int:
    """Bind to ephemeral, release, return — best-effort race-free test port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class _FakeOrderLookup:
    def __init__(self, rows: dict[str, OrderState]) -> None:
        self.rows = rows

    async def fetch(self, order_id: str) -> OrderState | None:
        return self.rows.get(order_id)


class _FakePositionLookup:
    async def fetch(self, position_id: str) -> PositionState | None:
        del position_id
        return None


class _FakeCancelEmitter:
    def __init__(self) -> None:
        self.submitted: list[str] = []

    async def submit_cancel(self, *, order_id: str) -> CancelOrderOutcome:
        self.submitted.append(order_id)
        return CancelOrderOutcome()


class _FakeCloseSubmitter:
    async def submit_close(
        self,
        *,
        position_id: str,
        position_selection_rationale: str,
        rule_breached: str,
        breach_details_current: float,
        breach_details_limit: float,
    ) -> ForceCloseOutcome:
        del (
            position_id,
            position_selection_rationale,
            rule_breached,
            breach_details_current,
            breach_details_limit,
        )
        return ForceCloseOutcome(envelope_id="MON.test.1")


class _FakeHaltModeRepo:
    def __init__(self) -> None:
        self.record = HaltModeRecord(enabled=False, reason=None, applied_at=None)

    async def read(self) -> HaltModeRecord:
        return self.record

    async def write(self, record: HaltModeRecord) -> None:
        self.record = record


def _build_deps() -> ControlSurfaceDependencies:
    return ControlSurfaceDependencies(
        order_lookup=_FakeOrderLookup(rows={"ord-1": OrderState(order_id="ord-1", status="open")}),
        position_lookup=_FakePositionLookup(),
        cancel_emitter=_FakeCancelEmitter(),
        close_submitter=_FakeCloseSubmitter(),
        halt_mode_repo=_FakeHaltModeRepo(),
        event_emitter=SSEEventEmitter(),
    )


def _build_session() -> MonitorSession:
    return new_session(mode="paper")


def _build_config() -> ContinuousMonitorConfig:
    return ContinuousMonitorConfig(
        breach_evaluation_cadence_seconds=60,
        greeks_refresh_interval_minutes=15,
        greeks_refresh_underlying_move_threshold_pct=2.0,
        underlying_stream_provider="alpaca-iex",
        max_reconnect_attempts=5,
        supervisor_shutdown_timeout_seconds=5,
        control_port=8766,
    )


@pytest.mark.asyncio
class TestSupervisorIntegration:
    async def test_set_halt_mode_via_real_http(self) -> None:
        port = _free_port()
        deps = _build_deps()
        supervisor = MonitorSupervisor(session=_build_session(), config=_build_config())
        supervisor.register_task(
            name="control_surface",
            coro_fn=make_control_surface_task(deps=deps, port=port),
        )

        async def _client_request() -> None:
            # Wait for the server to bind (best-effort; small poll loop).
            for _ in range(50):
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            f"http://127.0.0.1:{port}/control/set_halt_mode",
                            json={"enabled": True, "reason": "integration smoke"},
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

        async with asyncio.TaskGroup() as tg:
            supervisor_task = tg.create_task(supervisor.run(), name="supervisor")
            try:
                await _client_request()
            finally:
                supervisor.request_stop()
                # Wait for the supervisor to drain.
                await supervisor_task

        # Verify the verb actually mutated the repo.
        repo = deps.halt_mode_repo
        assert isinstance(repo, _FakeHaltModeRepo)
        assert repo.record.enabled is True

    async def test_loopback_only_bind(self) -> None:
        """The Uvicorn config pins host to ``127.0.0.1``."""
        from alphamind.execution.continuous_monitor.control.app import (
            build_app,
            build_uvicorn_config,
        )

        deps = _build_deps()
        app = build_app(deps)
        config = build_uvicorn_config(app=app, port=8766)
        assert config.host == "127.0.0.1"
        assert config.host != "0.0.0.0"
        assert config.access_log is False
