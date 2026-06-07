"""Tests for ``command_center.views.live_operations`` (story 05b / ALP-672).

Covers:

* :func:`update_schedule_cache` — populates the module-level cache from a
  ``next_trigger_changed`` event; ignores unrelated event types.
* ``GET /api/views/live`` — returns the documented panes' data; falls back
  gracefully when the DB has no invocations / alerts.
* ``GET /api/views/schedule`` — serves the cached schedule state.

Auth is bypassed via ``dependency_overrides`` (same pattern as the control
tests) so these tests focus on the view logic, not the session gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import MappingProxyType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.dependencies import current_session
from alphamind.command_center.persistence.session import build_cc_writer_session_factory
from alphamind.command_center.persistence.tables import CommandCenterBase
from alphamind.command_center.views import live_operations as mod
from alphamind.command_center.views.live_operations import (
    ScheduleViewResponse,
    build_views_router,
    update_schedule_cache,
)
from alphamind.persistence.models import Base
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

_SESSION_ID = operator_session_id("sess-live-test")
_PROCESS_LIFETIME_ID = "plt-live-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def production_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory production DB factory with one seeded invocation row."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                process_role="pipeline",
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=1,
                hostname="test",
                git_sha="abc",
                git_branch="main",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="x",
                claude_agent_sdk_version="x",
                os_release="test",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def cc_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """In-memory command-center DB factory."""
    factory = build_cc_writer_session_factory(":memory:")
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
def views_app(
    production_session_factory: async_sessionmaker[AsyncSession],
    cc_factory: async_sessionmaker[AsyncSession],
) -> Iterator[FastAPI]:
    """Minimal FastAPI app wired with the views router + auth bypassed."""
    app = FastAPI()
    app.state.foreign_reader_session_factory = production_session_factory
    app.state.cc_writer_session_factory = cc_factory
    app.include_router(build_views_router(), prefix="/api/views")
    # Bypass auth so view logic is exercised in isolation.
    app.dependency_overrides[current_session] = lambda: _SESSION_ID
    yield app


@pytest.fixture
def client(views_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(views_app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def _reset_schedule_cache() -> Iterator[None]:
    """Reset the module-level schedule cache before each test."""
    original = dict(mod._schedule_cache)
    mod._schedule_cache.clear()
    mod._schedule_cache.update({"paused": False, "triggers": [], "cached_at": None})
    yield
    mod._schedule_cache.clear()
    mod._schedule_cache.update(original)


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# update_schedule_cache unit tests
# ---------------------------------------------------------------------------


class TestUpdateScheduleCache:
    def test_ignores_non_trigger_event(self) -> None:
        event = PipelineEvent(
            event_type=PipelineEventType.INVOCATION_STARTED,
            payload={"invocation_id": "inv-1"},
        )
        update_schedule_cache(event)
        assert mod._schedule_cache["triggers"] == []
        assert mod._schedule_cache["cached_at"] is None

    def test_ignores_monitor_event(self) -> None:
        event = MonitorEvent(
            event_type=MonitorEventType.HEARTBEAT,
            payload={},
        )
        # MonitorEvent has a different type than PipelineEvent; the
        # update_schedule_cache function expects PipelineEvent only.
        # Type narrowing — call with compatible type.
        update_schedule_cache(event)  # type: ignore[arg-type]
        assert mod._schedule_cache["triggers"] == []

    def test_single_trigger_from_payload(self) -> None:
        event = PipelineEvent(
            event_type=PipelineEventType.NEXT_TRIGGER_CHANGED,
            payload=MappingProxyType(
                {
                    "next_trigger_at": "2026-05-26T09:30:00Z",
                    "next_trigger_type": "market_open",
                    "paused": False,
                }
            ),
        )
        update_schedule_cache(event)
        assert len(mod._schedule_cache["triggers"]) == 1
        assert mod._schedule_cache["triggers"][0]["trigger_at"] == "2026-05-26T09:30:00Z"
        assert mod._schedule_cache["paused"] is False
        assert mod._schedule_cache["cached_at"] is not None

    def test_paused_flag(self) -> None:
        event = PipelineEvent(
            event_type=PipelineEventType.NEXT_TRIGGER_CHANGED,
            payload=MappingProxyType(
                {
                    "next_trigger_at": "2026-05-26T09:30:00Z",
                    "next_trigger_type": "market_open",
                    "paused": True,
                }
            ),
        )
        update_schedule_cache(event)
        assert mod._schedule_cache["paused"] is True

    def test_next_triggers_list_used_when_present(self) -> None:
        triggers = [
            {"trigger_at": f"2026-05-26T0{i}:00:00Z", "trigger_type": "market_open"}
            for i in range(7)
        ]
        event = PipelineEvent(
            event_type=PipelineEventType.NEXT_TRIGGER_CHANGED,
            payload=MappingProxyType(
                {
                    "next_trigger_at": "2026-05-26T00:00:00Z",
                    "next_trigger_type": "market_open",
                    "next_triggers": triggers,
                    "paused": False,
                }
            ),
        )
        update_schedule_cache(event)
        # Capped at 5.
        assert len(mod._schedule_cache["triggers"]) == 5


# ---------------------------------------------------------------------------
# GET /api/views/live
# ---------------------------------------------------------------------------


class TestLiveViewEndpoint:
    def test_returns_200_with_empty_db(self, client: TestClient) -> None:
        resp = client.get("/api/views/live")
        assert resp.status_code == 200
        body = resp.json()
        assert "pipeline" in body
        assert "monitor" in body
        assert "active_alerts" in body
        assert "assembled_at" in body

    def test_pipeline_pane_none_when_no_invocations(self, client: TestClient) -> None:
        resp = client.get("/api/views/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pipeline"]["invocation_id"] is None

    def test_pipeline_pane_with_invocation(
        self,
        client: TestClient,
        production_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        import asyncio

        async def _seed() -> None:
            async with production_session_factory() as session:
                session.add(
                    InvocationRow(
                        invocation_id="inv-live-001",
                        process_lifetime_id=_PROCESS_LIFETIME_ID,
                        start_at="2026-05-26T09:30:00Z",
                        fill_collection_completed_at=None,
                        command_execution_completed_at=None,
                        trigger_type="scheduled",
                        trigger_source="market_open",
                        trigger_reason="scheduled",
                        git_sha_at_invocation="abc",
                        active_profile="default",
                        active_regime="normal",
                        active_mode="normal",
                        active_overlays_json="[]",
                        resolved_config_hash="hash",
                        resolved_config_snapshot_path="/dev/null",
                        feature_flags_snapshot_json="{}",
                        data_calibration_state_snapshot_path="/dev/null",
                        data_source_freshness_json="{}",
                    )
                )
                await session.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        resp = client.get("/api/views/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pipeline"]["invocation_id"] == "inv-live-001"
        assert body["pipeline"]["run_type"] == "scheduled"
        assert body["pipeline"]["status"] == "running"

    def test_active_alerts_empty(self, client: TestClient) -> None:
        resp = client.get("/api/views/live")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_alerts"] == []

    def test_active_alerts_returns_firing_rows(
        self,
        client: TestClient,
        cc_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Regression for finding #5 (Wave-5 review).

        The legacy query filtered on ``status = 'active'`` which is not a
        member of :class:`AlertStatus`; firing alerts silently disappeared
        from the live pane. The fix filters on
        :attr:`AlertStatus.FIRING.value` and we assert here that a seeded
        firing row surfaces in the response while an acknowledged row
        does not.
        """
        import asyncio

        from alphamind.command_center.persistence.codecs import AlertStatus
        from alphamind.command_center.persistence.tables import AlertRow

        async def _seed() -> None:
            async with cc_factory() as session:
                session.add(
                    AlertRow(
                        alert_id="al-firing",
                        rule_name="rule-a",
                        severity="critical",
                        status=AlertStatus.FIRING.value,
                        fired_at="2026-05-26T09:30:00Z",
                        acknowledged_at=None,
                        snoozed_until=None,
                        context_json="{}",
                    )
                )
                session.add(
                    AlertRow(
                        alert_id="al-acked",
                        rule_name="rule-b",
                        severity="important",
                        status=AlertStatus.ACKNOWLEDGED.value,
                        fired_at="2026-05-26T09:00:00Z",
                        acknowledged_at="2026-05-26T09:05:00Z",
                        snoozed_until=None,
                        context_json="{}",
                    )
                )
                await session.commit()

        asyncio.get_event_loop().run_until_complete(_seed())
        resp = client.get("/api/views/live")
        assert resp.status_code == 200
        ids = [a["alert_id"] for a in resp.json()["active_alerts"]]
        assert ids == ["al-firing"], (
            "only firing alerts should appear in the active-alerts pane; "
            "the previous query used the non-existent 'active' status string"
        )

    def test_monitor_pane_returns_placeholder(self, client: TestClient) -> None:
        """Regression for finding #4 (Wave-5 review).

        The legacy ``_read_monitor_status`` ran raw SQL against a
        non-existent ``timestamp`` column and filtered on event-type
        strings that aren't :class:`EventType` members. A blanket
        ``except Exception`` swallowed the resulting ``OperationalError``
        so callers got an empty model anyway — but a future operator
        debugging this path would see a fresh stack trace each tick.
        The fix stubs the pane to placeholder values until a dedicated
        persistence path lands. We assert here the response shape is
        well-formed and well-typed.
        """
        resp = client.get("/api/views/live")
        assert resp.status_code == 200
        monitor = resp.json()["monitor"]
        # Placeholder shape — explicit False / None so the UI can render
        # an "unwired" badge rather than be silently lied to.
        assert monitor["websocket_connected"] is False
        assert monitor["breach_active"] is False
        assert monitor["time_since_connect_seconds"] is None
        assert monitor["last_fill_at"] is None
        assert monitor["breach_rule"] is None


# ---------------------------------------------------------------------------
# GET /api/views/schedule
# ---------------------------------------------------------------------------


class TestScheduleViewEndpoint:
    def test_returns_cold_cache(self, client: TestClient) -> None:
        resp = client.get("/api/views/schedule")
        assert resp.status_code == 200
        body = resp.json()
        validated = ScheduleViewResponse.model_validate(body)
        assert validated.paused is False
        assert validated.triggers == []
        assert validated.cached_at is None

    def test_reflects_cache_after_update(self, client: TestClient) -> None:
        event = PipelineEvent(
            event_type=PipelineEventType.NEXT_TRIGGER_CHANGED,
            payload=MappingProxyType(
                {
                    "next_trigger_at": "2026-05-27T09:30:00Z",
                    "next_trigger_type": "market_open",
                    "paused": True,
                }
            ),
        )
        update_schedule_cache(event)

        resp = client.get("/api/views/schedule")
        assert resp.status_code == 200
        body = resp.json()
        validated = ScheduleViewResponse.model_validate(body)
        assert validated.paused is True
        assert len(validated.triggers) == 1
        assert validated.triggers[0].trigger_at == "2026-05-27T09:30:00Z"
        assert validated.cached_at is not None
