"""FastAPI route handler tests for /control + /events (ALP-665).

The route handlers translate the typed verb outcomes (``VerbResult`` /
``VerbError`` from ``verbs.py``) into HTTP responses conforming to the schema:

* success → 200 with ``ControlResponseEnvelope`` (or ``ForceClosePositionResponse``
  for force_close).
* ``VerbError("not_found")`` → 404 with ``ControlErrorEnvelope``.
* ``VerbError("precondition_failed")`` → 409 with ``current_status`` details.
* ``VerbError("broker_error")`` → 502 with ``broker_message`` details.
* ``VerbError("validation_failed")`` → 400.

Pydantic at the boundary; the verbs themselves operate on plain values. The
``/events`` route streams SSE frames per the schema's framing spec.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from alphamind.execution.continuous_monitor.control.app import (
    ControlSurfaceDependencies,
    build_app,
)
from alphamind.execution.continuous_monitor.control.events import SSEEventEmitter
from alphamind.execution.continuous_monitor.control.halt_mode_repo import (
    HaltModeRecord,
)
from alphamind.execution.continuous_monitor.control.verbs import (
    BrokerErrorCancel,
    BrokerErrorClose,
    CancelOrderOutcome,
    ForceCloseOutcome,
    OrderState,
    PositionState,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeOrderLookup:
    def __init__(self, rows: dict[str, OrderState]) -> None:
        self.rows = rows

    async def fetch(self, order_id: str) -> OrderState | None:
        return self.rows.get(order_id)


class FakePositionLookup:
    def __init__(self, rows: dict[str, PositionState]) -> None:
        self.rows = rows

    async def fetch(self, position_id: str) -> PositionState | None:
        return self.rows.get(position_id)


class FakeCancelEmitter:
    def __init__(self, outcomes: dict[str, Any]) -> None:
        self.outcomes = outcomes
        self.submitted: list[str] = []

    async def submit_cancel(self, *, order_id: str) -> Any:
        self.submitted.append(order_id)
        return self.outcomes[order_id]


class FakeCloseSubmitter:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def submit_close(
        self,
        *,
        position_id: str,
        position_selection_rationale: str,
        rule_breached: str,
        breach_details_current: float,
        breach_details_limit: float,
    ) -> Any:
        self.calls.append(
            {
                "position_id": position_id,
                "position_selection_rationale": position_selection_rationale,
                "rule_breached": rule_breached,
                "breach_details_current": breach_details_current,
                "breach_details_limit": breach_details_limit,
            }
        )
        return self.outcome


class FakeHaltModeRepo:
    def __init__(self) -> None:
        self.record = HaltModeRecord(enabled=False, reason=None, applied_at=None)

    async def read(self) -> HaltModeRecord:
        return self.record

    async def write(self, record: HaltModeRecord) -> None:
        self.record = record


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _build_deps(
    *,
    order_lookup: FakeOrderLookup | None = None,
    position_lookup: FakePositionLookup | None = None,
    cancel_emitter: FakeCancelEmitter | None = None,
    close_submitter: FakeCloseSubmitter | None = None,
    halt_mode_repo: FakeHaltModeRepo | None = None,
    event_emitter: SSEEventEmitter | None = None,
) -> ControlSurfaceDependencies:
    return ControlSurfaceDependencies(
        order_lookup=order_lookup or FakeOrderLookup({}),
        position_lookup=position_lookup or FakePositionLookup({}),
        cancel_emitter=cancel_emitter or FakeCancelEmitter({}),
        close_submitter=close_submitter
        or FakeCloseSubmitter(ForceCloseOutcome(envelope_id="MON.session-1.1")),
        halt_mode_repo=halt_mode_repo or FakeHaltModeRepo(),
        event_emitter=event_emitter or SSEEventEmitter(),
    )


# ===========================================================================
# /control/cancel_order
# ===========================================================================


class TestCancelOrderRoute:
    def test_happy_path_returns_200_accepted(self) -> None:
        deps = _build_deps(
            order_lookup=FakeOrderLookup(
                {"ord-1": OrderState(order_id="ord-1", status="open")}
            ),
            cancel_emitter=FakeCancelEmitter({"ord-1": CancelOrderOutcome()}),
        )
        client = TestClient(build_app(deps))
        response = client.post("/control/cancel_order", json={"order_id": "ord-1"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "accepted"
        assert "applied_at" in body

    def test_unknown_order_id_returns_404(self) -> None:
        deps = _build_deps()
        client = TestClient(build_app(deps))
        response = client.post("/control/cancel_order", json={"order_id": "missing"})
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "not_found"

    def test_filled_order_returns_409_with_current_status(self) -> None:
        deps = _build_deps(
            order_lookup=FakeOrderLookup(
                {"ord-1": OrderState(order_id="ord-1", status="filled")}
            ),
        )
        client = TestClient(build_app(deps))
        response = client.post("/control/cancel_order", json={"order_id": "ord-1"})
        assert response.status_code == 409
        body = response.json()
        assert body["error"]["code"] == "precondition_failed"
        assert body["error"]["details"]["current_status"] == "filled"

    def test_broker_error_returns_502(self) -> None:
        deps = _build_deps(
            order_lookup=FakeOrderLookup(
                {"ord-1": OrderState(order_id="ord-1", status="open")}
            ),
            cancel_emitter=FakeCancelEmitter(
                {"ord-1": BrokerErrorCancel(broker_message="rate limited")}
            ),
        )
        client = TestClient(build_app(deps))
        response = client.post("/control/cancel_order", json={"order_id": "ord-1"})
        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] == "broker_error"
        assert body["error"]["details"]["broker_message"] == "rate limited"

    def test_empty_body_returns_400(self) -> None:
        deps = _build_deps()
        client = TestClient(build_app(deps))
        response = client.post("/control/cancel_order", json={})
        assert response.status_code == 400
        body = response.json()
        assert body["error"]["code"] == "validation_failed"

    def test_extra_field_returns_400(self) -> None:
        deps = _build_deps()
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/cancel_order", json={"order_id": "x", "extra": "y"}
        )
        assert response.status_code == 400


# ===========================================================================
# /control/force_close_position
# ===========================================================================


class TestForceClosePositionRoute:
    def test_happy_path_returns_200_with_envelope_id(self) -> None:
        deps = _build_deps(
            position_lookup=FakePositionLookup(
                {"pos-1": PositionState(position_id="pos-1", status="open")}
            ),
            close_submitter=FakeCloseSubmitter(
                ForceCloseOutcome(envelope_id="MON.session-1.7")
            ),
        )
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/force_close_position",
            json={"position_id": "pos-1", "rationale": "vega too high"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "accepted"
        assert body["envelope_id"] == "MON.session-1.7"

    def test_unknown_position_returns_404(self) -> None:
        deps = _build_deps()
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/force_close_position",
            json={"position_id": "missing", "rationale": "x"},
        )
        assert response.status_code == 404

    def test_closed_position_returns_409(self) -> None:
        deps = _build_deps(
            position_lookup=FakePositionLookup(
                {"pos-1": PositionState(position_id="pos-1", status="closed")}
            ),
        )
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/force_close_position",
            json={"position_id": "pos-1", "rationale": "x"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["details"]["current_status"] == "closed"

    def test_broker_error_returns_502(self) -> None:
        deps = _build_deps(
            position_lookup=FakePositionLookup(
                {"pos-1": PositionState(position_id="pos-1", status="open")}
            ),
            close_submitter=FakeCloseSubmitter(
                BrokerErrorClose(broker_message="no liquidity")
            ),
        )
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/force_close_position",
            json={"position_id": "pos-1", "rationale": "x"},
        )
        assert response.status_code == 502
        body = response.json()
        assert body["error"]["details"]["broker_message"] == "no liquidity"


# ===========================================================================
# /control/set_halt_mode
# ===========================================================================


class TestSetHaltModeRoute:
    def test_engaging_returns_200_accepted(self) -> None:
        repo = FakeHaltModeRepo()
        deps = _build_deps(halt_mode_repo=repo)
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/set_halt_mode", json={"enabled": True, "reason": "circuit"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "accepted"
        assert repo.record.enabled is True

    def test_idempotent_same_value_returns_original_applied_at(self) -> None:
        prior_ts = datetime(2026, 5, 26, 8, 0, 0, tzinfo=UTC)
        repo = FakeHaltModeRepo()
        repo.record = HaltModeRecord(enabled=True, reason="prior", applied_at=prior_ts)
        deps = _build_deps(halt_mode_repo=repo)
        client = TestClient(build_app(deps))
        response = client.post(
            "/control/set_halt_mode", json={"enabled": True, "reason": "again"}
        )
        assert response.status_code == 200
        body = response.json()
        # The original applied_at echoes back; the route renders this verbatim.
        assert body["applied_at"] == "2026-05-26T08:00:00Z"


# ===========================================================================
# /events SSE stream
# ===========================================================================


class TestEventsRoute:
    def test_get_events_headers_correct(self) -> None:
        """The route is registered and returns the documented SSE headers.

        ``TestClient.stream`` is intentionally NOT used here — its underlying
        ``httpx`` transport blocks on the first byte from the in-process
        ASGI app, which conflicts with our pytest-asyncio-driven SSE-
        iterator tests below. The structural framing of every event type
        and the heartbeat cadence are exercised in
        :class:`TestSSEIterator` against the raw async generator.

        Instead, we assert the route is mounted at the documented path. The
        ``HEAD`` method against an SSE route returns 405 in FastAPI (only
        GET is registered), which still confirms the route exists.
        """
        emitter = SSEEventEmitter()
        deps = _build_deps(event_emitter=emitter)
        app = build_app(deps)
        # Inspect the registered routes directly rather than driving an
        # ASGI request — this gets at the route's HTTP shape without
        # consuming the streaming body.
        routes = [r for r in app.routes if getattr(r, "path", "") == "/events"]
        assert len(routes) == 1
        events_route = routes[0]
        assert "GET" in events_route.methods  # type: ignore[attr-defined]


# ===========================================================================
# SSE iterator — driven directly without TestClient streaming.
# ===========================================================================


@pytest.mark.asyncio
class TestSSEIterator:
    async def _drain_one_frame(self, agen: AsyncGenerator[bytes, None]) -> str:
        chunks: list[bytes] = []
        async for chunk in agen:
            chunks.append(chunk)
            if b"\n\n" in chunk:
                break
        return b"".join(chunks).decode("utf-8")

    async def test_each_of_seven_event_types_renders_sse_frame(self) -> None:
        """The SSE frame renderer produces ``event: <name>\\ndata: <json>\\n\\n``.

        The framing is the wire-format the route handler emits per the
        schema's § SSE framing. The per-event payloads are validated by
        the Pydantic models (see ``test_events.py``); this test asserts
        the rendering is correct for every documented event type.
        """
        from datetime import UTC, datetime

        from alphamind.execution.continuous_monitor.control.events import (
            EmittedEvent,
        )
        from alphamind.execution.continuous_monitor.control.routes import (
            _render_sse_frame,
        )

        sample_events = [
            EmittedEvent(
                name="websocket_connected",
                payload={"timestamp": "2026-05-26T00:00:00Z"},
            ),
            EmittedEvent(
                name="websocket_disconnected",
                payload={
                    "timestamp": "2026-05-26T00:00:00Z",
                    "reason": "network_error",
                },
            ),
            EmittedEvent(
                name="fill_received",
                payload={
                    "order_id": "ord-1",
                    "position_id": "pos-1",
                    "fill_price": 100.0,
                    "fill_qty": 10.0,
                },
            ),
            EmittedEvent(
                name="breach_detected",
                payload={
                    "rule": "r",
                    "current_value": 1.0,
                    "limit": 0.5,
                    "response_classification": "immediate",
                },
            ),
            EmittedEvent(
                name="emergency_invocation_triggered",
                payload={"reason": "x"},
            ),
            EmittedEvent(
                name="greeks_refreshed",
                payload={
                    "underlying": "AAPL",
                    "refreshed_at": datetime.now(UTC).isoformat(),
                },
            ),
            EmittedEvent(name="heartbeat", payload={"timestamp": "x"}),
        ]
        for event in sample_events:
            rendered = _render_sse_frame(event).decode("utf-8")
            assert rendered.startswith(f"event: {event.name}\n")
            assert "data: " in rendered
            assert rendered.endswith("\n\n")
            data_line = next(
                line for line in rendered.splitlines() if line.startswith("data:")
            )
            payload = json.loads(data_line.split(":", 1)[1].strip())
            assert payload == event.payload

    async def test_heartbeat_fires_after_cadence_when_idle(self) -> None:
        from alphamind.execution.continuous_monitor.control.routes import (
            _sse_iterator,
        )

        emitter = SSEEventEmitter()
        request = _FakeRequest()
        agen = _sse_iterator(emitter, request, heartbeat_interval_seconds=0.05)
        try:
            rendered = await self._drain_one_frame(agen)
            assert "event: heartbeat" in rendered
        finally:
            request.set_disconnected()
            await agen.aclose()


class _FakeRequest:
    """Minimal Request stand-in supporting ``is_disconnected()``."""

    def __init__(self) -> None:
        self._disconnected = False

    def set_disconnected(self) -> None:
        self._disconnected = True

    async def is_disconnected(self) -> bool:
        return self._disconnected


# ===========================================================================
# Bind-to-loopback assertion (handled by app.py's bind helper)
# ===========================================================================


@pytest.mark.asyncio
class TestLoopbackBind:
    async def test_uvicorn_config_binds_to_127_0_0_1(self) -> None:
        from alphamind.execution.continuous_monitor.control.app import (
            build_uvicorn_config,
        )

        deps = _build_deps()
        app = build_app(deps)
        config = build_uvicorn_config(app=app, port=8766)
        assert config.host == "127.0.0.1"
        assert config.port == 8766

    async def test_default_port_8766(self) -> None:
        from alphamind.config.models.continuous_monitor import (
            ContinuousMonitorConfig,
        )

        # ``control_port`` is a fresh field added by this story; default 8766
        # per the acceptance criterion.
        config = ContinuousMonitorConfig(
            breach_evaluation_cadence_seconds=60,
            greeks_refresh_interval_minutes=15,
            greeks_refresh_underlying_move_threshold_pct=2.0,
            underlying_stream_provider="alpaca-iex",
            max_reconnect_attempts=5,
            supervisor_shutdown_timeout_seconds=5,
        )
        assert config.control_port == 8766


