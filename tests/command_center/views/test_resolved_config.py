"""Tests for the resolved-config viewer endpoints (story 06c / ALP-684).

Covers:

* ``GET /api/views/config/resolved`` — per-invocation resolved-config
  bundle + source files.
* ``GET /api/views/config/resolved/diff`` — structured diff between two
  invocations' resolved configs.

Uses a simple ``FastAPI()`` test app with auth bypassed.  The
``/resolved`` (no invocation_id) path that falls back to the DB is
tested via a mock session factory; all other tests use the explicit
``?invocation_id=`` param, which bypasses the DB entirely.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.dependencies import csrf_required, current_session
from alphamind.command_center.views.configuration import (
    _reader_factory_config,
    build_configuration_router,
)

# ---------------------------------------------------------------------------
# Stub DB session factory (no-op; most tests use explicit invocation_id)
# ---------------------------------------------------------------------------


class _StubSession:
    """No-op async session that returns None for any query."""

    async def execute(self, *_args: Any, **_kw: Any) -> _StubSession:
        return self

    def scalars(self) -> _StubSession:
        return self

    def first(self) -> None:
        return None

    async def __aenter__(self) -> _StubSession:
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass


class _StubFactory:
    def __call__(self) -> _StubSession:
        return _StubSession()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEST_SESSION_ID = operator_session_id("sess-resolved-test")

_SAMPLE_BUNDLE: dict[str, Any] = {
    "profile": "default",
    "regime": "normal",
    "mode": "normal",
    "active_overlays": [],
    "feature_flags": {"some_flag": True},
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _override_auth(app: FastAPI) -> None:
    app.dependency_overrides[current_session] = lambda: _TEST_SESSION_ID
    app.dependency_overrides[csrf_required] = lambda: None


def _make_app(config_dir: Path, *, stub_reader: bool = True) -> FastAPI:
    app = FastAPI()
    app.state.config_dir = config_dir
    _override_auth(app)
    if stub_reader:
        app.dependency_overrides[_reader_factory_config] = lambda: _StubFactory()
    app.include_router(build_configuration_router(), prefix="/api/views/config")
    return app


def _write_snapshot(archive_root: Path, invocation_id: str, bundle: dict[str, Any]) -> Path:
    """Write a fake resolved_config.json under the date-partitioned layout."""
    invocation_dir = archive_root / "2026-05-10" / invocation_id
    invocation_dir.mkdir(parents=True, exist_ok=True)
    snapshot = invocation_dir / "resolved_config.json"
    snapshot.write_text(json.dumps(bundle, sort_keys=True, indent=2), encoding="utf-8")
    return snapshot


# ---------------------------------------------------------------------------
# Tests — GET /api/views/config/resolved (explicit invocation_id)
# ---------------------------------------------------------------------------


class TestResolvedConfigEndpoint:
    """``GET /api/views/config/resolved``."""

    def test_explicit_invocation_id_returns_bundle(self, tmp_path: Path) -> None:
        """``?invocation_id=`` param reads the snapshot and returns the bundle."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-resolved-001", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved",
                params={"invocation_id": "inv-resolved-001"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["invocation_id"] == "inv-resolved-001"
        assert body["bundle"]["profile"] == "default"
        assert "source_files" in body

    def test_missing_snapshot_returns_404(self, tmp_path: Path) -> None:
        """Invocation ID supplied but snapshot absent → 404."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved",
                params={"invocation_id": "inv-does-not-exist"},
            )

        assert resp.status_code == 404

    def test_no_invocation_id_with_stub_db_returns_404_when_empty(self, tmp_path: Path) -> None:
        """No rows in DB (stub returns None) → 404 with descriptive message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()

        # _StubFactory.first() returns None → no most-recent invocation → 404.
        app = _make_app(config_dir, stub_reader=True)

        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app, raise_server_exceptions=True) as client,
        ):
            resp = client.get("/api/views/config/resolved")

        assert resp.status_code == 404
        detail = resp.json()["detail"].lower()
        assert "most-recent" in detail or "invocation" in detail

    def test_source_files_keys_populated_for_profile_and_regime(self, tmp_path: Path) -> None:
        """Source-files map contains expected keys from the bundle metadata."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-resolved-002", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved",
                params={"invocation_id": "inv-resolved-002"},
            )

        assert resp.status_code == 200
        sf = resp.json()["source_files"]
        assert "profiles/default.yaml" in sf
        assert "regimes/normal.yaml" in sf

    def test_bundle_fields_present(self, tmp_path: Path) -> None:
        """Bundle carries the expected top-level keys from the snapshot."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-resolved-003", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved",
                params={"invocation_id": "inv-resolved-003"},
            )

        assert resp.status_code == 200
        bundle = resp.json()["bundle"]
        for key in ("profile", "regime", "mode", "active_overlays", "feature_flags"):
            assert key in bundle, f"missing bundle key {key!r}"


# ---------------------------------------------------------------------------
# Tests — GET /api/views/config/resolved/diff
# ---------------------------------------------------------------------------


class TestResolvedConfigDiff:
    """``GET /api/views/config/resolved/diff``."""

    def _make_diff_client(self, tmp_path: Path, archive_root: Path) -> TestClient:
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        app = _make_app(config_dir)
        # Patch archive_base_dir at the module level for the whole client context.
        # We return the client inside the patch below.
        return TestClient(app)

    def test_diff_identical_snapshots_returns_empty_diff(self, tmp_path: Path) -> None:
        """Two byte-identical snapshots → diff_lines is empty."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-diff-a", _SAMPLE_BUNDLE)
        _write_snapshot(archive_root, "inv-diff-b", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved/diff",
                params={
                    "from_invocation_id": "inv-diff-a",
                    "to_invocation_id": "inv-diff-b",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["diff_lines"] == []

    def test_diff_different_snapshots_returns_diff_lines(self, tmp_path: Path) -> None:
        """Two snapshots that differ → diff_lines is non-empty."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        bundle_a = {**_SAMPLE_BUNDLE, "regime": "normal"}
        bundle_b = {**_SAMPLE_BUNDLE, "regime": "volatile"}
        _write_snapshot(archive_root, "inv-diff-c", bundle_a)
        _write_snapshot(archive_root, "inv-diff-d", bundle_b)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved/diff",
                params={
                    "from_invocation_id": "inv-diff-c",
                    "to_invocation_id": "inv-diff-d",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["from_invocation_id"] == "inv-diff-c"
        assert body["to_invocation_id"] == "inv-diff-d"
        assert len(body["diff_lines"]) > 0
        full_diff = "".join(body["diff_lines"])
        assert "volatile" in full_diff

    def test_missing_from_snapshot_returns_404(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-diff-e", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved/diff",
                params={
                    "from_invocation_id": "inv-does-not-exist",
                    "to_invocation_id": "inv-diff-e",
                },
            )

        assert resp.status_code == 404

    def test_missing_to_snapshot_returns_404(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-diff-f", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved/diff",
                params={
                    "from_invocation_id": "inv-diff-f",
                    "to_invocation_id": "inv-does-not-exist",
                },
            )

        assert resp.status_code == 404

    def test_response_fields_present(self, tmp_path: Path) -> None:
        """Response carries from_invocation_id, to_invocation_id, diff_lines."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        archive_root = tmp_path / "archive"
        archive_root.mkdir()
        _write_snapshot(archive_root, "inv-diff-g", _SAMPLE_BUNDLE)
        _write_snapshot(archive_root, "inv-diff-h", _SAMPLE_BUNDLE)

        app = _make_app(config_dir)
        with (
            patch(
                "alphamind.command_center.views.configuration._archive_base_dir",
                return_value=archive_root,
            ),
            TestClient(app) as client,
        ):
            resp = client.get(
                "/api/views/config/resolved/diff",
                params={
                    "from_invocation_id": "inv-diff-g",
                    "to_invocation_id": "inv-diff-h",
                },
            )

        assert resp.status_code == 200
        body = resp.json()
        for key in ("from_invocation_id", "to_invocation_id", "diff_lines"):
            assert key in body, f"missing response field {key!r}"
