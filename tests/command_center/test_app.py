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

ALP-722 extends the contract: when the mount is active, deep SPA routes
(``/login``, ``/register``, any TanStack client route) receive ``index.html``
(200 text/html) via an explicit 404 fallback handler; API/auth/events paths
continue to 404 as JSON.

The healthz probe is the manual-smoke target named in the story AC:
``curl http://127.0.0.1:8080/healthz`` returns 200.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.routing import Mount
from fastapi.testclient import TestClient

from alphamind.command_center.__main__ import _warn_if_mixed_lan_bind_and_access
from alphamind.command_center.app import build_app
from alphamind.command_center.auth.webauthn import RealWebauthnVerifier
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
  alphamind_db_path: '{db}'
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
    story 05a (ALP-671) adds /api/alerts + acknowledge/snooze;
    story 05b (ALP-672) adds /api/views/live + /api/views/schedule;
    story 05c (ALP-673) adds /api/views/history/runs + failure-log preset;
    story 05e (ALP-675) adds /api/views/activity-log + event-types +
    saved-filters; story 05f (ALP-676) adds /api/views/portfolio/dashboard.

    Asserts that only the routes belonging to the merged stories are
    present at this point — stories 05d/05g-05j/06a-06c register their
    routers later.
    """

    def test_includes_healthz_auth_control_events_and_alerts_routes(
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
                "/api/alerts",
                "/api/alerts/{alert_id_raw}/acknowledge",
                "/api/alerts/{alert_id_raw}/snooze",
                # Story 05e (ALP-675) — activity-log explorer view routes.
                "/api/views/activity-log",
                "/api/views/activity-log/event-types",
                "/api/views/activity-log/saved-filters",
                "/api/views/history/brief-retrieval",
                "/api/views/history/runs",
                "/api/views/history/runs/preset/failure-log",
                "/api/views/history/runs/{invocation_id}",
                "/api/views/history/runs/{invocation_id}/archive/{section}/{filename}",
                "/api/views/live",
                # Story 05f (ALP-676) — portfolio dashboard.
                "/api/views/portfolio/dashboard",
                # Story 05g (ALP-677) — position detail.
                "/api/views/portfolio/positions/{position_id}",
                "/api/views/schedule",
                # Story 05h (ALP-678) — theses dashboard + detail view.
                "/api/views/portfolio/theses",
                "/api/views/portfolio/theses/{thesis_id}",
                # Story 05i (ALP-679) — config editor framework.
                "/api/views/config/path-exists",
                # Story 06a (ALP-682) — profiles + regimes file-picker.
                "/api/views/config/files",
                # Story 06c (ALP-684) — resolved-config + git diagnostic views.
                "/api/views/config/git/diff",
                "/api/views/config/git/history",
                "/api/views/config/git/status",
                "/api/views/config/resolved",
                "/api/views/config/resolved/diff",
                # Stories 05i + 06a use ``{config_file:path}`` so slugs with
                # slashes (``profiles/small``, ``regimes/normal``) route
                # correctly. Story 06b (ALP-683) registers GET on the same
                # dotted-path as story 05i's PUT — two FastAPI route objects
                # share the same ``path`` string; the sorted list materializes
                # both.
                "/api/views/config/schema/{config_file:path}",
                "/api/views/config/{config_file:path}",
                "/api/views/config/{config_file:path}",
                # Story 05j (ALP-680) — risk views.
                "/api/views/risk/calibration-mix",
                "/api/views/risk/guardrail-dashboard",
                "/api/views/risk/regime-timeline",
            ]
        )
        assert own_routes == expected, (
            f"unexpected routes registered after stories 02 + 03 + 04a + 04b + 04d + 05a + "
            f"05b + 05c + 05d + 05e + 05f + 05g + 05h + 05i + 05j + 06a + 06b + 06c — "
            f"found {own_routes}; expected {expected}."
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
        '<!doctype html><html><body><div id="root"></div>SPA</body></html>',
        encoding="utf-8",
    )
    cc_yaml = tmp_path / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "127.0.0.1"
  port: 8080
db:
  alphamind_db_path: '{db}'
frontend:
  dist_path: '{dist}'
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
    """Story 04d (ALP-670) + ALP-722 — StaticFiles mount + SPA fallback lifecycle.

    Three configurations (mount behavior):
    * Happy path — dist exists, dev mode off → mount registered, serves
      index.html on unmatched paths + deep SPA routes via fallback handler.
    * Missing dist — directory doesn't exist → mount skipped, daemon still
      runs (API surface remains usable).
    * Dev mode active — env var set → mount skipped regardless of dist.

    ALP-722 adds: the 404 fallback only applies to GET client routes; API,
    auth, events, asset, and non-GET paths still receive proper 404/405 JSON.
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
        """SPA fallback (ALP-722): even deep routes like /login serve index.html
        (the html=True on StaticFiles only handled directory requests; the added
        404 handler supplies the shell for TanStack Router paths).
        """
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

    def test_deep_spa_routes_serve_index_html(self, configs_with_dist: Path) -> None:
        """ALP-722: client-side routes (e.g. /login after 401 hard-redirect) must serve
        the SPA shell (text/html + index.html content) instead of 404 JSON. This is the
        regression the StaticFiles(html=True) alone did not catch.
        """
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            response = client.get("/login")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "SPA" in response.text

    def test_deep_spa_routes_include_root_div(self, configs_with_dist: Path) -> None:
        """ALP-722 AC: the served shell must contain the real SPA's <div id="root"> marker
        so that the React hydrate can find its mount point (exact text from index.html).
        """
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            # /register is another deep route mentioned in symptom + AC
            response = client.get("/register")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert '<div id="root"></div>' in response.text

    def test_api_and_auth_paths_still_404_as_json(self, configs_with_dist: Path) -> None:
        """ALP-722 AC: unknown API, auth, events, healthz paths must still 404 with
        application/json (no false-positive SPA HTML fallback that would mask real errors).
        """
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            for bad_path in ("/api/nonexistent", "/auth/foo", "/events/unknown", "/healthz/extra"):
                r = client.get(bad_path)
                assert r.status_code == 404, f"{bad_path} should 404"
                assert r.headers.get("content-type", "").startswith("application/json")

    def test_non_get_methods_do_not_spa_fallback(self, configs_with_dist: Path) -> None:
        """ALP-722 AC: POST/PUT etc to unknown paths must not receive HTML fallback
        (only GET client routes are eligible for SPA shell).
        """
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            r = client.post("/some/spa/route")
            # Either 404 or 405 (mount/router rejects); either way not 200 HTML.
            assert r.status_code in (404, 405)
            # If 404, must be JSON not HTML.
            if r.status_code == 404:
                assert r.headers.get("content-type", "").startswith("application/json")

    def test_missing_assets_still_404_not_html(self, configs_with_dist: Path) -> None:
        """ALP-722 AC: a missing bundle chunk (/assets/missing.js) must 404 (so browser
        can detect broken deploy) rather than receiving the index.html shell.
        """
        config_dir = configs_with_dist
        app = build_app(
            command_center_config=load_command_center_config(config_dir),
            security_config=load_security_config(config_dir),
            alerts_config=load_alerts_config(config_dir),
        )
        with TestClient(app) as client:
            r = client.get("/assets/missing.js")
            assert r.status_code == 404
            assert r.headers.get("content-type", "").startswith("application/json")


# ---------------------------------------------------------------------------
# ALP-726 (02 of ALP-724 LAN access): TDD tests for re-wired WebAuthn origin
# resolution from access block + cookies_secure from SecurityConfig + startup
# warning for mixed widened-bind + localhost-ish access host.
# Existing localhost behavior must be unchanged.
# ---------------------------------------------------------------------------


class TestLanAccessWebauthnResolverAndWarning:
    """ALP-726 ACs 1,3,4,5: LAN access block drives expected_origin (scheme+host+port);
    pure localhost (no access key) preserves historical http://localhost:8080;
    warning logged (via direct helper for caplog in scoped test) on mixed case;
    all prior wiring tests remain green for localhost.
    """

    def _write_lan_configs(
        self,
        tmp_path: Path,
        *,
        bind_host: str = "127.0.0.1",
        access_scheme: str = "http",
        access_host: str = "localhost",
        access_port: int | None = 8080,
        rp_id: str = "localhost",
    ) -> tuple[Path, Path, Path]:
        """Write minimal per-test YAMLs (no access key when caller passes default)."""
        repo_config = Path(__file__).parents[2] / "config"
        db = tmp_path / "test.db"
        db.touch()
        dist = tmp_path / "no-dist"
        dist.mkdir(exist_ok=True)

        access_block = ""
        if not (access_host == "localhost" and access_port == 8080 and rp_id == "localhost"):
            # include explicit access for LAN cases
            port_str = f"  port: {access_port}" if access_port is not None else ""
            access_block = f"""access:
  scheme: "{access_scheme}"
  host: "{access_host}"
{port_str}
"""

        cc_yaml = tmp_path / "command-center.yaml"
        cc_yaml.write_text(
            f"""bind:
  host: "{bind_host}"
  port: 8080
{access_block}db:
  alphamind_db_path: '{db}'
frontend:
  dist_path: '{dist}'
pipeline:
  control_url: "http://127.0.0.1:8765"
  events_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
  events_url: "http://127.0.0.1:8766"
""",
            encoding="utf-8",
        )
        sec_yaml = tmp_path / "security.yaml"
        sec_yaml.write_text(
            f"""session:
  duration_hours: 12
  cookie_name: "cc_session"
csrf:
  cookie_name: "cc_csrf"
webauthn:
  relying_party_id: "{rp_id}"
  relying_party_name: "AlphaMind Command Center"
""",
            encoding="utf-8",
        )
        (tmp_path / "alerts.yaml").write_bytes((repo_config / "alerts.yaml").read_bytes())
        return tmp_path, db, dist

    def test_resolver_uses_access_for_expected_origin(self, tmp_path: Path) -> None:
        """AC1 + AC5: LAN-style access block yields correct scheme://host:port origin
        (rp_id still sourced from security config).
        """
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="192.168.1.50",
            access_scheme="http",
            access_host="alphamind.local",
            access_port=8080,
            rp_id="alphamind.local",
        )
        cc_cfg = load_command_center_config(config_dir)
        sec_cfg = load_security_config(config_dir)
        alerts_cfg = load_alerts_config(config_dir)

        app = build_app(
            command_center_config=cc_cfg,
            security_config=sec_cfg,
            alerts_config=alerts_cfg,
        )
        verifier = app.state.webauthn_verifier
        assert isinstance(verifier, RealWebauthnVerifier)
        assert verifier._expected_origin == "http://alphamind.local:8080"
        assert verifier._relying_party_id == "alphamind.local"

    def test_localhost_no_access_block_preserves_historical_origin(self, tmp_path: Path) -> None:
        """AC4: omitted access: key + loopback bind still produces historical
        http://localhost:8080 exactly (zero behavior change for v1 installs).
        """
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="127.0.0.1",
            access_host="localhost",
            access_port=8080,
            rp_id="localhost",
        )
        cc_cfg = load_command_center_config(config_dir)
        sec_cfg = load_security_config(config_dir)
        alerts_cfg = load_alerts_config(config_dir)

        app = build_app(
            command_center_config=cc_cfg,
            security_config=sec_cfg,
            alerts_config=alerts_cfg,
        )
        verifier = app.state.webauthn_verifier
        assert isinstance(verifier, RealWebauthnVerifier)
        assert verifier._expected_origin == "http://localhost:8080"

    def test_warning_for_mixed_widened_bind_local_access(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AC3: widened bind + localhost-ish access -> clear WARNING from helper
        (exercised directly + caplog, per ALP-721 pattern).
        """
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="0.0.0.0",
            access_host="localhost",
            access_port=8080,
            rp_id="localhost",
        )
        cc_cfg = load_command_center_config(config_dir)

        caplog.set_level(logging.WARNING, logger="alphamind.command_center.__main__")
        _warn_if_mixed_lan_bind_and_access(cc_cfg)

        msg = (
            "bind.host=0.0.0.0 is not loopback but access.host=localhost still looks localhost-ish"
        )
        assert msg in caplog.text
        assert "access: block" in caplog.text or "RUNBOOK" in caplog.text

    def test_resolver_port_none_http_defaults_to_80(self, tmp_path: Path) -> None:
        """ALP-730 (03d) edge: access port=None for http -> resolver yields :80 (scheme default)."""
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="192.168.1.77",
            access_scheme="http",
            access_host="myhost.local",
            access_port=None,
            rp_id="myhost.local",
        )
        cc_cfg = load_command_center_config(config_dir)
        sec_cfg = load_security_config(config_dir)
        alerts_cfg = load_alerts_config(config_dir)

        app = build_app(
            command_center_config=cc_cfg,
            security_config=sec_cfg,
            alerts_config=alerts_cfg,
        )
        verifier = app.state.webauthn_verifier
        assert isinstance(verifier, RealWebauthnVerifier)
        assert verifier._expected_origin == "http://myhost.local:80"

    def test_resolver_port_none_https_defaults_to_443(self, tmp_path: Path) -> None:
        """ALP-730 (03d) edge: access port=None for https -> resolver yields :443."""
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="10.0.0.5",
            access_scheme="https",
            access_host="secure.local",
            access_port=None,
            rp_id="secure.local",
        )
        cc_cfg = load_command_center_config(config_dir)
        sec_cfg = load_security_config(config_dir)
        alerts_cfg = load_alerts_config(config_dir)

        app = build_app(
            command_center_config=cc_cfg,
            security_config=sec_cfg,
            alerts_config=alerts_cfg,
        )
        verifier = app.state.webauthn_verifier
        assert isinstance(verifier, RealWebauthnVerifier)
        assert verifier._expected_origin == "https://secure.local:443"

    def test_no_warning_when_access_host_is_non_localhostish(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """ALP-730 (03d) + LAN regression: widened bind + proper LAN
        access host produces no mixed warning.
        """
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="0.0.0.0",
            access_host="alphamind.local",
            access_port=8080,
            rp_id="alphamind.local",
        )
        cc_cfg = load_command_center_config(config_dir)

        caplog.set_level(logging.WARNING, logger="alphamind.command_center.__main__")
        _warn_if_mixed_lan_bind_and_access(cc_cfg)

        assert "still looks localhost-ish" not in caplog.text
        assert "bind.host=0.0.0.0" not in caplog.text

    def test_lan_config_with_access_block_exercises_full_resolver_path(
        self, tmp_path: Path
    ) -> None:
        """ALP-730 (03d): explicit LAN access block (non-default) through
        build_app (resolver + cookies_secure wiring).

        Boot-adjacent shape; coverage point for 03a suggestion interaction
        (LAN detection + access block in same _run sequence around token).
        """
        config_dir, _, _ = self._write_lan_configs(
            tmp_path,
            bind_host="192.168.1.42",
            access_scheme="http",
            access_host="operator.lan",
            access_port=8080,
            rp_id="operator.lan",
        )
        cc_cfg = load_command_center_config(config_dir)
        sec_cfg = load_security_config(config_dir)
        alerts_cfg = load_alerts_config(config_dir)

        app = build_app(
            command_center_config=cc_cfg,
            security_config=sec_cfg,
            alerts_config=alerts_cfg,
        )
        verifier = app.state.webauthn_verifier
        assert isinstance(verifier, RealWebauthnVerifier)
        assert verifier._expected_origin == "http://operator.lan:8080"
        # cookies_secure from security (ALP-725) still flows
        assert app.state.cookies_secure is False
