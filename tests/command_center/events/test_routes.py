"""Tests for the GET /api/events SSE route (ALP-669, story 04b).

The route handler:

* Gates behind :func:`current_session` — no valid cookie → 401.
* Returns ``StreamingResponse(media_type="text/event-stream")``.
* Per connection: subscribe to the multiplexer, loop on
  ``queue.get()``, emit SSE frames in the ``<source>:<event_name>``
  format.
* Heartbeat on idle: a ``heartbeat`` SSE frame every
  ``event_heartbeat_interval_seconds`` (default 15s; tests use 0.05s).

The frame format helpers (:func:`format_sse_frame`,
:func:`format_heartbeat_frame`) are pure functions over a
:class:`BrowserEventEnvelope`; tests them in isolation.

End-to-end SSE coverage boots a real Uvicorn server on an ephemeral
port and drives the route via ``httpx.AsyncClient.stream`` — mirrors
the pattern proven in :mod:`tests.scheduler.control.test_app`.
``TestClient.stream`` is unusable for this because it holds the
connection open in a worker thread that can't honor the route's
``request.is_disconnected()`` loop, so the test hangs on close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.events.models import BrowserEventEnvelope
from alphamind.command_center.events.multiplexer import EventMultiplexer
from alphamind.command_center.events.routes import (
    HEARTBEAT_SSE_EVENT,
    format_heartbeat_frame,
    format_sse_frame,
)

# ---------------------------------------------------------------------------
# Pure framing helpers
# ---------------------------------------------------------------------------


class TestFormatSseFrame:
    def test_emits_dual_source_event_name(self) -> None:
        envelope = BrowserEventEnvelope(
            source="pipeline",
            event="invocation_started",
            data={"invocation_id": "inv-1"},
        )
        frame = format_sse_frame(envelope)
        assert frame.startswith("event: pipeline:invocation_started\n")
        assert frame.endswith("\n\n")

    def test_data_payload_is_key_sorted_json(self) -> None:
        envelope = BrowserEventEnvelope(
            source="monitor",
            event="fill_received",
            data={"order_id": "ord-1", "fill_qty": 100, "fill_price": 1.5},
        )
        frame = format_sse_frame(envelope)
        data_lines = [
            line[len("data: ") :] for line in frame.split("\n") if line.startswith("data: ")
        ]
        assert len(data_lines) == 1
        payload = json.loads(data_lines[0])
        assert payload == {
            "source": "monitor",
            "event": "fill_received",
            "data": {"order_id": "ord-1", "fill_qty": 100, "fill_price": 1.5},
        }
        assert data_lines[0] == json.dumps(payload, sort_keys=True, separators=(",", ":"))


class TestFormatHeartbeatFrame:
    def test_emits_heartbeat_event_name(self) -> None:
        frame = format_heartbeat_frame()
        assert frame.startswith(f"event: {HEARTBEAT_SSE_EVENT}\n")
        assert frame.endswith("\n\n")

    def test_heartbeat_carries_timestamp_payload(self) -> None:
        frame = format_heartbeat_frame()
        data_lines = [
            line[len("data: ") :] for line in frame.split("\n") if line.startswith("data: ")
        ]
        payload = json.loads(data_lines[0])
        assert "timestamp" in payload


# ---------------------------------------------------------------------------
# Session gating — pre-flight (no stream open, so TestClient works fine)
# ---------------------------------------------------------------------------


class TestSessionGating:
    def test_unauthenticated_request_returns_401(self, events_app: FastAPI) -> None:
        with TestClient(events_app) as client:
            response = client.get("/api/events")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# End-to-end SSE via real Uvicorn server.
# ---------------------------------------------------------------------------


def _pick_ephemeral_port() -> int:
    """Return an OS-assigned ephemeral port (small bind-race acceptable)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return int(port)


async def _wait_until_listening(host: str, port: int, *, deadline_s: float) -> None:
    deadline = asyncio.get_event_loop().time() + deadline_s
    while asyncio.get_event_loop().time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.1):
                return
        except (OSError, ConnectionRefusedError):
            await asyncio.sleep(0.05)
    msg = f"Uvicorn never started listening on {host}:{port}"
    raise TimeoutError(msg)


class _ServerHandle:
    """Running Uvicorn + multiplexer + cookie so tests can publish + subscribe."""

    def __init__(
        self,
        *,
        task: asyncio.Task[None],
        server: uvicorn.Server,
        multiplexer: EventMultiplexer,
        cookie_value: str,
        cookie_name: str,
        port: int,
    ) -> None:
        self.task = task
        self.server = server
        self.multiplexer = multiplexer
        self.cookie_value = cookie_value
        self.cookie_name = cookie_name
        self.port = port

    async def shutdown(self) -> None:
        self.server.should_exit = True
        # Yield so Uvicorn observes the flag, then cancel.
        await asyncio.sleep(0)
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, BaseException):
            await self.task


async def _start_server(
    *,
    app: FastAPI,
    multiplexer: EventMultiplexer,
    cookie_value: str,
    cookie_name: str = "cc_session",
) -> _ServerHandle:
    port = _pick_ephemeral_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config=config)

    async def serve() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await server.serve()

    task = asyncio.create_task(serve())
    await _wait_until_listening("127.0.0.1", port, deadline_s=5.0)
    return _ServerHandle(
        task=task,
        server=server,
        multiplexer=multiplexer,
        cookie_value=cookie_value,
        cookie_name=cookie_name,
        port=port,
    )


async def _read_sse_records(
    url: str,
    *,
    cookies: dict[str, str],
    count: int,
    emit_callback: object | None = None,
) -> list[tuple[str, dict[str, object]]]:
    """Open the SSE stream, drain ``count`` records, return them."""
    records: list[tuple[str, dict[str, object]]] = []
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=5.0), cookies=cookies) as client,
        client.stream("GET", url) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        if emit_callback is not None:
            # Give the route a tick to register its subscriber.
            await asyncio.sleep(0.05)
            await emit_callback  # type: ignore[misc]
        buffer = ""
        async for chunk in response.aiter_text():
            buffer += chunk
            while "\n\n" in buffer:
                record_text, buffer = buffer.split("\n\n", 1)
                if record_text.strip():
                    records.append(_parse_sse_record(record_text))
                if len(records) >= count:
                    return records
    return records


def _parse_sse_record(record_text: str) -> tuple[str, dict[str, object]]:
    name = ""
    data_json = ""
    for line in record_text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data_json = line[len("data: ") :]
    return name, json.loads(data_json)


class TestSSEAuthAndContentType:
    async def test_authenticated_request_returns_event_stream(
        self,
        events_app: FastAPI,
        multiplexer: EventMultiplexer,
        session_cookie_value: str,
    ) -> None:
        handle = await _start_server(
            app=events_app, multiplexer=multiplexer, cookie_value=session_cookie_value
        )
        try:
            url = f"http://127.0.0.1:{handle.port}/api/events"
            # Just open and read one heartbeat to confirm 200 + headers.
            records = await _read_sse_records(
                url, cookies={handle.cookie_name: handle.cookie_value}, count=1
            )
            # No event publish => first record is the heartbeat.
            assert records[0][0] == HEARTBEAT_SSE_EVENT
        finally:
            await handle.shutdown()


class TestSSEHeartbeatCadence:
    async def test_idle_connection_emits_heartbeat(
        self,
        events_app: FastAPI,
        multiplexer: EventMultiplexer,
        session_cookie_value: str,
    ) -> None:
        handle = await _start_server(
            app=events_app, multiplexer=multiplexer, cookie_value=session_cookie_value
        )
        try:
            url = f"http://127.0.0.1:{handle.port}/api/events"
            records = await _read_sse_records(
                url, cookies={handle.cookie_name: handle.cookie_value}, count=1
            )
            assert records[0][0] == HEARTBEAT_SSE_EVENT
            assert "timestamp" in records[0][1]
        finally:
            await handle.shutdown()


class TestSSEEventDelivery:
    async def test_published_pipeline_event_reaches_browser(
        self,
        events_app: FastAPI,
        multiplexer: EventMultiplexer,
        session_cookie_value: str,
    ) -> None:
        handle = await _start_server(
            app=events_app, multiplexer=multiplexer, cookie_value=session_cookie_value
        )
        try:
            event = PipelineEvent(
                event_type=PipelineEventType.INVOCATION_STARTED,
                payload={"invocation_id": "inv-1", "run_type": "market_hours_rolling"},
            )
            url = f"http://127.0.0.1:{handle.port}/api/events"
            # Open stream, then publish after a tick so the subscriber
            # is registered, then read until we see the frame (skipping
            # any heartbeats that arrived first).
            records: list[tuple[str, dict[str, object]]] = []
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(5.0, read=5.0),
                    cookies={handle.cookie_name: handle.cookie_value},
                ) as client,
                client.stream("GET", url) as response,
            ):
                assert response.status_code == 200
                # Wait for subscriber to register, then publish.
                for _ in range(200):
                    if multiplexer.subscriber_count > 0:
                        break
                    await asyncio.sleep(0.005)
                await multiplexer.publish(event)
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        record_text, buffer = buffer.split("\n\n", 1)
                        if record_text.strip():
                            records.append(_parse_sse_record(record_text))
                        if any(r[0] == "pipeline:invocation_started" for r in records):
                            break
                    if any(r[0] == "pipeline:invocation_started" for r in records):
                        break
            match = next(r for r in records if r[0] == "pipeline:invocation_started")
            assert match[1]["source"] == "pipeline"
            assert match[1]["event"] == "invocation_started"
            assert match[1]["data"] == {
                "invocation_id": "inv-1",
                "run_type": "market_hours_rolling",
            }
        finally:
            await handle.shutdown()

    async def test_published_monitor_event_uses_dual_event_name(
        self,
        events_app: FastAPI,
        multiplexer: EventMultiplexer,
        session_cookie_value: str,
    ) -> None:
        handle = await _start_server(
            app=events_app, multiplexer=multiplexer, cookie_value=session_cookie_value
        )
        try:
            event = MonitorEvent(
                event_type=MonitorEventType.FILL_RECEIVED,
                payload={
                    "order_id": "ord-1",
                    "position_id": "pos-1",
                    "fill_price": 1.0,
                    "fill_qty": 1,
                },
            )
            url = f"http://127.0.0.1:{handle.port}/api/events"
            records: list[tuple[str, dict[str, object]]] = []
            async with (
                httpx.AsyncClient(
                    timeout=httpx.Timeout(5.0, read=5.0),
                    cookies={handle.cookie_name: handle.cookie_value},
                ) as client,
                client.stream("GET", url) as response,
            ):
                for _ in range(200):
                    if multiplexer.subscriber_count > 0:
                        break
                    await asyncio.sleep(0.005)
                await multiplexer.publish(event)
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        record_text, buffer = buffer.split("\n\n", 1)
                        if record_text.strip():
                            records.append(_parse_sse_record(record_text))
                        if any(r[0] == "monitor:fill_received" for r in records):
                            break
                    if any(r[0] == "monitor:fill_received" for r in records):
                        break
            match = next(r for r in records if r[0] == "monitor:fill_received")
            assert match[1]["source"] == "monitor"
            data = match[1]["data"]
            assert isinstance(data, dict)
            assert data["order_id"] == "ord-1"
        finally:
            await handle.shutdown()
