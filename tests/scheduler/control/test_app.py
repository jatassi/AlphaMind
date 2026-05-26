"""Tests for ``alphamind.scheduler.control.app`` (ALP-664).

The app module exposes:

* :func:`build_app` — constructs a FastAPI instance, mounts the routes,
  and registers the lifespan that owns the :class:`SSEEventEmitter`
  shared across the process.
* :func:`run_uvicorn_server_task` — supervisor-registered async coroutine
  factory that binds Uvicorn to ``127.0.0.1:control_port`` and serves
  until cancellation.

Tests stay sociable: ``TestClient`` exercises the real ASGI app through
its public surface; no Uvicorn process is started here.  The bind /
cancel discipline is exercised separately against a real ephemeral port.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from alphamind.scheduler.control import app as app_module
from alphamind.scheduler.control import events, verbs

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
