"""Tests for the activity-log view endpoints (story 05e / ALP-675).

Covers:

* ``GET /api/views/activity-log/event-types`` — returns the EventType enum.
* ``GET /api/views/activity-log/saved-filters`` — returns the "Operator
  actions" preset.
* ``GET /api/views/activity-log`` — paginated rows with all filter dimensions:
  event_type, invocation_id, position_id, thesis_id, order_id, source,
  time_from, time_to, page, page_size.

All tests use an on-disk SQLite file (required because the
``foreign_reader_session_factory`` rejects ``:memory:`` — SQLite rejects
``?mode=ro`` on in-memory databases).  The writer session uses
``?check_same_thread=False`` aiosqlite so it can seed rows in a different
async context from the reader.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alphamind.command_center.app import build_app
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.persistence.models import Base
from alphamind.portfolio_state.events.types import EventSource, EventType
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_INVOCATION_ID = "inv-test-001"
_PROCESS_LIFETIME_ID = "plt-test-001"


def _make_config_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write per-test YAML files into ``tmp_path``; return the dir."""
    repo_config = pathlib.Path(__file__).parents[3] / "config"
    db = tmp_path / "test.db"
    db.touch()
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: '{db}'
frontend:
  dist_path: "{tmp_path}/dist-does-not-exist"
pipeline:
  control_url: "http://127.0.0.1:8765"
  events_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
  events_url: "http://127.0.0.1:8766"
""",
        encoding="utf-8",
    )
    (tmp_path / "security.yaml").write_bytes((repo_config / "security.yaml").read_bytes())
    (tmp_path / "alerts.yaml").write_bytes((repo_config / "alerts.yaml").read_bytes())
    return tmp_path


def _make_row(
    entry_id: str,
    *,
    event_type: str = EventType.PM_DECISION,
    event_group: str = "PM_DECISION",
    source: str = EventSource.COMMAND_EXECUTOR,
    entry_at: str = "2026-01-01T00:00:00Z",
    position_id: str | None = None,
    thesis_id: str | None = None,
    order_id: str | None = None,
) -> ActivityLogRow:
    """Build a minimal ActivityLogRow for seeding tests."""
    return ActivityLogRow(
        entry_id=entry_id,
        invocation_id=_INVOCATION_ID,
        entry_at=entry_at,
        event_type=event_type,
        event_group=event_group,
        position_id=position_id,
        thesis_id=thesis_id,
        order_id=order_id,
        source=source,
        detail_json=json.dumps({"stub": True}),
    )


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    """Create an on-disk SQLite DB seeded with the required FK rows."""
    return tmp_path / "seed.db"


@pytest.fixture
async def seeded_db(db_path: pathlib.Path) -> pathlib.Path:
    """Seed the on-disk DB with a process lifetime + invocation."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                process_role="pipeline",
                process_start_at="2026-01-01T00:00:00Z",
                process_pid=1,
                hostname="test-host",
                git_sha="abc123",
                git_branch="main",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="a" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="0.0.0",
                claude_agent_sdk_version="0.0.0",
                os_release="test-os",
            )
        )
        session.add(
            InvocationRow(
                invocation_id=_INVOCATION_ID,
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                start_at="2026-01-01T00:00:00Z",
                trigger_type="manual",
                trigger_source="test",
                trigger_reason="test",
                git_sha_at_invocation="abc123",
                active_profile="default",
                active_regime="normal",
                active_mode="normal",
                active_overlays_json="{}",
                resolved_config_hash="a" * 64,
                resolved_config_snapshot_path="/dev/null",
                feature_flags_snapshot_json="{}",
                data_calibration_state_snapshot_path="/dev/null",
                data_source_freshness_json="{}",
            )
        )
        await session.commit()
    await engine.dispose()
    return db_path


@pytest.fixture
async def seeded_db_with_rows(seeded_db: pathlib.Path) -> pathlib.Path:
    """Add activity_log rows to the already-seeded DB."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{seeded_db}")
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        bind=engine, expire_on_commit=False
    )
    async with factory() as session:
        session.add(
            _make_row(
                "e-001",
                event_type=EventType.PM_DECISION,
                event_group="PM_DECISION",
                source=EventSource.COMMAND_EXECUTOR,
                entry_at="2026-01-01T10:00:00Z",
            )
        )
        session.add(
            _make_row(
                "e-002",
                event_type=EventType.POSITION_OPENED,
                event_group="POSITION_LIFECYCLE",
                source=EventSource.FILL_PROCESSOR,
                entry_at="2026-01-01T11:00:00Z",
                position_id="pos-001",
            )
        )
        session.add(
            _make_row(
                "e-003",
                event_type=EventType.ORDER_SUBMITTED,
                event_group="ORDER_LIFECYCLE",
                source=EventSource.OPERATOR_CONSOLE,
                entry_at="2026-01-01T12:00:00Z",
                order_id="ord-001",
            )
        )
        session.add(
            _make_row(
                "e-004",
                event_type=EventType.THESIS_CREATED,
                event_group="THESIS",
                source=EventSource.COMMAND_EXECUTOR,
                entry_at="2026-01-01T09:00:00Z",
                thesis_id="the-001",
            )
        )
        await session.commit()
    await engine.dispose()
    return seeded_db


def _build_test_client(config_dir: pathlib.Path) -> TestClient:
    """Build a TestClient from the per-test config dir."""
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests — event-types endpoint
# ---------------------------------------------------------------------------


class TestEventTypesEndpoint:
    def test_returns_all_event_type_values(self, tmp_path: pathlib.Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log/event-types")
        assert resp.status_code == 200
        data = resp.json()
        assert "event_types" in data
        assert set(data["event_types"]) == {e.value for e in EventType}

    def test_response_is_list_of_strings(self, tmp_path: pathlib.Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log/event-types")
        data = resp.json()
        assert all(isinstance(v, str) for v in data["event_types"])


# ---------------------------------------------------------------------------
# Tests — saved-filters endpoint
# ---------------------------------------------------------------------------


class TestSavedFiltersEndpoint:
    def test_returns_operator_actions_preset(self, tmp_path: pathlib.Path) -> None:
        config_dir = _make_config_dir(tmp_path)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log/saved-filters")
        assert resp.status_code == 200
        data = resp.json()
        assert "saved_filters" in data
        presets = data["saved_filters"]
        assert len(presets) == 1
        assert presets[0]["name"] == "Operator actions"
        assert presets[0]["params"]["source"] == [EventSource.OPERATOR_CONSOLE.value]


# ---------------------------------------------------------------------------
# Tests — main paginated endpoint
# ---------------------------------------------------------------------------


class TestActivityLogEndpoint:
    def test_empty_db_returns_zero_rows(
        self,
        seeded_db: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        # Override DB path to the seeded on-disk file.
        _patch_db_path(config_dir, seeded_db)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["rows"] == []
        assert data["has_more"] is False

    def test_returns_all_rows_unfiltered(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log")
        data = resp.json()
        assert data["total"] == 4

    def test_rows_ordered_newest_first(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log")
        rows = resp.json()["rows"]
        timestamps = [r["entry_at"] for r in rows]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_filter_by_single_event_type(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"event_type": EventType.PM_DECISION},
            )
        data = resp.json()
        assert data["total"] == 1
        assert data["rows"][0]["event_type"] == EventType.PM_DECISION

    def test_filter_by_multi_event_type(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params=[
                    ("event_type", EventType.PM_DECISION),
                    ("event_type", EventType.POSITION_OPENED),
                ],
            )
        data = resp.json()
        assert data["total"] == 2

    def test_filter_by_invocation_id(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"invocation_id": _INVOCATION_ID},
            )
        data = resp.json()
        assert data["total"] == 4

    def test_filter_by_nonexistent_invocation_id_returns_empty(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"invocation_id": "inv-does-not-exist"},
            )
        data = resp.json()
        assert data["total"] == 0

    def test_filter_by_position_id(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"position_id": "pos-001"},
            )
        data = resp.json()
        assert data["total"] == 1
        assert data["rows"][0]["position_id"] == "pos-001"

    def test_filter_by_thesis_id(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"thesis_id": "the-001"},
            )
        data = resp.json()
        assert data["total"] == 1
        assert data["rows"][0]["thesis_id"] == "the-001"

    def test_filter_by_order_id(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"order_id": "ord-001"},
            )
        data = resp.json()
        assert data["total"] == 1
        assert data["rows"][0]["order_id"] == "ord-001"

    def test_filter_by_source(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"source": EventSource.OPERATOR_CONSOLE},
            )
        data = resp.json()
        assert data["total"] == 1
        assert data["rows"][0]["source"] == EventSource.OPERATOR_CONSOLE

    def test_filter_by_time_from(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"time_from": "2026-01-01T10:30:00Z"},
            )
        data = resp.json()
        # e-002 (11:00) + e-003 (12:00) = 2
        assert data["total"] == 2

    def test_filter_by_time_to(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"time_to": "2026-01-01T10:30:00Z"},
            )
        data = resp.json()
        # e-001 (10:00) + e-004 (09:00) = 2
        assert data["total"] == 2

    def test_pagination_page_size(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"page_size": 2},
            )
        data = resp.json()
        assert len(data["rows"]) == 2
        assert data["total"] == 4
        assert data["has_more"] is True

    def test_pagination_second_page(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"page": 2, "page_size": 2},
            )
        data = resp.json()
        assert len(data["rows"]) == 2
        assert data["has_more"] is False

    def test_page_size_enforced_at_200(
        self,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        with _build_test_client(config_dir) as client:
            resp = client.get(
                "/api/views/activity-log",
                params={"page_size": 201},
            )
        assert resp.status_code == 422

    def test_row_has_required_fields(
        self,
        seeded_db_with_rows: pathlib.Path,
        tmp_path: pathlib.Path,
    ) -> None:
        config_dir = _make_config_dir(tmp_path)
        _patch_db_path(config_dir, seeded_db_with_rows)
        with _build_test_client(config_dir) as client:
            resp = client.get("/api/views/activity-log", params={"page_size": 1})
        row = resp.json()["rows"][0]
        required = {
            "entry_id",
            "invocation_id",
            "entry_at",
            "event_type",
            "event_group",
            "source",
            "detail_json",
        }
        assert required.issubset(set(row.keys()))
        # detail_json must be valid JSON
        assert json.loads(row["detail_json"]) == {"stub": True}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_db_path(config_dir: pathlib.Path, db_path: pathlib.Path) -> None:
    """Overwrite the command-center.yaml DB path to point at ``db_path``."""
    cc_yaml = config_dir / "command-center.yaml"
    text = cc_yaml.read_text(encoding="utf-8")
    import re

    text = re.sub(r"alphamind_db_path:.*", f"alphamind_db_path: '{db_path}'", text)
    cc_yaml.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
