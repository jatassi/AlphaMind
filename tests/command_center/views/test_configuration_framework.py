"""Tests for the config editor framework (story 05i / ALP-679).

Three layers:

* :class:`ReloadPolicy` enum + :func:`reload_policy_of` extraction from
  ``Annotated`` Pydantic field hints — applied to ``CommandCenterConfig``,
  ``SecurityConfig``, ``AlertsConfig`` per acceptance criterion.
* ``GET /api/views/config/schema/{config_file}`` — form-schema metadata
  derived from the file's Pydantic model + reload-policy decorators.
* ``PUT /api/views/config/{config_file}`` — atomic file write via
  ``_kernel/atomic_io`` after all three validation layers pass.

Scoped pytest: ``uv run pytest tests/command_center/views/ -n auto``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from alphamind.command_center.views.configuration import (
    ReloadPolicy,
    build_configuration_router,
    reload_policy_of,
)


class TestReloadPolicyEnum:
    def test_has_invocation_time_and_deploy_time_members(self) -> None:
        # Two policies per the design — invocation-time-reload (most fields)
        # and deploy-time-only (paths in main.yaml, SQLite pragmas, package
        # versions, .env location). The badge UI dispatches on the enum.
        members = {ReloadPolicy.INVOCATION_TIME, ReloadPolicy.DEPLOY_TIME}
        assert len(members) == 2

    def test_invocation_time_default_extracted_when_no_annotation(self) -> None:
        from alphamind.command_center.config import SessionConfig

        # SessionConfig.duration_hours carries no ReloadPolicy annotation in
        # the type hint by default; the extractor returns INVOCATION_TIME
        # because every YAML knob reloads at next invocation unless
        # explicitly marked deploy-time-only. This test pins the default
        # behavior on a field that has no explicit annotation.
        policy = reload_policy_of(SessionConfig, "duration_hours")
        assert policy is ReloadPolicy.INVOCATION_TIME

    def test_deploy_time_extracted_when_annotated(self) -> None:
        from alphamind.command_center.config import (
            CommandCenterConfig,
            DbConfig,
            FrontendConfig,
        )

        # Per docs/design/configuration-management.md § Runtime vs. deploy-
        # time classification, "paths in main.yaml" are deploy-time-only:
        # the daemon resolves them at startup, so an in-flight edit only
        # takes effect after a process restart.
        assert reload_policy_of(DbConfig, "alphamind_db_path") is ReloadPolicy.DEPLOY_TIME
        assert reload_policy_of(FrontendConfig, "dist_path") is ReloadPolicy.DEPLOY_TIME
        # bind.host + bind.port are deploy-time because the Uvicorn server
        # binds to them at startup; changing the YAML mid-flight does not
        # rebind the socket.
        assert reload_policy_of(CommandCenterConfig, "bind") is ReloadPolicy.DEPLOY_TIME

    def test_session_duration_invocation_time(self) -> None:
        from alphamind.command_center.config import SessionConfig

        # Session cookie duration is an invocation-time knob: existing
        # sessions are unaffected, but new sessions issued after the
        # next config reload pick up the new value.
        assert reload_policy_of(SessionConfig, "duration_hours") is ReloadPolicy.INVOCATION_TIME

    def test_alerts_rules_is_invocation_time(self) -> None:
        from alphamind.command_center.config import AlertsConfig

        # Alert rule edits reload at next invocation per the design.
        assert reload_policy_of(AlertsConfig, "rules") is ReloadPolicy.INVOCATION_TIME


class TestSchemaEndpoint:
    """``GET /api/views/config/schema/{config_file}``.

    Returns the form-schema metadata derived from the file's Pydantic
    model + reload-policy decorators. The frontend consumes this to
    dispatch per-control-type renders.
    """

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        return TestClient(app)

    def test_unknown_config_file_returns_404(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/not-a-real-file")
        assert response.status_code == 404

    def test_command_center_schema_returned(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        assert response.status_code == 200
        body = response.json()
        # Each field has a dotted path + control_type + reload_policy.
        paths = {field["path"] for field in body["fields"]}
        assert "bind.host" in paths
        assert "bind.port" in paths
        assert "db.alphamind_db_path" in paths

    def test_command_center_db_path_is_deploy_time(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        db_path_field = next(
            field for field in body["fields"] if field["path"] == "db.alphamind_db_path"
        )
        assert db_path_field["reload_policy"] == "deploy_time"

    def test_command_center_pipeline_url_is_invocation_time(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        url_field = next(
            field for field in body["fields"] if field["path"] == "pipeline.control_url"
        )
        # Unannotated → default INVOCATION_TIME per the framework contract.
        assert url_field["reload_policy"] == "invocation_time"

    def test_number_control_type_for_int_field(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        port_field = next(field for field in body["fields"] if field["path"] == "bind.port")
        assert port_field["control_type"] == "number"
        # Field constraints (ge=1, le=65535) surface for client-side bounds
        # enforcement before the PUT lands at the backend.
        assert port_field["constraints"]["minimum"] == 1
        assert port_field["constraints"]["maximum"] == 65535

    def test_string_control_type_for_str_field(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        host_field = next(field for field in body["fields"] if field["path"] == "bind.host")
        assert host_field["control_type"] == "string"

    def test_security_schema_returned(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/security")
        assert response.status_code == 200
        body = response.json()
        paths = {field["path"] for field in body["fields"]}
        assert "session.duration_hours" in paths
        assert "csrf.cookie_name" in paths

    def test_alerts_schema_returned(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/alerts")
        assert response.status_code == 200
        body = response.json()
        paths = {field["path"] for field in body["fields"]}
        assert "channels.discord.webhook_url_env" in paths


class TestPathExistsEndpoint:
    """``GET /api/views/config/path-exists`` — PathInput probe."""

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        return TestClient(app)

    def test_existing_path_returns_true(self, tmp_path: Path) -> None:
        client = self._client()
        existing_file = tmp_path / "real.txt"
        existing_file.write_text("data", encoding="utf-8")
        response = client.get(f"/api/views/config/path-exists?path={existing_file}")
        assert response.status_code == 200
        assert response.json() == {"exists": True}

    def test_missing_path_returns_false(self, tmp_path: Path) -> None:
        client = self._client()
        missing = tmp_path / "does-not-exist.txt"
        response = client.get(f"/api/views/config/path-exists?path={missing}")
        assert response.status_code == 200
        assert response.json() == {"exists": False}


class TestPutEndpoint:
    """``PUT /api/views/config/{config_file}``.

    Atomic file write via :func:`alphamind._kernel.atomic_io.atomic_write_text`
    after the three validation layers (parse / cross-ref / semantic) pass.
    Rejects with the layered error envelope on failure.
    """

    def _client(self, config_dir: Path) -> TestClient:
        app = FastAPI()
        app.state.config_dir = config_dir
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        return TestClient(app)

    def _valid_command_center_yaml(self, db_path: Path) -> str:
        # All values match the shipped command-center.yaml shape so the
        # parse layer accepts the body.
        return (
            "bind:\n"
            '  host: "127.0.0.1"\n'
            "  port: 8080\n"
            "db:\n"
            f'  alphamind_db_path: "{db_path}"\n'
            "frontend:\n"
            '  dist_path: "dist"\n'
            "pipeline:\n"
            '  control_url: "http://127.0.0.1:8765"\n'
            '  events_url: "http://127.0.0.1:8765"\n'
            "monitor:\n"
            '  control_url: "http://127.0.0.1:8766"\n'
            '  events_url: "http://127.0.0.1:8766"\n'
        )

    def test_unknown_config_file_returns_404(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        response = client.put(
            "/api/views/config/not-a-real-file",
            json={"yaml": "x: 1\n"},
        )
        assert response.status_code == 404

    def test_parse_failure_returns_422_with_parse_layer_envelope(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        # bind.port out of range — Pydantic Field(ge=1, le=65535) rejects.
        bad_yaml = (
            "bind:\n"
            '  host: "127.0.0.1"\n'
            "  port: 999999\n"
            "db:\n"
            '  alphamind_db_path: "/tmp/x.db"\n'
            "frontend:\n"
            '  dist_path: "dist"\n'
            "pipeline:\n"
            '  control_url: "http://127.0.0.1:8765"\n'
            '  events_url: "http://127.0.0.1:8765"\n'
            "monitor:\n"
            '  control_url: "http://127.0.0.1:8766"\n'
            '  events_url: "http://127.0.0.1:8766"\n'
        )
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": bad_yaml},
        )
        assert response.status_code == 422
        body = response.json()
        # Layered envelope — parse failures carry a `parse` key with
        # per-field error rows.
        assert "parse" in body["detail"]
        assert body["detail"]["parse"], "parse layer must list the offending field"
        # The other layers are present-but-empty so the frontend can render
        # the three-bucket banner unconditionally.
        assert body["detail"]["cross_reference"] == []
        assert body["detail"]["semantic"] == []

    def test_malformed_yaml_returns_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": ":::not-valid-yaml:::"},
        )
        assert response.status_code == 422

    def test_successful_put_atomically_writes_file(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        # Seed an existing file so the atomic-replace is exercised.
        target_path.write_text("placeholder: true\n", encoding="utf-8")

        good_yaml = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": good_yaml},
        )

        assert response.status_code == 200
        body = response.json()
        # Response carries the deploy-time-only field flag so the
        # frontend can surface a restart-needed reminder.
        assert "deploy_time_fields_changed" in body
        # And the file was atomically replaced — no .tmp lingering.
        assert target_path.read_text(encoding="utf-8") == good_yaml
        assert not (tmp_path / "command-center.yaml.tmp").exists()

    def test_successful_put_creates_file_when_missing(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        assert not target_path.exists()

        good_yaml = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": good_yaml},
        )

        assert response.status_code == 200
        assert target_path.read_text(encoding="utf-8") == good_yaml

    def test_failed_put_does_not_touch_existing_file(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        existing = "existing: value\n"
        target_path.write_text(existing, encoding="utf-8")

        bad_yaml = "bind:\n  host: ''\n  port: 0\n"  # parse failure
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": bad_yaml},
        )

        assert response.status_code == 422
        # The existing file is unchanged — rejection happens before the
        # atomic write fires.
        assert target_path.read_text(encoding="utf-8") == existing
        assert not (tmp_path / "command-center.yaml.tmp").exists()

    def test_deploy_time_flag_false_when_only_invocation_time_changed(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        # Seed an existing valid file.
        original = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        target_path.write_text(original, encoding="utf-8")

        # Edit only the (invocation-time) pipeline.control_url.
        new_yaml = original.replace(
            'control_url: "http://127.0.0.1:8765"',
            'control_url: "http://127.0.0.1:9999"',
        )

        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": new_yaml},
        )
        assert response.status_code == 200
        assert response.json()["deploy_time_fields_changed"] is False

    def test_deploy_time_flag_true_when_db_path_changed(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        original = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        target_path.write_text(original, encoding="utf-8")

        # Change DB path — deploy-time field.
        new_yaml = self._valid_command_center_yaml(tmp_path / "different.db")
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": new_yaml},
        )
        assert response.status_code == 200
        assert response.json()["deploy_time_fields_changed"] is True


class TestLayeredValidationHooks:
    """Cross-reference + semantic hooks produce layered envelope rejections.

    The framework registers per-file hooks; per-file editors in 06a/06b
    wire the existing ``config/validation/`` chain. This test exercises
    the framework's hook contract using a fake hook attached to a stand-
    alone :class:`ConfigFile` registration.
    """

    def test_cross_reference_failure_returns_envelope(self) -> None:
        from alphamind.command_center.config import SecurityConfig
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            ConfigUpdateRequest,
            run_validation,
        )

        entry = ConfigFile(
            slug="security",
            model=SecurityConfig,
            filename="security.yaml",
            cross_reference_hook=lambda _m: ["bogus cross-reference mismatch"],
        )

        # Valid YAML for SecurityConfig.
        valid_yaml = (
            "session:\n"
            "  duration_hours: 12\n"
            '  cookie_name: "cc_session"\n'
            "csrf:\n"
            '  cookie_name: "cc_csrf"\n'
            "webauthn:\n"
            '  relying_party_id: "localhost"\n'
            '  relying_party_name: "AlphaMind"\n'
        )
        _model, report = run_validation(entry, valid_yaml)
        assert report.parse == []
        assert len(report.cross_reference) == 1
        assert report.cross_reference[0].message == "bogus cross-reference mismatch"
        assert report.semantic == []
        # Type confirmation: the request body model exists and parses cleanly.
        req = ConfigUpdateRequest(yaml=valid_yaml)
        assert req.yaml == valid_yaml

    def test_semantic_failure_returns_envelope(self) -> None:
        from alphamind.command_center.config import SecurityConfig
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            run_validation,
        )

        entry = ConfigFile(
            slug="security",
            model=SecurityConfig,
            filename="security.yaml",
            semantic_hook=lambda _m: ["invariant violated"],
        )

        valid_yaml = (
            "session:\n"
            "  duration_hours: 12\n"
            '  cookie_name: "cc_session"\n'
            "csrf:\n"
            '  cookie_name: "cc_csrf"\n'
            "webauthn:\n"
            '  relying_party_id: "localhost"\n'
            '  relying_party_name: "AlphaMind"\n'
        )
        _model, report = run_validation(entry, valid_yaml)
        assert report.parse == []
        assert report.cross_reference == []
        assert len(report.semantic) == 1
        assert report.semantic[0].message == "invariant violated"

    def test_router_mounted_under_views_config_prefix(self, tmp_path: Path) -> None:
        """build_app wires build_configuration_router under /api/views/config."""
        import shutil

        from alphamind.command_center.app import build_app
        from alphamind.command_center.config import (
            load_alerts_config,
            load_command_center_config,
            load_security_config,
        )

        # Mirror the shipped config tree into tmp_path so build_app's
        # loaders find their three YAML files.
        repo_config = Path(__file__).parents[3] / "config"
        cc_yaml = tmp_path / "command-center.yaml"
        # Substitute an isolated DB path so build_app's lifespan can later
        # construct engines without touching the host's data dir.
        cc_text = (
            (repo_config / "command-center.yaml")
            .read_text(encoding="utf-8")
            .replace(
                '"%USERPROFILE%/AlphaMind/data/alphamind.db"',
                f'"{tmp_path}/alphamind.db"',
            )
        )
        cc_yaml.write_text(cc_text, encoding="utf-8")
        shutil.copy(repo_config / "security.yaml", tmp_path / "security.yaml")
        shutil.copy(repo_config / "alerts.yaml", tmp_path / "alerts.yaml")

        app = build_app(
            command_center_config=load_command_center_config(tmp_path),
            security_config=load_security_config(tmp_path),
            alerts_config=load_alerts_config(tmp_path),
            config_dir=tmp_path,
        )

        # build_app threads config_dir onto app.state for the PUT handler.
        assert app.state.config_dir == tmp_path
        # The schema endpoint surfaces under the mounted prefix without
        # exercising the lifespan (it has no DB dependency).
        client = TestClient(app)
        response = client.get("/api/views/config/schema/security")
        assert response.status_code == 200
        assert response.json()["slug"] == "security"

    def test_cross_reference_short_circuits_semantic(self) -> None:
        from alphamind.command_center.config import SecurityConfig
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            run_validation,
        )

        # If cross-reference fails, the semantic hook does not run.
        semantic_calls: list[str] = []

        def semantic_hook(_m: object) -> list[str]:
            semantic_calls.append("called")
            return ["should not be reached"]

        entry = ConfigFile(
            slug="security",
            model=SecurityConfig,
            filename="security.yaml",
            cross_reference_hook=lambda _m: ["fails first"],
            semantic_hook=semantic_hook,
        )
        valid_yaml = (
            "session:\n"
            "  duration_hours: 12\n"
            '  cookie_name: "cc_session"\n'
            "csrf:\n"
            '  cookie_name: "cc_csrf"\n'
            "webauthn:\n"
            '  relying_party_id: "localhost"\n'
            '  relying_party_name: "AlphaMind"\n'
        )
        _model, report = run_validation(entry, valid_yaml)
        assert report.cross_reference and not report.semantic
        assert semantic_calls == []
