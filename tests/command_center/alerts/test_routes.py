"""Tests for :mod:`alphamind.command_center.alerts.routes` (story 05a / ALP-671).

Uses FastAPI's ``dependency_overrides`` to bypass the auth + CSRF
gates for the verb-behavior tests; a dedicated class verifies the
gates are declared on the mutating endpoints. Audit emission verified
by reading back :class:`ActivityLogRow` from the production-Base
factory.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.ids import (
    alert_id,
    alert_rule_name,
    operator_session_id,
)
from alphamind.command_center.alerts.persistence import (
    insert_fired,
    load_alert,
)
from alphamind.command_center.alerts.routes import build_alerts_router
from alphamind.command_center.auth.dependencies import (
    csrf_required,
    current_session,
)
from alphamind.command_center.persistence.codecs import AlertSeverity, AlertStatus
from alphamind.portfolio_state.events.types import EventSource
from alphamind.state.tables.activity_log import ActivityLogRow

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
SESSION_ID = "sess-alerts-routes-test"


# ---------------------------------------------------------------------------
# App fixture.
# ---------------------------------------------------------------------------


@pytest.fixture
def alerts_app(
    cc_writer_factory: async_sessionmaker[AsyncSession],
    production_factory: async_sessionmaker[AsyncSession],
) -> Iterator[FastAPI]:
    """FastAPI app wired with the alerts router + override gates."""
    from tests.command_center.alerts.conftest import PROCESS_LIFETIME_ID

    app = FastAPI()
    app.state.cc_writer_session_factory = cc_writer_factory
    app.state.production_session_factory = production_factory
    app.state.process_lifetime_id = PROCESS_LIFETIME_ID
    app.state.clock = lambda: _NOW

    async def _ok_session() -> str:
        return operator_session_id(SESSION_ID)

    async def _ok_csrf() -> None:
        return None

    app.dependency_overrides[current_session] = _ok_session
    app.dependency_overrides[csrf_required] = _ok_csrf

    app.include_router(build_alerts_router())
    yield app


@pytest.fixture
def client(alerts_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(alerts_app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/alerts
# ---------------------------------------------------------------------------


class TestListActive:
    @pytest.mark.asyncio
    async def test_returns_active_alerts(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context_json='{"foo": "bar"}',
            fired_at=_NOW - timedelta(minutes=5),
        )
        response = client.get("/api/alerts")
        assert response.status_code == 200
        body = response.json()
        assert len(body["alerts"]) == 1
        alert = body["alerts"][0]
        assert alert["alert_id"] == str(new_id)
        assert alert["rule_name"] == "test_rule"
        assert alert["severity"] == "critical"
        assert alert["status"] == "firing"

    def test_empty_returns_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/alerts")
        assert response.status_code == 200
        body = response.json()
        assert body["alerts"] == []


# ---------------------------------------------------------------------------
# acknowledge endpoint behavior
# ---------------------------------------------------------------------------


class TestAcknowledge:
    @pytest.mark.asyncio
    async def test_ack_mutates_row(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        response = client.post(f"/api/alerts/{new_id}/acknowledge")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "acknowledged"
        record = await load_alert(cc_writer_factory, alert_id_=new_id)
        assert record is not None
        assert record.status == AlertStatus.ACKNOWLEDGED

    @pytest.mark.asyncio
    async def test_ack_writes_activity_log_row(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        production_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        response = client.post(f"/api/alerts/{new_id}/acknowledge")
        assert response.status_code == 200
        async with production_factory() as session:
            rows = await session.execute(
                select(ActivityLogRow).where(
                    ActivityLogRow.source == EventSource.OPERATOR_CONSOLE.value
                )
            )
            entries = list(rows.scalars().all())
        # One ack audit row.
        ack_rows = [e for e in entries if "acknowledge" in e.detail_json]
        assert len(ack_rows) >= 1
        ack_row = ack_rows[0]
        assert str(new_id) in ack_row.detail_json
        assert ack_row.source == EventSource.OPERATOR_CONSOLE.value

    def test_missing_alert_returns_404(self, client: TestClient) -> None:
        response = client.post("/api/alerts/alert-missing/acknowledge")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_double_ack_is_idempotent(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.IMPORTANT,
            context_json="{}",
            fired_at=_NOW,
        )
        first = client.post(f"/api/alerts/{new_id}/acknowledge")
        second = client.post(f"/api/alerts/{new_id}/acknowledge")
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["status"] == "acknowledged"
        assert second.json()["status"] == "acknowledged"


# ---------------------------------------------------------------------------
# snooze endpoint behavior
# ---------------------------------------------------------------------------


class TestSnooze:
    @pytest.mark.asyncio
    async def test_snooze_mutates_row(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        future = (_NOW + timedelta(hours=1)).isoformat()
        response = client.post(
            f"/api/alerts/{new_id}/snooze",
            json={"snoozed_until": future},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "snoozed"
        record = await load_alert(cc_writer_factory, alert_id_=new_id)
        assert record is not None
        assert record.status == AlertStatus.SNOOZED

    @pytest.mark.asyncio
    async def test_snooze_writes_audit_row(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
        production_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        future = (_NOW + timedelta(hours=1)).isoformat()
        response = client.post(
            f"/api/alerts/{new_id}/snooze",
            json={"snoozed_until": future},
        )
        assert response.status_code == 200
        async with production_factory() as session:
            rows = await session.execute(
                select(ActivityLogRow).where(
                    ActivityLogRow.source == EventSource.OPERATOR_CONSOLE.value
                )
            )
            entries = list(rows.scalars().all())
        snooze_rows = [e for e in entries if "snooze" in e.detail_json]
        assert len(snooze_rows) >= 1
        assert str(new_id) in snooze_rows[0].detail_json

    def test_past_snoozed_until_returns_400(
        self,
        client: TestClient,
    ) -> None:
        past = (_NOW - timedelta(hours=1)).isoformat()
        response = client.post(
            "/api/alerts/alert-2026-05-26-abcd1234/snooze",
            json={"snoozed_until": past},
        )
        # 400 (in the future) takes precedence; the route reaches the
        # past-time check before the row lookup.
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_snooze_acked_returns_409(
        self,
        client: TestClient,
        cc_writer_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        new_id = await insert_fired(
            cc_writer_factory,
            rule_name=alert_rule_name("test_rule"),
            severity=AlertSeverity.CRITICAL,
            context_json="{}",
            fired_at=_NOW,
        )
        # Ack first.
        client.post(f"/api/alerts/{new_id}/acknowledge")
        future = (_NOW + timedelta(hours=1)).isoformat()
        response = client.post(
            f"/api/alerts/{new_id}/snooze",
            json={"snoozed_until": future},
        )
        assert response.status_code == 409

    def test_naive_snoozed_until_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/alerts/alert-2026-05-26-abcd1234/snooze",
            json={"snoozed_until": "2026-05-26T13:00:00"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Auth/CSRF gate declared.
# ---------------------------------------------------------------------------


class TestRouteAuthGatesDeclared:
    def _route_dependencies(self, app: FastAPI, path: str) -> set[object]:
        from fastapi.routing import APIRoute

        for route in app.router.routes:
            if isinstance(route, APIRoute) and route.path == path:
                return {d.call for d in route.dependant.dependencies}
        msg = f"route {path!r} not found in app"
        raise AssertionError(msg)

    def test_list_route_declares_current_session(self, alerts_app: FastAPI) -> None:
        deps = self._route_dependencies(alerts_app, "/api/alerts")
        assert current_session in deps

    @pytest.mark.parametrize(
        "path",
        [
            "/api/alerts/{alert_id_raw}/acknowledge",
            "/api/alerts/{alert_id_raw}/snooze",
        ],
    )
    def test_mutating_routes_declare_session_and_csrf(
        self,
        alerts_app: FastAPI,
        path: str,
    ) -> None:
        deps = self._route_dependencies(alerts_app, path)
        assert current_session in deps, f"route {path!r} missing Depends(current_session)"
        assert csrf_required in deps, f"route {path!r} missing Depends(csrf_required)"


# Suppress unused import warning.
_ = alert_id
