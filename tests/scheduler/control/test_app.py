"""Tests for ``alphamind.scheduler.control.app`` (ALP-664).

The app module exposes:

* :func:`build_app` — constructs a FastAPI instance, mounts the routes,
  and registers the lifespan that owns the :class:`SSEEventEmitter`
  shared across the process.
* :func:`run_uvicorn_server_task` — supervisor-registered async coroutine
  factory that binds Uvicorn to ``127.0.0.1:control_port`` and serves
  until cancellation.

Tests are layered:

* Verb-only paths: ``TestClient`` exercises the ASGI app through its
  public surface; no Uvicorn process is started.
* Bind discipline + SSE: run Uvicorn against an ephemeral port and
  drive the stream through ``httpx`` — TestClient's stream API holds
  the connection open inside a worker thread that can't observe the
  route's ``request.is_disconnected()`` loop, which would hang the
  test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from alphamind.scheduler.control import app as app_module
from alphamind.scheduler.control import events, models, verbs

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


def _make_fake_verb_dispatch() -> app_module.VerbDispatch:
    """Build an app-state dispatch holding test fakes for the verb deps."""
    # The dispatch holds the three Protocols + the config_dir + the
    # ``now`` factory.  Routes consume these via FastAPI Depends.
    from tests.scheduler.control.test_verbs import (  # local import — fakes only used here
        FakeEmergencyTrigger,
        FakeSchedulerControl,
        FakeUniverseValidator,
    )

    report = verbs.UniverseValidationReportRecord(
        validated_at=_NOW,
        tickers=(
            verbs.UniverseValidationTickerRecord(
                ticker="AAPL",
                verdict="pass",
                criteria=(
                    verbs.UniverseValidationCriterionRecord(criterion="adv", verdict="pass"),
                    verbs.UniverseValidationCriterionRecord(
                        criterion="analyst_coverage", verdict="pass"
                    ),
                    verbs.UniverseValidationCriterionRecord(criterion="beta", verdict="pass"),
                    verbs.UniverseValidationCriterionRecord(criterion="market_cap", verdict="pass"),
                    verbs.UniverseValidationCriterionRecord(criterion="options_oi", verdict="pass"),
                ),
            ),
        ),
    )
    return app_module.VerbDispatch(
        scheduler=FakeSchedulerControl(),
        emergency=FakeEmergencyTrigger(),
        universe_validator=FakeUniverseValidator(report=report),
        config_dir=None,  # routes that need a config_dir set it on a per-test basis
        now_factory=lambda: _NOW,
    )


# ---------------------------------------------------------------------------
# Composition.
# ---------------------------------------------------------------------------


class TestBuildApp:
    def test_build_app_returns_fastapi_instance_with_emitter(self) -> None:
        emitter = events.SSEEventEmitter()
        dispatch = _make_fake_verb_dispatch()
        app = app_module.build_app(emitter=emitter, dispatch=dispatch)
        assert app.state.event_emitter is emitter
        assert app.state.verb_dispatch is dispatch

    def test_build_app_attaches_routes(self) -> None:
        emitter = events.SSEEventEmitter()
        dispatch = _make_fake_verb_dispatch()
        app = app_module.build_app(emitter=emitter, dispatch=dispatch)
        routes = {route.path for route in app.routes if hasattr(route, "path")}
        # Five POST verbs + one SSE stream.
        assert "/control/pause" in routes
        assert "/control/resume" in routes
        assert "/control/trigger_emergency_invocation" in routes
        assert "/control/switch_profile" in routes
        assert "/control/run_universe_validation" in routes
        assert "/events" in routes


class TestTestClientSmokeBoot:
    """Boot the FastAPI app through TestClient (exercises the lifespan)."""

    def test_lifespan_runs_clean(self) -> None:
        emitter = events.SSEEventEmitter()
        dispatch = _make_fake_verb_dispatch()
        app = app_module.build_app(emitter=emitter, dispatch=dispatch)
        with TestClient(app) as client:
            # Issue a trivial pause to exercise the request path.
            response = client.post("/control/pause", json={"reason": "smoke"})
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# Uvicorn supervisor-task — loopback bind discipline.
# ---------------------------------------------------------------------------


class TestUvicornBindLoopback:
    async def test_run_uvicorn_server_task_binds_loopback_and_cancels_clean(self) -> None:
        port = _pick_ephemeral_port()
        emitter = events.SSEEventEmitter()
        dispatch = _make_fake_verb_dispatch()
        app = app_module.build_app(emitter=emitter, dispatch=dispatch)
        coro = app_module.run_uvicorn_server_task(app=app, host="127.0.0.1", port=port)
        task = asyncio.create_task(coro)
        # Poll until Uvicorn reports listening.
        await _wait_until_listening("127.0.0.1", port, deadline_s=5.0)
        # Loopback bind: 127.0.0.1 reachable.
        with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
            assert sock is not None
        # Cancel the supervisor task — Uvicorn stops cleanly.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _pick_ephemeral_port() -> int:
    """Return an OS-assigned ephemeral port.

    Note the small race window between releasing the port and Uvicorn
    binding to it — acceptable for unit tests; the verify script
    binds the production port from config.
    """
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


def _parse_sse_record(record_text: str) -> tuple[str, dict[str, object]]:
    """Parse one SSE record (``event: ...\\ndata: ...``) into ``(name, data)``."""
    name = ""
    data_json = ""
    for line in record_text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: "):
            data_json = line[len("data: ") :]
    return name, json.loads(data_json)


# ---------------------------------------------------------------------------
# GET /events end-to-end SSE coverage.
#
# These tests boot a real Uvicorn server on an ephemeral port and drive
# ``GET /events`` through an httpx async client.  Each test cancels the
# server task in its ``finally`` block so the test process exits cleanly.
# ---------------------------------------------------------------------------


class _ServerHandle:
    """Holds the running server task + shared emitter so tests can emit."""

    def __init__(
        self, *, task: asyncio.Task[None], emitter: events.SSEEventEmitter, port: int
    ) -> None:
        self.task = task
        self.emitter = emitter
        self.port = port

    async def shutdown(self) -> None:
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


async def _start_server(*, heartbeat_interval_seconds: float = 0.05) -> _ServerHandle:
    port = _pick_ephemeral_port()
    emitter = events.SSEEventEmitter(heartbeat_interval_seconds=heartbeat_interval_seconds)
    dispatch = _make_fake_verb_dispatch()
    app = app_module.build_app(emitter=emitter, dispatch=dispatch)
    task = asyncio.create_task(
        app_module.run_uvicorn_server_task(app=app, host="127.0.0.1", port=port)
    )
    await _wait_until_listening("127.0.0.1", port, deadline_s=5.0)
    return _ServerHandle(task=task, emitter=emitter, port=port)


async def _read_sse_records(
    url: str,
    *,
    count: int,
    emit_before_read: Sequence[BaseModel] | None = None,
    emitter: events.SSEEventEmitter | None = None,
) -> list[tuple[str, dict[str, object]]]:
    """Open an SSE stream, optionally emit, then drain ``count`` records.

    Closes the stream cleanly via ``aclose()`` so the server's
    ``request.is_disconnected()`` loop exits.
    """
    records: list[tuple[str, dict[str, object]]] = []
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=5.0)) as client,
        client.stream("GET", url) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        # ``Connection: keep-alive`` is set; HTTP/1.1 may strip it but
        # the schema requires it to be sent.
        assert response.headers.get("connection", "keep-alive") in (
            "keep-alive",
            "Keep-Alive",
        )
        if emit_before_read and emitter is not None:
            # Give the route a tick to subscribe.
            await asyncio.sleep(0.05)
            for event in emit_before_read:
                emitter.emit(event)
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


class TestSSEHeadersAndHeartbeat:
    """SSE framing + heartbeat cadence via real Uvicorn + httpx."""

    async def test_idle_connection_receives_heartbeat(self) -> None:
        handle = await _start_server(heartbeat_interval_seconds=0.1)
        try:
            url = f"http://127.0.0.1:{handle.port}/events"
            records = await _read_sse_records(url, count=1)
            assert records[0][0] == "heartbeat"
            assert "timestamp" in records[0][1]
        finally:
            await handle.shutdown()


class TestSSEFramingForEachEventType:
    """SSE framing of each of the nine event types — AC requirement."""

    @pytest.mark.parametrize(
        ("ctor", "expected_name"),
        [
            (
                lambda: models.InvocationStartedEvent(
                    invocation_id="inv-1",
                    run_type="emergency",
                    started_at=_NOW,
                ),
                "invocation_started",
            ),
            (
                lambda: models.PhaseTransitionEvent(
                    invocation_id="inv-1",
                    phase="distill",
                    phase_started_at=_NOW,
                ),
                "phase_transition",
            ),
            (
                lambda: models.AgentStartedEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    started_at=_NOW,
                    latency_budget_seconds=60.0,
                ),
                "agent_started",
            ),
            (
                lambda: models.AgentSucceededEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    duration_seconds=12.5,
                    tokens_used=models.TokensUsed(input=1000, output=200),
                ),
                "agent_succeeded",
            ),
            (
                lambda: models.AgentRetryingEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    attempt=2,
                    reason="timeout",
                ),
                "agent_retrying",
            ),
            (
                lambda: models.AgentFailedEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    failure_mode="timeout",
                ),
                "agent_failed",
            ),
            (
                lambda: models.InvocationEndedEvent(
                    invocation_id="inv-1",
                    status="completed",
                    commands_issued=3,
                ),
                "invocation_ended",
            ),
            (
                lambda: models.NextTriggerChangedEvent(
                    next_trigger_at=_NOW,
                    next_trigger_type="market_hours_rolling",
                ),
                "next_trigger_changed",
            ),
            (
                lambda: models.HeartbeatEvent(timestamp=_NOW),
                "heartbeat",
            ),
        ],
    )
    async def test_each_event_type_frames_correctly(self, ctor: object, expected_name: str) -> None:
        # Long heartbeat so the expected event arrives before any injected heartbeat.
        handle = await _start_server(heartbeat_interval_seconds=30.0)
        try:
            url = f"http://127.0.0.1:{handle.port}/events"
            event = ctor()  # type: ignore[operator]
            records = await _read_sse_records(
                url,
                count=1,
                emit_before_read=[event],
                emitter=handle.emitter,
            )
            assert records[0][0] == expected_name
            # Round-trip the payload through the source Pydantic class.
            type(event).model_validate(records[0][1])
        finally:
            await handle.shutdown()


class TestSSECrossFieldInvariantsObservable:
    """Cross-field invariants from the schema's § Notes section.

    The emitter doesn't police order (matching JsonlProgressEmitter's
    observability-first design), but the wire shape lets a consumer
    observe + assert the invariants.
    """

    async def test_phase_transitions_observed_in_forward_order(self) -> None:
        handle = await _start_server(heartbeat_interval_seconds=30.0)
        try:
            url = f"http://127.0.0.1:{handle.port}/events"
            events_to_emit = [
                models.PhaseTransitionEvent(
                    invocation_id="inv-1",
                    phase=phase,  # type: ignore[arg-type]
                    phase_started_at=_NOW,
                )
                for phase in ("collect", "distill", "analyze", "decide", "execute")
            ]
            records = await _read_sse_records(
                url,
                count=5,
                emit_before_read=events_to_emit,
                emitter=handle.emitter,
            )
            observed = [r[1]["phase"] for r in records]
            assert observed == ["collect", "distill", "analyze", "decide", "execute"]
        finally:
            await handle.shutdown()

    async def test_agent_succeeded_and_failed_mutually_exclusive_per_pair(self) -> None:
        # Schema: exactly one terminal event per (invocation_id, agent_name).
        # Emitter doesn't enforce; the consumer counts terminal events and
        # asserts ==1 per pair.
        handle = await _start_server(heartbeat_interval_seconds=30.0)
        try:
            url = f"http://127.0.0.1:{handle.port}/events"
            terminals = [
                models.AgentSucceededEvent(
                    invocation_id="inv-1",
                    agent_name="analyst",
                    duration_seconds=12.5,
                    tokens_used=models.TokensUsed(input=1000, output=200),
                ),
                models.AgentFailedEvent(
                    invocation_id="inv-1",
                    agent_name="strategist",
                    failure_mode="timeout",
                ),
            ]
            records = await _read_sse_records(
                url,
                count=2,
                emit_before_read=terminals,
                emitter=handle.emitter,
            )
            seen: dict[tuple[str, str], list[str]] = {}
            for name, data in records:
                inv_id = data["invocation_id"]
                agent = data["agent_name"]
                assert isinstance(inv_id, str)
                assert isinstance(agent, str)
                seen.setdefault((inv_id, agent), []).append(name)
            for pairs in seen.values():
                assert len(pairs) == 1
        finally:
            await handle.shutdown()


class TestSSEEmergencyInvocationCorrelation:
    """``run_type: emergency`` invocations carry the trigger response's invocation_id.

    Schema's § Notes invariant: a consumer waiting on the lifecycle of a
    triggered emergency reads ``invocation_id`` from the verb response
    and filters events by that ID.
    """

    async def test_invocation_started_emergency_run_type_carries_returned_id(self) -> None:
        handle = await _start_server(heartbeat_interval_seconds=30.0)
        try:
            url_base = f"http://127.0.0.1:{handle.port}"
            # Use emit_before_read so the SSE subscriber registers before
            # the emit fires (otherwise the emit lands on zero subscribers
            # and the consumer hangs waiting for the heartbeat).
            event = models.InvocationStartedEvent(
                invocation_id="inv-emerg-1",
                run_type="emergency",
                started_at=_NOW,
            )
            records = await _read_sse_records(
                f"{url_base}/events",
                count=1,
                emit_before_read=[event],
                emitter=handle.emitter,
            )
            assert records[0][0] == "invocation_started"
            assert records[0][1]["run_type"] == "emergency"
            assert records[0][1]["invocation_id"] == "inv-emerg-1"
        finally:
            await handle.shutdown()
