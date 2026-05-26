"""Tests for ``command_center.control.routes`` (story 04a / ALP-668).

Uses FastAPI's ``dependency_overrides`` to bypass the auth + CSRF
gates for verb-behavior testing; a separate test class verifies the
gates are declared on every route. Full end-to-end auth + CSRF flow
is the responsibility of ``tests/command_center/auth/`` — these tests
focus on the proxy / response-rendering layer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center._kernel.control import ControlErrorCode, ControlResult
from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.dependencies import (
    csrf_required,
    current_session,
)
from alphamind.command_center.control.monitor_client import (
    FakeMonitorClient,
    MonitorForceClosePositionResult,
)
from alphamind.command_center.control.pipeline_client import (
    FakePipelineClient,
    PipelineSwitchProfileResult,
    PipelineTriggerEmergencyResult,
)
from alphamind.command_center.control.routes import build_control_router
from alphamind.persistence.models import Base
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

PROCESS_LIFETIME_ID = "plt-routes-test"
SESSION_ID = "sess-routes-test"
_FROZEN_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def production_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=PROCESS_LIFETIME_ID,
                process_role="monitor",
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=42,
                hostname="t",
                git_sha="d",
                git_branch="t",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="x",
                claude_agent_sdk_version="x",
                os_release="t",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def fake_pipeline_client() -> FakePipelineClient:
    return FakePipelineClient()


@pytest.fixture
def fake_monitor_client() -> FakeMonitorClient:
    return FakeMonitorClient()


@pytest.fixture
def control_app(
    production_session_factory: async_sessionmaker[AsyncSession],
    fake_pipeline_client: FakePipelineClient,
    fake_monitor_client: FakeMonitorClient,
) -> Iterator[FastAPI]:
    """FastAPI app wired with the control router + fake clients.

    Auth + CSRF dependencies are overridden so route behavior can be
    exercised without seeding ``operator_sessions``. The dedicated
    ``TestRouteAuthGates`` class confirms the gates ARE declared on
    every route.
    """
    app = FastAPI()
    app.state.production_session_factory = production_session_factory
    app.state.process_lifetime_id = PROCESS_LIFETIME_ID
    app.state.pipeline_client = fake_pipeline_client
    app.state.monitor_client = fake_monitor_client
    app.state.clock = lambda: _FROZEN_NOW

    async def _ok_session() -> str:
        return operator_session_id(SESSION_ID)

    async def _ok_csrf() -> None:
        return None

    app.dependency_overrides[current_session] = _ok_session
    app.dependency_overrides[csrf_required] = _ok_csrf

    app.include_router(build_control_router(), prefix="/api/control")
    yield app


@pytest.fixture
def client(control_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(control_app) as c:
        yield c


# ---------------------------------------------------------------------------
# Happy-path verb dispatch.
# ---------------------------------------------------------------------------


class TestPipelineVerbsHappyPath:
    def test_pause_returns_accepted_envelope(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        r = client.post("/api/control/pause", json={"reason": "drawdown"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert body["applied_at"].startswith("2026-05-26T12:00:00")
        assert fake_pipeline_client.calls == [("pause", {"reason": "drawdown"})]

    def test_resume_returns_accepted_envelope(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        r = client.post("/api/control/resume", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"
        assert fake_pipeline_client.calls == [("resume", {})]

    def test_trigger_emergency_returns_invocation_id(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        fake_pipeline_client.set_trigger_emergency_response(
            PipelineTriggerEmergencyResult(
                result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                invocation_id="inv-99",
            )
        )
        r = client.post(
            "/api/control/trigger_emergency_invocation",
            json={"reason": "halt"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["invocation_id"] == "inv-99"

    def test_switch_profile_returns_accepted(self, client: TestClient) -> None:
        r = client.post("/api/control/switch_profile", json={"profile_name": "large"})
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"

    def test_run_universe_validation_returns_report(self, client: TestClient) -> None:
        r = client.post("/api/control/run_universe_validation", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert body["report"]["tickers"][0]["ticker"] == "AAPL"
        assert len(body["report"]["tickers"][0]["criteria"]) == 5


class TestMonitorVerbsHappyPath:
    def test_cancel_order_returns_accepted(
        self, client: TestClient, fake_monitor_client: FakeMonitorClient
    ) -> None:
        r = client.post("/api/control/cancel_order", json={"order_id": "ord-1"})
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"
        assert fake_monitor_client.calls == [("cancel_order", {"order_id": "ord-1"})]

    def test_force_close_position_returns_envelope_id(self, client: TestClient) -> None:
        r = client.post(
            "/api/control/force_close_position",
            json={"position_id": "pos-1", "rationale": "exit"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["envelope_id"] == "MON.s1.42"

    def test_set_halt_mode_returns_accepted(self, client: TestClient) -> None:
        r = client.post(
            "/api/control/set_halt_mode",
            json={"enabled": True, "reason": "vol spike"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# Error-envelope mapping.
# ---------------------------------------------------------------------------


class TestErrorCodeToHttpStatusMapping:
    def test_validation_failed_maps_to_400(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        fake_pipeline_client.set_pause_response(
            ControlResult.failure(
                error_code=ControlErrorCode.VALIDATION_FAILED,
                error_detail="bad reason",
            )
        )
        r = client.post("/api/control/pause", json={"reason": "x"})
        assert r.status_code == 400
        body = r.json()
        assert body["detail"]["error"]["code"] == "validation_failed"

    def test_not_found_maps_to_404(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        fake_pipeline_client.set_switch_profile_response(
            PipelineSwitchProfileResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.NOT_FOUND, error_detail="no profile"
                )
            )
        )
        r = client.post("/api/control/switch_profile", json={"profile_name": "zz"})
        assert r.status_code == 404
        assert r.json()["detail"]["error"]["code"] == "not_found"

    def test_precondition_failed_maps_to_409(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        fake_pipeline_client.set_pause_response(
            ControlResult.failure(
                error_code=ControlErrorCode.PRECONDITION_FAILED,
                error_detail="already paused",
            )
        )
        r = client.post("/api/control/pause", json={"reason": "x"})
        assert r.status_code == 409

    def test_cooldown_active_maps_to_409(
        self, client: TestClient, fake_pipeline_client: FakePipelineClient
    ) -> None:
        fake_pipeline_client.set_trigger_emergency_response(
            PipelineTriggerEmergencyResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.COOLDOWN_ACTIVE,
                    error_detail="60s remaining",
                )
            )
        )
        r = client.post("/api/control/trigger_emergency_invocation", json={"reason": "x"})
        assert r.status_code == 409
        assert r.json()["detail"]["error"]["code"] == "cooldown_active"

    def test_broker_error_maps_to_502(
        self, client: TestClient, fake_monitor_client: FakeMonitorClient
    ) -> None:
        fake_monitor_client.set_force_close_response(
            MonitorForceClosePositionResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.BROKER_ERROR,
                    error_detail="broker rejected",
                )
            )
        )
        r = client.post(
            "/api/control/force_close_position",
            json={"position_id": "pos-1", "rationale": "x"},
        )
        assert r.status_code == 502

    def test_internal_error_maps_to_500(
        self, client: TestClient, fake_monitor_client: FakeMonitorClient
    ) -> None:
        fake_monitor_client.set_cancel_order_response(
            ControlResult.failure(
                error_code=ControlErrorCode.INTERNAL_ERROR,
                error_detail="upstream burped",
            )
        )
        r = client.post("/api/control/cancel_order", json={"order_id": "ord-1"})
        assert r.status_code == 500


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


class TestRequestValidation:
    def test_pause_empty_reason_rejected(self, client: TestClient) -> None:
        r = client.post("/api/control/pause", json={"reason": ""})
        assert r.status_code == 422

    def test_pause_extra_field_rejected(self, client: TestClient) -> None:
        r = client.post("/api/control/pause", json={"reason": "x", "bogus": True})
        assert r.status_code == 422

    def test_force_close_missing_rationale_rejected(self, client: TestClient) -> None:
        r = client.post("/api/control/force_close_position", json={"position_id": "pos-1"})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Auth + CSRF gates declared.
# ---------------------------------------------------------------------------


class TestRouteAuthGatesDeclared:
    """The 8 routes MUST declare ``Depends(current_session)`` + ``Depends(csrf_required)``."""

    def _route_dependencies(self, app: FastAPI, path: str) -> set[object]:
        """Pull the set of dependency-call function objects out of the route signature."""
        from fastapi.routing import APIRoute

        for route in app.router.routes:
            if isinstance(route, APIRoute) and route.path == path:
                return {d.call for d in route.dependant.dependencies}
        msg = f"route {path!r} not found in app"
        raise AssertionError(msg)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/control/pause",
            "/api/control/resume",
            "/api/control/trigger_emergency_invocation",
            "/api/control/switch_profile",
            "/api/control/run_universe_validation",
            "/api/control/cancel_order",
            "/api/control/force_close_position",
            "/api/control/set_halt_mode",
        ],
    )
    def test_route_declares_current_session_and_csrf_required(
        self, control_app: FastAPI, path: str
    ) -> None:
        deps = self._route_dependencies(control_app, path)
        assert current_session in deps, f"route {path!r} missing Depends(current_session)"
        assert csrf_required in deps, f"route {path!r} missing Depends(csrf_required)"


# ---------------------------------------------------------------------------
# Auth gate live (without overrides) — 401 / 403 paths.
# ---------------------------------------------------------------------------


@pytest.fixture
def unauth_app(
    production_session_factory: async_sessionmaker[AsyncSession],
    fake_pipeline_client: FakePipelineClient,
    fake_monitor_client: FakeMonitorClient,
) -> Iterator[FastAPI]:
    """Like ``control_app`` but WITHOUT dependency_overrides.

    Exercises the real auth + CSRF gates so 401 / 403 paths are
    confirmed at the route layer.
    """
    from alphamind.command_center.config import (
        CsrfConfig,
        SecurityConfig,
        SessionConfig,
        WebauthnConfig,
    )
    from alphamind.command_center.persistence.session import (
        build_cc_writer_session_factory,
    )
    from alphamind.command_center.persistence.tables import CommandCenterBase

    app = FastAPI()
    # Minimal security wiring (no session row → 401).
    cc_factory = build_cc_writer_session_factory(":memory:")
    cc_engine = cc_factory.kw["bind"]

    import asyncio

    async def _setup() -> None:
        async with cc_engine.begin() as conn:
            await conn.run_sync(CommandCenterBase.metadata.create_all)

    asyncio.get_event_loop().run_until_complete(_setup())

    app.state.cc_writer_session_factory = cc_factory
    app.state.security_config = SecurityConfig(
        session=SessionConfig(duration_hours=12, cookie_name="cc_session"),
        csrf=CsrfConfig(cookie_name="cc_csrf"),
        webauthn=WebauthnConfig(relying_party_id="localhost", relying_party_name="AlphaMind"),
    )
    app.state.session_signing_secret = b"x" * 32
    app.state.clock = lambda: _FROZEN_NOW
    app.state.production_session_factory = production_session_factory
    app.state.process_lifetime_id = PROCESS_LIFETIME_ID
    app.state.pipeline_client = fake_pipeline_client
    app.state.monitor_client = fake_monitor_client

    app.include_router(build_control_router(), prefix="/api/control")
    try:
        yield app
    finally:
        asyncio.get_event_loop().run_until_complete(cc_engine.dispose())


class TestAuthGateLive:
    def test_pause_without_session_cookie_returns_401(self, unauth_app: FastAPI) -> None:
        with TestClient(unauth_app) as c:
            r = c.post("/api/control/pause", json={"reason": "x"})
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# F3 + F4 — explicit 500 when app.state is missing required wiring.
# ---------------------------------------------------------------------------


class TestBuildControlRouterFreshInstance:
    """``build_control_router()`` must return a NEW :class:`APIRouter`
    on each call. A module-level singleton would carry response-model
    overrides / dependency_overrides / middleware between apps and
    surface as cross-test contamination (F6).
    """

    def test_returns_new_instance_per_call(self) -> None:
        router_a = build_control_router()
        router_b = build_control_router()
        assert router_a is not router_b

    def test_registering_against_two_apps_does_not_cross_pollute(
        self,
        production_session_factory: async_sessionmaker[AsyncSession],
        fake_pipeline_client: FakePipelineClient,
        fake_monitor_client: FakeMonitorClient,
    ) -> None:
        # App A: pipeline client raises pre-condition_failed on pause.
        client_a_pipeline = FakePipelineClient()
        client_a_pipeline.set_pause_response(
            ControlResult.failure(
                error_code=ControlErrorCode.PRECONDITION_FAILED,
                error_detail="already paused",
            )
        )

        async def _ok_session() -> str:
            return operator_session_id(SESSION_ID)

        async def _ok_csrf() -> None:
            return None

        app_a = FastAPI()
        app_a.state.production_session_factory = production_session_factory
        app_a.state.process_lifetime_id = PROCESS_LIFETIME_ID
        app_a.state.pipeline_client = client_a_pipeline
        app_a.state.monitor_client = fake_monitor_client
        app_a.state.clock = lambda: _FROZEN_NOW
        app_a.dependency_overrides[current_session] = _ok_session
        app_a.dependency_overrides[csrf_required] = _ok_csrf
        app_a.include_router(build_control_router(), prefix="/api/control")

        # App B: pipeline client returns success on pause. If the
        # routers shared state (module-level singleton), App B's
        # request would still hit App A's wiring via leaked
        # dependency_overrides or response models.
        app_b = FastAPI()
        app_b.state.production_session_factory = production_session_factory
        app_b.state.process_lifetime_id = PROCESS_LIFETIME_ID
        app_b.state.pipeline_client = fake_pipeline_client
        app_b.state.monitor_client = fake_monitor_client
        app_b.state.clock = lambda: _FROZEN_NOW
        app_b.dependency_overrides[current_session] = _ok_session
        app_b.dependency_overrides[csrf_required] = _ok_csrf
        app_b.include_router(build_control_router(), prefix="/api/control")

        with TestClient(app_a) as c_a:
            r_a = c_a.post("/api/control/pause", json={"reason": "drawdown"})
        with TestClient(app_b) as c_b:
            r_b = c_b.post("/api/control/pause", json={"reason": "drawdown"})

        # If the routers were shared, App B would inherit App A's
        # pipeline client and return 409. Independent routers carry
        # independent dispatch — each app sees its own client.
        assert r_a.status_code == 409
        assert r_b.status_code == 200


class TestRequiredAppStateGuards:
    """``_make_ctx`` must raise HTTP 500 with a clear detail when the
    required ``production_session_factory`` / ``process_lifetime_id`` fields
    are ``None`` on ``app.state``. Without the guard, ``ProxyContext``
    construction propagates a ``TypeError`` deep in ``operator_invocation``
    (F3, F4).
    """

    def _build_app_with_state_holes(
        self,
        fake_pipeline_client: FakePipelineClient,
        fake_monitor_client: FakeMonitorClient,
        *,
        production_session_factory: object | None,
        process_lifetime_id: object | None,
    ) -> FastAPI:
        app = FastAPI()
        app.state.production_session_factory = production_session_factory
        app.state.process_lifetime_id = process_lifetime_id
        app.state.pipeline_client = fake_pipeline_client
        app.state.monitor_client = fake_monitor_client
        app.state.clock = lambda: _FROZEN_NOW

        async def _ok_session() -> str:
            return operator_session_id(SESSION_ID)

        async def _ok_csrf() -> None:
            return None

        app.dependency_overrides[current_session] = _ok_session
        app.dependency_overrides[csrf_required] = _ok_csrf
        app.include_router(build_control_router(), prefix="/api/control")
        return app

    def test_missing_production_session_factory_returns_500(
        self,
        fake_pipeline_client: FakePipelineClient,
        fake_monitor_client: FakeMonitorClient,
    ) -> None:
        app = self._build_app_with_state_holes(
            fake_pipeline_client,
            fake_monitor_client,
            production_session_factory=None,
            process_lifetime_id=PROCESS_LIFETIME_ID,
        )
        with TestClient(app) as c:
            r = c.post("/api/control/pause", json={"reason": "x"})
        assert r.status_code == 500
        assert "production_session_factory" in r.json()["detail"]

    def test_missing_process_lifetime_id_returns_500(
        self,
        production_session_factory: async_sessionmaker[AsyncSession],
        fake_pipeline_client: FakePipelineClient,
        fake_monitor_client: FakeMonitorClient,
    ) -> None:
        app = self._build_app_with_state_holes(
            fake_pipeline_client,
            fake_monitor_client,
            production_session_factory=production_session_factory,
            process_lifetime_id=None,
        )
        with TestClient(app) as c:
            r = c.post("/api/control/pause", json={"reason": "x"})
        assert r.status_code == 500
        assert "process_lifetime_id" in r.json()["detail"]
