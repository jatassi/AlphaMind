"""Tests for the git-history and git-status endpoints (story 06c / ALP-684).

Covers:

* ``GET /api/views/config/git/history?file=`` — git log for a config file.
* ``GET /api/views/config/git/diff?file=&from_sha=&to_sha=`` — text diff.
* ``GET /api/views/config/git/status?file=`` — tracking state.

Uses the actual repo git history (``config/`` tree is committed), so tests
are naturally scoped to files that exist in git.  Path-traversal and
security tests use ``tmp_path``-scoped sandboxes.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.dependencies import csrf_required, current_session
from alphamind.command_center.views.configuration import build_configuration_router

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_TEST_SESSION_ID = operator_session_id("sess-git-test")

# The real config/ dir from the repo root — at least some YAML files in it
# are git-tracked, so we can test against them without mocking git.
_REPO_CONFIG_DIR = Path(__file__).parents[3] / "config"


def _override_auth(app: FastAPI) -> None:
    app.dependency_overrides[current_session] = lambda: _TEST_SESSION_ID
    app.dependency_overrides[csrf_required] = lambda: None


def _client_for_config_dir(config_dir: Path) -> TestClient:
    app = FastAPI()
    app.state.config_dir = config_dir
    _override_auth(app)
    app.include_router(build_configuration_router(), prefix="/api/views/config")
    return TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Tests — GET /api/views/config/git/history
# ---------------------------------------------------------------------------


class TestGitHistoryEndpoint:
    """``GET /api/views/config/git/history?file=``."""

    def test_known_tracked_file_returns_commits(self) -> None:
        """``guardrails.yaml`` is git-tracked — must return at least one commit."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/history", params={"file": "guardrails.yaml"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["file"] == "guardrails.yaml"
        # At least one commit must exist for a production-tracked config file.
        assert len(body["commits"]) > 0

    def test_commit_entry_fields_present(self) -> None:
        """Each commit entry has sha, short_sha, author, date, subject."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/history", params={"file": "guardrails.yaml"})
        assert resp.status_code == 200
        for commit in resp.json()["commits"]:
            for field in ("sha", "short_sha", "author", "date", "subject"):
                assert field in commit, f"missing commit field {field!r}"

    def test_file_with_no_git_history_returns_empty_commits(self) -> None:
        """A file that exists on disk but has no git history returns empty commits."""
        # ``new-nonexistent.yaml`` has never been committed → empty list.
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/history", params={"file": "new-nonexistent.yaml"})
        assert resp.status_code == 200
        assert resp.json()["commits"] == []

    def test_path_traversal_rejected(self) -> None:
        """``../`` traversal → 400."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get(
            "/api/views/config/git/history", params={"file": "../src/alphamind/config/snapshot.py"}
        )
        assert resp.status_code == 400

    def test_absolute_path_rejected(self) -> None:
        """Absolute path → 400."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/history", params={"file": "/etc/passwd"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests — GET /api/views/config/git/diff
# ---------------------------------------------------------------------------


class TestGitDiffEndpoint:
    """``GET /api/views/config/git/diff``."""

    def _get_two_recent_shas(self) -> tuple[str, str] | None:
        """Return (older_sha, newer_sha) for guardrails.yaml or None."""
        import subprocess

        result = subprocess.run(
            ["git", "log", "--format=%H", "--", "config/guardrails.yaml"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_CONFIG_DIR.parent),
        )
        shas = result.stdout.strip().splitlines()
        if len(shas) < 2:
            return None
        return shas[1], shas[0]  # older, newer

    def test_diff_between_two_shas_returns_diff_text(self) -> None:
        """Diff between two real commits may return text (or empty for no change)."""
        pair = self._get_two_recent_shas()
        if pair is None:
            pytest.skip("Not enough commits to diff guardrails.yaml")
        older_sha, newer_sha = pair
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get(
            "/api/views/config/git/diff",
            params={"file": "guardrails.yaml", "from_sha": older_sha, "to_sha": newer_sha},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["file"] == "guardrails.yaml"
        assert body["from_sha"] == older_sha
        assert body["to_sha"] == newer_sha
        assert "diff_text" in body
        # diff_text is a string (may be empty if the file didn't change between those commits).
        assert isinstance(body["diff_text"], str)

    def test_response_fields_present(self) -> None:
        """Response always has file, from_sha, to_sha, diff_text keys."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get(
            "/api/views/config/git/diff",
            params={
                "file": "guardrails.yaml",
                "from_sha": "0" * 40,
                "to_sha": "1" * 40,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        for key in ("file", "from_sha", "to_sha", "diff_text"):
            assert key in body, f"missing response field {key!r}"

    def test_path_traversal_rejected(self) -> None:
        """``../`` in file param → 400 regardless of SHA validity."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get(
            "/api/views/config/git/diff",
            params={
                "file": "../../pyproject.toml",
                "from_sha": "a" * 40,
                "to_sha": "b" * 40,
            },
        )
        assert resp.status_code == 400

    def test_no_writes_issued_to_git(self) -> None:
        """Verify only read-only git commands are issued (no ``commit``, etc.)."""
        calls: list[tuple[str, ...]] = []
        original_run_git = __import__(
            "alphamind.command_center.views.configuration",
            fromlist=["_run_git"],
        )._run_git

        async def spy_run_git(*args: str, cwd: Path) -> tuple[int, str, str]:
            calls.append(args)
            result: tuple[int, str, str] = await original_run_git(*args, cwd=cwd)
            return result

        with patch(
            "alphamind.command_center.views.configuration._run_git",
            side_effect=spy_run_git,
        ):
            client = _client_for_config_dir(_REPO_CONFIG_DIR)
            client.get(
                "/api/views/config/git/diff",
                params={
                    "file": "guardrails.yaml",
                    "from_sha": "0" * 40,
                    "to_sha": "1" * 40,
                },
            )

        # None of the issued git sub-commands should be a write command.
        write_commands = {"commit", "push", "add", "rm", "reset", "clean", "checkout"}
        for cmd_args in calls:
            for arg in cmd_args:
                assert arg not in write_commands, (
                    f"Unexpected write git command {arg!r} in subprocess call {cmd_args!r}"
                )


# ---------------------------------------------------------------------------
# Tests — GET /api/views/config/git/status
# ---------------------------------------------------------------------------


class TestGitStatusEndpoint:
    """``GET /api/views/config/git/status``."""

    def test_tracked_file_shows_tracked_true(self) -> None:
        """``guardrails.yaml`` is tracked in git."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/status", params={"file": "guardrails.yaml"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["tracked"] is True

    def test_response_fields_present(self) -> None:
        """Response carries file, tracked, has_uncommitted_changes, untracked."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/status", params={"file": "guardrails.yaml"})
        assert resp.status_code == 200
        body = resp.json()
        for key in ("file", "tracked", "has_uncommitted_changes", "untracked"):
            assert key in body, f"missing response field {key!r}"

    def test_nonexistent_git_file_shows_tracked_false(self) -> None:
        """A file name that has never been committed returns tracked=False."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get(
            "/api/views/config/git/status", params={"file": "does-not-exist-ever.yaml"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["tracked"] is False

    def test_path_traversal_rejected(self) -> None:
        """Path-traversal in ``file=`` → 400."""
        client = _client_for_config_dir(_REPO_CONFIG_DIR)
        resp = client.get("/api/views/config/git/status", params={"file": "../pyproject.toml"})
        assert resp.status_code == 400

    def test_no_git_commit_issued(self) -> None:
        """Status endpoint never calls ``git commit``."""
        issued: list[tuple[str, ...]] = []

        async def mock_run_git(*args: str, cwd: Path) -> tuple[int, str, str]:
            issued.append(args)
            return 0, "", ""

        with patch(
            "alphamind.command_center.views.configuration._run_git",
            side_effect=mock_run_git,
        ):
            client = _client_for_config_dir(_REPO_CONFIG_DIR)
            client.get("/api/views/config/git/status", params={"file": "guardrails.yaml"})

        for cmd_args in issued:
            assert "commit" not in cmd_args, f"git commit issued unexpectedly: {cmd_args!r}"
