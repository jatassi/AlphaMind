"""Tests for ``command_center.views.history`` per-invocation detail endpoints (ALP-674).

Covers:

* ``GET /api/views/history/runs/{invocation_id}`` — full detail with header +
  archive sections + activity-log panes.
* ``GET /api/views/history/runs/{invocation_id}/archive/{section}/{filename}``
  — archive-file streaming with path-traversal protection.
* ``GET /api/views/brief-retrieval`` — brief-retrieval store sections keyed by
  reference-ID prefix.

Uses an in-memory SQLite DB seeded via the same fixture pattern as
``test_run_history.py``.  Archive-file tests write temporary files into a
``tmp_path``-scoped directory that is injected via the
``_archive_base_dir`` monkeypatch.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Generator
from pathlib import Path
from unittest.mock import patch

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
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.invocations import InvocationRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

_PROCESS_LIFETIME_ID = "plt-detail-test"


def _make_invocation(
    *,
    invocation_id: str,
    start_at: str,
    trigger_source: str = "market_open",
    phase1_completed_at: str | None = None,
    phase2_completed_at: str | None = None,
    snapshot_metadata_json: str | None = None,
) -> InvocationRow:
    return InvocationRow(
        invocation_id=invocation_id,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        start_at=start_at,
        phase1_completed_at=phase1_completed_at,
        phase2_completed_at=phase2_completed_at,
        trigger_type="scheduled",
        trigger_source=trigger_source,
        trigger_reason="test-detail",
        git_sha_at_invocation="deadbeef",
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
        command_execution_summary_json=None,
        staleness_flag=None,
        snapshot_metadata_json=snapshot_metadata_json,
    )


def _make_activity_row(
    *,
    entry_id: str,
    invocation_id: str,
    entry_at: str,
    event_type: str,
    event_group: str,
    source: str = "FILL_PROCESSOR",
    detail_json: str = "{}",
) -> ActivityLogRow:
    return ActivityLogRow(
        entry_id=entry_id,
        invocation_id=invocation_id,
        entry_at=entry_at,
        event_type=event_type,
        event_group=event_group,
        position_id=None,
        order_id=None,
        thesis_id=None,
        source=source,
        detail_json=detail_json,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_db(tmp_path: Path) -> AsyncIterator[Path]:
    """Create and seed an on-disk SQLite DB; yield the path."""
    db_path = tmp_path / "test_detail.db"
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
                git_sha="deadbeef",
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
        # Completed invocation with PM and command/fill activity-log entries.
        session.add(
            _make_invocation(
                invocation_id="inv-detail-001",
                start_at="2026-05-10T09:00:00",
                trigger_source="market_open",
                phase1_completed_at="2026-05-10T09:05:00",
                phase2_completed_at="2026-05-10T09:10:00",
                snapshot_metadata_json='{"abort_reason": null, "error_summary": "none"}',
            )
        )
        # Failed invocation with abort reason.
        session.add(
            _make_invocation(
                invocation_id="inv-detail-002",
                start_at="2026-05-11T10:00:00",
                trigger_source="pre_close",
                snapshot_metadata_json='{"abort_reason": "context-overflow"}',
            )
        )
        # Activity-log entries for inv-detail-001.
        session.add(
            _make_activity_row(
                entry_id="e-pm-001",
                invocation_id="inv-detail-001",
                entry_at="2026-05-10T09:08:00",
                event_type="PM_DECISION",
                event_group="PM_DECISION",
                source="GUARDRAIL_LAYER",
                detail_json=json.dumps({"verdict": "APPROVE", "commands": []}),
            )
        )
        session.add(
            _make_activity_row(
                entry_id="e-order-001",
                invocation_id="inv-detail-001",
                entry_at="2026-05-10T09:09:00",
                event_type="ORDER_SUBMITTED",
                event_group="ORDER_LIFECYCLE",
                source="COMMAND_EXECUTOR",
                detail_json=json.dumps({"order_id": "ord-001"}),
            )
        )
        session.add(
            _make_activity_row(
                entry_id="e-fill-001",
                invocation_id="inv-detail-001",
                entry_at="2026-05-10T09:09:30",
                event_type="ORDER_FILLED",
                event_group="ORDER_LIFECYCLE",
                source="COMMAND_EXECUTOR",
                detail_json=json.dumps({"order_id": "ord-001"}),
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
    config_dir, _ = configs
    app = build_app(
        command_center_config=load_command_center_config(config_dir),
        security_config=load_security_config(config_dir),
        alerts_config=load_alerts_config(config_dir),
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Invocation detail endpoint tests
# ---------------------------------------------------------------------------


class TestInvocationDetailHeader:
    def test_returns_200_for_existing_invocation(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        assert resp.status_code == 200

    def test_returns_404_for_missing_invocation(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/does-not-exist")
        assert resp.status_code == 404

    def test_header_fields_present(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        body = resp.json()
        header = body["header"]
        for key in (
            "invocation_id",
            "run_type",
            "started_at",
            "ended_at",
            "status",
            "phase1_completed_at",
            "phase2_completed_at",
            "duration_seconds",
            "trigger_type",
            "trigger_reason",
            "git_sha",
            "active_profile",
            "active_regime",
            "active_mode",
            "abort_reason",
            "error_summary",
        ):
            assert key in header, f"missing header field {key!r}"

    def test_header_status_completed(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        header = resp.json()["header"]
        assert header["status"] == "completed"
        assert header["invocation_id"] == "inv-detail-001"
        assert header["run_type"] == "market_open"

    def test_header_status_failed_with_abort_reason(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-002")
        header = resp.json()["header"]
        assert header["status"] == "failed"
        assert header["abort_reason"] == "context-overflow"

    def test_header_duration_seconds(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        header = resp.json()["header"]
        assert header["duration_seconds"] == pytest.approx(10 * 60)

    def test_archive_sections_present_in_response(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        body = resp.json()
        # archive_sections list is always present (may be empty).
        assert "archive_sections" in body
        assert isinstance(body["archive_sections"], list)

    def test_pm_entries_present(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        body = resp.json()
        pm = body["pm_entries"]
        assert len(pm) == 1
        assert pm[0]["event_type"] == "PM_DECISION"
        assert pm[0]["entry_id"] == "e-pm-001"

    def test_command_fill_entries_present(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-001")
        body = resp.json()
        cf = body["command_fill_entries"]
        assert len(cf) == 2
        types = {e["event_type"] for e in cf}
        assert types == {"ORDER_SUBMITTED", "ORDER_FILLED"}

    def test_no_cross_invocation_leakage(self, client: TestClient) -> None:
        resp = client.get("/api/views/history/runs/inv-detail-002")
        body = resp.json()
        # inv-detail-002 has no activity-log entries.
        assert body["pm_entries"] == []
        assert body["command_fill_entries"] == []


# ---------------------------------------------------------------------------
# Tests: archive-file streaming with path-traversal guard
# ---------------------------------------------------------------------------


class TestArchiveFileStreaming:
    """Tests for archive-file streaming use a patched archive base dir."""

    def _make_archive(self, tmp_path: Path, invocation_id: str) -> Path:
        """Create a minimal archive directory tree under tmp_path."""
        # Use a fixed timestamp that matches inv-detail-001's start_at.
        # The resolver scans for directories starting with "09-00-00".
        date_dir = tmp_path / "archive" / "2026-05-10"
        inv_dir = date_dir / "09-00-00_market_open"
        (inv_dir / "distillation").mkdir(parents=True)
        (inv_dir / "analysis").mkdir()
        (inv_dir / "distillation" / "tech_sector.md").write_text(
            "# Tech distillation\nSome content.",
            encoding="utf-8",
        )
        (inv_dir / "analysis" / "synthesis.md").write_text(
            "# Synthesis\n[CR-1] First finding.\n[CR-2] Second finding.",
            encoding="utf-8",
        )
        return tmp_path / "archive"

    def test_streams_existing_file(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path, "inv-detail-001")
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            resp = client.get(
                "/api/views/history/runs/inv-detail-001/archive/distillation/tech_sector.md"
            )
        assert resp.status_code == 200
        assert "Tech distillation" in resp.text

    def test_404_for_missing_invocation(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path, "inv-detail-001")
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            resp = client.get(
                "/api/views/history/runs/no-such-inv/archive/distillation/tech_sector.md"
            )
        assert resp.status_code == 404

    def test_404_when_archive_absent(self, client: TestClient, tmp_path: Path) -> None:
        # Point archive base at a directory that exists but has no inv dir.
        empty_base = tmp_path / "empty_archive"
        empty_base.mkdir()
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=empty_base,
        ):
            resp = client.get(
                "/api/views/history/runs/inv-detail-001/archive/distillation/tech_sector.md"
            )
        assert resp.status_code == 404

    def test_404_for_missing_file(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path, "inv-detail-001")
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            resp = client.get(
                "/api/views/history/runs/inv-detail-001/archive/distillation/no_file.md"
            )
        assert resp.status_code == 404

    def test_path_traversal_rejected(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path, "inv-detail-001")
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            # Attempt directory traversal via section segment.
            resp = client.get(
                "/api/views/history/runs/inv-detail-001/archive/distillation/..%2F..%2Fetc%2Fpasswd"
            )
        assert resp.status_code in {400, 404}


# ---------------------------------------------------------------------------
# Brief-retrieval endpoint tests
# ---------------------------------------------------------------------------


class TestBriefRetrieval:
    def _make_archive(self, tmp_path: Path) -> Path:
        date_dir = tmp_path / "archive" / "2026-05-10"
        inv_dir = date_dir / "09-00-00_market_open"
        (inv_dir / "analysis").mkdir(parents=True)
        (inv_dir / "analysis" / "synthesis.md").write_text(
            "## CR-1\nFirst correlation finding.\n## CR-2\nSecond correlation finding.",
            encoding="utf-8",
        )
        (inv_dir / "analysis" / "qualitative_brief.md").write_text(
            "## QR-1\nFirst qualitative item.\n## QR-2\nSecond qualitative item.",
            encoding="utf-8",
        )
        return tmp_path / "archive"

    def test_returns_200_with_valid_prefix(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path)
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            resp = client.get(
                "/api/views/history/brief-retrieval?invocation_id=inv-detail-001&ref_prefix=CR"
            )
        assert resp.status_code == 200

    def test_sections_keyed_by_prefix(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path)
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            resp = client.get(
                "/api/views/history/brief-retrieval?invocation_id=inv-detail-001&ref_prefix=CR"
            )
        body = resp.json()
        assert body["ref_prefix"] == "CR"
        assert body["invocation_id"] == "inv-detail-001"
        sections = body["sections"]
        assert len(sections) >= 2
        ref_ids = [s["ref_id"] for s in sections]
        assert "CR-1" in ref_ids
        assert "CR-2" in ref_ids

    def test_qr_prefix_returns_qualitative(self, client: TestClient, tmp_path: Path) -> None:
        archive_base = self._make_archive(tmp_path)
        with patch(
            "alphamind.command_center.views.history._archive_base_dir",
            return_value=archive_base,
        ):
            resp = client.get(
                "/api/views/history/brief-retrieval?invocation_id=inv-detail-001&ref_prefix=QR"
            )
        body = resp.json()
        ref_ids = [s["ref_id"] for s in body["sections"]]
        assert "QR-1" in ref_ids

    def test_unknown_prefix_returns_400(self, client: TestClient) -> None:
        resp = client.get(
            "/api/views/history/brief-retrieval?invocation_id=inv-detail-001&ref_prefix=UNKNOWN"
        )
        assert resp.status_code == 400

    def test_missing_invocation_returns_404(self, client: TestClient) -> None:
        resp = client.get(
            "/api/views/history/brief-retrieval?invocation_id=no-such-inv&ref_prefix=CR"
        )
        assert resp.status_code == 404

    def test_returns_empty_sections_when_archive_absent(self, client: TestClient) -> None:
        # No archive — response shape still valid, sections empty.
        resp = client.get(
            "/api/views/history/brief-retrieval?invocation_id=inv-detail-001&ref_prefix=AR"
        )
        assert resp.status_code == 200
        body = resp.json()
        # AR maps to adaptive_research.md which doesn't exist in default archive.
        assert body["sections"] == []


# ---------------------------------------------------------------------------
# Unit tests: path-traversal guard (_safe_archive_file)
# ---------------------------------------------------------------------------


class TestSafeArchiveFile:
    def test_valid_path_resolves(self, tmp_path: Path) -> None:
        from alphamind.command_center.views.history import _safe_archive_file

        root = tmp_path / "archive_root"
        (root / "analysis").mkdir(parents=True)
        (root / "analysis" / "synthesis.md").write_text("content", encoding="utf-8")

        result = _safe_archive_file(root, "analysis", "synthesis.md")
        assert result.is_relative_to(root.resolve())

    def test_traversal_raises_value_error(self, tmp_path: Path) -> None:
        from alphamind.command_center.views.history import _safe_archive_file

        root = tmp_path / "archive_root"
        root.mkdir()

        with pytest.raises(ValueError, match="Path traversal"):
            _safe_archive_file(root, "analysis", "../../etc/passwd")


# ---------------------------------------------------------------------------
# anyio backend
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _anyio_backend() -> str:
    return "asyncio"
