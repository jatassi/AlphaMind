"""Tests for ``command_center.views.history`` run history endpoints (ALP-673).

Covers:

* ``GET /api/views/history/runs`` — paginated list with filter dimensions.
* ``GET /api/views/history/runs/preset/failure-log`` — status ∈ {failed,
  partial} preset.

Uses an in-memory SQLite DB seeded with diverse rows so each filter
dimension is exercised against real data.

All tests use the ``TestClient`` backed by the ``build_app``-assembled
application with injected session factories. The ``foreign_reader_session``
is replaced by a thin test double that reads from the same in-memory DB
the writer seeded. Because SQLite ``?mode=ro`` rejects in-memory databases,
the test builds both a writer and a reader against the same on-disk
per-test file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from alphamind.command_center.app import build_app
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.persistence.models import Base as ProductionBase
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

_PROCESS_LIFETIME_ID = "plt-history-test"


def _make_row(
    *,
    invocation_id: str,
    start_at: str,
    trigger_source: str = "market_open",
    fill_collection_completed_at: str | None = None,
    command_execution_completed_at: str | None = None,
    command_execution_summary_json: str | None = None,
    snapshot_metadata_json: str | None = None,
    staleness_flag: int | None = None,
) -> InvocationRow:
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=start_at,
        fill_collection_completed_at=fill_collection_completed_at,
        command_execution_completed_at=command_execution_completed_at,
        trigger_type="scheduled",
        trigger_source=trigger_source,
        trigger_reason="test",
        git_sha_at_invocation="abc123",
        active_profile="default",
        active_regime="normal",
        active_mode="normal",
        active_overlays_json="[]",
        resolved_config_hash="hash",
        resolved_config_snapshot_path="/dev/null",
        feature_flags_snapshot_json="{}",
        data_calibration_state_snapshot_path="/dev/null",
        data_source_freshness_json="{}",
        fill_collection_summary_json=None,
        command_execution_summary_json=command_execution_summary_json,
        staleness_flag=staleness_flag,
        snapshot_metadata_json=snapshot_metadata_json,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_db(tmp_path: Path) -> AsyncIterator[Path]:
    """Create and seed an on-disk SQLite DB; yield the path."""
    db_path = tmp_path / "test_history.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(ProductionBase.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                process_role="monitor",
                process_start_at="2026-01-01T00:00:00Z",
                process_pid=1,
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
        # Row 1: completed, market_open, 5 commands submitted, 0 rejected
        session.add(
            _make_row(
                invocation_id="inv-001",
                start_at="2026-05-01T09:00:00",
                trigger_source="market_open",
                fill_collection_completed_at="2026-05-01T09:05:00",
                command_execution_completed_at="2026-05-01T09:10:00",
                command_execution_summary_json='{"commands_submitted": 5, "commands_rejected": 0}',
            )
        )
        # Row 2: partial (phase1 done, phase2 missing), market_hours_rolling
        session.add(
            _make_row(
                invocation_id="inv-002",
                start_at="2026-05-02T10:00:00",
                trigger_source="market_hours_rolling",
                fill_collection_completed_at="2026-05-02T10:05:00",
                command_execution_completed_at=None,
            )
        )
        # Row 3: failed (both phases missing), pre_close
        session.add(
            _make_row(
                invocation_id="inv-003",
                start_at="2026-05-03T15:00:00",
                trigger_source="pre_close",
                fill_collection_completed_at=None,
                command_execution_completed_at=None,
                snapshot_metadata_json='{"abort_reason": "context-overflow"}',
            )
        )
        # Row 4: completed, market_open, 3 commands submitted, 2 rejected
        session.add(
            _make_row(
                invocation_id="inv-004",
                start_at="2026-05-04T09:00:00",
                trigger_source="market_open",
                fill_collection_completed_at="2026-05-04T09:05:00",
                command_execution_completed_at="2026-05-04T09:11:00",
                command_execution_summary_json='{"commands_submitted": 3, "commands_rejected": 2}',
            )
        )
        # Row 5: partial, emergency
        session.add(
            _make_row(
                invocation_id="inv-005",
                start_at="2026-05-05T12:00:00",
                trigger_source="emergency",
                fill_collection_completed_at="2026-05-05T12:03:00",
                command_execution_completed_at=None,
            )
        )
        await session.commit()

    try:
        yield db_path
    finally:
        await engine.dispose()


@pytest.fixture
def configs(tmp_path: Path, seeded_db: Path) -> tuple[Path, Path]:
    """Return (config_dir, db_path) for building the test app."""
    repo_config = Path(__file__).parents[3] / "config"
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: '{seeded_db}'
frontend:
  dist_path: '{tmp_path}/dist-does-not-exist'
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
    return tmp_path, seeded_db


@pytest.fixture
def client(configs: tuple[Path, Path]) -> Generator[TestClient]:
    """TestClient backed by the full app with the seeded DB.

    Uses the ``with TestClient(app)`` context manager form so the
    lifespan runs and the ``foreign_reader_session_factory`` lands on
    ``app.state`` before any request is issued.
    """
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: basic list endpoint
# ---------------------------------------------------------------------------


class TestRunHistoryList:
    def test_all_rows_returned_by_default(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 5
        assert body["page"] == 1
        assert body["page_size"] == 50

    def test_default_sort_newest_first(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = resp.json()["items"]
        started_ats = [i["started_at"] for i in items]
        assert started_ats == sorted(started_ats, reverse=True)

    def test_columns_present(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        item = resp.json()["items"][0]
        for key in (
            "invocation_id",
            "started_at",
            "ended_at",
            "duration_seconds",
            "run_type",
            "status",
            "commands_submitted",
            "commands_rejected",
            "abort_reason",
        ):
            assert key in item, f"missing column {key!r}"

    def test_status_completed_derived(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = {i["invocation_id"]: i for i in resp.json()["items"]}
        assert items["inv-001"]["status"] == "completed"
        assert items["inv-001"]["ended_at"] == "2026-05-01T09:10:00"
        assert items["inv-001"]["duration_seconds"] == pytest.approx(10 * 60)

    def test_status_partial_derived(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = {i["invocation_id"]: i for i in resp.json()["items"]}
        assert items["inv-002"]["status"] == "partial"
        assert items["inv-002"]["ended_at"] == "2026-05-02T10:05:00"

    def test_status_failed_derived(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = {i["invocation_id"]: i for i in resp.json()["items"]}
        assert items["inv-003"]["status"] == "failed"
        assert items["inv-003"]["ended_at"] is None
        assert items["inv-003"]["abort_reason"] == "context-overflow"

    def test_commands_summary_extracted(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = {i["invocation_id"]: i for i in resp.json()["items"]}
        assert items["inv-001"]["commands_submitted"] == 5
        assert items["inv-001"]["commands_rejected"] == 0
        assert items["inv-004"]["commands_submitted"] == 3
        assert items["inv-004"]["commands_rejected"] == 2

    def test_run_type_column(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = {i["invocation_id"]: i for i in resp.json()["items"]}
        assert items["inv-001"]["run_type"] == "market_open"
        assert items["inv-002"]["run_type"] == "market_hours_rolling"


class TestRunHistoryFilters:
    def test_filter_by_status_completed(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=completed")
        body = resp.json()
        assert body["total"] == 2
        assert all(i["status"] == "completed" for i in body["items"])

    def test_filter_by_status_partial(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=partial")
        body = resp.json()
        assert body["total"] == 2
        assert all(i["status"] == "partial" for i in body["items"])

    def test_filter_by_status_failed(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=failed")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-003"

    def test_filter_by_multiple_statuses(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=failed&status=partial")
        body = resp.json()
        assert body["total"] == 3
        statuses = {i["status"] for i in body["items"]}
        assert statuses == {"failed", "partial"}

    def test_filter_by_run_type(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?run_type=market_open")
        body = resp.json()
        assert body["total"] == 2
        assert all(i["run_type"] == "market_open" for i in body["items"])

    def test_filter_date_from(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?date_from=2026-05-04")
        body = resp.json()
        assert body["total"] == 2
        for item in body["items"]:
            assert item["started_at"] >= "2026-05-04"

    def test_filter_date_to(self, client: TestClient) -> None:
        # Use a full timestamp so the string comparison includes inv-002
        # (start_at="2026-05-02T10:00:00"). A bare date "2026-05-02" would
        # be lexicographically less than "2026-05-02T..." so inv-002 would
        # be excluded.
        resp = client.get("/api/views/history/runs?date_to=2026-05-02T23:59:59")
        body = resp.json()
        assert body["total"] == 2

    def test_filter_commands_gt(self, client: TestClient) -> None:
        # Only inv-001 (5 submitted) and inv-004 (3 submitted) have > 2
        resp = client.get("/api/views/history/runs?commands_gt=2")
        body = resp.json()
        assert body["total"] == 2
        ids = {i["invocation_id"] for i in body["items"]}
        assert ids == {"inv-001", "inv-004"}

    def test_filter_has_errors_true(self, client: TestClient) -> None:
        # Only inv-004 has rejected commands
        resp = client.get("/api/views/history/runs?has_errors=true")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-004"

    def test_filter_has_errors_false(self, client: TestClient) -> None:
        # inv-001 (0 rejected), inv-002/003/005 (no summary json)
        resp = client.get("/api/views/history/runs?has_errors=false")
        body = resp.json()
        # inv-004 excluded; all others pass
        ids = {i["invocation_id"] for i in body["items"]}
        assert "inv-004" not in ids


class TestRunHistoryPagination:
    def test_pagination_first_page(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page=1&page_size=2")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["page"] == 1
        assert body["page_size"] == 2

    def test_pagination_last_page(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page=3&page_size=2")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 1  # only 1 row on the last page

    def test_page_size_cap_respected(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page_size=200")
        # FastAPI Query(le=100) returns 422 for page_size > 100
        assert resp.status_code == 422

    def test_page_size_zero_rejected(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page_size=0")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: failure-log preset endpoint
# ---------------------------------------------------------------------------


class TestFailureLogPreset:
    def test_returns_failed_and_partial_only(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        statuses = {i["status"] for i in body["items"]}
        assert statuses == {"failed", "partial"}

    def test_no_completed_rows(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log")
        body = resp.json()
        assert not any(i["status"] == "completed" for i in body["items"])

    def test_preset_date_from_filter_applies(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log?date_from=2026-05-03")
        body = resp.json()
        # inv-003 (failed, 05-03), inv-005 (partial, 05-05) — both >= 05-03;
        # inv-002 (partial, 05-02) is excluded.
        assert body["total"] == 2

    def test_preset_run_type_filter_applies(self, client: TestClient) -> None:
        resp = client.get(
            "/api/views/history/runs/preset/failure-log?run_type=market_hours_rolling"
        )
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-002"

    def test_preset_pagination(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log?page=1&page_size=1")
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 1


# ---------------------------------------------------------------------------
# anyio backend
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
