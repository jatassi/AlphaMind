"""Tests for the events-stream client Protocols + impls (ALP-669, story 04b).

The Protocols define the SSE-streaming surface the upstream-consumer
task talks to. Production binds to httpx-backed implementations; tests
inject in-memory fakes that yield canned ``(event_name, payload)``
tuples.

Two Protocols, not one — even though the method signatures are
identical, the nominal-type distinction lets mypy catch "plumbed the
monitor client into the pipeline consumer" bugs at type-check time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from alphamind.command_center.events.clients import (
    FakeMonitorEventsClient,
    FakePipelineEventsClient,
    HttpxMonitorEventsClient,
    HttpxPipelineEventsClient,
    MonitorEventsClient,
    PipelineEventsClient,
)


def _collect_frames(client: PipelineEventsClient | MonitorEventsClient) -> Any:
    """Helper: consume an entire stream() and return the list of frames."""

    async def _run() -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        async with client.stream() as frames:
            async for name, data in frames:
                out.append((name, dict(data)))
        return out

    return _run


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class TestFakePipelineEventsClient:
    async def test_yields_canned_frames_in_order(self) -> None:
        client = FakePipelineEventsClient(
            frames=[
                ("invocation_started", {"invocation_id": "inv-1", "run_type": "market_hours_rolling"}),
                ("agent_started", {"invocation_id": "inv-1", "agent_name": "analyst"}),
            ]
        )
        out = await _collect_frames(client)()
        assert out == [
            ("invocation_started", {"invocation_id": "inv-1", "run_type": "market_hours_rolling"}),
            ("agent_started", {"invocation_id": "inv-1", "agent_name": "analyst"}),
        ]

    async def test_stream_exhausts_then_returns_cleanly(self) -> None:
        client = FakePipelineEventsClient(frames=[])
        out = await _collect_frames(client)()
        assert out == []

    async def test_protocol_compliance(self) -> None:
        # Static-type discipline: the fake satisfies the Protocol shape.
        c: PipelineEventsClient = FakePipelineEventsClient(frames=[])
        assert c is not None


class TestFakeMonitorEventsClient:
    async def test_yields_canned_frames_in_order(self) -> None:
        client = FakeMonitorEventsClient(
            frames=[
                ("websocket_connected", {"timestamp": "2026-05-26T00:00:00+00:00"}),
                ("fill_received", {"order_id": "ord-1", "position_id": "pos-1", "fill_price": 1.0, "fill_qty": 1}),
            ]
        )
        out = await _collect_frames(client)()
        assert len(out) == 2

    async def test_protocol_compliance(self) -> None:
        c: MonitorEventsClient = FakeMonitorEventsClient(frames=[])
        assert c is not None


# ---------------------------------------------------------------------------
# httpx-backed impls — parsing
# ---------------------------------------------------------------------------


SSE_BODY = (
    "event: invocation_started\n"
    'data: {"invocation_id":"inv-1","run_type":"market_hours_rolling"}\n'
    "\n"
    "event: heartbeat\n"
    'data: {"timestamp":"2026-05-26T00:00:00+00:00"}\n'
    "\n"
)


def _mock_transport_streaming(body: str) -> httpx.MockTransport:
    """Return a MockTransport whose response streams *body* chunked by line."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode("utf-8"),
        )

    return httpx.MockTransport(handler)


class TestHttpxPipelineEventsClient:
    async def test_parses_sse_frames_from_upstream(self) -> None:
        client = HttpxPipelineEventsClient(
            base_url="http://upstream",
            transport=_mock_transport_streaming(SSE_BODY),
        )
        out: list[tuple[str, dict[str, Any]]] = []
        async with client.stream() as frames:
            async for name, data in frames:
                out.append((name, dict(data)))
        assert out == [
            ("invocation_started", {"invocation_id": "inv-1", "run_type": "market_hours_rolling"}),
            ("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"}),
        ]

    async def test_targets_events_endpoint_under_base_url(self) -> None:
        seen: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, content=b"")

        client = HttpxPipelineEventsClient(
            base_url="http://upstream",
            transport=httpx.MockTransport(handler),
        )
        async with client.stream():
            pass
        assert seen == ["http://upstream/events"]


class TestHttpxMonitorEventsClient:
    async def test_parses_sse_frames_from_upstream(self) -> None:
        body = (
            "event: fill_received\n"
            'data: {"order_id":"ord-1","position_id":"pos-1","fill_price":1.0,"fill_qty":1}\n'
            "\n"
        )
        client = HttpxMonitorEventsClient(
            base_url="http://upstream",
            transport=_mock_transport_streaming(body),
        )
        out: list[tuple[str, dict[str, Any]]] = []
        async with client.stream() as frames:
            async for name, data in frames:
                out.append((name, dict(data)))
        assert out == [
            (
                "fill_received",
                {"order_id": "ord-1", "position_id": "pos-1", "fill_price": 1.0, "fill_qty": 1},
            )
        ]

    async def test_skips_malformed_json_in_data_line(self) -> None:
        body = (
            "event: fill_received\n"
            "data: {not-json-here\n"
            "\n"
            "event: heartbeat\n"
            'data: {"timestamp":"2026-05-26T00:00:00+00:00"}\n'
            "\n"
        )
        client = HttpxMonitorEventsClient(
            base_url="http://upstream",
            transport=_mock_transport_streaming(body),
        )
        out: list[tuple[str, dict[str, Any]]] = []
        async with client.stream() as frames:
            async for name, data in frames:
                out.append((name, dict(data)))
        # Malformed frame dropped; good frame survives.
        assert out == [("heartbeat", {"timestamp": "2026-05-26T00:00:00+00:00"})]


class TestHttpxClientErrorPropagates:
    """A non-2xx response surfaces an exception, not a silent no-yield.

    The consumer task uses the exception to trigger its reconnect-with-
    backoff path — silent termination would leave the upstream-down
    state invisible.
    """

    async def test_http_5xx_raises(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, content=b"upstream down")

        client = HttpxPipelineEventsClient(
            base_url="http://upstream",
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(httpx.HTTPStatusError):
            async with client.stream() as frames:
                async for _ in frames:
                    pytest.fail("yielded a frame for a 503 response")
