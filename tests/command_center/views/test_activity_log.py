"""Tests for ``command_center.views.activity_log`` (story 05e / ALP-675).

Tests are structured in two classes:

* ``TestQueryActivityLog`` -- core _execute_query function + filter/pagination
  behaviour against an in-memory DB seeded with activity_log rows.
* ``TestActivityLogRoutes`` -- FastAPI TestClient end-to-end for the three
  endpoints (paginated query, event-types, saved-filters), verifying auth
  gating and response shapes.

The test DB is on-disk (``tmp_path``) because the ``?mode=ro`` reader factory
rejects ``:memory:`` databases. The production schema is bootstrapped via
``Base.metadata.create_all`` (which brings the activity_log table in via
``alphamind.state.tables``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import alphamind.state.tables  # noqa: F401 -- registers activity_log on Base.metadata
from alphamind.command_center.auth.dependencies import current_session
from alphamind.command_center.views.activity_log import (
    _ActivityLogFilters,
    _execute_query,
    build_activity_log_router,
)
from alphamind.persistence.models import Base as ProductionBase
from alphamind.portfolio_state.events.types import EventSource, EventType
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_PROCESS_LIFETIME_ID = "plt-activity-log-test"
_INVOCATION_ID = "inv-activity-log-test"
_INVOCATION_ID_2 = "inv-activity-log-test-2"
_SESSION_ID = "sess-activity-log-test"


def _default_filters(**overrides: object) -> _ActivityLogFilters:
    """Return a no-op _ActivityLogFilters with optional field overrides."""
    base: dict[str, object] = {
        "event_type": None,
        "invocation_id": None,
        "position_id": None,
        "thesis_id": None,
        "order_id": None,
        "source": None,
        "time_from": None,
        "time_to": None,
        "page": 1,
        "page_size": 50,
    }
    base.update(overrides)
    return _ActivityLogFilters(**base)  # type: ignore[arg-type]


@pytest.fixture
async def db_path(tmp_path: Path) -> AsyncIterator[Path]:
    """On-disk SQLite with the production schema + seeded process_lifetime + invocation rows."""
    path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(ProductionBase.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                process_role="monitor",
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=42,
                hostname="test-host",
                git_sha="deadbeef",
                git_branch="test",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="x",
                claude_agent_sdk_version="x",
                os_release="test-os",
            )
        )
        session.add(
            InvocationRow(
                invocation_id=_INVOCATION_ID,
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                start_at="2026-05-26T09:00:00Z",
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                git_sha_at_invocation="deadbeef",
                active_profile="medium",
                active_regime="normal",
                active_mode="normal",
                active_overlays_json="[]",
                resolved_config_hash="0" * 64,
                resolved_config_snapshot_path="/dev/null",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="/dev/null",
                data_source_freshness_json="{}",
                phase1_completed_at=None,
                phase2_completed_at=None,
            )
        )
        session.add(
            InvocationRow(
                invocation_id=_INVOCATION_ID_2,
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                start_at="2026-05-26T10:00:00Z",
                trigger_type="manual",
                trigger_source="cli",
                trigger_reason="test",
                git_sha_at_invocation="deadbeef",
                active_profile="medium",
                active_regime="normal",
                active_mode="normal",
                active_overlays_json="[]",
                resolved_config_hash="0" * 64,
                resolved_config_snapshot_path="/dev/null",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="/dev/null",
                data_source_freshness_json="{}",
                phase1_completed_at=None,
                phase2_completed_at=None,
            )
        )
        await session.commit()
    await engine.dispose()
    yield path


def _make_entry(
    entry_id: str,
    invocation_id: str = _INVOCATION_ID,
    event_type: str = EventType.POSITION_OPENED.value,
    event_group: str = "POSITION_LIFECYCLE",
    source: str = EventSource.COMMAND_EXECUTOR.value,
    entry_at: str = "2026-05-26T09:01:00Z",
    position_id: str | None = "pos-001",
    order_id: str | None = None,
    thesis_id: str | None = None,
    detail_json: str = "{}",
) -> ActivityLogRow:
    return ActivityLogRow(
        entry_id=entry_id,
        invocation_id=invocation_id,
        entry_at=entry_at,
        event_type=event_type,
        event_group=event_group,
        position_id=position_id,
        order_id=order_id,
        thesis_id=thesis_id,
        source=source,
        detail_json=detail_json,
    )


async def _seed_rows(db_path: Path, rows: list[ActivityLogRow]) -> None:
    """Insert activity_log rows into the test DB."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()
    await engine.dispose()


@pytest.fixture
async def reader_factory(
    db_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Plain read-write session factory for seeded test DB."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Tests for _execute_query (unit layer, no HTTP)
# ---------------------------------------------------------------------------


class TestQueryActivityLog:
    async def test_empty_table_returns_zero_total(
        self,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        async with reader_factory() as session:
            result = await _execute_query(session, _default_filters())
        assert result.total == 0
        assert result.rows == []
        assert result.has_more is False

    async def test_unfiltered_returns_all_rows(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", entry_at="2026-05-26T09:00:00Z"),
                _make_entry("e-2", entry_at="2026-05-26T09:01:00Z"),
                _make_entry("e-3", entry_at="2026-05-26T09:02:00Z"),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(session, _default_filters())
        assert result.total == 3
        assert len(result.rows) == 3
        # Newest first (entry_at DESC)
        assert result.rows[0].entry_id == "e-3"
        assert result.rows[2].entry_id == "e-1"

    async def test_event_type_filter(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", event_type=EventType.POSITION_OPENED.value),
                _make_entry("e-2", event_type=EventType.ORDER_SUBMITTED.value),
                _make_entry("e-3", event_type=EventType.POSITION_OPENED.value),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(
                session,
                _default_filters(event_type=[EventType.POSITION_OPENED.value]),
            )
        assert result.total == 2
        assert all(r.event_type == EventType.POSITION_OPENED.value for r in result.rows)

    async def test_multi_select_event_type_filter(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", event_type=EventType.POSITION_OPENED.value),
                _make_entry("e-2", event_type=EventType.ORDER_SUBMITTED.value),
                _make_entry("e-3", event_type=EventType.HALT_ACTIVATED.value),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(
                session,
                _default_filters(
                    event_type=[
                        EventType.POSITION_OPENED.value,
                        EventType.ORDER_SUBMITTED.value,
                    ]
                ),
            )
        assert result.total == 2

    async def test_invocation_id_filter(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", invocation_id=_INVOCATION_ID),
                _make_entry("e-2", invocation_id=_INVOCATION_ID_2),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(
                session,
                _default_filters(invocation_id=_INVOCATION_ID_2),
            )
        assert result.total == 1
        assert result.rows[0].invocation_id == _INVOCATION_ID_2

    async def test_source_filter(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", source=EventSource.OPERATOR_CONSOLE.value),
                _make_entry("e-2", source=EventSource.FILL_PROCESSOR.value),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(
                session,
                _default_filters(source=[EventSource.OPERATOR_CONSOLE.value]),
            )
        assert result.total == 1
        assert result.rows[0].source == EventSource.OPERATOR_CONSOLE.value

    async def test_time_range_filter(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", entry_at="2026-05-26T08:00:00Z"),
                _make_entry("e-2", entry_at="2026-05-26T09:00:00Z"),
                _make_entry("e-3", entry_at="2026-05-26T10:00:00Z"),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(
                session,
                _default_filters(
                    time_from="2026-05-26T08:30:00Z",
                    time_to="2026-05-26T09:30:00Z",
                ),
            )
        assert result.total == 1
        assert result.rows[0].entry_id == "e-2"

    async def test_position_id_filter(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        await _seed_rows(
            db_path,
            [
                _make_entry("e-1", position_id="pos-001"),
                _make_entry("e-2", position_id="pos-002"),
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(
                session,
                _default_filters(position_id="pos-001"),
            )
        assert result.total == 1
        assert result.rows[0].position_id == "pos-001"

    async def test_pagination(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        rows = [_make_entry(f"e-{i}", entry_at=f"2026-05-26T09:{i:02d}:00Z") for i in range(5)]
        await _seed_rows(db_path, rows)
        async with reader_factory() as session:
            page1 = await _execute_query(session, _default_filters(page_size=3))
            page2 = await _execute_query(session, _default_filters(page=2, page_size=3))
        assert page1.total == 5
        assert len(page1.rows) == 3
        assert page1.has_more is True
        assert len(page2.rows) == 2
        assert page2.has_more is False

    async def test_detail_json_parsed_into_detail_field(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        detail_payload = {"symbol": "AAPL", "qty": 100, "price": "180.50"}
        await _seed_rows(
            db_path,
            [_make_entry("e-1", detail_json=json.dumps(detail_payload))],
        )
        async with reader_factory() as session:
            result = await _execute_query(session, _default_filters())
        assert len(result.rows) == 1
        assert result.rows[0].detail == detail_payload

    async def test_envelope_id_extracted_from_detail(
        self,
        db_path: Path,
        reader_factory: async_sessionmaker[AsyncSession],  # type: ignore[type-arg]
    ) -> None:
        detail_payload = {"envelope_id": "env-abc123", "verdict": "APPROVED"}
        await _seed_rows(
            db_path,
            [
                _make_entry(
                    "e-1",
                    event_type=EventType.PM_DECISION.value,
                    event_group="PM_DECISION",
                    detail_json=json.dumps(detail_payload),
                )
            ],
        )
        async with reader_factory() as session:
            result = await _execute_query(session, _default_filters())
        assert result.rows[0].envelope_id == "env-abc123"

    async def test_invalid_detail_json_yields_none_detail(
        self,
        db_path: Path,
    ) -> None:
        """Corrupt detail_json does not crash the view -- detail is None."""
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                sqlalchemy.text(
                    "INSERT INTO activity_log "
                    "(entry_id, invocation_id, entry_at, event_type, event_group, "
                    "position_id, order_id, thesis_id, source, detail_json) "
                    "VALUES (:eid, :inv, :eat, :et, :eg, NULL, NULL, NULL, :src, :dj)"
                ),
                {
                    "eid": "e-corrupt",
                    "inv": _INVOCATION_ID,
                    "eat": "2026-05-26T09:00:00Z",
                    "et": EventType.POSITION_OPENED.value,
                    "eg": "POSITION_LIFECYCLE",
                    "src": EventSource.COMMAND_EXECUTOR.value,
                    "dj": "NOT VALID JSON!!!",
                },
            )
            await session.commit()
        await engine.dispose()

        engine2 = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        factory2 = async_sessionmaker(bind=engine2, expire_on_commit=False)
        try:
            async with factory2() as session2:
                result = await _execute_query(session2, _default_filters())
            assert result.rows[0].detail is None
        finally:
            await engine2.dispose()


# ---------------------------------------------------------------------------
# Tests for the FastAPI routes (via TestClient)
# ---------------------------------------------------------------------------


def _build_test_app(db_path: Path) -> FastAPI:
    """Build a minimal FastAPI app that mounts the activity log router.

    Bypasses auth via dependency_overrides -- auth is tested separately in
    ``tests/command_center/auth/``. The ``foreign_reader_session_factory``
    is a plain read-write in-process factory (not the ``?mode=ro`` variant)
    so we can seed rows in the same process.
    """
    app = FastAPI()
    engine_rw = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    rw_factory = async_sessionmaker(bind=engine_rw, expire_on_commit=False)
    app.state.foreign_reader_session_factory = rw_factory
    router = build_activity_log_router()
    app.include_router(router, prefix="/api/views/activity-log")
    app.dependency_overrides[current_session] = lambda: _SESSION_ID
    return app


class TestActivityLogRoutes:
    def test_get_returns_200_with_empty_result(self, db_path: Path) -> None:
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get("/api/views/activity-log")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0
        assert data["rows"] == []
        assert data["has_more"] is False

    def test_get_requires_auth(self, db_path: Path) -> None:
        """Without the override the dependency raises 401/500."""
        app = FastAPI()
        engine_rw = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        rw_factory = async_sessionmaker(bind=engine_rw, expire_on_commit=False)
        app.state.foreign_reader_session_factory = rw_factory
        router = build_activity_log_router()
        app.include_router(router, prefix="/api/views/activity-log")
        # No dependency_overrides -- auth will fail (no session_signing_secret etc.)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/views/activity-log")
        assert response.status_code in (401, 500)

    def test_get_event_types_returns_all_enum_values(self, db_path: Path) -> None:
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get("/api/views/activity-log/event-types")
        assert response.status_code == 200
        data = response.json()
        assert "event_types" in data
        expected = {et.value for et in EventType}
        returned = set(data["event_types"])
        assert expected == returned

    def test_get_saved_filters_returns_operator_actions(self, db_path: Path) -> None:
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get("/api/views/activity-log/saved-filters")
        assert response.status_code == 200
        data = response.json()
        assert "saved_filters" in data
        presets = data["saved_filters"]
        assert len(presets) == 1
        assert presets[0]["id"] == "operator_actions"
        assert presets[0]["label"] == "Operator actions"
        assert "source" in presets[0]["filters"]
        assert EventSource.OPERATOR_CONSOLE.value in presets[0]["filters"]["source"]

    def test_page_size_capped_at_200(self, db_path: Path) -> None:
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get("/api/views/activity-log?page_size=201")
        assert response.status_code == 422

    def test_event_type_filter_via_query_param(self, db_path: Path) -> None:
        """Multi-value event_type filter is accepted as repeated query params."""
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get(
                "/api/views/activity-log",
                params=[
                    ("event_type", EventType.POSITION_OPENED.value),
                    ("event_type", EventType.ORDER_SUBMITTED.value),
                ],
            )
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0  # empty DB

    def test_source_filter_via_query_param(self, db_path: Path) -> None:
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get(
                "/api/views/activity-log",
                params=[("source", EventSource.OPERATOR_CONSOLE.value)],
            )
        assert response.status_code == 200
        assert response.json()["total"] == 0

    def test_response_shape_matches_model(self, db_path: Path) -> None:
        """Response body is a valid ActivityLogPage (all required keys present)."""
        app = _build_test_app(db_path)
        with TestClient(app) as client:
            response = client.get("/api/views/activity-log")
        assert response.status_code == 200
        body = response.json()
        required_keys = {"rows", "total", "page", "page_size", "has_more"}
        assert required_keys.issubset(body.keys())


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
