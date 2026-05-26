"""Tests for ``command_center.app`` (story 02 / ALP-666).

Covers the FastAPI composition root:

* :func:`build_app` returns a :class:`FastAPI` instance.
* The ``/healthz`` route returns 200 with ``{"status": "ok"}``.
* Lifespan wires the dual session factories onto ``app.state``.

Story 04d (ALP-670) extends:

* The ``StaticFiles`` mount at ``/`` is registered when ``dist_path``
  exists and dev mode is off.
* The mount is skipped when ``COMMAND_CENTER_DEV_MODE`` is set.
* The mount fail-closes when ``dist_path`` doesn't exist.

The healthz probe is the manual-smoke target named in the story AC:
``curl http://127.0.0.1:8080/healthz`` returns 200.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient

from alphamind.command_center.app import build_app
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)


@pytest.fixture
def configs(tmp_path: Path) -> tuple[Path, Path]:
    # Use the shipped configs; override the DB path so the lifespan's
    # engine construction uses a per-test SQLite file.
    repo_config = Path(__file__).parents[2] / "config"
    db = tmp_path / "test.db"
    db.touch()
    # Build a per-test command-center.yaml with the per-test DB path.
    # The frontend.dist_path points at a per-test path that is guaranteed
    # NOT to exist — the default-fixture tests exercise the no-dist
    # codepath of story 04d's StaticFiles mount (mount skipped, warning
    # logged). Tests that need a real dist/ use the configs_with_dist
    # fixture below.
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: "{db}"
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
    # Reuse the shipped security + alerts YAMLs as-is.
    (tmp_path / "security.yaml").write_bytes((repo_config / "security.yaml").read_bytes())
    (tmp_path / "alerts.yaml").write_bytes((repo_config / "alerts.yaml").read_bytes())
    return tmp_path, db


class TestBuildApp:
    def test_returns_fastapi_instance(self, configs: tuple[Path, Path]) -> None:
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        assert app.title == "AlphaMind command center"


class TestHealthzRoute:
    def test_returns_200_with_status_ok(self, configs: tuple[Path, Path]) -> None:
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestLifespanWiresSessionFactories:
    def test_factories_attached_to_app_state(self, configs: tuple[Path, Path]) -> None:
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app):
            # TestClient enters the lifespan; the factories must be on
            # app.state for downstream routers (added in later stories)
            # to pull off Request.app.state.
            assert hasattr(app.state, "cc_writer_session_factory")
            assert hasattr(app.state, "foreign_reader_session_factory")

    def test_production_session_factory_attached_to_app_state(
        self, configs: tuple[Path, Path]
    ) -> None:
        """Composition root threads the production factory into build_app.

        The factory lands on ``app.state.production_session_factory``
        for the operator-invocation bridge (story 04a). The lifespan
        does NOT own the engine; the composition root's
        ``engine_pair_context`` does. Here we pass a sentinel value to
        confirm the stash.
        """
        config_dir, _ = configs
        sentinel = object()
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
            production_session_factory=sentinel,  # type: ignore[arg-type]
        )
        assert app.state.production_session_factory is sentinel

    def test_production_session_factory_default_none_when_unset(
        self, configs: tuple[Path, Path]
    ) -> None:
        """Tests that don't exercise the operator-action bridge can omit it."""
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        assert app.state.production_session_factory is None


class TestLifespanWiresEventMultiplexer:
    """Story 04b (ALP-669): build_app wires an EventMultiplexer onto
    app.state and exposes a hook for registering the upstream consumer
    tasks on the supervisor.
    """

    def test_multiplexer_attached_to_app_state(self, configs: tuple[Path, Path]) -> None:
        from alphamind.command_center.events.multiplexer import EventMultiplexer

        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app):
            assert isinstance(app.state.event_multiplexer, EventMultiplexer)

    def test_consumer_task_factories_attached_to_app_state(
        self, configs: tuple[Path, Path]
    ) -> None:
        """The two upstream-consumer task factories are exposed so the
        composition root can register them on the supervisor's
        TaskGroup at process startup.
        """
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app):
            factories = app.state.event_consumer_task_factories
            assert set(factories) == {
                "events_pipeline_consumer",
                "events_monitor_consumer",
                "schedule_cache_subscriber",
            }
            assert callable(factories["events_pipeline_consumer"])
            assert callable(factories["events_monitor_consumer"])
            assert callable(factories["schedule_cache_subscriber"])


class TestRegisteredRoutes:
    """Story 02 ships /healthz; story 03 (ALP-667) adds /auth/*; story 04a
    (ALP-668) adds /api/control/*; story 04b (ALP-669) adds /api/events;
    story 04d (ALP-670) adds /auth/me + the StaticFiles mount at /;
    story 05b (ALP-672) adds /api/views/live + /api/views/schedule;
    story 05c (ALP-673) adds /api/views/history/runs + failure-log preset;
    story 05e (ALP-675) adds /api/views/activity-log + event-types +
    saved-filters; story 05f (ALP-676) adds /api/views/portfolio/dashboard.

    Asserts that only the routes belonging to the merged stories are
    present at this point — stories 05a (alerts) / remaining view stories
    register their routers later.
    """

    def test_includes_healthz_auth_control_and_events_routes(
        self, configs: tuple[Path, Path]
    ) -> None:
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        # FastAPI's auto-attached paths: /openapi.json, /docs,
        # /docs/oauth2-redirect, /redoc.
        framework_paths = {
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
        }
        own_routes = sorted(
            route.path
            for route in app.routes
            if hasattr(route, "path") and route.path not in framework_paths
        )
        # The test fixture's dist_path points at a directory that does not
        # exist at test time (Vite hasn't run); story 04d's mount fail-
        # closes with a warning rather than crashing, so the StaticFiles
        # mount is NOT registered here. Production tests that materialize
        # a dist/ would see an extra "/" mount entry.
        expected = sorted(
            [
                "/healthz",
                "/auth/register/begin",
                "/auth/register/complete",
                "/auth/login/begin",
                "/auth/login/complete",
                "/auth/me",
                "/auth/logout",
                "/api/control/pause",
                "/api/control/resume",
                "/api/control/trigger_emergency_invocation",
                "/api/control/switch_profile",
                "/api/control/run_universe_validation",
                "/api/control/cancel_order",
                "/api/control/force_close_position",
                "/api/control/set_halt_mode",
                "/api/events",
                # Story 05e (ALP-675) — activity-log explorer view routes.
                "/api/views/activity-log",
                "/api/views/activity-log/event-types",
                "/api/views/activity-log/saved-filters",
                "/api/views/history/runs",
                "/api/views/history/runs/preset/failure-log",
                "/api/views/live",
                # Story 05f (ALP-676) — portfolio dashboard.
                "/api/views/portfolio/dashboard",
                "/api/views/schedule",
            ]
        )
        assert own_routes == expected, (
            f"unexpected routes registered after stories "
            f"02 + 03 + 04a + 04b + 04d + 05b + 05c + 05e + 05f — "
            f"found {own_routes}; expected {expected}. Stories 05a / "
            f"remaining view stories register their routers later."
        )


# ---------------------------------------------------------------------------
# Story 04d / ALP-670 — StaticFiles mount + dev-mode toggle.
# ---------------------------------------------------------------------------


@pytest.fixture
def configs_with_dist(tmp_path: Path) -> Path:
    """Per-test config dir with a materialized dist/ directory.

    The fixture writes a minimal ``dist/index.html`` so the StaticFiles
    mount has something to serve. The path is recorded into
    ``command-center.yaml`` as the ``frontend.dist_path`` value.
    """
    repo_config = Path(__file__).parents[2] / "config"
    db = tmp_path / "test.db"
    db.touch()
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><body>SPA</body></html>", encoding="utf-8"
    )
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: "{db}"
frontend:
  dist_path: "{dist}"
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


class TestStaticFilesMount:
    """Story 04d (ALP-670) — StaticFiles mount lifecycle.

    Three configurations:
    * Happy path — dist exists, dev mode off → mount registered, serves
      index.html on unmatched paths.
    * Missing dist — directory doesn't exist → mount skipped, daemon still
      runs (API surface remains usable).
    * Dev mode active — env var set → mount skipped regardless of dist.
    """

    def test_mount_registered_when_dist_exists(self, configs_with_dist: Path) -> None:
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        mounts = [r for r in app.routes if isinstance(r, Mount)]
        assert len(mounts) == 1, f"expected one StaticFiles mount, found {mounts}"
        assert mounts[0].name == "frontend"

    def test_mount_serves_index_html(self, configs_with_dist: Path) -> None:
        """SPA fallback: an unmatched path serves index.html (TanStack Router
        client-side routes resolve once the bundle hydrates)."""
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            response = client.get("/")
        assert response.status_code == 200
        assert "SPA" in response.text

    def test_mount_skipped_when_dist_missing(self, configs: tuple[Path, Path]) -> None:
        """Default test fixture omits dist/. The mount fail-closes with a
        warning rather than crashing the daemon."""
        config_dir, _ = configs
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        mounts = [r for r in app.routes if isinstance(r, Mount)]
        assert mounts == [], f"expected no StaticFiles mount, found {mounts}"

    def test_mount_skipped_in_dev_mode(
        self, configs_with_dist: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dev mode set → mount skipped even when dist exists."""
        monkeypatch.setenv("COMMAND_CENTER_DEV_MODE", "1")
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        mounts = [r for r in app.routes if isinstance(r, Mount)]
        assert mounts == [], f"expected no StaticFiles mount in dev mode, found {mounts}"

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "ON", "True"])
    def test_dev_mode_truthy_values(
        self, configs_with_dist: Path, monkeypatch: pytest.MonkeyPatch, truthy: str
    ) -> None:
        """Any case-insensitive truthy value activates dev mode."""
        monkeypatch.setenv("COMMAND_CENTER_DEV_MODE", truthy)
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        mounts = [r for r in app.routes if isinstance(r, Mount)]
        assert mounts == [], f"expected dev mode active for value {truthy!r}, found {mounts}"
