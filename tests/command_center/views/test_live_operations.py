"""Tests for ``command_center.views.live_operations`` (story 05b / ALP-672).

Coverage:

* ``GET /api/views/live`` — returns ``LiveSnapshot`` shape; empty when no
  data; populates invocation + alert rows when seeded.
* ``GET /api/views/schedule`` — returns ``ScheduleSnapshot`` from module
  cache; single entry from current value when no history.
* :func:`make_schedule_cache_subscriber` — queue receives
  ``PipelineEvent(NEXT_TRIGGER_CHANGED, {...})`` and updates the cache.
* Auth gate — both endpoints return 401 without a valid session cookie.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.events import PipelineEvent, PipelineEventType
from alphamind.command_center._kernel.ids import (
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.auth.repository import insert_credential, insert_session
from alphamind.command_center.auth.sessions import (
    SessionCookiePayload,
    encode_session_cookie,
)
from alphamind.command_center.config import (
    CsrfConfig,
    SecurityConfig,
    SessionConfig,
    WebauthnConfig,
)
from alphamind.command_center.persistence.codecs import (
    OperatorSessionRecord,
    WebauthnCredentialRecord,
)
from alphamind.command_center.persistence.session import build_cc_writer_session_factory
from alphamind.command_center.persistence.tables import (
    AlertRow,
    CommandCenterBase,
)
from alphamind.command_center.views.live_operations import (
    _ScheduleCache,
    build_live_operations_router,
    make_schedule_cache_subscriber,
    set_schedule_cache_for_test,
)
from alphamind.persistence.models import Base
from alphamind.state.tables import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

_SECRET = b"test-secret-key-32-bytes-long!!!!"
_SESSION_ID = "sess-views-test-1"
_CRED_ID = "cred-views-test"
_EXPIRES_AT = "2099-01-01T00:00:00Z"
_NOW = datetime(2026, 5, 26, 0, 0, 0, tzinfo=UTC)
_PROCESS_LIFETIME_ID = "plt-views-test-001"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def security_config() -> SecurityConfig:
    return SecurityConfig(
        session=SessionConfig(duration_hours=12, cookie_name="cc_session"),
        csrf=CsrfConfig(cookie_name="cc_csrf"),
        webauthn=WebauthnConfig(
            relying_party_id="localhost",
            relying_party_name="AlphaMind Command Center",
        ),
    )


@pytest.fixture
async def cc_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    factory = build_cc_writer_session_factory(":memory:")
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def prod_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Production DB factory with invocations table + seeded process lifetime."""
    from sqlalchemy.ext.asyncio import create_async_engine

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
                process_pid=99,
                hostname="test-host",
                git_sha="abc123",
                git_branch="main",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="not-installed",
                claude_agent_sdk_version="not-installed",
                os_release="test-os",
            )
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture
async def session_cookie_value(
    cc_factory: async_sessionmaker[AsyncSession],
) -> str:
    await insert_credential(
        cc_factory,
        WebauthnCredentialRecord(
            credential_id=webauthn_credential_id(_CRED_ID),
            public_key="pk",
            sign_count=0,
            transports="internal",
            created_at="2026-05-26T00:00:00Z",
        ),
    )
    await insert_session(
        cc_factory,
        OperatorSessionRecord(
            session_id=operator_session_id(_SESSION_ID),
            credential_id=webauthn_credential_id(_CRED_ID),
            expires_at=_EXPIRES_AT,
            csrf_token_hash="any",
            created_at="2026-05-26T00:00:00Z",
        ),
    )
    return encode_session_cookie(
        SessionCookiePayload(
            session_id=operator_session_id(_SESSION_ID),
            expires_at=_EXPIRES_AT,
        ),
        secret=_SECRET,
    )


def _build_app(
    *,
    cc_factory: async_sessionmaker[AsyncSession],
    security_config: SecurityConfig,
    foreign_reader_factory: Any | None = None,
    fresh_cache: _ScheduleCache | None = None,
) -> FastAPI:
    """Minimal FastAPI app wired with the live-operations router."""
    app = FastAPI()
    app.state.security_config = security_config
    app.state.cc_writer_session_factory = cc_factory
    app.state.session_signing_secret = _SECRET
    app.state.clock = lambda: _NOW
    app.state.cookies_secure = False
    if foreign_reader_factory is not None:
        app.state.foreign_reader_session_factory = foreign_reader_factory
    # Inject an isolated cache so tests don't share global state.
    cache = fresh_cache or _ScheduleCache()
    set_schedule_cache_for_test(cache)
    app.state._live_ops_test_cache = cache
    app.include_router(build_live_operations_router())
    return app


# ---------------------------------------------------------------------------
# Auth gate tests
# ---------------------------------------------------------------------------


class TestAuthGate:
    def test_live_requires_auth(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
    ) -> None:
        app = _build_app(cc_factory=cc_factory, security_config=security_config)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/views/live")
        assert resp.status_code == 401

    def test_schedule_requires_auth(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
    ) -> None:
        app = _build_app(cc_factory=cc_factory, security_config=security_config)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get("/api/views/schedule")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/views/live — shape + content tests
# ---------------------------------------------------------------------------


class TestGetLive:
    def test_returns_empty_snapshot_with_no_data(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        app = _build_app(cc_factory=cc_factory, security_config=security_config)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/live",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["invocation"] is None
        assert body["active_alerts"] == []
        assert body["next_trigger_at"] is None
        assert body["next_trigger_type"] is None

    def test_returns_invocation_when_seeded(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        prod_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        # Seed an invocation row in the prod DB.
        import asyncio

        async def _seed() -> None:
            async with prod_factory() as session:
                session.add(
                    InvocationRow(
                        invocation_id="inv-live-001",
                        process_lifetime_id=_PROCESS_LIFETIME_ID,
                        start_at="2026-05-26T10:00:00Z",
                        phase1_completed_at=None,
                        phase2_completed_at=None,
                        trigger_type="scheduled",
                        trigger_source="pre_open",
                        trigger_reason="cron",
                        git_sha_at_invocation="abc",
                        active_profile="default",
                        active_regime="normal",
                        active_mode="normal",
                        active_overlays_json="[]",
                        resolved_config_hash="h",
                        resolved_config_snapshot_path="/dev/null",
                        feature_flags_snapshot_json="{}",
                        data_calibration_state_snapshot_path="/dev/null",
                        data_source_freshness_json="{}",
                    )
                )
                await session.commit()

        asyncio.get_event_loop().run_until_complete(_seed())

        # prod_factory IS the foreign_reader in this test (both point at the
        # same in-memory DB via the same engine — good enough for assertion).
        app = _build_app(
            cc_factory=cc_factory,
            security_config=security_config,
            foreign_reader_factory=prod_factory,
        )
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/live",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["invocation"] is not None
        assert body["invocation"]["invocation_id"] == "inv-live-001"
        assert body["invocation"]["active_mode"] == "normal"

    def test_returns_active_alerts_when_seeded(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        import asyncio as _asyncio

        async def _seed() -> None:
            async with cc_factory() as session:
                session.add(
                    AlertRow(
                        alert_id="alrt-001",
                        rule_name="test_rule",
                        severity="critical",
                        status="firing",
                        fired_at="2026-05-26T09:00:00Z",
                        acknowledged_at=None,
                        snoozed_until=None,
                        context_json='{"detail": "test"}',
                    )
                )
                await session.commit()

        _asyncio.get_event_loop().run_until_complete(_seed())

        app = _build_app(cc_factory=cc_factory, security_config=security_config)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/live",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["active_alerts"]) == 1
        assert body["active_alerts"][0]["alert_id"] == "alrt-001"
        assert body["active_alerts"][0]["severity"] == "critical"

    def test_acknowledged_alerts_excluded(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        import asyncio as _asyncio

        async def _seed() -> None:
            async with cc_factory() as session:
                session.add(
                    AlertRow(
                        alert_id="alrt-ack-001",
                        rule_name="ack_rule",
                        severity="operational",
                        status="acknowledged",
                        fired_at="2026-05-26T09:00:00Z",
                        acknowledged_at="2026-05-26T09:05:00Z",
                        snoozed_until=None,
                        context_json="{}",
                    )
                )
                await session.commit()

        _asyncio.get_event_loop().run_until_complete(_seed())

        app = _build_app(cc_factory=cc_factory, security_config=security_config)
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/live",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        assert resp.json()["active_alerts"] == []


# ---------------------------------------------------------------------------
# GET /api/views/schedule — cache-read tests
# ---------------------------------------------------------------------------


class TestGetSchedule:
    def test_returns_empty_schedule_on_cold_start(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        cache = _ScheduleCache()
        app = _build_app(
            cc_factory=cc_factory,
            security_config=security_config,
            fresh_cache=cache,
        )
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/schedule",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["entries"] == []
        assert body["paused"] is False

    def test_returns_current_trigger_as_single_entry(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        cache = _ScheduleCache(
            next_trigger_at="2026-05-26T16:00:00Z",
            next_trigger_type="pre_close",
        )
        app = _build_app(
            cc_factory=cc_factory,
            security_config=security_config,
            fresh_cache=cache,
        )
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/schedule",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["entries"]) == 1
        assert body["entries"][0]["next_trigger_at"] == "2026-05-26T16:00:00Z"
        assert body["entries"][0]["next_trigger_type"] == "pre_close"

    def test_returns_last_5_entries_from_history(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        cache = _ScheduleCache(
            next_trigger_at="2026-05-26T20:00:00Z",
            next_trigger_type="off_hours_rolling",
            raw_payloads=[
                {"next_trigger_at": f"2026-05-26T{h:02d}:00:00Z", "next_trigger_type": "scheduled"}
                for h in range(10)
            ],
        )
        app = _build_app(
            cc_factory=cc_factory,
            security_config=security_config,
            fresh_cache=cache,
        )
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/schedule",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        body = resp.json()
        # Only last 5 payloads are returned.
        assert len(body["entries"]) == 5

    def test_paused_flag_reflected(
        self,
        cc_factory: async_sessionmaker[AsyncSession],
        security_config: SecurityConfig,
        session_cookie_value: str,
    ) -> None:
        cache = _ScheduleCache(paused=True)
        app = _build_app(
            cc_factory=cc_factory,
            security_config=security_config,
            fresh_cache=cache,
        )
        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.get(
                "/api/views/schedule",
                cookies={"cc_session": session_cookie_value},
            )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True


# ---------------------------------------------------------------------------
# make_schedule_cache_subscriber — event-driven cache update
# ---------------------------------------------------------------------------


class TestScheduleCacheSubscriber:
    @pytest.mark.asyncio
    async def test_next_trigger_changed_updates_cache(self) -> None:
        cache = _ScheduleCache()
        queue = make_schedule_cache_subscriber(cache=cache)
        # Simulate the multiplexer publishing a NEXT_TRIGGER_CHANGED event.
        event = PipelineEvent(
            event_type=PipelineEventType.NEXT_TRIGGER_CHANGED,
            payload={
                "next_trigger_at": "2026-05-26T18:00:00Z",
                "next_trigger_type": "pre_close",
            },
        )
        await queue.put(event)
        # Drain via the subscriber coroutine for one iteration.
        drain = queue._drain_coroutine  # type: ignore[attr-defined]

        async def _run_once() -> None:
            task = asyncio.create_task(drain())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_once()
        assert cache.next_trigger_at == "2026-05-26T18:00:00Z"
        assert cache.next_trigger_type == "pre_close"

    @pytest.mark.asyncio
    async def test_non_trigger_events_ignored(self) -> None:
        from alphamind.command_center._kernel.events import MonitorEvent, MonitorEventType

        cache = _ScheduleCache()
        queue = make_schedule_cache_subscriber(cache=cache)
        event = MonitorEvent(
            event_type=MonitorEventType.WEBSOCKET_CONNECTED,
            payload={"ts": "2026-05-26T10:00:00Z"},
        )
        await queue.put(event)
        drain = queue._drain_coroutine  # type: ignore[attr-defined]

        async def _run_once() -> None:
            task = asyncio.create_task(drain())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_once()
        assert cache.next_trigger_at is None
        assert cache.next_trigger_type is None
