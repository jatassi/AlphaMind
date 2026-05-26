"""Tests for ``command_center.control.monitor_client`` (story 04a / ALP-668)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from alphamind.command_center._kernel.control import ControlErrorCode, ControlResult
from alphamind.command_center.control.monitor_client import (
    FakeMonitorClient,
    MonitorForceClosePositionResult,
    RealMonitorClient,
)


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> RealMonitorClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return RealMonitorClient(base_url="http://127.0.0.1:8766", http_client=http)


class TestRealMonitorClientHappyPaths:
    async def test_cancel_order_success(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/control/cancel_order"
            return httpx.Response(
                200,
                json={"status": "accepted", "applied_at": "2026-05-26T12:00:00Z"},
            )

        client = _make_client(handler)
        result = await client.cancel_order(order_id="ord-1")
        assert result.ok is True
        assert result.applied_at == "2026-05-26T12:00:00Z"

    async def test_force_close_success(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/control/force_close_position"
            return httpx.Response(
                200,
                json={
                    "status": "accepted",
                    "applied_at": "2026-05-26T12:00:00Z",
                    "envelope_id": "MON.sess-1.42",
                },
            )

        client = _make_client(handler)
        out = await client.force_close_position(position_id="pos-1", rationale="exit")
        assert isinstance(out, MonitorForceClosePositionResult)
        assert out.result.ok is True
        assert out.envelope_id == "MON.sess-1.42"

    async def test_set_halt_mode_success(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "accepted", "applied_at": "2026-05-26T12:00:00Z"},
            )

        client = _make_client(handler)
        result = await client.set_halt_mode(enabled=True, reason="dd")
        assert result.ok is True


class TestRealMonitorClientErrorPaths:
    async def test_cancel_order_404(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"error": {"code": "not_found", "detail": "unknown order"}},
            )

        client = _make_client(handler)
        result = await client.cancel_order(order_id="missing")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.NOT_FOUND

    async def test_force_close_502_broker_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                502,
                json={
                    "error": {
                        "code": "broker_error",
                        "detail": "broker rejected close",
                        "details": {"broker_message": "rejected"},
                    }
                },
            )

        client = _make_client(handler)
        out = await client.force_close_position(position_id="pos-1", rationale="x")
        assert out.result.ok is False
        assert out.result.error_code == ControlErrorCode.BROKER_ERROR
        assert out.envelope_id is None

    async def test_set_halt_mode_409_precondition_failed(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "precondition_failed",
                        "detail": "already halted",
                    }
                },
            )

        client = _make_client(handler)
        result = await client.set_halt_mode(enabled=True, reason="x")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.PRECONDITION_FAILED

    async def test_transport_error_maps_to_internal_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _make_client(handler)
        result = await client.cancel_order(order_id="ord-1")
        assert result.ok is False
        assert result.error_code == ControlErrorCode.INTERNAL_ERROR

    async def test_force_close_missing_envelope_id_is_internal_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"status": "accepted", "applied_at": "2026-05-26T12:00:00Z"},
            )

        client = _make_client(handler)
        out = await client.force_close_position(position_id="pos-1", rationale="x")
        assert out.result.ok is False
        assert out.result.error_code == ControlErrorCode.INTERNAL_ERROR


class TestFakeMonitorClient:
    async def test_default_cancel_order_success(self) -> None:
        client = FakeMonitorClient()
        result = await client.cancel_order(order_id="ord-1")
        assert result.ok is True
        assert client.calls == [("cancel_order", {"order_id": "ord-1"})]

    async def test_default_force_close_success(self) -> None:
        client = FakeMonitorClient()
        out = await client.force_close_position(position_id="pos-1", rationale="x")
        assert out.result.ok is True
        assert out.envelope_id == "MON.s1.42"
        assert client.calls == [
            (
                "force_close_position",
                {"position_id": "pos-1", "rationale": "x"},
            )
        ]

    async def test_set_force_close_failure(self) -> None:
        client = FakeMonitorClient()
        client.set_force_close_response(
            MonitorForceClosePositionResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.PRECONDITION_FAILED,
                    error_detail="already closed",
                )
            )
        )
        out = await client.force_close_position(position_id="pos-1", rationale="x")
        assert out.result.ok is False
        assert out.envelope_id is None

    async def test_set_halt_mode_response_override(self) -> None:
        client = FakeMonitorClient()
        client.set_set_halt_mode_response(
            ControlResult.failure(
                error_code=ControlErrorCode.VALIDATION_FAILED,
                error_detail="reason empty",
            )
        )
        result = await client.set_halt_mode(enabled=True, reason="x")
        assert result.ok is False


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
