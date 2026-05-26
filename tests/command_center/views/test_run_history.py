"""Tests for ``command_center.views.history`` (story 05c / ALP-673).

Covers:

* ``GET /api/views/history/runs`` — paginated + filtered list of invocations.
* ``GET /api/views/history/runs/preset/failure-log`` — pre-filtered to
  status ∈ {failed, partial}.

Tests use an on-disk SQLite DB (required by the foreign-reader factory's
``?mode=ro`` constraint on ``:memory:``). Rows are seeded directly via a
plain writer engine (not via the cc writer factory) so this test module
has no dependency on the cc writer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from alphamind.command_center.app import build_app
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.command_center.persistence.tables import CommandCenterBase
from alphamind.persistence.models import Base as ProductionBase
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

_LIFETIME_ID = "lifetime-001"
_LIFETIME_ROW = {
    "process_lifetime_id": _LIFETIME_ID,
    "process_role": "pipeline",
    "process_start_at": "2026-01-01T00:00:00Z",
    "process_pid": 1234,
    "hostname": "testhost",
    "git_sha": "abc123",
    "git_branch": "main",
    "git_dirty": 0,
    "python_version": "3.12.0",
    "pip_freeze_hash": "hash0",
    "pip_freeze_snapshot_path": "/tmp/pip.txt",
    "anthropic_sdk_version": "0.1.0",
    "claude_agent_sdk_version": "0.1.0",
    "os_release": "Windows 10",
}


def _make_invocation(
    invocation_id: str,
    *,
    start_at: str = "2026-01-02T00:00:00Z",
    phase2_completed_at: str | None = "2026-01-02T00:05:00Z",
    trigger_source: str = "pre_open",
    abort_reason: str | None = None,
    command_count: int = 0,
    rejection_count: int = 0,
) -> dict[str, object]:
    """Build a minimal InvocationRow seed dict."""
    summary = {
        "command_count": command_count,
        "rejection_count": rejection_count,
    }
    if abort_reason:
        summary["abort_reason"] = abort_reason

    return {
        "invocation_id": invocation_id,
        "process_lifetime_id": _LIFETIME_ID,
        "start_at": start_at,
        "phase1_completed_at": None,
        "phase2_completed_at": phase2_completed_at,
        "trigger_type": "scheduled",
        "trigger_source": trigger_source,
        "trigger_reason": "test",
        "git_sha_at_invocation": "abc123",
        "active_profile": "medium",
        "active_regime": "normal",
        "active_mode": "normal",
        "active_overlays_json": "[]",
        "resolved_config_hash": "hash1",
        "resolved_config_snapshot_path": "/tmp/cfg.json",
        "feature_flags_snapshot_json": "{}",
        "data_calibration_state_snapshot_path": "/tmp/cal.json",
        "data_source_freshness_json": "{}",
        "fill_collection_summary_json": None,
        "command_execution_summary_json": json.dumps(summary),
        "staleness_flag": None,
        "snapshot_metadata_json": None,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_db(tmp_path: Path) -> AsyncIterator[Path]:
    """On-disk DB with production + cc schemas, seeded with diverse rows."""
    db_path = tmp_path / "test.db"

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(ProductionBase.metadata.create_all)
        await conn.run_sync(CommandCenterBase.metadata.create_all)

    # Seed rows directly so tests don't depend on the cc-writer factory.
    async with engine.begin() as conn:
        await conn.execute(ProcessLifetimeRow.__table__.insert(), _LIFETIME_ROW)
        invocations = [
            _make_invocation(
                "inv-completed",
                start_at="2026-01-02T00:00:00Z",
                phase2_completed_at="2026-01-02T00:05:00Z",
                trigger_source="pre_open",
                command_count=5,
                rejection_count=0,
            ),
            _make_invocation(
                "inv-failed",
                start_at="2026-01-03T00:00:00Z",
                phase2_completed_at=None,
                trigger_source="pre_close",
                abort_reason="timeout",
                command_count=2,
                rejection_count=1,
            ),
            _make_invocation(
                "inv-partial",
                start_at="2026-01-04T00:00:00Z",
                phase2_completed_at="2026-01-04T00:10:00Z",
                trigger_source="pre_open",
                abort_reason="model_api_error",
                command_count=3,
                rejection_count=2,
            ),
            _make_invocation(
                "inv-no-cmds",
                start_at="2026-01-05T00:00:00Z",
                phase2_completed_at="2026-01-05T00:03:00Z",
                trigger_source="off_hours_rolling",
                command_count=0,
                rejection_count=0,
            ),
            _make_invocation(
                "inv-many-cmds",
                start_at="2026-01-06T00:00:00Z",
                phase2_completed_at="2026-01-06T00:08:00Z",
                trigger_source="pre_open",
                command_count=12,
                rejection_count=0,
            ),
        ]
        await conn.execute(InvocationRow.__table__.insert(), invocations)

    await engine.dispose()
    yield db_path


@pytest.fixture
def configs(tmp_path: Path, seeded_db: Path) -> tuple[Path, Path]:
    """Per-test config dir pointing at the seeded DB."""
    repo_config = Path(__file__).parents[3] / "config"
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: "{seeded_db}"
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
    return tmp_path, seeded_db


@pytest.fixture
def client(configs: tuple[Path, Path]) -> TestClient:
    """TestClient wired to the full app with the seeded DB."""
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
    )
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Tests: GET /api/views/history/runs
# ---------------------------------------------------------------------------


class TestRunHistory:
    def test_returns_200_with_all_rows(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 5

    def test_response_contains_documented_columns(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        item = resp.json()["items"][0]
        expected_keys = {
            "invocation_id",
            "started_at",
            "ended_at",
            "duration_seconds",
            "run_type",
            "status",
            "command_count",
            "rejection_count",
            "abort_reason",
        }
        assert expected_keys <= set(item.keys())

    def test_default_sort_started_at_desc(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs")
        items = resp.json()["items"]
        started_ats = [it["started_at"] for it in items]
        assert started_ats == sorted(started_ats, reverse=True)

    def test_filter_by_status_completed(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=completed")
        body = resp.json()
        assert all(it["status"] == "completed" for it in body["items"])
        ids = {it["invocation_id"] for it in body["items"]}
        assert "inv-completed" in ids
        assert "inv-many-cmds" in ids
        assert "inv-no-cmds" in ids

    def test_filter_by_status_failed(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=failed")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-failed"

    def test_filter_by_status_partial(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=partial")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-partial"

    def test_filter_by_multiple_status(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=failed&status=partial")
        body = resp.json()
        assert body["total"] == 2
        ids = {it["invocation_id"] for it in body["items"]}
        assert ids == {"inv-failed", "inv-partial"}

    def test_filter_by_run_type(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?run_type=off_hours_rolling")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-no-cmds"

    def test_filter_date_from(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?date_from=2026-01-04T00:00:00Z")
        body = resp.json()
        assert body["total"] == 3
        ids = {it["invocation_id"] for it in body["items"]}
        assert ids == {"inv-partial", "inv-no-cmds", "inv-many-cmds"}

    def test_filter_date_to(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?date_to=2026-01-02T23:59:59Z")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-completed"

    def test_filter_commands_gt(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?commands_gt=5")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-many-cmds"

    def test_filter_has_errors_true(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?has_errors=true")
        body = resp.json()
        ids = {it["invocation_id"] for it in body["items"]}
        assert "inv-failed" in ids
        assert "inv-partial" in ids
        assert "inv-completed" not in ids

    def test_filter_has_errors_false(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?has_errors=false")
        body = resp.json()
        for item in body["items"]:
            assert item["abort_reason"] is None
            assert item["rejection_count"] == 0

    def test_pagination_page_size(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page=1&page_size=2")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["page"] == 1
        assert body["page_size"] == 2

    def test_pagination_page_2(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page=2&page_size=2")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 2
        assert body["page"] == 2

    def test_pagination_last_page_partial(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page=3&page_size=2")
        body = resp.json()
        assert body["total"] == 5
        assert len(body["items"]) == 1

    def test_page_size_cap_at_100(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?page_size=200")
        assert resp.status_code == 422  # FastAPI rejects > max

    def test_duration_seconds_computed(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=completed")
        items = resp.json()["items"]
        completed = next(it for it in items if it["invocation_id"] == "inv-completed")
        assert completed["duration_seconds"] == pytest.approx(300.0)

    def test_abort_reason_populated(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs?status=failed")
        item = resp.json()["items"][0]
        assert item["abort_reason"] == "timeout"


# ---------------------------------------------------------------------------
# Tests: GET /api/views/history/runs/preset/failure-log
# ---------------------------------------------------------------------------


class TestFailureLogPreset:
    def test_returns_only_failed_and_partial(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        statuses = {it["status"] for it in body["items"]}
        assert statuses <= {"failed", "partial"}

    def test_completed_rows_excluded(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log")
        ids = {it["invocation_id"] for it in resp.json()["items"]}
        assert "inv-completed" not in ids
        assert "inv-no-cmds" not in ids
        assert "inv-many-cmds" not in ids

    def test_preset_accepts_date_filter(self, client: TestClient) -> None:
        resp = client.get(
            "/api/views/history/runs/preset/failure-log?date_from=2026-01-04T00:00:00Z"
        )
        body = resp.json()
        # Only inv-partial is on 2026-01-04; inv-failed is 2026-01-03
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-partial"

    def test_preset_accepts_run_type_filter(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log?run_type=pre_open")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["invocation_id"] == "inv-partial"

    def test_preset_pagination(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/preset/failure-log?page=1&page_size=1")
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 1
